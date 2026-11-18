"""Yerel ağ: switch üzerindeki makineleri bulma, modem (Superbox) ve internet durumu.

Superbox bir sunucu değil, ağın internet çıkışı olan modemdir: küme düğümü olarak değil, ağ geçidi olarak izlenir.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import random
import re
import socket
import subprocess
from collections import deque

import httpx

from .util import log, now

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


def _run(args: list[str], timeout: float = 6.0) -> str:
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, creationflags=flags)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def default_gateway() -> str:
    if os.name == "nt":
        best, metric = "", 1 << 30
        for m in re.finditer(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+\d+\.\d+\.\d+\.\d+\s+(\d+)", _run(["route", "print", "-4", "0.0.0.0"]), re.M):
            if int(m.group(2)) < metric:
                best, metric = m.group(1), int(m.group(2))
        return best
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000":
                    return socket.inet_ntoa(bytes.fromhex(p[2])[::-1])
    except OSError:
        pass
    return ""


def arp_table() -> dict[str, str]:
    out = {}
    if os.name == "nt":
        for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})", _run(["arp", "-a"])):
            out[m.group(1)] = m.group(2).replace("-", ":").lower()
    else:
        try:
            with open("/proc/net/arp") as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if len(p) >= 4 and p[3] != "00:00:00:00:00:00":
                        out[p[0]] = p[3].lower()
        except OSError:
            pass
    return out


def local_network() -> dict:
    """Bu bilgisayarın switch'e bağlı olduğu arayüz, adres ve alt ağ."""
    gw = default_gateway()
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((gw or "10.255.255.255", 9))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    out = {"ip": ip, "gateway": gw, "iface": "", "cidr": "", "speed_mbps": None, "mac": ""}
    if psutil and ip:
        try:
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                v4 = next((a for a in addrs if a.family == socket.AF_INET and a.address == ip), None)
                if not v4:
                    continue
                out["iface"] = name
                if v4.netmask:
                    out["cidr"] = str(ipaddress.ip_network(f"{ip}/{v4.netmask}", strict=False))
                st = stats.get(name)
                out["speed_mbps"] = (st.speed or None) if st else None
                out["mac"] = next((a.address.replace("-", ":").lower() for a in addrs if getattr(a.family, "name", "") in ("AF_LINK", "AF_PACKET")), "")
                break
        except Exception:
            pass
    if ip and not out["cidr"]:
        out["cidr"] = str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return out


async def tcp_ping(host: str, port: int, timeout: float = 1.5) -> float | None:
    """TCP bağlantı süresi (ms). Bağlantı reddi de cihazın ayakta olduğunu gösterir."""
    t0 = now()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
    except (ConnectionRefusedError, ConnectionResetError):
        pass
    except Exception:
        return None
    return round((now() - t0) * 1000, 1)


class Network:
    def __init__(self, ctx):
        self.ctx, self.db = ctx, ctx.db
        self.info: dict = {}
        self.modem = {"ip": "", "up": None, "rtt_ms": None}
        self.internet = {"up": None, "rtt_ms": None, "dns": None}
        self.hist: deque = deque(maxlen=720)
        self.last_scan: dict = {}
        self._task: asyncio.Task | None = None
        self._relocating: set[int] = set()
        self._bg: set[asyncio.Task] = set()
        self._prev_wan = (now(), 0, 0)

    def start(self) -> None:
        self._task = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._bg):
            t.cancel()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ---- modem ve internet ----------------------------------------------------
    async def _watch(self) -> None:
        tick = 0
        while True:
            try:
                await self._probe(tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ağ izleme")
            tick += 1
            await asyncio.sleep(5)

    async def _probe(self, tick: int) -> None:
        if self.ctx.sim:
            self.info = {"ip": "192.168.50.10", "gateway": "192.168.50.1", "iface": "Ethernet", "cidr": "192.168.50.0/24", "speed_mbps": 1000, "mac": "02:00:00:00:00:10"}
            self.modem = {"ip": "192.168.50.1", "up": True, "rtt_ms": round(random.uniform(0.6, 1.8), 1)}
            self.internet = {"up": True, "rtt_ms": round(random.uniform(32, 58), 1), "dns": True}
        else:
            if tick % 12 == 0 or not self.info:
                self.info = await asyncio.to_thread(local_network)
            ip = (self.db.get("net.modem_ip") or "").strip() or self.info.get("gateway") or ""
            rtt = None
            if ip:
                for port in (80, 443, 53):
                    rtt = await tcp_ping(ip, port, 1.2)
                    if rtt is not None:
                        break
            self.modem = {"ip": ip, "up": rtt is not None if ip else None, "rtt_ms": rtt}
            inet = await tcp_ping("1.1.1.1", 443, 2.5)
            if inet is None:
                inet = await tcp_ping("8.8.8.8", 443, 2.5)
            self.internet.update(up=inet is not None, rtt_ms=inet)
            if tick % 12 == 0:
                try:
                    await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo("huggingface.co", 443), 4)
                    self.internet["dns"] = True
                except Exception:
                    self.internet["dns"] = False
        t = now()
        f, u = self.ctx.fabric, self.ctx.uplink
        down, up = f.wan_down + u.down_bytes, u.up_bytes
        p = self._prev_wan
        dt = max(0.5, t - p[0])
        self.hist.append({"t": round(t, 1), "modem": self.modem["rtt_ms"], "inet": self.internet["rtt_ms"],
                          "down": int(max(0, down - p[1]) / dt), "up": int(max(0, up - p[2]) / dt)})
        self._prev_wan = (t, down, up)

    def status(self) -> dict:
        return {"local": self.info, "modem": {**self.modem, "name": self.db.get("net.modem_name") or "Modem"}, "internet": self.internet,
                "hist": {k: [x[k] for x in self.hist] for k in ("t", "modem", "inet", "down", "up")}, "last_scan": self.last_scan}

    # ---- ağ taraması ----------------------------------------------------------
    async def scan(self, cidr: str = "", ssh_port: int = 22, agent_port: int = 7777) -> dict:
        info = self.info or await asyncio.to_thread(local_network)
        cidr = (cidr or info.get("cidr") or "").strip()
        if self.ctx.sim:
            return self._sim_scan(cidr or "192.168.50.0/24")
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError(f"Geçersiz ağ: {cidr}")
        if net.version != 4 or net.num_addresses > 1024:
            raise ValueError("Tarama için en fazla /22 büyüklüğünde bir IPv4 ağı girin")
        hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
        sem = asyncio.Semaphore(120)
        known = {n.host: n for n in self.ctx.cluster.nodes.values()}
        modem = self.modem.get("ip") or info.get("gateway")
        http = self.ctx.cluster.http
        found: dict[str, dict] = {}

        async def probe(ip: str) -> None:
            async with sem:
                e = {"ip": ip, "ssh": False, "banner": "", "ubuntu": False, "agent": False, "agent_id": "", "agent_version": ""}
                try:
                    r, w = await asyncio.wait_for(asyncio.open_connection(ip, ssh_port), 0.5)
                    e["ssh"] = True
                    try:
                        e["banner"] = (await asyncio.wait_for(r.readline(), 0.7)).decode("latin-1").strip()[:80]
                    except Exception:
                        pass
                    e["ubuntu"] = "ubuntu" in e["banner"].lower()
                    w.close()
                except Exception:
                    pass
                if await tcp_open(ip, agent_port):
                    try:
                        j = (await http.get(f"http://{ip}:{agent_port}/healthz", timeout=1.5)).json()
                        e.update(agent=True, agent_id=j.get("id", ""), agent_version=j.get("agent", ""))
                    except Exception:
                        pass
                if e["ssh"] or e["agent"] or ip == modem:
                    found[ip] = e

        async def tcp_open(ip: str, port: int) -> bool:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.4)
                w.close()
                return True
            except Exception:
                return False

        await asyncio.gather(*(probe(h) for h in hosts))
        arp = await asyncio.to_thread(arp_table)
        for ip, mac in arp.items():
            try:
                if ipaddress.ip_address(ip) in net and ip not in found and not mac.startswith(("ff:ff", "01:00:5e", "33:33")):
                    found[ip] = {"ip": ip, "ssh": False, "banner": "", "ubuntu": False, "agent": False, "agent_id": "", "agent_version": ""}
            except ValueError:
                pass
        rows = []
        for ip, e in found.items():
            n = known.get(ip)
            kind = "modem" if ip == modem else "self" if ip == info.get("ip") else "node" if n else "agent" if e["agent"] else "ubuntu" if e["ubuntu"] else "ssh" if e["ssh"] else "other"
            rows.append({**e, "mac": arp.get(ip, ""), "kind": kind, "node": n.name if n else "", "node_id": n.id if n else None})
        rows.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
        self.last_scan = {"ts": now(), "cidr": cidr, "hosts": rows, "scanned": len(hosts)}
        return self.last_scan

    def _sim_scan(self, cidr: str) -> dict:
        rows = [{"ip": "192.168.50.1", "ssh": False, "banner": "", "ubuntu": False, "agent": False, "agent_id": "", "agent_version": "", "mac": "02:00:5e:00:00:01",
                 "kind": "modem", "node": "", "node_id": None}]
        for n in sorted(self.ctx.cluster.nodes.values(), key=lambda x: x.host):
            rows.append({"ip": n.host, "ssh": True, "banner": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13", "ubuntu": True, "agent": True, "agent_id": "", "agent_version": "0.3.0",
                         "mac": n.mac, "kind": "node", "node": n.name, "node_id": n.id})
        rows.append({"ip": "192.168.50.31", "ssh": True, "banner": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13", "ubuntu": True, "agent": False, "agent_id": "", "agent_version": "",
                     "mac": "02:00:00:00:00:31", "kind": "ubuntu", "node": "", "node_id": None})
        self.last_scan = {"ts": now(), "cidr": cidr, "hosts": rows, "scanned": 254}
        return self.last_scan

    # ---- adresi değişen makineyi yeniden bulma --------------------------------
    def relocate_soon(self, n) -> None:
        if n.sim or n.id in self._relocating or not (n.hw or {}).get("node_id"):
            return
        self._relocating.add(n.id)
        self._spawn(self._relocate(n))

    async def _relocate(self, n) -> None:
        """Modem DHCP ile adres değiştirirse makine kaybolmasın: alt ağda aynı kimlikli ajanı arar."""
        try:
            await asyncio.sleep(2)
            info = self.info or await asyncio.to_thread(local_network)
            if not info.get("cidr"):
                return
            net = ipaddress.ip_network(info["cidr"], strict=False)
            if net.num_addresses > 1024:
                return
            want, http, sem = n.hw["node_id"], self.ctx.cluster.http, asyncio.Semaphore(64)
            hit: list[str] = []

            async def check(ip: str) -> None:
                async with sem:
                    try:
                        _, w = await asyncio.wait_for(asyncio.open_connection(ip, n.agent_port), 0.4)
                        w.close()
                        j = (await http.get(f"http://{ip}:{n.agent_port}/healthz", timeout=1.5)).json()
                        if j.get("id") == want:
                            hit.append(ip)
                    except Exception:
                        pass

            await asyncio.gather(*(check(str(h)) for h in net.hosts()))
            for ip in hit:
                r = await http.get(f"http://{ip}:{n.agent_port}/v1/info", headers={"Authorization": f"Bearer {n.agent_token}"}, timeout=5)
                if r.status_code == 200 and r.json().get("node_id") == want and ip != n.host:
                    self.db.x("UPDATE nodes SET host=? WHERE id=?", (ip, n.id))
                    self.db.event("info", "node", f"{n.name} yeni adreste bulundu: {n.host} → {ip}", n.id)
                    self.ctx.cluster.reload()
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("yeniden bulma %s: %s", n.name, e)
        finally:
            self._relocating.discard(n.id)


async def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            return (await c.get(url)).status_code < 500
    except Exception:
        return False
