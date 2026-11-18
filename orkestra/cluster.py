"""Küme çekirdeği.

* NodeRT / DepRT  : bellekteki çalışma durumu (düğümler ve model dağıtımları)
* Cluster         : düğüm yoklama, uzlaştırma (istenen durum -> gerçek durum), yerleştirme, güç yönetimi
* HttpAgentClient : node ajanı ile HTTP iletişimi
* send_wol        : Wake-on-LAN sihirli paketi
"""
from __future__ import annotations

import asyncio
import re
import socket
from collections import deque

import httpx

from . import runtimes as rtm
from .util import EMA, jdump, jload, log, now


def _local_ips() -> set[str]:
    """Bu makinenin adresleri: kontrol sunucusuyla aynı makinede çalışan düğümü (ana PC) tanımak için."""
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


LOCAL_IPS = _local_ips()


# --------------------------------------------------------------------------- ajan istemcisi
class AgentError(Exception):
    pass


class HttpAgentClient:
    def __init__(self, host: str, port: int, token: str, http: httpx.AsyncClient):
        self.host = host
        self.base = f"http://{host}:{port}"
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = http

    async def _req(self, method: str, path: str, timeout: float = 5.0, **kw):
        r = await self.http.request(method, self.base + path, headers=self.headers, timeout=timeout, **kw)
        if r.status_code >= 400:
            msg = r.text[:200]
            try:
                msg = r.json().get("error", msg)
            except Exception:
                pass
            raise AgentError(f"HTTP {r.status_code}: {msg}")
        return r.json()

    async def info(self):
        return await self._req("GET", "/v1/info")

    async def metrics(self):
        return await self._req("GET", "/v1/metrics")

    async def launch(self, spec: dict):
        return await self._req("POST", "/v1/launch", json=spec, timeout=20)

    async def stop(self, proc_id: str, grace: int = 15):
        return await self._req("POST", "/v1/stop", json={"id": proc_id, "grace_s": grace}, timeout=grace + 10)

    async def logs(self, proc_id: str, tail: int = 200) -> str:
        r = await self._req("GET", f"/v1/logs?id={proc_id}&tail={int(tail)}")
        return r.get("text", "")

    async def power(self, action: str):
        return await self._req("POST", "/v1/power", json={"action": action})

    # ---- ağ üzerinden dağıtım -------------------------------------------------
    async def fabric_state(self):
        return await self._req("GET", "/v1/fabric/state", timeout=10)

    async def fabric_jobs(self):
        return await self._req("GET", "/v1/fabric/jobs")

    async def fabric_log(self, jid: str, tail: int = 200) -> str:
        return (await self._req("GET", f"/v1/fabric/log?id={jid}&tail={int(tail)}")).get("text", "")

    async def fabric_fetch(self, spec: dict):
        return await self._req("POST", "/v1/fabric/fetch", json=spec, timeout=15)

    async def fabric_install(self, spec: dict):
        return await self._req("POST", "/v1/fabric/install", json=spec, timeout=15)

    async def fabric_build(self, spec: dict):
        return await self._req("POST", "/v1/fabric/build", json=spec, timeout=15)

    async def fabric_link(self, spec: dict):
        return await self._req("POST", "/v1/fabric/link", json=spec, timeout=60)

    async def fabric_remove(self, spec: dict):
        return await self._req("POST", "/v1/fabric/remove", json=spec, timeout=120)

    async def fabric_cancel(self, jid: str):
        return await self._req("POST", "/v1/fabric/cancel", json={"id": jid})

    async def fabric_update(self, spec: dict):
        return await self._req("POST", "/v1/fabric/update", json=spec, timeout=20)

    async def fabric_bwtest(self, spec: dict):
        return await self._req("POST", "/v1/fabric/bwtest", json=spec, timeout=30)

    def runtime_base(self, port: int) -> str:
        return f"http://{self.host}:{port}"


# --------------------------------------------------------------------------- Wake-on-LAN
def magic_packet(mac: str) -> bytes:
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(h) != 12:
        raise ValueError(f"Geçersiz MAC adresi: {mac!r}")
    b = bytes.fromhex(h)
    return b"\xff" * 6 + b * 16


def send_wol(mac: str, host_hint: str = "", port_list=(9, 7)) -> list[str]:
    """Sihirli paketi hem sınırlı hem alt ağ yayınına (ör. 192.168.1.255) gönderir.

    Birden çok ağ kartı olan bilgisayarda (Ethernet + VPN + Wi-Fi) paket doğru karttan, yani switch'e
    bağlı olandan çıksın diye gönderim hedef makineye giden adresten yapılır.
    """
    pkt = magic_packet(mac)
    targets = ["255.255.255.255"]
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)\.\d+$", host_hint or "")
    src = ""
    if m:
        targets.append(f"{m.group(1)}.{m.group(2)}.{m.group(3)}.255")
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((host_hint, 9))
            src = probe.getsockname()[0]
            probe.close()
        except OSError:
            src = ""
    sent = []
    for t in targets:
        for p in port_list:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if src:
                    s.bind((src, 0))
                s.sendto(pkt, (t, p))
                s.close()
                sent.append(f"{t}:{p}")
            except OSError as e:
                log.warning("WoL gönderilemedi %s:%s -> %s", t, p, e)
    return sent


# --------------------------------------------------------------------------- çalışma durumu nesneleri
class NodeRT:
    def __init__(self, row: dict):
        self.status = row["last_status"] if row["last_status"] in ("sleeping", "offline") else "unknown"
        self.persisted_status = row["last_status"]
        self.intent: str | None = None
        self.intent_since = 0.0
        self.metrics: dict = {}
        self.procs: dict = {}
        self.fail = 0
        self.last_seen = 0.0
        self.last_error = ""
        self.last_info = 0.0
        self.next_poll = 0.0
        self.draining = False
        self.thermal = "ok"  # ok | warn | crit
        self.hot_polls = 0
        self.cool_since = 0.0
        self.max_temp = 0.0
        self.health = "unknown"
        self.health_reasons: list[str] = []
        self.idle_since: float | None = None
        self.reboots: deque = deque(maxlen=12)
        self.last_recover_wake = 0.0
        self.gpu_lost = False
        self.hist: deque = deque(maxlen=1200)  # canlı grafikler için son örnekler (~1 saat)
        self.tok_prev: tuple | None = None
        self.hw: dict = jload(row["hw"], {})
        self.apply_row(row)

    def apply_row(self, row: dict) -> None:
        self.id = row["id"]
        self.name = row["name"]
        self.host = row["host"]
        self.agent_port = row["agent_port"]
        self.agent_token = row["agent_token"]
        self.mac = row["mac"]
        self.role = row["role"]
        self.tags = jload(row["tags"], [])
        self.enabled = bool(row["enabled"])
        self.allow_sleep = bool(row["allow_sleep"])
        self.allow_restart = bool(row["allow_restart"])
        self.always_on = bool(row["always_on"])
        self.weight = float(row["weight"])
        self.warn_c = float(row["warn_c"])
        self.crit_c = float(row["crit_c"])
        self.sim = bool(row["sim"])
        self.rt_paths: dict = jload(row.get("rt_paths"), {}) or {}  # runtime anahtarı -> bu makinedeki çalıştırılabilir yol
        self.is_self = (not self.sim) and row["host"] in LOCAL_IPS  # kontrol sunucusunun çalıştığı makine


class DepRT:
    def __init__(self, row: dict):
        self.id = row["id"]
        self.model_id = row["model_id"]
        self.node_id = row["node_id"]
        self.port = row["port"]
        self.desired = row["desired"]
        self.origin = row["origin"]
        self.pinned = bool(row["pinned"])
        self.gpus: list[int] = jload(row["gpus"], [])
        self.created_at = row["created_at"]
        self.last_used = row["last_used_at"] or row["created_at"]
        self.state = "pending"  # pending | syncing | starting | ready | failed | stopped
        self.sync: dict | None = None  # dosya aktarımı/kurulum ilerlemesi
        self.build = ""
        self.probed = False
        self.probe: asyncio.Task | None = None
        self.probe_fail = 0
        self.inflight = 0
        self.served = 0
        self.tok_count = 0
        self.started_at = 0.0
        self.ready_at = 0.0
        self.launch_ts = 0.0
        self.fail = 0
        self.next_try = 0.0
        self.last_error = ""
        self.last_health = 0.0
        self.health_fail = 0
        self.hook_done = False
        self.prep: asyncio.Task | None = None
        self.tps = EMA(0.3)
        self.ttft = EMA(0.3)

    @property
    def proc_id(self) -> str:
        return f"dep-{self.id}"


class Placement:
    def __init__(self, node: NodeRT, gpus: list[int], cpu: bool, evict: list[DepRT], score: float):
        self.node = node
        self.gpus = gpus
        self.cpu = cpu
        self.evict = evict
        self.score = score

    @property
    def needs_wake(self) -> bool:
        return self.node.status == "sleeping"


OS_RAM_GB = 2.0


# --------------------------------------------------------------------------- küme
class Cluster:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.nodes: dict[int, NodeRT] = {}
        self.deps: dict[int, DepRT] = {}
        self.models: dict[int, dict] = {}
        self.runtimes: dict[str, dict] = {}
        self.http = httpx.AsyncClient(limits=httpx.Limits(max_connections=200, max_keepalive_connections=50), timeout=5.0)
        self._clients: dict[int, tuple] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        self.bad_places: dict[tuple[int, int], float] = {}
        self.kick = asyncio.Event()
        self.listeners: list = []
        self._last_store = 0.0
        self.reload()

    # ---- yükleme ------------------------------------------------------------
    def reload(self) -> None:
        rows = {r["id"]: r for r in self.db.q("SELECT * FROM nodes")}
        for nid, r in rows.items():
            if nid in self.nodes:
                self.nodes[nid].apply_row(r)
            else:
                self.nodes[nid] = NodeRT(r)
        for nid in list(self.nodes):
            if nid not in rows:
                self.nodes.pop(nid)
                self._clients.pop(nid, None)
        self.models = {r["id"]: r for r in self.db.q("SELECT * FROM models")}
        self.runtimes = {r["key"]: r for r in self.db.q("SELECT * FROM runtimes")}
        drows = {r["id"]: r for r in self.db.q("SELECT * FROM deployments")}
        for did, r in drows.items():
            if did in self.deps:
                d = self.deps[did]
                d.desired, d.pinned, d.origin = r["desired"], bool(r["pinned"]), r["origin"]
            else:
                self.deps[did] = DepRT(r)
        for did in list(self.deps):
            if did not in drows:
                self.deps.pop(did)
        self.kick.set() if self._tasks else None

    def client(self, n: NodeRT):
        key = (n.host, n.agent_port, n.agent_token, n.sim)
        c = self._clients.get(n.id)
        if c is None or c[0] != key:
            if n.sim and self.ctx.sim:
                cl = self.ctx.sim.client_for(n)
            else:
                cl = HttpAgentClient(n.host, n.agent_port, n.agent_token, self.http)
            self._clients[n.id] = (key, cl)
        return self._clients[n.id][1]

    def _lock(self, nid: int) -> asyncio.Lock:
        return self._locks.setdefault(nid, asyncio.Lock())

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def deps_on(self, node_id: int) -> list[DepRT]:
        return [d for d in self.deps.values() if d.node_id == node_id]

    def deps_of(self, model_id: int) -> list[DepRT]:
        return [d for d in self.deps.values() if d.model_id == model_id]

    def dep_base(self, d: DepRT) -> str:
        return self.client(self.nodes[d.node_id]).runtime_base(d.port)

    def notify(self, model_name: str) -> None:
        for cb in self.listeners:
            try:
                cb(model_name)
            except Exception:
                log.exception("listener")

    # ---- yaşam döngüsü ------------------------------------------------------
    def start(self) -> None:
        for coro in (self._poll_loop(), self._reconcile_loop(), self._housekeeping_loop()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in list(self._bg):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._bg, return_exceptions=True)
        await self.http.aclose()

    # ---- yoklama ------------------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            try:
                t = now()
                due = [n for n in self.nodes.values() if n.next_poll <= t]
                if due:
                    await asyncio.gather(*(self.poll_node(n) for n in due), return_exceptions=True)
                if t - self._last_store >= 10:
                    self._last_store = t
                    self._store_metrics()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll_loop")
            await asyncio.sleep(1.0)

    async def poll_node(self, n: NodeRT) -> None:
        t = now()
        fast = n.status in ("online", "waking", "rebooting", "unknown") or n.intent
        n.next_poll = t + (3.0 if fast else 8.0)
        cl = self.client(n)
        try:
            m = await asyncio.wait_for(cl.metrics(), timeout=4.5)
        except Exception as e:
            n.fail += 1
            n.last_error = f"{type(e).__name__}: {str(e)[:140]}"
            self._on_fail(n)
            return
        up_now = n.status != "online"
        n.fail = 0
        n.last_seen = t
        n.metrics = m
        n.procs = {p["id"]: p for p in m.get("procs", [])}
        self.ctx.fabric.on_metrics(n, m)
        if up_now:
            self._on_up(n)
        elif n.intent in ("sleep", "reboot") and t - n.intent_since > 90:
            self.db.event("warn", "power", f"{n.name}: '{n.intent}' komutu uygulanmadı, düğüm hâlâ açık (sudo izni/BIOS?)", n.id)
            n.intent, n.draining = None, False
            self.kick.set()
        if up_now or not n.hw or t - n.last_info > 60:  # izin/yol değişiklikleri (orkestra-agent-allow) bir dakikada görünür
            try:
                info = await asyncio.wait_for(cl.info(), timeout=4.5)
                self._apply_info(n, info)
                n.last_info = t
            except Exception:
                pass
        self._evaluate(n)
        self._sample(n, t)

    def _sample(self, n: NodeRT, t: float) -> None:
        """Canlı grafikler için bu yoklamanın özetini tampona ekler (token/sn dahil)."""
        m = n.metrics
        gp = m.get("gpus") or []
        net = m.get("net") or {}
        deps = self.deps_on(n.id)
        tok = sum(d.tok_count for d in deps)
        tps = 0.0
        if n.tok_prev and t > n.tok_prev[0]:
            tps = max(0.0, (tok - n.tok_prev[1]) / (t - n.tok_prev[0]))
        n.tok_prev = (t, tok)
        n.hist.append({
            "t": round(t, 1), "cpu": m.get("cpu"), "mem": (m.get("mem") or {}).get("pct"), "ct": m.get("cpu_temp"),
            "pw": round(sum(g.get("power_w") or 0 for g in gp) + (m.get("cpu_power_w") or 0), 1),
            "rx": net.get("rx_bps"), "tx": net.get("tx_bps"), "in": sum(d.inflight for d in deps), "tps": round(tps, 1),
            "g": [[g.get("util"), (g["mem_used_gb"] / g["mem_total_gb"] * 100) if g.get("mem_total_gb") else None,
                   g.get("temp"), g.get("power_w")] for g in gp],
        })

    def live(self, window: float = 300.0, since: float = 0.0) -> dict:
        """Tüm düğümlerin son örneklerini sütunlu (columnar) biçimde döndürür."""
        t0 = max(since, now() - window)
        out = {}
        for n in self.nodes.values():
            rows = [s for s in n.hist if s["t"] > t0]
            if not rows and since:
                continue
            ng = max((len(s["g"]) for s in rows), default=0)
            col = {k: [s[k] for s in rows] for k in ("t", "cpu", "mem", "ct", "pw", "rx", "tx", "in", "tps")}
            for k, idx in (("gu", 0), ("gm", 1), ("gt", 2), ("gp", 3)):
                col[k] = [[(s["g"][i][idx] if i < len(s["g"]) else None) for s in rows] for i in range(ng)]
            out[str(n.id)] = col
        return {"now": now(), "nodes": out}

    def _set_status(self, n: NodeRT, status: str) -> None:
        n.status = status
        if status in ("sleeping", "offline"):
            n.draining, n.intent = False, None  # uyuyan/kapalı düğüm artık "boşaltılıyor" değildir; yerleştirme için aday olabilir
        if status in ("sleeping", "offline", "online") and n.persisted_status != status:
            n.persisted_status = status
            self.db.x("UPDATE nodes SET last_status=? WHERE id=?", (status, n.id))

    def _on_up(self, n: NodeRT) -> None:
        prev = n.status
        self._set_status(n, "online")
        n.draining = False
        n.intent = None
        n.fail = 0
        n.idle_since = None
        n.hot_polls = 0
        if prev != "unknown":
            self.db.event("info", "node", f"{n.name} çevrimiçi ({prev} → online)", n.id)
        self.kick.set()

    def _on_fail(self, n: NodeRT) -> None:
        st, t = n.status, now()
        if st == "online":
            if n.intent in ("sleep", "reboot") and n.fail >= 2:
                if n.intent == "sleep":
                    self._set_status(n, "sleeping")
                    self.db.event("info", "power", f"{n.name} uykuda", n.id)
                else:
                    n.status = "rebooting"
                    self.db.event("info", "power", f"{n.name} yeniden başlıyor", n.id)
            elif n.fail >= 4:
                self._set_status(n, "offline")
                self.db.event("error", "node", f"{n.name} bağlantısı kesildi: {n.last_error}", n.id)
                self.ctx.network.relocate_soon(n)
        elif st == "waking":
            if t - n.intent_since > float(self.db.get("auto.wake_timeout_s")):
                self._set_status(n, "offline")
                n.intent = None
                self.db.event("error", "power", f"{n.name} uyandırılamadı (zaman aşımı)", n.id)
        elif st == "rebooting":
            if t - n.intent_since > 900:
                self._set_status(n, "offline")
                n.intent = None
                self.db.event("error", "power", f"{n.name} yeniden başlatma sonrası dönmedi", n.id)
        elif st == "unknown" and n.fail >= 2:
            self._set_status(n, "sleeping" if n.persisted_status == "sleeping" else "offline")

    def _apply_info(self, n: NodeRT, info: dict) -> None:
        n.hw = info
        self.ctx.fabric.on_info(n, info)
        mac = info.get("primary_mac") or ""
        if mac and not n.mac:
            n.mac = mac
            self.db.x("UPDATE nodes SET mac=? WHERE id=?", (mac, n.id))
        self.db.x("UPDATE nodes SET hw=? WHERE id=?", (jdump(info), n.id))

    def _evaluate(self, n: NodeRT) -> None:
        m = n.metrics
        temps = [g.get("temp") for g in m.get("gpus", []) if g.get("temp") is not None]
        if m.get("cpu_temp") is not None:
            temps.append(m["cpu_temp"])
        n.max_temp = max(temps) if temps else 0.0
        reasons, level = [], "ok"
        t = now()
        if n.max_temp >= n.crit_c:
            n.hot_polls += 1
            n.cool_since = 0.0
            reasons.append(f"sıcaklık {n.max_temp:.0f}°C (kritik ≥ {n.crit_c:.0f})")
            level = "crit"
            if n.hot_polls >= 3 and n.thermal != "crit":
                n.thermal = "crit"
                self.db.event("error", "thermal", f"{n.name} aşırı ısındı: {n.max_temp:.0f}°C", n.id)
        else:
            n.hot_polls = 0
            if n.thermal == "crit":
                if n.max_temp < n.warn_c - 3:
                    n.cool_since = n.cool_since or t
                    if t - n.cool_since >= 30:
                        n.thermal = "ok"
                        n.cool_since = 0.0
                        self.db.event("info", "thermal", f"{n.name} soğudu: {n.max_temp:.0f}°C", n.id)
                else:
                    n.cool_since = 0.0
            if n.thermal != "crit":
                n.thermal = "warn" if n.max_temp >= n.warn_c else "ok"
            if n.max_temp >= n.warn_c:
                reasons.append(f"sıcaklık yüksek {n.max_temp:.0f}°C")
                level = "warn" if level == "ok" else level
        mem = (m.get("mem") or {}).get("pct", 0)
        if mem >= 95:
            reasons.append(f"bellek %{mem:.0f}")
            level = "warn" if level == "ok" else level
        disk = (m.get("disk") or {}).get("pct", 0)
        if disk >= 95:
            reasons.append(f"disk %{disk:.0f} dolu")
            level = "warn" if level == "ok" else level
        hw_gpus = len((n.hw or {}).get("gpus", []))
        n.gpu_lost = bool(hw_gpus and len(m.get("gpus", [])) < hw_gpus and m.get("gpu_error"))
        if n.gpu_lost:
            reasons.append("GPU kayıp: " + str(m.get("gpu_error"))[:80])
            level = "crit"
        n.health, n.health_reasons = level, reasons

    def _store_metrics(self) -> None:
        t = int(now())
        rows = []
        for n in self.nodes.values():
            if n.status != "online" or not n.metrics:
                continue
            m = n.metrics
            gp = m.get("gpus") or []
            gu = sum(g.get("util") or 0 for g in gp) / len(gp) if gp else None
            gm = max(((g["mem_used_gb"] / g["mem_total_gb"] * 100) for g in gp if g.get("mem_total_gb")), default=None)
            gt = max((g["temp"] for g in gp if g.get("temp") is not None), default=None)
            power = sum(g.get("power_w") or 0 for g in gp) + (m.get("cpu_power_w") or 0)
            net = ((m.get("net") or {}).get("rx_bps") or 0) + ((m.get("net") or {}).get("tx_bps") or 0)
            infl = sum(d.inflight for d in self.deps_on(n.id))
            rows.append((n.id, t, m.get("cpu"), (m.get("mem") or {}).get("pct"), m.get("cpu_temp"), gu, gm, gt, power, net, infl))
        if rows:
            self.db.xmany("INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)

    async def _housekeeping_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(300)
                days = max(1, int(self.db.get("sys.retention_days")))
                cutoff = now() - days * 86400
                self.db.x("DELETE FROM metrics WHERE ts < ?", (int(cutoff),))
                self.db.x("DELETE FROM req_log WHERE ts < ?", (cutoff,))
                self.db.x("DELETE FROM events WHERE ts < ?", (cutoff,))
                for k, until in list(self.bad_places.items()):
                    if until < now():
                        self.bad_places.pop(k, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping")

    # ---- uzlaştırma (reconcile) --------------------------------------------
    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.kick.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self.kick.clear()
            try:
                await asyncio.gather(*(self._reconcile_node(n) for n in list(self.nodes.values())), return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile_loop")
            await asyncio.sleep(0.2)

    async def _reconcile_node(self, n: NodeRT) -> None:
        lock = self._lock(n.id)
        if lock.locked():
            return
        async with lock:
            deps = self.deps_on(n.id)
            if n.status != "online":
                for d in deps:
                    if d.desired == "running" and d.state != "failed":
                        d.state = "pending"
                return
            cl = self.client(n)
            for pid, p in list(n.procs.items()):
                if not pid.startswith("dep-") or p.get("state") != "running":
                    continue
                try:
                    did = int(pid[4:])
                except ValueError:
                    continue
                d = self.deps.get(did)
                if d is None or d.desired != "running":
                    await self._stop_proc(cl, n, pid)
            await asyncio.gather(*(self._reconcile_dep(n, d, cl) for d in deps), return_exceptions=True)

    async def _stop_proc(self, cl, n: NodeRT, proc_id: str) -> None:
        try:
            await cl.stop(proc_id)
        except Exception as e:
            log.warning("stop %s@%s: %s", proc_id, n.name, e)
        n.procs.pop(proc_id, None)

    def _dep_failed(self, n: NodeRT, d: DepRT, m: dict, msg: str) -> None:
        t = now()
        d.state = "failed"
        d.fail += 1
        d.last_error = msg
        d.hook_done = False
        d.prep = None
        d.sync = None
        d.probed, d.probe, d.probe_fail = False, None, 0
        d.next_try = t + min(300, 8 * 2 ** (d.fail - 1))
        self.db.event("warn", "deploy", f"{m['name']}@{n.name}: {msg} (deneme {d.fail})", n.id)
        if d.fail >= 3 and d.origin == "autoscaler" and not d.pinned:
            self.bad_places[(m["id"], n.id)] = t + 600
            self.spawn(self.remove_deployment(d.id, f"{m['name']} {n.name} üzerinde 3 kez başarısız oldu"))

    async def _launch(self, n: NodeRT, d: DepRT, m: dict, rt: dict, cl, local: dict | None = None) -> None:
        total = 0.0
        if d.gpus:
            for g in n.hw.get("gpus", []):
                if g.get("index") == d.gpus[0]:
                    total = float(g.get("vram_gb", 0))
        spec = rtm.build_spec(rt, m, {"id": d.id, "port": d.port, "gpus": d.gpus}, total, self.db.get("hf.token") or "",
                              exe_override=(n.rt_paths.get(rt["key"]) or "").strip(), local=local, hw=n.hw)
        await cl.launch(spec)
        t = now()
        d.state, d.started_at, d.launch_ts, d.health_fail = "starting", t, t, 0
        d.sync, d.build = None, (local or {}).get("build", "")
        d.probed, d.probe, d.probe_fail = False, None, 0
        d.hook_done = not rt.get("hook")
        self.db.event("info", "deploy", f"{m['name']} → {n.name}:{d.port} başlatıldı", n.id)

    async def _health(self, n: NodeRT, d: DepRT, rt: dict) -> bool:
        try:
            r = await self.http.get(self.dep_base(d) + rt["health"], timeout=2.5)
            return r.status_code == 200
        except Exception:
            return False

    async def _reconcile_dep(self, n: NodeRT, d: DepRT, cl) -> None:
        t = now()
        m = self.models.get(d.model_id)
        proc = n.procs.get(d.proc_id)
        running = bool(proc and proc.get("state") == "running")
        if d.desired != "running" or not m or not m["enabled"]:
            if running:
                await self._stop_proc(cl, n, d.proc_id)
            d.state = "stopped"
            return
        if n.draining or n.thermal == "crit" or not n.enabled:
            if running:
                await self._stop_proc(cl, n, d.proc_id)
            d.state = "pending"
            return
        rt = self.runtimes.get(m["runtime"])
        if rt is None:
            d.state, d.last_error = "failed", f"runtime tanımsız: {m['runtime']}"
            return
        if not running:
            if d.state in ("starting", "ready") and t - d.launch_ts > 12:
                rc = proc.get("rc") if proc else None
                self._dep_failed(n, d, m, f"süreç sonlandı (rc={rc})")
                return
            if d.state == "starting" and t - d.launch_ts <= 12:
                return
            if t < d.next_try:
                return
            res = self.ctx.fabric.ensure(n, d, m, rt)
            if res["state"] == "working":
                d.state, d.sync = "syncing", {"step": res.get("step"), "pct": res.get("pct")}
                return
            if res["state"] == "failed":
                self._dep_failed(n, d, m, res["error"])
                return
            try:
                await self._launch(n, d, m, rt, cl, res)
            except Exception as e:
                self._dep_failed(n, d, m, f"başlatma hatası: {e}")
            return
        if d.state in ("pending", "failed", "stopped", "syncing"):
            d.state = "starting"
            d.sync = None
            d.started_at = proc.get("started") or t
            d.launch_ts = d.launch_ts or t
            d.hook_done = not rt.get("hook")
        if t - d.last_health < 2.5:
            return
        d.last_health = t
        ok = await self._health(n, d, rt)
        if ok and rt.get("hook") == "ollama" and not d.hook_done:
            if d.prep is None:
                d.prep = self.spawn(self._ollama_prepare(n, d, m))
            ok = False
        if ok and d.state != "ready" and m["kind"] == "llm" and self.db.get("deploy.smoke_test") and not d.probed:
            ok = False  # sağlık yeşil olsa da gerçek bir yanıt üretmeden trafik verilmez
            if d.probe is None or d.probe.done():
                if d.probe_fail >= 4:
                    await self._stop_proc(cl, n, d.proc_id)
                    self._dep_failed(n, d, m, f"deneme çıkarımı başarısız: {d.last_error}")
                    return
                d.probe = self.spawn(self._smoke(n, d, m, rt))
        if ok:
            d.health_fail = 0
            if d.state != "ready":
                d.state, d.ready_at, d.fail, d.last_error = "ready", t, 0, ""
                self.db.event("info", "deploy", f"{m['name']} hazır: {n.name}:{d.port} ({t - d.started_at:.0f} sn)", n.id)
                self.notify(m["name"])
        elif d.state == "ready":
            d.health_fail += 1
            if d.health_fail >= 10:
                await self._stop_proc(cl, n, d.proc_id)
                self._dep_failed(n, d, m, "sağlık kontrolü başarısız (25 sn)")
        elif d.state == "starting" and t - d.started_at > m["startup_timeout_s"] and (d.prep is None or d.prep.done()):
            await self._stop_proc(cl, n, d.proc_id)
            self._dep_failed(n, d, m, f"başlatma zaman aşımı ({m['startup_timeout_s']} sn)")

    async def _smoke(self, n: NodeRT, d: DepRT, m: dict, rt: dict) -> None:
        """Modelin gerçekten yanıt ürettiğini doğrular (1 token)."""
        url = self.dep_base(d) + rtm.rt_path(rt, "chat", "/v1/chat/completions")
        body = {"model": rtm.upstream_model(rt, m), "messages": [{"role": "user", "content": "merhaba"}], "max_tokens": 1, "stream": False}
        try:
            r = await self.http.post(url, json=body, timeout=max(60.0, float(m["startup_timeout_s"]) / 3))
            if r.status_code == 200 and r.json().get("choices"):
                d.probed, d.probe_fail = True, 0
                d.last_health = 0.0
                self.kick.set()
                return
            d.last_error = f"HTTP {r.status_code}: {r.text[:160]}"
        except Exception as e:
            d.last_error = f"{type(e).__name__}: {str(e)[:160]}"
        if d.state == "starting":
            d.probe_fail += 1

    async def _ollama_prepare(self, n: NodeRT, d: DepRT, m: dict) -> None:
        base = self.dep_base(d)
        try:
            await self.http.post(base + "/api/pull", json={"model": m["repo"], "stream": False}, timeout=3600)
            await self.http.post(base + "/api/generate", json={"model": m["repo"], "prompt": "", "keep_alive": -1}, timeout=900)
            d.hook_done = True
        except Exception as e:
            d.last_error = f"ollama hazırlık hatası: {e}"
            d.prep = None

    # ---- dağıtım yönetimi ---------------------------------------------------
    def next_port(self, n: NodeRT) -> int:
        used = {d.port for d in self.deps_on(n.id)} | {p.get("port") for p in n.procs.values() if p.get("port")}
        p = 18000
        while p in used:
            p += 1
        return p

    def create_deployment(self, model_id: int, node_id: int, origin: str = "manual", pinned: bool = False,
                          gpus: list[int] | None = None) -> DepRT:
        n = self.nodes[node_id]
        port = self.next_port(n)
        did = self.db.x(
            "INSERT INTO deployments(model_id,node_id,port,desired,origin,pinned,gpus,created_at,last_used_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (model_id, node_id, port, "running", origin, 1 if pinned else 0, jdump(gpus or []), now(), now()),
        )
        d = DepRT(self.db.one("SELECT * FROM deployments WHERE id=?", (did,)))
        self.deps[did] = d
        self.kick.set()
        return d

    async def remove_deployment(self, dep_id: int, reason: str = "") -> None:
        d = self.deps.get(dep_id)
        if not d:
            return
        n = self.nodes.get(d.node_id)
        m = self.models.get(d.model_id)
        d.desired = "stopped"
        self.deps.pop(dep_id, None)
        self.db.x("DELETE FROM deployments WHERE id=?", (dep_id,))
        if n and n.status == "online":
            await self._stop_proc(self.client(n), n, d.proc_id)
        if m:
            self.db.event("info", "deploy", f"{m['name']} kaldırıldı ({n.name if n else '?'}:{d.port})" + (f" — {reason}" if reason else ""),
                          n.id if n else None)
        self.notify(m["name"] if m else "")

    def set_desired(self, dep_id: int, desired: str) -> None:
        d = self.deps[dep_id]
        d.desired = desired
        if desired == "running":
            d.fail, d.next_try = 0, 0.0
        self.db.x("UPDATE deployments SET desired=? WHERE id=?", (desired, dep_id))
        self.kick.set()

    async def restart_deployment(self, dep_id: int) -> None:
        d = self.deps[dep_id]
        n = self.nodes[d.node_id]
        if n.status == "online":
            await self._stop_proc(self.client(n), n, d.proc_id)
        d.state, d.fail, d.next_try, d.launch_ts = "pending", 0, 0.0, 0.0
        d.hook_done, d.prep = False, None
        self.kick.set()

    def touch(self, d: DepRT) -> None:
        d.last_used = now()

    async def logs(self, node_id: int, proc_id: str, tail: int = 200) -> str:
        n = self.nodes[node_id]
        if n.status != "online":
            raise ValueError("Düğüm çevrimiçi değil")
        return await self.client(n).logs(proc_id, tail)

    # ---- kapasite ve yerleştirme -------------------------------------------
    def _free(self, n: NodeRT, ignore=frozenset()):
        hw_gpus = n.hw.get("gpus") or []
        live = {g["index"]: g for g in (n.metrics.get("gpus") or [])} if n.status == "online" else {}
        reserved = {g["index"]: 0.0 for g in hw_gpus}
        ram_res = 0.0
        for d in self.deps_on(n.id):
            if d.desired != "running" or d.id in ignore:
                continue
            dm = self.models.get(d.model_id)
            if not dm:
                continue
            if d.gpus:
                per = rtm.model_vram(dm) / len(d.gpus)
                for gi in d.gpus:
                    reserved[gi] = reserved.get(gi, 0.0) + per
                ram_res += rtm.ram_for_gpu_run(dm)
            else:
                ram_res += rtm.ram_for_cpu_run(dm)
        gfree = {}
        for g in hw_gpus:
            i = g["index"]
            obs = (live.get(i) or {}).get("mem_used_gb", 0.0)
            gfree[i] = g["vram_gb"] - (reserved[i] if ignore else max(reserved[i], obs))
        ram_total = float(n.hw.get("ram_gb", 0.0))
        ram_obs = float((n.metrics.get("mem") or {}).get("used_gb", 0.0)) if n.status == "online" else 0.0
        used = (ram_res + OS_RAM_GB) if ignore else max(ram_res + OS_RAM_GB, ram_obs)
        return gfree, ram_total - used

    def _fit(self, n: NodeRT, m: dict, rt: dict, mode: str, ignore=frozenset()):
        gfree, rfree = self._free(n, ignore)
        gc = int(m["gpu_count"])
        if gc > 0:
            per = rtm.model_vram(m) / gc
            ok = [(i, f) for i, f in gfree.items() if f >= per]
            ok.sort(key=lambda kv: kv[1], reverse=(mode == "performance"))
            if len(ok) >= gc and rfree >= rtm.ram_for_gpu_run(m):
                return [i for i, _ in ok[:gc]], False
            if not m["cpu_ok"] or rt["needs_gpu"]:
                return None
        if rt["needs_gpu"]:
            return None
        if (gc == 0 or m["cpu_ok"]) and rfree >= rtm.ram_for_cpu_run(m):
            return [], True
        return None

    def _evictable(self, n: NodeRT, m_req: dict) -> list[DepRT]:
        out = []
        t = now()
        for d in self.deps_on(n.id):
            if d.desired != "running" or d.pinned or d.inflight > 0 or t - d.last_used < 30:
                continue
            dm = self.models.get(d.model_id)
            if not dm or dm["priority"] > m_req["priority"] or dm["id"] == m_req["id"]:
                continue
            alive = [x for x in self.deps_of(dm["id"]) if x.desired == "running" and x.id != d.id]
            if len(alive) < dm["min_replicas"]:
                continue
            out.append(d)
        out.sort(key=lambda d: (self.models[d.model_id]["priority"], d.last_used))
        return out

    def can_host(self, n: NodeRT, m: dict, rt: dict) -> bool:
        """Boş olsa bu makine modeli çalıştırabilir mi? (ön dağıtım kararı için)"""
        if not n.hw or not self.rt_available(n, rt):
            return False
        ignore = frozenset(d.id for d in self.deps_on(n.id))
        return self._fit(n, m, rt, "balanced", ignore) is not None

    def on_battery(self, n: NodeRT) -> bool:
        p = (n.metrics or {}).get("power") or {}
        return p.get("ac") is False and p.get("battery_pct") is not None

    def exe_for(self, n: NodeRT, rt: dict) -> str:
        """Bu makinede runtime için kullanılacak çalıştırılabilir dosya (makineye özel yol varsa o)."""
        return (n.rt_paths.get(rt["key"]) or "").strip() or rt["exe"]

    def rt_available(self, n: NodeRT, rt: dict) -> bool:
        """Runtime'ın çalıştırılabilir dosyası bu makinede var ve ajanda izinli mi? (ajan bildirmediyse: bilinmiyor = evet)"""
        if self.ctx.fabric.builds_of(rt["key"]):
            return self.ctx.fabric.runtime_ok(n, rt)
        if n.sim:
            return True
        hw = n.hw or {}
        if hw.get("allow_exe") is None or not isinstance(hw.get("runtimes"), dict):
            return True
        return self.exe_for(n, rt) in hw["runtimes"]

    def runtime_nodes(self, rt: dict) -> tuple[list[NodeRT], list[NodeRT]]:
        ok, no = [], []
        for n in self.nodes.values():
            (ok if self.rt_available(n, rt) else no).append(n)
        return ok, no

    def plan_placement(self, m: dict, *, allow_wake: bool = True, allow_evict: bool = True,
                       exclude=()) -> Placement | None:
        rt = self.runtimes.get(m["runtime"])
        if rt is None:
            return None
        mode = self.db.get("auto.mode")
        prefer = set(jload(m["prefer_tags"], []))
        avoid = set(jload(m["exclude_tags"], []))
        t = now()
        best: Placement | None = None
        for n in self.nodes.values():
            if n.id in exclude or not n.enabled or n.draining or n.thermal == "crit":
                continue
            if self.bad_places.get((m["id"], n.id), 0) > t or set(n.tags) & avoid or not n.hw or not self.rt_available(n, rt):
                continue
            if self.db.get("deploy.avoid_battery") and self.on_battery(n):
                continue
            if n.status in ("online", "waking"):
                pass
            elif n.status == "sleeping" and allow_wake and self.db.get("auto.allow_wake") and n.mac:
                pass
            else:
                continue
            fit = self._fit(n, m, rt, mode)
            evict: list[DepRT] = []
            if fit is None and allow_evict and n.status == "online":
                ign = set()
                for d in self._evictable(n, m):
                    evict.append(d)
                    ign.add(d.id)
                    fit = self._fit(n, m, rt, mode, frozenset(ign))
                    if fit is not None:
                        break
                if fit is None:
                    continue
            elif fit is None:
                continue
            score = self._score(n, m, fit, evict, mode, prefer)
            if best is None or score > best.score:
                best = Placement(n, fit[0], fit[1], evict, score)
        return best

    def _score(self, n: NodeRT, m: dict, fit, evict, mode: str, prefer: set) -> float:
        gpus, cpu = fit
        s = 100.0 if n.status == "online" else 60.0 if n.status == "waking" else 0.0
        perf = sum(g.get("vram_gb", 0) for g in n.hw.get("gpus", [])) * n.weight
        evict_ids = {d.id for d in evict}
        others = [d for d in self.deps_on(n.id) if d.desired == "running" and d.id not in evict_ids]
        if mode == "performance":
            s += min(60.0, perf) - (40 if cpu else 0)
        elif mode == "eco":
            s += (40 if others and n.status == "online" else 0) - perf * 0.2
        else:
            s += min(30.0, perf * 0.5)
        if cpu:
            s -= 25
        s += (10 if mode == "eco" else -8) * len(others)
        if set(n.tags) & prefer:
            s += 25
        if m.get("source") == "hf" and self.ctx.fabric.has_model(n, m):
            s += 35  # dosyalar zaten orada: ağdan aktarım beklenmez
        if n.thermal == "warn":
            s -= 40
        s -= 15 * sum(1 for d in others if d.model_id == m["id"])
        s -= 20 * len(evict)
        return s

    async def deploy(self, m: dict, p: Placement, origin: str = "autoscaler", pinned: bool = False) -> DepRT:
        for d in p.evict:
            await self.remove_deployment(d.id, f"{m['name']} için yer açıldı")
        if p.node.status == "sleeping":
            await self.wake(p.node, f"{m['name']} için")
        return self.create_deployment(m["id"], p.node.id, origin, pinned, p.gpus)

    # ---- güç yönetimi -------------------------------------------------------
    async def wake(self, n: NodeRT, reason: str = "") -> None:
        if n.status == "online":
            return
        if not n.mac:
            raise ValueError("Bu düğüm için MAC adresi tanımlı değil (Wake-on-LAN için gerekli)")
        if n.sim and self.ctx.sim:
            self.ctx.sim.wake(n.id)
        else:
            sent = send_wol(n.mac, n.host)
            if not sent:
                raise RuntimeError("Wake-on-LAN paketi gönderilemedi (ağ arayüzü?)")
        n.status, n.intent, n.intent_since, n.fail = "waking", "wake", now(), 0
        n.next_poll = 0.0
        self.db.event("info", "power", f"{n.name} uyandırılıyor (WoL)" + (f" — {reason}" if reason else ""), n.id)

    async def power(self, n: NodeRT, action: str, reason: str = "", force: bool = False) -> None:
        """sleep | reboot | poweroff — önce modelleri nazikçe durdurur."""
        if action == "wake":
            return await self.wake(n, reason)
        if action not in ("sleep", "reboot", "poweroff"):
            raise ValueError("Bilinmeyen güç işlemi")
        if n.is_self:
            raise ValueError("Bu makine kontrol sunucusunu çalıştırıyor; uykuya alınamaz, yeniden başlatılamaz ya da kapatılamaz "
                             "(gerekirse makinede elle yapın)")
        if n.status != "online":
            raise ValueError("Düğüm çevrimiçi değil")
        if not force and any(d.inflight for d in self.deps_on(n.id)):
            raise ValueError("Düğümde işlenen istekler var; önce boşalmasını bekleyin")
        n.draining = True
        try:
            cl = self.client(n)
            for d in list(self.deps_on(n.id)):
                if d.origin == "autoscaler" and not d.pinned:
                    await self.remove_deployment(d.id, f"{n.name} {action} için")
                else:
                    await self._stop_proc(cl, n, d.proc_id)
                    d.state = "pending"
            agent_action = {"sleep": "suspend", "reboot": "reboot", "poweroff": "poweroff"}[action]
            n.intent = "sleep" if action in ("sleep", "poweroff") else "reboot"
            n.intent_since = now()
            await cl.power(agent_action)
            if action == "reboot":
                n.reboots.append(now())
            self.db.event("info", "power", f"{n.name}: {action} komutu gönderildi" + (f" — {reason}" if reason else ""), n.id)
        except Exception:
            n.draining, n.intent = False, None
            raise

    def reboots_last_hour(self, n: NodeRT) -> int:
        t = now()
        return sum(1 for x in n.reboots if t - x < 3600)

    # ---- görünümler ---------------------------------------------------------
    def dep_view(self, d: DepRT) -> dict:
        m = self.models.get(d.model_id) or {}
        n = self.nodes.get(d.node_id)
        return {
            "id": d.id, "model_id": d.model_id, "model": m.get("name"), "kind": m.get("kind"),
            "node_id": d.node_id, "node": n.name if n else None, "port": d.port,
            "state": d.state, "sync": d.sync, "build": d.build, "desired": d.desired, "origin": d.origin, "pinned": d.pinned,
            "inflight": d.inflight, "max_conc": m.get("max_concurrency", 0), "gpus": d.gpus,
            "started_at": d.started_at, "ready_at": d.ready_at, "fail": d.fail, "error": d.last_error,
            "tps": round(d.tps.get(), 1), "ttft_ms": int(d.ttft.get()), "served": d.served,
            "last_used": d.last_used,
        }

    def node_view(self, n: NodeRT, detail: bool = False) -> dict:
        m = n.metrics if n.status == "online" else {}
        gp = m.get("gpus") or []
        t = now()
        v = {
            "id": n.id, "name": n.name, "host": n.host, "agent_port": n.agent_port, "mac": n.mac,
            "role": n.role, "tags": n.tags, "enabled": n.enabled, "allow_sleep": n.allow_sleep,
            "allow_restart": n.allow_restart, "always_on": n.always_on, "weight": n.weight,
            "warn_c": n.warn_c, "crit_c": n.crit_c, "sim": n.sim, "is_self": n.is_self, "rt_paths": n.rt_paths,
            "allow_exe": (n.hw or {}).get("allow_exe"), "runtime_paths": (n.hw or {}).get("runtimes") or {},
            "status": n.status, "intent": n.intent, "draining": n.draining, "thermal": n.thermal,
            "health": n.health if n.status == "online" else n.status, "health_reasons": n.health_reasons,
            "last_seen": n.last_seen, "last_error": n.last_error if n.status != "online" else "",
            "cpu": m.get("cpu"), "load1": m.get("load1"),
            "mem": m.get("mem"), "cpu_temp": m.get("cpu_temp"), "disk": m.get("disk"), "net": m.get("net"),
            "uptime_s": m.get("uptime_s"), "gpus": gp, "gpu_error": m.get("gpu_error", ""), "power": m.get("power"),
            "power_w": round(sum(g.get("power_w") or 0 for g in gp) + (m.get("cpu_power_w") or 0), 1) if m else None,
            "max_temp": n.max_temp if n.status == "online" else None,
            "hw": {"cpu": (n.hw or {}).get("cpu"), "ram_gb": (n.hw or {}).get("ram_gb"), "gpus": (n.hw or {}).get("gpus", []),
                   "os": (n.hw or {}).get("os"), "hostname": (n.hw or {}).get("hostname"), "laptop": (n.hw or {}).get("laptop"),
                   "platform": (n.hw or {}).get("platform"), "gpu_caps": (n.hw or {}).get("gpu_caps"), "agent": (n.hw or {}).get("agent"),
                   "tools": (n.hw or {}).get("tools"),
                   "runtimes": sorted(((n.hw or {}).get("runtimes") or {}).keys())},
            "idle_s": int(t - n.idle_since) if n.idle_since else 0,
            "deployments": [self.dep_view(d) for d in self.deps_on(n.id)],
            "reboots_1h": self.reboots_last_hour(n),
        }
        return v
