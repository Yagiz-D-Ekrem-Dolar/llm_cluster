#!/usr/bin/env python3
"""Röle sunucusu (VPS için): siteniz buraya istek atar, küme dışarıdan içeri port açmadan bu röleye bağlanır.

    python3 relay.py --secret GİZLİ --host 127.0.0.1 --port 8088

Modem CGNAT arkasında olsa da çalışır; çünkü bağlantıyı küme başlatır. Yalnızca standart kütüphane kullanır.
HTTPS için önüne Caddy/nginx koyun ya da --cert/--key verin.

Dışa açık uçlar : /v1/*  (site → küme), /healthz
Küme uçları     : /__tunnel/poll, /__tunnel/push, /__tunnel/body   (Bearer GİZLİ)
"""
import argparse
import asyncio
import base64
import hmac
import json
import os
import secrets
import ssl
import struct
import time
import zlib
from urllib.parse import parse_qs, urlsplit

VERSION = 1
MAX_BODY = 32 * 1024 * 1024
INLINE_BODY = 64 * 1024
HEAD_WAIT = 330
ALIVE_S = 40
REDELIVER_S = 6
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
REASON = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large",
          429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}


class Pending:
    def __init__(self, method, path, query, headers, body, ip):
        self.id = secrets.token_hex(12)
        self.method, self.path, self.query, self.headers, self.body, self.ip = method, path, query, headers, body, ip
        self.frames: asyncio.Queue = asyncio.Queue()
        self.delivered = 0.0
        self.acked = 0.0
        self.last_seq = -1
        self.finished = False
        self.cancelled = False
        self.t0 = time.time()

    def describe(self):
        d = {"id": self.id, "m": self.method, "p": self.path, "q": self.query, "h": self.headers, "ip": self.ip, "n": len(self.body), "b": None}
        if len(self.body) <= INLINE_BODY:
            d["b"] = base64.b64encode(self.body).decode()
        return d


class Relay:
    def __init__(self, secret):
        self.secret = secret
        self.items = {}
        self.queue = []
        self.wake = asyncio.Event()
        self.last_poll = 0.0
        self.inst = ""
        self.cancels = set()

    def alive(self):
        return time.time() - self.last_poll < ALIVE_S

    def add(self, p):
        self.items[p.id] = p
        self.queue.append(p)
        self.wake.set()

    def forget(self, p):
        p.finished = True
        self.items.pop(p.id, None)
        self.cancels.discard(p.id)
        if p in self.queue:
            self.queue.remove(p)

    def collect(self, limit):
        t = time.time()
        out = []
        while self.queue and len(out) < limit:
            p = self.queue.pop(0)
            p.delivered = t
            out.append(p)
        for p in self.items.values():
            if len(out) >= limit:
                break
            if p.delivered and not p.acked and t - p.delivered > REDELIVER_S and p not in out:
                p.delivered = t
                out.append(p)
        return out

    async def poll(self, body, ip):
        self.last_poll = time.time()
        self.inst = str(body.get("inst", ""))
        active = set(body.get("active") or [])
        for rid in body.get("ack") or []:
            p = self.items.get(rid)
            if p and not p.acked:
                p.acked = time.time()
                active.add(rid)
        t = time.time()
        for p in list(self.items.values()):  # küme yeniden başladıysa bekleyen istek askıda kalmasın
            if p.acked and p.acked < t - 3 and p.id not in active:
                p.frames.put_nowait({"t": "x", "m": "küme isteği kaybetti"})
        deadline = t + min(25.0, float(body.get("wait", 20)))
        limit = min(64, int(body.get("max", 16)))
        while True:
            items = self.collect(limit)
            if items or self.cancels or time.time() >= deadline:
                break
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=min(deadline - time.time(), 2.0))
            except asyncio.TimeoutError:
                pass
            self.last_poll = time.time()
        cancels = list(self.cancels)
        self.cancels.clear()
        self.last_poll = time.time()
        return {"v": VERSION, "requests": [p.describe() for p in items], "cancel": cancels, "t": time.time(), "ip": ip}

    def push(self, raw):
        if raw[:1] == b"\x01":
            raw = zlib.decompress(raw[1:])
        else:
            raw = raw[1:]
        off = 0
        while off < len(raw):
            hl = struct.unpack_from(">I", raw, off)[0]
            off += 4
            h = json.loads(raw[off:off + hl])
            off += hl
            dl = struct.unpack_from(">I", raw, off)[0]
            off += 4
            data = raw[off:off + dl]
            off += dl
            p = self.items.get(h["i"])
            if not p or h["s"] <= p.last_seq:
                continue
            p.last_seq = h["s"]
            h["d"] = data
            p.frames.put_nowait(h)
        cancels = list(self.cancels)
        self.cancels.clear()
        return {"ok": True, "cancel": cancels}


def jresp(code, obj, extra=()):
    b = json.dumps(obj, ensure_ascii=False).encode()
    head = f"HTTP/1.1 {code} {REASON.get(code, 'OK')}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(b)}\r\n"
    for k, v in extra:
        head += f"{k}: {v}\r\n"
    return head.encode() + b"\r\n" + b


def err(code, msg, kind):
    return jresp(code, {"error": {"message": msg, "type": "api_error", "code": kind}})


async def read_request(r):
    line = await r.readline()
    if not line:
        return None
    parts = line.decode("latin-1").strip().split(" ")
    if len(parts) != 3:
        return None
    method, target, ver = parts
    headers = []
    while True:
        h = await r.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers.append((k.strip(), v.strip()))
    low = {k.lower(): v for k, v in headers}
    body = b""
    if "chunked" in low.get("transfer-encoding", "").lower():
        chunks, total = [], 0
        while True:
            n = int((await r.readline()).split(b";")[0].strip() or b"0", 16)
            if n == 0:
                await r.readline()
                break
            total += n
            if total > MAX_BODY:
                raise ValueError("büyük")
            chunks.append(await r.readexactly(n))
            await r.readline()
        body = b"".join(chunks)
    else:
        n = int(low.get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("büyük")
        if n:
            body = await r.readexactly(n)
    return method, target, ver, headers, low, body


async def stream_response(relay, p, reader, writer):
    """Küme yanıtını istemciye aktarır; istemci koparsa kümeye iptal bildirir."""
    started, chunked = False, False
    deadline = time.time() + HEAD_WAIT
    try:
        while True:
            try:
                f = await asyncio.wait_for(p.frames.get(), timeout=0.4)
            except asyncio.TimeoutError:
                if reader.at_eof() or writer.is_closing():   # istemci gitti: kümedeki üretim de dursun
                    p.cancelled = True
                    relay.cancels.add(p.id)
                    relay.wake.set()
                    return False
                if time.time() > deadline:
                    if not started:
                        writer.write(err(504, "Küme zamanında yanıt vermedi", "cluster_timeout"))
                        await writer.drain()
                    return False
                continue
            deadline = time.time() + (900 if started or f["t"] == "h" else HEAD_WAIT)
            t = f["t"]
            if t == "h":
                started = True
                head = f"HTTP/1.1 {f['st']} {REASON.get(f['st'], 'OK')}\r\n"
                names = set()
                for k, v in f.get("hd") or []:
                    if k.lower() in HOP and k.lower() != "content-length":
                        continue
                    names.add(k.lower())
                    head += f"{k}: {v}\r\n"
                chunked = "content-length" not in names
                if chunked:
                    head += "Transfer-Encoding: chunked\r\n"
                head += "X-Accel-Buffering: no\r\nCache-Control: no-cache\r\n\r\n"
                writer.write(head.encode("latin-1", "replace"))
                await writer.drain()
            elif t == "d":
                d = f["d"]
                if d:
                    writer.write(b"%x\r\n%s\r\n" % (len(d), d) if chunked else d)
                    await writer.drain()
            elif t == "e":
                if chunked:
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()
                return True
            elif t == "x":
                if not started:
                    writer.write(err(502, f.get("m") or "Küme hatası", "cluster_error"))
                    await writer.drain()
                return False
    except (ConnectionError, asyncio.IncompleteReadError):
        p.cancelled = True
        relay.cancels.add(p.id)
        relay.wake.set()
        return False
    finally:
        relay.forget(p)


def make_handler(relay):
    async def handle(reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            while True:
                try:
                    req = await asyncio.wait_for(read_request(reader), timeout=120)
                except ValueError:
                    writer.write(err(413, "İstek çok büyük", "too_large"))
                    await writer.drain()
                    break
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                if req is None:
                    break
                method, target, ver, headers, low, body = req
                u = urlsplit(target)
                path = u.path
                ip = (low.get("x-forwarded-for", "").split(",")[0].strip()) or peer[0]
                i = path.find("/__tunnel/")
                if i >= 0:
                    action = path[i + 10:]
                    if not hmac.compare_digest((low.get("authorization", "")[7:] or low.get("x-tunnel-key", "")).encode(), relay.secret.encode()):
                        writer.write(err(401, "Geçersiz tünel anahtarı", "bad_tunnel_key"))
                    elif action == "poll" and method == "POST":
                        writer.write(jresp(200, await relay.poll(json.loads(body or b"{}"), ip)))
                    elif action == "push" and method == "POST":
                        writer.write(jresp(200, relay.push(body)))
                    elif action == "body" and method == "GET":
                        p = relay.items.get(parse_qs(u.query).get("id", [""])[0])
                        if p:
                            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {len(p.body)}\r\n\r\n".encode() + p.body)
                        else:
                            writer.write(err(404, "İstek yok", "not_found"))
                    else:
                        writer.write(err(404, "Bulunamadı", "not_found"))
                    await writer.drain()
                    continue
                if path.endswith("/healthz"):
                    writer.write(jresp(200, {"status": "ok", "cluster": relay.alive(), "age_s": round(time.time() - relay.last_poll, 1) if relay.last_poll else None}))
                    await writer.drain()
                    continue
                i = path.find("/v1/")
                if i < 0:
                    writer.write(err(404, "Bulunamadı", "not_found"))
                    await writer.drain()
                    continue
                if method != "OPTIONS" and not (low.get("authorization") or low.get("x-api-key")):
                    writer.write(err(401, "Kimlik doğrulama gerekli (Authorization: Bearer <anahtar>)", "missing_api_key"))
                    await writer.drain()
                    continue
                if not relay.alive():
                    writer.write(err(503, "Küme şu anda bağlı değil", "cluster_offline"))
                    await writer.drain()
                    continue
                p = Pending(method, path[i:], u.query, [[k, v] for k, v in headers if k.lower() not in HOP], body, ip)
                relay.add(p)
                ok = await stream_response(relay, p, reader, writer)
                if not ok or ver == "HTTP/1.0" or low.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
    return handle


async def amain(a):
    relay = Relay(a.secret)
    ctx = None
    if a.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(a.cert, a.key)
    srv = await asyncio.start_server(make_handler(relay), a.host, a.port, ssl=ctx, limit=1 << 20)
    print(f"röle dinliyor: {a.host}:{a.port}", flush=True)
    async with srv:
        await srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Küme rölesi")
    ap.add_argument("--secret", default=os.environ.get("RELAY_SECRET", "__RELAY_SECRET__"))
    ap.add_argument("--host", default=os.environ.get("RELAY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8088")))
    ap.add_argument("--cert", default="")
    ap.add_argument("--key", default="")
    a = ap.parse_args()
    if len(a.secret) < 16 or a.secret.startswith("__"):
        raise SystemExit("Tünel anahtarı gerekli: --secret (panelden alın)")
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
