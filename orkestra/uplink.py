"""Siteye bağlantı: küme dışarı doğru bağlanır, hosting'deki site istekleri bu yoldan kümeye gelir.

Superbox gibi mobil modemler çoğunlukla CGNAT arkasındadır; dışarıdan içeri port açılamaz. Bu yüzden:

  relay      : hosting'e yüklenen röle (PHP / Node.js / Python) — küme uzun süreli istekle işleri alır
  cloudflare : cloudflared tüneli (hazır ise)
  direct     : modemde gerçek IP + port yönlendirme varsa

Röle protokolü düz HTTP'dir (WebSocket gerekmez), bu yüzden paylaşımlı hosting ve ters vekillerden geçer.
Kopan bağlantıda istek yeniden teslim edilir, yanıt parçaları sıra numarasıyla tekilleştirilir.
"""
from __future__ import annotations

import asyncio
import base64
import re
import shutil
import struct
import zlib
from collections import deque

import httpx

from .util import jdump, log, now, rand_token

DROP_REQ = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
            "host", "content-length", "cookie", "accept-encoding", "x-forwarded-for", "x-tunnel-key"}
DROP_RESP = {"connection", "keep-alive", "transfer-encoding", "date", "server", "te", "trailer", "upgrade"}
LANES = 3
MAX_QUEUE = 4 * 1024 * 1024


class Lane:
    def __init__(self):
        self.q: deque = deque()
        self.size = 0
        self.ev = asyncio.Event()
        self.room = asyncio.Event()
        self.room.set()

    async def put(self, frame: tuple) -> None:
        await self.room.wait()
        self.q.append(frame)
        self.size += len(frame[1])
        self.ev.set()
        if self.size > MAX_QUEUE:
            self.room.clear()

    def take(self, limit: int = 1 << 20) -> list:
        out, n = [], 0
        while self.q and n < limit:
            f = self.q.popleft()
            out.append(f)
            n += len(f[1]) + 64
            self.size -= len(f[1])
        if self.size <= MAX_QUEUE:
            self.room.set()
        if not self.q:
            self.ev.clear()
        return out


def encode_frames(frames: list) -> bytes:
    parts = []
    for hd, data in frames:
        h = jdump(hd).encode()
        parts += [struct.pack(">I", len(h)), h, struct.pack(">I", len(data)), data]
    raw = b"".join(parts)
    if len(raw) > 400:
        z = zlib.compress(raw, 1)
        if len(z) < len(raw):
            return b"\x01" + z
    return b"\x00" + raw


class Uplink:
    def __init__(self, ctx):
        self.ctx, self.db = ctx, ctx.db
        self.internal = rand_token(24)   # ağ geçidi yalnızca bu başlıkla gelen istemci IP'sine güvenir
        self.inst = rand_token(8)
        self.st = {"mode": "off", "state": "off", "error": "", "since": now(), "rtt_ms": None, "seen_ip": "", "public_url": "", "last_ok": 0.0}
        self.up_bytes = self.down_bytes = self.total = 0
        self.hist: deque = deque(maxlen=720)
        self.active: dict[str, asyncio.Task] = {}
        self.finished: deque = deque(maxlen=512)
        self.acks: list[str] = []
        self.lanes: list[Lane] = []
        self.seq: dict[str, int] = {}
        self.workers: list[asyncio.Task] = []
        self.proc: asyncio.subprocess.Process | None = None
        self.http: httpx.AsyncClient | None = None
        self.local: httpx.AsyncClient | None = None
        self._sup: asyncio.Task | None = None
        self._cfg = None
        self._retiring: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._sampler: asyncio.Task | None = None

    # ---- yaşam döngüsü ------------------------------------------------------
    def start(self) -> None:
        self._sup = asyncio.create_task(self._supervise())
        self._sampler = asyncio.create_task(self._sample())

    async def stop(self) -> None:
        for t in (self._sup, self._sampler):
            if t:
                t.cancel()
        await self._stop_mode()

    def config(self) -> tuple:
        d = self.db
        return (d.get("uplink.mode"), (d.get("uplink.relay_url") or "").strip().rstrip("/"), d.get("uplink.secret") or "",
                d.get("uplink.cf_token") or "", d.get("uplink.cf_path") or "cloudflared", (d.get("uplink.public_url") or "").strip().rstrip("/"))

    async def apply(self) -> None:
        """Ayar değiştiyse bağlantıyı hemen yeniden kurar."""
        async with self._lock:
            cfg = self.config()
            if cfg != self._cfg:
                await self._stop_mode()
                self._cfg = cfg
                await self._start_mode(cfg)

    async def _supervise(self) -> None:
        while True:
            try:
                await self.apply()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("uplink")
            await asyncio.sleep(2.0)

    def _set(self, state: str, error: str = "") -> None:
        if state != self.st["state"]:
            self.st["since"] = now()
            if state == "up":
                self.db.event("info", "uplink", "Siteye bağlantı kuruldu")
            elif self.st["state"] == "up":
                self.db.event("warn", "uplink", f"Siteye bağlantı koptu: {error}")
        self.st["state"], self.st["error"] = state, error

    async def _stop_mode(self) -> None:
        for t in self.workers + list(self.active.values()):
            t.cancel()
        await asyncio.gather(*self.workers, *self.active.values(), return_exceptions=True)
        self.workers, self.active, self.lanes, self.seq = [], {}, [], {}
        self.acks = []
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        for name in ("http", "local"):
            c = getattr(self, name)
            if c:
                await c.aclose()
                setattr(self, name, None)

    @property
    def local_base(self) -> str:
        b = self.ctx.boot
        return f"http://127.0.0.1:{b.public_port if b.public else b.admin_port}"

    async def _start_mode(self, cfg: tuple) -> None:
        mode, url, secret, cf_token, cf_path, public = cfg
        self.st.update(mode=mode, rtt_ms=None, seen_ip="", public_url=public)
        if self.ctx.sim and mode != "off":
            self.st["public_url"] = public or url or "https://siteniz.com/llm/relay.php"
            return self._set("up")
        if mode == "off":
            return self._set("off")
        if mode == "direct":
            return self._set("up" if public else "error", "" if public else "Genel adres girilmemiş")
        if mode == "cloudflare":
            self.workers.append(asyncio.create_task(self._cloudflared(cf_token, cf_path, public)))
            return self._set("connecting")
        if not url or len(secret) < 16:
            return self._set("error", "Röle adresi ve tünel anahtarı gerekli")
        self.st["public_url"] = public or url
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=40.0), headers={"Authorization": f"Bearer {secret}", "X-Tunnel-Key": secret,
                                                                                        "User-Agent": "kume-uplink"},
                                      limits=httpx.Limits(max_connections=16, max_keepalive_connections=8), follow_redirects=True)
        self.local = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None, write=120.0), limits=httpx.Limits(max_connections=None, max_keepalive_connections=64))
        self.lanes = [Lane() for _ in range(LANES)]
        self._set("connecting")
        self.workers += [asyncio.create_task(self._poll_loop(url)) for _ in range(2)]
        self.workers.append(asyncio.create_task(self._ping_loop(url)))
        self.workers += [asyncio.create_task(self._push_loop(url, lane)) for lane in self.lanes]

    # ---- röle: istek alma -------------------------------------------------------
    async def _poll_once(self, url: str, wait: float, limit: int) -> float:
        acks, self.acks = self.acks, []
        t0 = now()
        try:
            body = {"v": 1, "inst": self.inst, "wait": wait, "max": limit, "ack": acks, "active": list(self.active)}
            r = await self.http.post(f"{url}/__tunnel/poll", content=jdump(body).encode(), headers={"Content-Type": "application/json"})
            if r.status_code == 401:
                raise PermissionError("Tünel anahtarı röleyle uyuşmuyor")
            if r.status_code != 200:
                raise IOError(f"röle HTTP {r.status_code}: {r.text[:120]}")
            j = r.json()
        except BaseException:
            self.acks = acks + self.acks
            raise
        self.st.update(last_ok=now(), seen_ip=j.get("ip") or self.st["seen_ip"])
        self._set("up")
        self.down_bytes += len(r.content)
        for rid in j.get("cancel") or []:
            t = self.active.get(rid)
            if t:
                t.cancel()
        for q in j.get("requests") or []:
            rid = q["id"]
            self.acks.append(rid)
            if rid in self.active or rid in self.finished:
                continue
            self.total += 1
            self.active[rid] = asyncio.create_task(self._handle(url, q))
        return (now() - t0) * 1000

    async def _poll_loop(self, url: str) -> None:
        backoff = 0.5
        while True:
            try:
                await self._poll_once(url, 20, 16)
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except PermissionError as e:
                self._set("error", str(e))
                await asyncio.sleep(10)
            except Exception as e:
                self._set("down", f"{type(e).__name__}: {str(e)[:160]}")
                await asyncio.sleep(backoff)
                backoff = min(15.0, backoff * 1.7)

    async def _ping_loop(self, url: str) -> None:
        """Beklemesiz kısa yoklama: gidiş-dönüş süresini ölçer, onayları hemen iletir."""
        while True:
            try:
                self.st["rtt_ms"] = int(await self._poll_once(url, 0, 0))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            for _ in range(40):
                await asyncio.sleep(0.25)
                if self.acks:
                    break

    def _emit_lane(self, rid: str) -> Lane:
        return self.lanes[int(rid[:6], 16) % LANES]

    async def _emit(self, rid: str, kind: str, data: bytes = b"", **extra) -> None:
        s = self.seq.get(rid, -1) + 1
        self.seq[rid] = s
        await self._emit_lane(rid).put(({"i": rid, "s": s, "t": kind, **extra}, data))

    async def _handle(self, url: str, q: dict) -> None:
        rid = q["id"]
        sent_head = False
        try:
            body = base64.b64decode(q["b"]) if q.get("b") is not None else None
            if body is None:
                r = await self.http.get(f"{url}/__tunnel/body", params={"id": rid}, timeout=httpx.Timeout(20.0, read=180.0))
                r.raise_for_status()
                body = r.content
            self.down_bytes += len(body)
            if not str(q["p"]).startswith("/v1/"):
                raise ValueError("izin verilmeyen yol")
            if len(self.active) > int(self.db.get("uplink.max_streams")):
                err = jdump({"error": {"message": "Sunucu yoğun, biraz sonra tekrar deneyin", "type": "api_error", "code": "busy"}}).encode()
                await self._emit(rid, "h", st=503, hd=[["Content-Type", "application/json"]])
                await self._emit(rid, "d", err)
                await self._emit(rid, "e")
                return
            headers = [(k, v) for k, v in q.get("h") or [] if k.lower() not in DROP_REQ and not k.lower().startswith("x-orkestra")]
            headers += [("X-Orkestra-Tunnel", self.internal), ("X-Orkestra-Client-Ip", str(q.get("ip") or ""))]
            target = self.local_base + q["p"] + (("?" + q["q"]) if q.get("q") else "")
            req = self.local.build_request(q["m"], target, headers=headers, content=body)
            resp = await self.local.send(req, stream=True)
            try:
                hd = [[k, v] for k, v in resp.headers.multi_items() if k.lower() not in DROP_RESP]
                await self._emit(rid, "h", st=resp.status_code, hd=hd)
                sent_head = True
                async for chunk in resp.aiter_raw():
                    if chunk:
                        await self._emit(rid, "d", chunk)
            finally:
                await resp.aclose()
            await self._emit(rid, "e")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("tünel isteği hatası %s: %s", q.get("p"), e)
            try:
                await self._emit(rid, "x", m="Küme isteği işleyemedi" if not sent_head else "akış kesildi")
            except Exception:
                pass
        finally:
            self.finished.append(rid)
            t = asyncio.ensure_future(self._retire(rid))
            self._retiring.add(t)
            t.add_done_callback(self._retiring.discard)

    async def _retire(self, rid: str) -> None:
        """Son parça gönderilene kadar istek "etkin" sayılır; röle onu kayıp sanmasın."""
        lane = self._emit_lane(rid) if self.lanes else None
        for _ in range(200):
            if lane is None or not any(f[0]["i"] == rid for f in lane.q):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        self.active.pop(rid, None)
        self.seq.pop(rid, None)

    # ---- röle: yanıt gönderme -----------------------------------------------------
    async def _push_loop(self, url: str, lane: Lane) -> None:
        while True:
            await lane.ev.wait()
            frames = lane.take()
            if not frames:
                continue
            body = encode_frames(frames)
            delay = 0.1
            for _ in range(240):
                try:
                    r = await self.http.post(f"{url}/__tunnel/push", content=body, headers={"Content-Type": "application/octet-stream"})
                    if r.status_code != 200:
                        raise IOError(f"HTTP {r.status_code}")
                    self.up_bytes += len(body)
                    for rid in r.json().get("cancel") or []:
                        t = self.active.get(rid)
                        if t:
                            t.cancel()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("tünel gönderimi: %s", e)
                    await asyncio.sleep(delay)
                    delay = min(3.0, delay * 1.6)

    # ---- cloudflared ---------------------------------------------------------
    async def _cloudflared(self, token: str, path: str, public: str) -> None:
        exe = shutil.which(path) or path
        port = self.ctx.boot.public_port
        args = ["tunnel", "--no-autoupdate"] + (["run", "--token", token] if token else ["--url", f"http://127.0.0.1:{port}"])
        backoff = 2.0
        while True:
            try:
                self.proc = await asyncio.create_subprocess_exec(exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except (FileNotFoundError, PermissionError):
                self._set("error", f"cloudflared bulunamadı: {path}")
                await asyncio.sleep(10)
                continue
            self._set("connecting")
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace")
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", s)
                if m and not public:
                    self.st["public_url"] = m.group(0)
                if "Registered tunnel connection" in s or "Connection registered" in s:
                    self.st["last_ok"] = now()
                    self._set("up")
                    backoff = 2.0
            await self.proc.wait()
            self._set("down", f"cloudflared kapandı (çıkış {self.proc.returncode})")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    # ---- durum ---------------------------------------------------------------
    async def _sample(self) -> None:
        prev = (now(), 0, 0)
        while True:
            await asyncio.sleep(5)
            t = now()
            dt = max(0.1, t - prev[0])
            self.hist.append({"t": round(t, 1), "rtt": self.st["rtt_ms"] if self.st["state"] == "up" else None,
                              "up": int((self.up_bytes - prev[1]) / dt), "down": int((self.down_bytes - prev[2]) / dt), "active": len(self.active)})
            prev = (t, self.up_bytes, self.down_bytes)

    def status(self) -> dict:
        return {**self.st, "active": len(self.active), "total": self.total, "up_bytes": self.up_bytes, "down_bytes": self.down_bytes,
                "hist": {k: [x[k] for x in self.hist] for k in ("t", "rtt", "up", "down", "active")}}

    def public_base(self) -> str:
        return (self.st.get("public_url") or self.db.get("uplink.public_url") or "").rstrip("/")


def relay_source(kind: str, secret: str) -> tuple[str, bytes]:
    """Panelin indirdiği röle dosyası: tünel anahtarı içine yazılmış olarak."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "relay"
    name = {"php": "relay.php", "node": "relay.mjs", "python": "relay.py"}[kind]
    data = (root / name).read_text(encoding="utf-8").replace("__RELAY_SECRET__", secret)
    return name, data.encode("utf-8")

