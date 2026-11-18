"""OpenAI uyumlu ağ geçidi: sitenin konuştuğu tek kapı.

* Servis adları: site sabit bir ad kullanır (ör. sohbet), arkadaki model değiştirilebilir, sırayla yedeklenir
* Paketler: kullanıcı başına istek, eşzamanlılık ve günlük token sınırı; ücretli paketler kuyrukta önce gelir
* Yük dengeleme + konuşma yakınlığı (prefix cache), kuyruk, soğuk başlatma, başka replikaya geçiş
* Akış (SSE) ve ses akışı aktarımı; uzun beklemelerde bağlantıyı canlı tutan yorum satırları
* Kullanım kaydı: faturalama için kullanıcı/gün/servis bazında token, ses süresi, karakter
"""
from __future__ import annotations

import asyncio
import codecs
import itertools
import json
import random
import time
from collections import Counter, OrderedDict, defaultdict, deque

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from . import chatlog
from . import runtimes as rtm
from .auth import Principal
from .util import jdump, jload, log, now


PATH_KEYS = {"/v1/completions": "completions", "/v1/embeddings": "embeddings",
             "/v1/audio/transcriptions": "stt", "/v1/audio/speech": "tts"}


class GatewayError(Exception):
    def __init__(self, status: int, message: str, code: str = "server_error", retry_after: int = 0):
        super().__init__(message)
        self.status, self.message, self.code, self.retry_after = status, message, code, retry_after


def err_body(message: str, code: str = "server_error", typ: str = "api_error") -> dict:
    return {"error": {"message": message, "type": typ, "code": code}}


def today() -> str:
    return time.strftime("%Y-%m-%d")


class Gateway:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cluster = ctx.cluster
        self.db = ctx.db
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=900.0, write=120.0, pool=None),
            limits=httpx.Limits(max_connections=400, max_keepalive_connections=100),
        )
        self.waiters: dict[str, list] = defaultdict(list)
        self.ranks: dict[str, Counter] = defaultdict(Counter)
        self._seq = itertools.count()
        self.iwait: dict[str, int] = defaultdict(int)
        self.cold: dict[str, int] = defaultdict(int)
        self.last_ts: dict[str, float] = {}
        self.recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=3000))
        self.affinity: OrderedDict[str, int] = OrderedDict()
        self.interactive_inflight = 0
        self.last_busy = now()
        self.services: dict[str, dict] = {}
        self.user_inflight: Counter = Counter()
        self.user_rate: dict[str, list] = {}
        self.day_tokens: dict[tuple, int] = {}
        self.reload_services()
        self.cluster.listeners.append(self._on_cluster_change)

    async def close(self) -> None:
        await self.http.aclose()

    # ---- yardımcılar --------------------------------------------------------
    def by_name(self, name: str) -> dict | None:
        for m in self.cluster.models.values():
            if m["name"] == name:
                return m
        return None

    def reload_services(self) -> None:
        out = {}
        for r in self.db.q("SELECT * FROM services"):
            out[r["name"]] = {**r, "targets": jload(r["targets"], []), "options": jload(r["options"], {}), "enabled": bool(r["enabled"]), "is_default": bool(r["is_default"])}
        self.services = out

    def _ready_count(self, m: dict) -> int:
        return len(self._candidates(m))

    def resolve(self, name: str, kinds: tuple) -> tuple[dict, dict | None]:
        """İstekteki adı modele çevirir: önce servis adı, sonra doğrudan model adı. Ad yoksa varsayılan servis."""
        svc = self.services.get(name) if name else next((s for s in self.services.values() if s["is_default"] and s["enabled"] and s["kind"] in kinds), None)
        if svc is not None and (svc["enabled"] or not name):
            models = [m for m in (self.by_name(t) for t in svc["targets"]) if m and m["enabled"]]
            if not models:
                raise GatewayError(503, f"'{svc['name']}' servisinin çalışır modeli yok", "no_model")
            if svc["kind"] not in kinds:
                raise GatewayError(400, f"'{svc['name']}' bu uç için uygun değil (tür: {svc['kind']})", "invalid_model_kind")
            return next((m for m in models if self._ready_count(m)), models[0]), svc
        m = self.by_name(name) if name else next((x for x in self.cluster.models.values() if x["enabled"] and x["kind"] in kinds), None)
        if not m or not m["enabled"]:
            raise GatewayError(404, f"'{name}' modeli bulunamadı", "model_not_found")
        if m["kind"] not in kinds:
            raise GatewayError(400, f"'{m['name']}' bu uç için uygun değil (tür: {m['kind']})", "invalid_model_kind")
        return m, None

    def _on_cluster_change(self, name: str) -> None:
        names = [name] if name else list(self.waiters)
        for n in names:
            self._wake(n, 1000)

    def _wake(self, name: str, n: int = 1) -> None:
        ws = self.waiters.get(name)
        woke = 0
        while ws and woke < n:
            e = min(ws, key=lambda x: x[:2])
            ws.remove(e)
            if not e[2].done():
                e[2].set_result(None)
                woke += 1

    def demand(self, name: str) -> dict:
        m = self.by_name(name)
        infl = sum(d.inflight for d in self.cluster.deps_of(m["id"])) if m else 0
        dq = self.recent[name]
        t = now()
        while dq and t - dq[0] > 60:
            dq.popleft()
        return {"inflight": infl, "waiting": self.iwait[name], "cold": self.cold[name],
                "last_ts": self.last_ts.get(name, 0.0), "rpm": len(dq)}

    def busy(self) -> bool:
        return self.interactive_inflight > 0 or any(v > 0 for v in self.iwait.values())

    def idle_minutes(self) -> float:
        return 0.0 if self.busy() else (now() - self.last_busy) / 60.0

    # ---- paketler ve kullanıcı sınırları --------------------------------------
    def plan_for(self, name: str) -> tuple[str, dict]:
        plans = self.db.get("svc.plans") or {}
        key = name if name in plans else self.db.get("svc.default_plan")
        return key, plans.get(key) or {"rank": 1, "rpm": 0, "day_tokens": 0, "max_conc": 0, "max_tokens": 0}

    def used_today(self, user: str) -> int:
        k = (today(), user)
        v = self.day_tokens.get(k)
        if v is None:
            if len(self.day_tokens) > 20000:
                self.day_tokens.clear()
            r = self.db.one("SELECT COALESCE(SUM(in_tok+out_tok),0) n FROM usage_daily WHERE day=? AND user_id=?", k)
            v = self.day_tokens[k] = int(r["n"])
        return v

    def admit(self, user: str, plan_name: str, plan: dict, p: Principal):
        """Kullanıcı sınırlarını uygular; istek bitince çağrılacak serbest bırakma işlevini döndürür."""
        if p.kind not in ("key", "user") or not user or user.startswith("key:"):
            return lambda: None
        t = now()
        rpm = int(plan.get("rpm") or 0)
        if rpm:
            b = self.user_rate.setdefault(user, [float(rpm), t])
            b[0] = min(float(rpm), b[0] + (t - b[1]) * rpm / 60.0)
            b[1] = t
            if b[0] < 1:
                raise GatewayError(429, "Çok hızlı istek gönderiliyor, biraz bekleyin", "user_rate_limit", int(60 / rpm) + 1)
            b[0] -= 1
            if len(self.user_rate) > 50000:
                self.user_rate.clear()
        day = int(plan.get("day_tokens") or 0)
        if day and self.used_today(user) >= day:
            raise GatewayError(429, "Günlük kullanım hakkınız doldu", "quota_exceeded", 3600)
        conc = int(plan.get("max_conc") or 0)
        if conc and self.user_inflight[user] >= conc:
            raise GatewayError(429, "Aynı anda çok fazla istek var, öncekinin bitmesini bekleyin", "too_many_concurrent", 2)
        self.user_inflight[user] += 1
        done = [False]

        def release():
            if not done[0]:
                done[0] = True
                self.user_inflight[user] -= 1
                if self.user_inflight[user] <= 0:
                    self.user_inflight.pop(user, None)
        return release

    def add_usage(self, user: str, plan: str, service: str, kind: str, in_tok: int = 0, out_tok: int = 0, audio_s: float = 0.0, chars: int = 0) -> None:
        if not user:
            return
        d = today()
        try:
            self.db.x("INSERT INTO usage_daily(day,user_id,plan,service,kind,requests,in_tok,out_tok,audio_s,chars) VALUES(?,?,?,?,?,1,?,?,?,?) "
                      "ON CONFLICT(day,user_id,service,kind) DO UPDATE SET requests=requests+1, in_tok=in_tok+excluded.in_tok, out_tok=out_tok+excluded.out_tok, "
                      "audio_s=audio_s+excluded.audio_s, chars=chars+excluded.chars, plan=excluded.plan",
                      (d, user, plan, service, kind, int(in_tok), int(out_tok), float(audio_s), int(chars)))
            k = (d, user)
            if k in self.day_tokens:
                self.day_tokens[k] += int(in_tok) + int(out_tok)
        except Exception:
            log.exception("kullanım kaydı")

    # ---- replika seçimi -----------------------------------------------------
    def _candidates(self, m: dict, exclude=()):
        out = []
        for d in self.cluster.deps_of(m["id"]):
            if d.state != "ready" or d.desired != "running" or d.id in exclude:
                continue
            n = self.cluster.nodes.get(d.node_id)
            if not n or n.status != "online" or n.draining or n.thermal == "crit":
                continue
            out.append((d, n))
        return out

    def _pick(self, m: dict, conv_key: str, prio: str, exclude=()):
        cands = self._candidates(m, exclude)
        if not cands:
            return None
        mc = max(1, int(m["max_concurrency"]))
        if prio == "low":
            if self.iwait[m["name"]] > 0:
                return None
            lim = max(1, mc // 2)
        else:
            lim = mc
        free = [(d, n) for d, n in cands if d.inflight < lim]
        if not free:
            return None
        if conv_key and self.db.get("gw.affinity"):
            pref = self.affinity.get(conv_key)
            for d, n in free:
                if d.id == pref:
                    self.affinity.move_to_end(conv_key)
                    return d
        best_tps = max((d.tps.get() for d, _ in free), default=0.0)

        def score(dn):
            d, n = dn
            speed = max(0.2, n.weight)
            if best_tps > 0 and d.tps.get() > 0:   # ölçülen hız: hızlı GPU daha çok iş alır
                speed = max(0.15, d.tps.get() / best_tps)
            s = (d.inflight + 0.5) / mc / speed
            if n.thermal == "warn":
                s += 0.5
            return s + random.random() * 0.01

        d, _ = min(free, key=score)
        if conv_key:
            self.affinity[conv_key] = d.id
            self.affinity.move_to_end(conv_key)
            while len(self.affinity) > 5000:
                self.affinity.popitem(last=False)
        return d

    async def _acquire(self, m: dict, conv_key: str, prio: str, exclude=(), rank: int = 1):
        """Boş yuvası olan hazır bir replika döndürür; yoksa kuyruklar / soğuk başlatmayı bekler.

        Küçük rank önce hizmet alır; bekledikçe öne geçer (ücretsiz kullanıcı aç kalmaz).
        """
        name = m["name"]
        t0 = now()
        interactive = prio != "low"
        counted_cold = False
        ranks = self.ranks[name]
        ranks[rank] += 1
        seq = next(self._seq)
        if interactive:
            self.iwait[name] += 1
        try:
            while True:
                eff = max(0, rank - int((now() - t0) / 15))
                blocked = any(c > 0 and r < eff for r, c in ranks.items())
                d = None if blocked else self._pick(m, conv_key, prio, exclude)
                if d is not None:
                    d.inflight += 1
                    return d, now() - t0
                reps = [x for x in self.cluster.deps_of(m["id"]) if x.desired == "running" and x.id not in exclude]
                ready_any = any(x.state == "ready" for x in reps)
                if not ready_any and not counted_cold and prio != "low":
                    self.cold[name] += 1
                    counted_cold = True
                if not ready_any and prio != "low":
                    self.ctx.autoscaler.request_capacity(m)
                    if not reps and not self.db.get("auto.enabled"):
                        raise GatewayError(503, f"'{name}' için çalışan replika yok ve otomatik ölçekleme kapalı", "no_replica")
                limit = float(self.db.get("gw.cold_start_timeout_s")) if not ready_any else float(self.db.get("gw.queue_timeout_s"))
                left = limit - (now() - t0)
                if left <= 0:
                    raise GatewayError(503, f"'{name}' için {limit:.0f} sn içinde boş replika bulunamadı", "timeout", 5)
                if sum(self.iwait.values()) > int(self.db.get("gw.max_queue")):
                    raise GatewayError(429, "Kuyruk dolu, daha sonra tekrar deneyin", "queue_full", 5)
                fut = asyncio.get_running_loop().create_future()
                entry = (rank, seq, fut)
                self.waiters[name].append(entry)
                try:
                    await asyncio.wait_for(fut, timeout=min(2.0, left))
                except asyncio.TimeoutError:
                    pass
                finally:
                    try:
                        self.waiters[name].remove(entry)
                    except ValueError:
                        pass
        finally:
            ranks[rank] -= 1
            if ranks[rank] <= 0:
                ranks.pop(rank, None)
            if interactive:
                self.iwait[name] -= 1
            if counted_cold:
                self.cold[name] -= 1

    def _release(self, d, m: dict) -> None:
        d.inflight = max(0, d.inflight - 1)
        d.served += 1
        self.cluster.touch(d)
        self._wake(m["name"], 1)

    def _mark_error(self, d) -> None:
        d.last_health = 0.0
        d.health_fail += 2
        self.cluster.kick.set()

    def _note_request(self, name: str, prio: str) -> None:
        t = now()
        self.last_ts[name] = t
        self.recent[name].append(t)
        if prio != "low":
            self.last_busy = t

    def _log_req(self, kind, model, node_id, user_id, status, ms, ttft, in_tok, out_tok, prio) -> None:
        try:
            self.db.x("INSERT INTO req_log(ts,kind,model,node_id,user_id,status,ms,ttft_ms,in_tok,out_tok,prio) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (now(), kind, model, node_id, user_id, status, ms, ttft, in_tok, out_tok, prio))
        except Exception:
            log.exception("req_log")

    # ---- sohbet -------------------------------------------------------------
    async def chat(self, body: dict, principal: Principal, *, user_id: str, conv_id: str = "", source: str = "api",
                   priority: str = "normal", log_turn: bool = True, plan: str = "", rank: int = 1, on_done=None):
        """-> (status, headers, payload)  payload: dict (JSON) ya da async bayt üreteci (akış)."""
        requested = str(body.get("model") or "")
        m, svc = self.resolve(requested, ("llm",))
        name = m["name"]
        public = svc["name"] if svc else name
        if not (principal.allows(public) or principal.allows(name)):
            raise GatewayError(403, "Bu anahtar bu modele erişemez", "forbidden")
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            raise GatewayError(400, "messages alanı gerekli", "invalid_request")
        rt = self.cluster.runtimes.get(m["runtime"])
        if rt is None:
            raise GatewayError(500, f"runtime tanımsız: {m['runtime']}", "config")
        conv = conv_id or chatlog.derive_conv_id(user_id, msgs)
        stream = bool(body.get("stream"))
        up = dict(body)
        up["model"] = rtm.upstream_model(rt, m)
        injected = False
        if stream and "stream_options" not in up:
            up["stream_options"] = {"include_usage": True}
            injected = True
        self._note_request(name, priority)
        t0 = now()
        interactive = priority != "low"
        if interactive:
            self.interactive_inflight += 1
        do_log = log_turn and principal.log_chats and bool(self.db.get("gw.log_chats"))
        key_id = principal.key_id

        def finish_turn(dep, node, text, finish, usage, ttft_ms, status, code=200):
            total = int((now() - t0) * 1000)
            in_tok = (usage or {}).get("prompt_tokens") or sum(len(chatlog.msg_text(x)) for x in msgs) // 3
            out_tok = (usage or {}).get("completion_tokens") or max(0, len(text) // 3)
            if dep is not None and out_tok > 3 and total - ttft_ms > 50:
                dep.tps.add(out_tok / ((total - ttft_ms) / 1000.0))
            if dep is not None and ttft_ms:
                dep.ttft.add(ttft_ms)
            self._log_req("chat", name, dep.node_id if dep else None, user_id, code, total, ttft_ms, in_tok, out_tok, priority)
            if principal.kind in ("key", "user") and code < 400:
                self.add_usage(user_id, plan, public, "chat", in_tok, out_tok)
            if do_log and (text or status != "ok"):
                try:
                    chatlog.record_turn(self.db, conv_id=conv, user_id=user_id, model=public, node=node.name if node else "",
                                        messages=msgs, reply=text, finish=finish, in_tok=in_tok, out_tok=out_tok,
                                        ttft_ms=ttft_ms, total_ms=total, status=status, key_id=key_id, source=source)
                except Exception:
                    log.exception("record_turn")

        def leave():
            if interactive:
                self.interactive_inflight -= 1
                self.last_busy = now()
            if on_done:
                on_done()

        excluded: set[int] = set()
        attempt = 0
        last_err = ""
        handed = False
        try:
            while attempt < 3:
                dep, waited = await self._acquire(m, conv, priority, excluded, rank)
                node = self.cluster.nodes[dep.node_id]
                url = self.cluster.dep_base(dep) + rtm.rt_path(rt, "chat", "/v1/chat/completions")
                hdr = {"x-orkestra-node": node.name, "x-orkestra-model": name, "x-orkestra-wait-ms": str(int(waited * 1000)),
                       "x-orkestra-conversation": conv}
                tstart = now()
                if not stream:
                    try:
                        r = await self.http.post(url, json=up)
                    except httpx.HTTPError as e:
                        self._release(dep, m)
                        self._mark_error(dep)
                        excluded.add(dep.id)
                        attempt += 1
                        last_err = f"{type(e).__name__}: {e}"
                        continue
                    self._release(dep, m)
                    if r.status_code >= 500 and attempt < 2:
                        excluded.add(dep.id)
                        attempt += 1
                        last_err = f"HTTP {r.status_code}"
                        if r.status_code != 503:
                            self._mark_error(dep)
                        continue
                    try:
                        data = r.json()
                    except Exception:
                        data = err_body(r.text[:300], "upstream_invalid")
                    text, finish, usage = "", None, data.get("usage") if isinstance(data, dict) else None
                    try:
                        ch = data["choices"][0]
                        text = (ch.get("message") or {}).get("content") or ""
                        finish = ch.get("finish_reason")
                    except Exception:
                        pass
                    if isinstance(data, dict) and data.get("model"):
                        data["model"] = public
                    ttft = int((now() - tstart) * 1000)
                    dep.tok_count += int((usage or {}).get("completion_tokens") or max(0, len(text) // 3))
                    finish_turn(dep, node, text, finish, usage, ttft, "ok" if r.status_code == 200 else "error", r.status_code)
                    return r.status_code, hdr, data
                # --- akış ---
                try:
                    req = self.http.build_request("POST", url, json=up)
                    resp = await self.http.send(req, stream=True)
                except httpx.HTTPError as e:
                    self._release(dep, m)
                    self._mark_error(dep)
                    excluded.add(dep.id)
                    attempt += 1
                    last_err = f"{type(e).__name__}: {e}"
                    continue
                if resp.status_code != 200:
                    raw = await resp.aread()
                    await resp.aclose()
                    self._release(dep, m)
                    if resp.status_code == 400 and injected and b"stream_options" in raw:
                        up.pop("stream_options", None)
                        injected = False
                        continue
                    if resp.status_code >= 500 and attempt < 2:
                        excluded.add(dep.id)
                        attempt += 1
                        last_err = f"HTTP {resp.status_code}"
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = err_body(raw.decode("utf-8", "ignore")[:300], "upstream_error")
                    finish_turn(dep, node, "", None, None, 0, "error", resp.status_code)
                    return resp.status_code, hdr, data
                handed = True
                return 200, hdr, Relay(self, resp, dep, node, m, finish_turn, leave, tstart)
            raise GatewayError(502, f"Yukarı akış hatası: {last_err}", "upstream_error", 3)
        finally:
            if not handed:
                leave()

    # ---- basit uçlar (completions, embeddings, ses) --------------------------
    async def simple(self, kind_ok: tuple, path: str, body: dict, principal: Principal, *, user_id: str,
                     files: dict | None = None, data: dict | None = None, priority: str = "normal",
                     plan: str = "", rank: int = 1, on_done=None, stream: bool = False):
        """Model adına göre bir replikaya istek gönderir ve yanıtı aynen döndürür. stream=True: yanıt parçalı aktarılır."""
        name = str(body.get("model") or (data or {}).get("model") or "")
        m, svc = self.resolve(name, kind_ok)
        name = m["name"]
        public = svc["name"] if svc else name
        if not (principal.allows(public) or principal.allows(name)):
            raise GatewayError(403, "Bu anahtar bu modele erişemez", "forbidden")
        opts = (svc or {}).get("options") or {}
        rt = self.cluster.runtimes.get(m["runtime"])
        up_model = rtm.upstream_model(rt, m) if rt else name
        kind = PATH_KEYS.get(path, "")
        up_path = rtm.rt_path(rt, kind, path)  # yamalı/özel runtime'larda farklı uç nokta yolu olabilir
        self._note_request(name, priority)
        t0 = now()
        interactive = priority != "low"
        if interactive:
            self.interactive_inflight += 1
        excluded: set[int] = set()
        last_err = ""
        handed = False

        def account(dep, status, payload_size=0):
            ms = int((now() - t0) * 1000)
            self._log_req(path.split("/")[-1], name, dep.node_id if dep else None, user_id, status, ms, 0, 0, 0, priority)
            if principal.kind in ("key", "user") and status < 400:
                if kind == "tts":
                    self.add_usage(user_id, plan, public, "tts", chars=len(str(body.get("input") or "")))
                elif kind == "stt":
                    size = sum(len(v[1]) for v in (files or {}).values())
                    self.add_usage(user_id, plan, public, "stt", audio_s=size * 8 / 32000.0)
                else:
                    self.add_usage(user_id, plan, public, kind or "api")

        def leave():
            if interactive:
                self.interactive_inflight -= 1
                self.last_busy = now()
            if on_done:
                on_done()

        try:
            for attempt in range(3):
                dep, _ = await self._acquire(m, "", priority, excluded, rank)
                node = self.cluster.nodes[dep.node_id]
                url = self.cluster.dep_base(dep) + up_path
                hdr = {"x-orkestra-node": node.name, "x-orkestra-model": name}
                try:
                    if files is not None:
                        d2 = {**{k: str(v) for k, v in opts.items() if k in ("language", "prompt", "temperature", "response_format")}, **(data or {})}
                        d2["model"] = up_model
                        req = self.http.build_request("POST", url, files=files, data=d2, timeout=600)
                    else:
                        b2 = {**({k: v for k, v in opts.items() if k in ("voice", "response_format", "speed")} if kind == "tts" else {}), **body}
                        b2["model"] = up_model
                        req = self.http.build_request("POST", url, json=b2, timeout=600)
                    r = await self.http.send(req, stream=True)
                except httpx.HTTPError as e:
                    self._release(dep, m)
                    self._mark_error(dep)
                    excluded.add(dep.id)
                    last_err = f"{type(e).__name__}: {e}"
                    continue
                if r.status_code >= 500 and attempt < 2:
                    await r.aclose()
                    self._release(dep, m)
                    excluded.add(dep.id)
                    last_err = f"HTTP {r.status_code}"
                    continue
                ct = r.headers.get("content-type", "application/json")
                if stream and r.status_code == 200:
                    handed = True
                    return 200, hdr, RawRelay(self, r, dep, m, account, leave), ct
                try:
                    content = await r.aread()
                finally:
                    await r.aclose()
                    self._release(dep, m)
                account(dep, r.status_code)
                return r.status_code, hdr, content, ct
            raise GatewayError(502, f"Yukarı akış hatası: {last_err}", "upstream_error", 3)
        finally:
            if not handed:
                leave()

    # ---- dahili çağrı (distillation, demo trafiği, deneme) ----------------------
    async def internal_chat(self, model: str, messages: list, *, user_id: str = "internal", conv_id: str = "",
                            source: str = "internal", priority: str = "normal", max_tokens: int | None = None,
                            temperature: float | None = None, log_turn: bool = True) -> dict:
        body: dict = {"model": model, "messages": messages, "stream": False}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        try:
            status, hdr, payload = await self.chat(body, Principal("internal", "internal"), user_id=user_id, conv_id=conv_id,
                                                   source=source, priority=priority, log_turn=log_turn)
        except GatewayError as e:
            return {"ok": False, "status": e.status, "error": e.message}
        if status != 200:
            msg = (payload.get("error") or {}).get("message", str(payload)[:200]) if isinstance(payload, dict) else str(payload)
            return {"ok": False, "status": status, "error": msg}
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except Exception:
            return {"ok": False, "status": 502, "error": "geçersiz yanıt"}
        return {"ok": True, "text": text, "usage": payload.get("usage") or {}, "node": hdr.get("x-orkestra-node", "")}

    # ---- site için servis durumu ------------------------------------------------
    def service_view(self, s: dict) -> dict:
        ready = queue = inflight = cap = 0
        models = []
        for t in s["targets"]:
            m = self.by_name(t)
            if not m:
                models.append({"name": t, "missing": True})
                continue
            c = self._candidates(m)
            dm = self.demand(t)
            ready += len(c)
            queue += dm["waiting"]
            inflight += dm["inflight"]
            cap += len(c) * int(m["max_concurrency"])
            models.append({"name": t, "ready": len(c), "enabled": bool(m["enabled"]), "min_replicas": m["min_replicas"], "rpm": dm["rpm"]})
        return {"name": s["name"], "kind": s["kind"], "enabled": s["enabled"], "is_default": s["is_default"], "targets": s["targets"], "options": s["options"],
                "notes": s["notes"], "ready": ready, "queue": queue, "inflight": inflight, "capacity": cap, "models": models}


class Relay:
    """Yukarı akış SSE yanıtını istemciye aktarır.

    Bitişte (ya da istemci koparsa) yuvayı serbest bırakır, sayaçları düşer ve konuşmayı kaydeder.
    Üst akım sustuğunda yorum satırı göndererek vekil/hosting zaman aşımlarını önler.
    cleanup() tekrar çağrılabilir; hem üreteç finally'sinden hem de BackgroundTask'tan çağrılır.
    """

    def __init__(self, gw, resp, dep, node, m, finish_turn, leave, tstart):
        self.gw, self.resp, self.dep, self.node, self.m = gw, resp, dep, node, m
        self.finish_turn, self.leave, self.tstart = finish_turn, leave, tstart
        self.dec = codecs.getincrementaldecoder("utf-8")("ignore")
        self.buf, self.parts, self.usage, self.finish = "", [], None, None
        self.ttft, self.status, self.done = 0, "ok", False

    def __aiter__(self):
        return self._gen()

    def _parse(self, chunk: bytes) -> None:
        self.buf += self.dec.decode(chunk)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                j = json.loads(payload)
            except Exception:
                continue
            if j.get("usage"):
                self.usage = j["usage"]
            ch = (j.get("choices") or [{}])[0]
            delta = (ch.get("delta") or {}).get("content")
            if delta:
                if not self.ttft:
                    self.ttft = max(1, int((now() - self.tstart) * 1000))
                self.parts.append(delta)
                self.dep.tok_count += 1
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]

    async def _gen(self):
        it = self.resp.aiter_bytes().__aiter__()
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=12)
                if not done:
                    yield b": ping\n\n"
                    continue
                task, pending = pending, None
                try:
                    chunk = task.result()
                except StopAsyncIteration:
                    break
                yield chunk
                self._parse(chunk)
        except (asyncio.CancelledError, GeneratorExit):
            self.status = "client_disconnect"
            raise
        except httpx.HTTPError as e:
            self.status = "upstream_error"
            yield ("data: " + jdump(err_body(f"akış kesildi: {e}", "stream_error")) + "\n\n").encode()
        finally:
            if pending is not None:
                pending.cancel()
            await self.cleanup()

    async def cleanup(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            await self.resp.aclose()
        except Exception:
            pass
        self.gw._release(self.dep, self.m)
        self.finish_turn(self.dep, self.node, "".join(self.parts), self.finish, self.usage, self.ttft, self.status, 200)
        self.leave()


class RawRelay:
    """Ses gibi ikili yanıtları parça parça aktarır: ilk ses bütün metin üretilmeden duyulur."""

    def __init__(self, gw, resp, dep, m, account, leave):
        self.gw, self.resp, self.dep, self.m, self.account, self.leave = gw, resp, dep, m, account, leave
        self.done = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        try:
            async for chunk in self.resp.aiter_raw():
                yield chunk
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            await self.resp.aclose()
        except Exception:
            pass
        self.gw._release(self.dep, self.m)
        self.account(self.dep, 200)
        self.leave()


# ---------------------------------------------------------------------------- HTTP uçları
def make_router(ctx, admin: bool) -> APIRouter:
    r = APIRouter()
    gw: Gateway = ctx.gateway
    auth = ctx.auth

    def who(request: Request) -> Principal:
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        if tok:
            p = auth.verify_user_token(tok) if tok.startswith("ut1.") else auth.lookup_key(tok)
            if p:
                return p
            raise GatewayError(401, "Geçersiz API anahtarı", "invalid_api_key")
        if admin and auth.session_valid(request.cookies.get(auth.COOKIE)):
            return Principal("admin", "admin")
        raise GatewayError(401, "Kimlik doğrulama gerekli (Authorization: Bearer <anahtar>)", "missing_api_key")

    def ident(request: Request, body: dict, p: Principal) -> tuple[str, str, dict]:
        """İstek sahibi kullanıcı ve paketi. İmzalı kullanıcı jetonunda değerler jetondan gelir."""
        if p.kind == "user":
            uid, plan_name = p.user_id, p.plan
        else:
            uid = str(request.headers.get("x-user-id") or body.get("user") or "").strip()[:128]
            plan_name = str(request.headers.get("x-user-plan") or "").strip()[:32]
        if not uid:
            uid = p.name if p.kind not in ("key", "user") else f"key:{p.name}"
        name, plan = gw.plan_for(plan_name)
        return uid, name, plan

    def clamp_tokens(body: dict, plan: dict) -> None:
        cap = int(plan.get("max_tokens") or 0)
        if not cap:
            return
        cur = body.get("max_tokens") or body.get("max_completion_tokens")
        body["max_tokens"] = min(int(cur), cap) if cur else cap
        body.pop("max_completion_tokens", None)

    def error_response(e: GatewayError) -> JSONResponse:
        hdr = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return JSONResponse(err_body(e.message, e.code), status_code=e.status, headers=hdr)

    async def guarded(request: Request, fn):
        try:
            p = who(request)
            if not auth.rate_ok(p):
                raise GatewayError(429, "İstek sınırı aşıldı", "rate_limit", 2)
            return await fn(p)
        except GatewayError as e:
            return error_response(e)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse(err_body(f"Geçersiz istek: {e}", "invalid_request"), status_code=400)

    @r.get("/v1/models")
    async def models(request: Request):
        try:
            p = who(request)
        except GatewayError as e:
            return error_response(e)
        data = []
        if gw.services and p.kind in ("key", "user"):
            for s in gw.services.values():
                if s["enabled"] and p.allows(s["name"]):
                    v = gw.service_view(s)
                    data.append({"id": s["name"], "object": "model", "created": int(s["created_at"]), "owned_by": "local",
                                 "x_info": {"kind": s["kind"], "ready": v["ready"] > 0, "default": s["is_default"]}})
            return {"object": "list", "data": data}
        for m in ctx.cluster.models.values():
            if not m["enabled"] or not p.allows(m["name"]):
                continue
            deps = ctx.cluster.deps_of(m["id"])
            data.append({"id": m["name"], "object": "model", "created": int(m["created_at"]), "owned_by": "local",
                         "x_orkestra": {"kind": m["kind"], "ready": sum(1 for d in deps if d.state == "ready"),
                                        "loading": sum(1 for d in deps if d.state in ("starting", "pending", "syncing") and d.desired == "running")}})
        return {"object": "list", "data": data}

    @r.get("/v1/status")
    async def status(request: Request):
        """Site için servis durumu: hazır mı, yoğun mu?"""
        try:
            p = who(request)
        except GatewayError as e:
            return error_response(e)
        out = []
        for s in gw.services.values():
            if s["enabled"] and p.allows(s["name"]):
                v = gw.service_view(s)
                busy = v["queue"] > 0 or (v["capacity"] and v["inflight"] >= v["capacity"])
                out.append({"name": s["name"], "kind": s["kind"], "ready": v["ready"] > 0, "busy": bool(busy), "queue": v["queue"]})
        return {"status": "ok", "services": out, "ts": now()}

    @r.get("/v1/usage")
    async def usage(request: Request, user: str = "", days: int = 30):
        """Faturalama/gösterim için kullanıcı kullanımı."""
        try:
            p = who(request)
        except GatewayError as e:
            return error_response(e)
        uid = p.user_id if p.kind == "user" else user.strip()
        if not uid:
            return error_response(GatewayError(400, "user parametresi gerekli", "invalid_request"))
        since = time.strftime("%Y-%m-%d", time.localtime(now() - max(1, min(366, days)) * 86400))
        rows = ctx.db.q("SELECT day,service,kind,requests,in_tok,out_tok,audio_s,chars FROM usage_daily WHERE user_id=? AND day>=? ORDER BY day DESC", (uid, since))
        plan_name, plan = gw.plan_for(p.plan if p.kind == "user" else str(request.headers.get("x-user-plan") or ""))
        limit = int(plan.get("day_tokens") or 0)
        used = gw.used_today(uid)
        return {"user": uid, "plan": plan_name, "today": {"tokens": used, "limit": limit, "remaining": max(0, limit - used) if limit else None}, "days": rows}

    @r.post("/v1/chat/completions")
    async def chat(request: Request):
        async def run(p: Principal):
            body = await request.json()
            uid, plan_name, plan = ident(request, body, p)
            clamp_tokens(body, plan) if p.kind in ("key", "user") else None
            release = gw.admit(uid, plan_name, plan, p)
            prio = "low" if request.headers.get("x-priority", "").lower() == "low" and p.kind not in ("key", "user") else "normal"
            source = "panel" if p.kind == "admin" else request.headers.get("x-source", "site")[:32]
            kw = dict(user_id=uid, conv_id=request.headers.get("x-conversation-id", "")[:64], source=source, priority=prio,
                      plan=plan_name, rank=int(plan.get("rank", 1)), on_done=release)
            task = asyncio.create_task(gw.chat(body, p, **kw))
            stream_hdr = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            try:
                if body.get("stream"):
                    done, _ = await asyncio.wait({task}, timeout=6)
                    if not done:  # kuyruk/soğuk başlatma: bağlantı canlı kalsın, hata olursa akış içinde bildirilir
                        return StreamingResponse(_waiting_stream(task), media_type="text/event-stream", headers=stream_hdr)
                status, hdr, payload = await task
            except (GatewayError, asyncio.CancelledError):
                release()
                raise
            if isinstance(payload, dict):
                return JSONResponse(payload, status_code=status, headers=hdr)
            return StreamingResponse(payload, media_type="text/event-stream", headers={**hdr, **stream_hdr}, background=BackgroundTask(payload.cleanup))
        return await guarded(request, run)

    async def _waiting_stream(task: asyncio.Task):
        relay = None
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=8)
                if done:
                    break
                yield b": kuyrukta\n\n"
            try:
                _, _, payload = task.result()
            except GatewayError as e:
                yield ("data: " + jdump(err_body(e.message, e.code)) + "\n\ndata: [DONE]\n\n").encode()
                return
            if isinstance(payload, dict):
                yield ("data: " + jdump(payload) + "\n\ndata: [DONE]\n\n").encode()
                return
            relay = payload
            async for chunk in relay:
                yield chunk
        finally:
            if not task.done():
                task.cancel()
            elif relay is None and not task.cancelled() and task.exception() is None:
                res = task.result()[2]
                if not isinstance(res, dict):
                    await res.cleanup()

    def plain(fn_kind: tuple, path: str):
        async def handler(request: Request):
            async def run(p: Principal):
                body = await request.json()
                uid, plan_name, plan = ident(request, body, p)
                release = gw.admit(uid, plan_name, plan, p)
                try:
                    st, hdr, content, ct = await gw.simple(fn_kind, path, body, p, user_id=uid, plan=plan_name, rank=int(plan.get("rank", 1)), on_done=release)
                except GatewayError:
                    release()
                    raise
                return Response(content=content, status_code=st, media_type=ct, headers=hdr)
            return await guarded(request, run)
        return handler

    r.add_api_route("/v1/completions", plain(("llm",), "/v1/completions"), methods=["POST"])
    r.add_api_route("/v1/embeddings", plain(("embedding", "llm"), "/v1/embeddings"), methods=["POST"])

    @r.post("/v1/audio/transcriptions")
    async def stt(request: Request):
        async def run(p: Principal):
            limit = int(ctx.db.get("gw.max_upload_mb")) * 1024 * 1024
            if int(request.headers.get("content-length") or 0) > limit:
                raise GatewayError(413, f"Ses dosyası çok büyük (en çok {limit // 1048576} MB)", "too_large")
            form = await request.form()
            f = form.get("file")
            if f is None or not hasattr(f, "read"):
                raise GatewayError(400, "file alanı gerekli", "invalid_request")
            content = await f.read()
            data = {k: v for k, v in form.items() if k != "file" and isinstance(v, str)}
            files = {"file": (getattr(f, "filename", "audio.webm") or "audio.webm", content,
                              getattr(f, "content_type", None) or "application/octet-stream")}
            uid, plan_name, plan = ident(request, data, p)
            release = gw.admit(uid, plan_name, plan, p)
            try:
                st, hdr, out, ct = await gw.simple(("stt",), "/v1/audio/transcriptions", {}, p, user_id=uid, files=files, data=data,
                                                   plan=plan_name, rank=int(plan.get("rank", 1)), on_done=release)
            except GatewayError:
                release()
                raise
            return Response(content=out, status_code=st, media_type=ct, headers=hdr)
        return await guarded(request, run)

    @r.post("/v1/audio/speech")
    async def tts(request: Request):
        async def run(p: Principal):
            body = await request.json()
            if not str(body.get("input") or "").strip():
                raise GatewayError(400, "input alanı gerekli", "invalid_request")
            uid, plan_name, plan = ident(request, body, p)
            release = gw.admit(uid, plan_name, plan, p)
            try:
                st, hdr, out, ct = await gw.simple(("tts",), "/v1/audio/speech", body, p, user_id=uid, plan=plan_name,
                                                   rank=int(plan.get("rank", 1)), on_done=release, stream=True)
            except GatewayError:
                release()
                raise
            if isinstance(out, (bytes, bytearray)):
                return Response(content=out, status_code=st, media_type=ct, headers=hdr)
            return StreamingResponse(out, status_code=st, media_type=ct, headers={**hdr, "X-Accel-Buffering": "no"}, background=BackgroundTask(out.cleanup))
        return await guarded(request, run)

    return r
