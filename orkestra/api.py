"""Yönetici REST API'si (panelin kullandığı uçlar)."""
from __future__ import annotations

import asyncio
import io
import json
import platform
import re
import socket
import sys
import time
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from . import __version__, chatlog
from . import runtimes as rtm
from .auth import RateLimited
from .cluster import LOCAL_IPS
from .db import SECRET_KEYS, SETTINGS_DEFAULTS
from .fabric import repo_variants
from .uplink import relay_source
from .util import jdump, jload, now, rand_token

NODE_SPEC = dict(name=str, host=str, agent_port=int, agent_token=str, mac=str, role=str, tags=list, enabled=bool,
                 allow_sleep=bool, allow_restart=bool, always_on=bool, weight=float, warn_c=float, crit_c=float)
MODEL_SPEC = dict(name=str, repo=str, runtime=str, kind=str, upstream=str, ctx=int, vram_gb=float, ram_gb=float,
                  gpu_count=int, cpu_ok=bool, min_replicas=int, max_replicas=int, priority=int, max_concurrency=int,
                  idle_unload_min=int, startup_timeout_s=int, prefer_tags=list, exclude_tags=list, extra_args=str,
                  enabled=bool, notes=str, source=str, file_glob=str)
JSON_COLS = {"tags", "prefer_tags", "exclude_tags", "rt_paths"}
RANGES = {"1h": (3600, 30), "6h": (21600, 180), "24h": (86400, 600), "7d": (604800, 3600)}


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def conv(v, typ):
    if typ is bool:
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "evet")
        return bool(v)
    if typ is int:
        return int(float(v))
    if typ is float:
        return float(v)
    if typ is list:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [str(x).strip() for x in v]
    return str(v).strip()


def clean(body: dict, spec: dict) -> dict:
    out = {}
    for k, typ in spec.items():
        if k in body and body[k] is not None:
            try:
                out[k] = conv(body[k], typ)
            except Exception:
                raise HTTPException(400, f"Geçersiz değer: {k}")
    return out


def to_db(vals: dict) -> dict:
    out = {}
    for k, v in vals.items():
        out[k] = jdump(v) if k in JSON_COLS else (int(v) if isinstance(v, bool) else v)
    return out


def norm_mac(mac: str) -> str:
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if not h:
        return ""
    if len(h) != 12:
        raise HTTPException(400, "MAC adresi 12 onaltılık hane olmalı (ör. aa:bb:cc:dd:ee:ff)")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).lower()


def make_router(ctx) -> APIRouter:
    db, c, auth = ctx.db, ctx.cluster, ctx.auth
    boot = ctx.boot

    def admin(request: Request):
        if not auth.session_valid(request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "Oturum gerekli")
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-orkestra") != "1":
            raise HTTPException(403, "Geçersiz istek")

    open_r = APIRouter(prefix="/api")
    r = APIRouter(prefix="/api", dependencies=[Depends(admin)])

    def controller_url(request: Request) -> str:
        u = (db.get("net.controller_url") or "").strip()
        if u:
            return u.rstrip("/")
        host = request.url.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1", ""):
            host = lan_ip()
        return f"http://{host}:{boot.admin_port}"

    def node_or_404(nid: int):
        n = c.nodes.get(nid)
        if not n:
            raise HTTPException(404, "Düğüm bulunamadı")
        return n

    def model_or_404(mid: int) -> dict:
        m = c.models.get(mid)
        if not m:
            raise HTTPException(404, "Model bulunamadı")
        return m

    # ======================================================================= kimlik
    @open_r.get("/auth/state")
    async def auth_state(request: Request):
        return {"configured": auth.configured(), "authenticated": auth.session_valid(request.cookies.get(auth.COOKIE)),
                "demo": bool(ctx.sim), "version": __version__}

    @open_r.post("/auth/setup")
    async def auth_setup(request: Request, response: Response):
        if auth.configured():
            raise HTTPException(409, "Parola zaten belirlenmiş")
        if request.headers.get("x-orkestra") != "1":
            raise HTTPException(403, "Geçersiz istek")
        b = await request.json()
        try:
            auth.set_password(str(b.get("password", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        tok = auth.login(str(b["password"]), request.client.host if request.client else "?")
        response.set_cookie(auth.COOKIE, tok, httponly=True, samesite="strict", max_age=auth.TTL)
        return {"ok": True}

    @open_r.post("/auth/login")
    async def auth_login(request: Request, response: Response):
        if request.headers.get("x-orkestra") != "1":
            raise HTTPException(403, "Geçersiz istek")
        b = await request.json()
        try:
            tok = auth.login(str(b.get("password", "")), request.client.host if request.client else "?")
        except RateLimited as e:
            raise HTTPException(429, str(e))
        if not tok:
            raise HTTPException(401, "Parola hatalı")
        response.set_cookie(auth.COOKIE, tok, httponly=True, samesite="strict", max_age=auth.TTL)
        return {"ok": True}

    @open_r.post("/auth/logout")
    async def auth_logout(request: Request, response: Response):
        auth.logout(request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    # ======================================================================= genel bakış
    def series_requests(minutes: int = 30) -> list[dict]:
        t0 = int(now() // 60) * 60 - (minutes - 1) * 60
        rows = db.q("SELECT CAST(ts/60 AS INT)*60 b, COUNT(*) n, COALESCE(SUM(out_tok),0) tok, "
                    "AVG(CASE WHEN ttft_ms>0 THEN ttft_ms END) ttft FROM req_log WHERE ts>=? AND kind='chat' GROUP BY b", (t0,))
        by = {x["b"]: x for x in rows}
        out = []
        for i in range(minutes):
            b = t0 + i * 60
            x = by.get(b)
            out.append({"ts": b, "rpm": x["n"] if x else 0, "tps": round(x["tok"] / 60.0, 1) if x else 0,
                        "ttft": int(x["ttft"]) if x and x["ttft"] else 0})
        return out

    def series_temps(minutes: int = 30) -> list[dict]:
        t0 = int(now() // 60) * 60 - (minutes - 1) * 60
        rows = db.q("SELECT (ts/60)*60 b, AVG(gpu_temp) gt, MAX(gpu_temp) gtm, AVG(cpu_temp) ct FROM metrics WHERE ts>=? GROUP BY b", (t0,))
        prow = db.q("SELECT b, SUM(p) p FROM (SELECT node_id, (ts/60)*60 b, AVG(power) p FROM metrics WHERE ts>=? "
                    "GROUP BY node_id, b) GROUP BY b", (t0,))
        by = {x["b"]: x for x in rows}
        pw = {x["b"]: x["p"] for x in prow}
        out = []
        for i in range(minutes):
            b = t0 + i * 60
            x = by.get(b)
            out.append({"ts": b, "gpu_t": round(x["gt"], 1) if x and x["gt"] is not None else None,
                        "gpu_tmax": round(x["gtm"], 1) if x and x["gtm"] is not None else None,
                        "cpu_t": round(x["ct"], 1) if x and x["ct"] is not None else None,
                        "power": round(pw.get(b) or 0)})
        return out

    @r.get("/overview")
    async def overview():
        t = now()
        nodes = [c.node_view(n) for n in sorted(c.nodes.values(), key=lambda x: x.name)]
        cnt = {k: sum(1 for n in nodes if n["status"] == k) for k in ("online", "sleeping", "offline", "waking", "rebooting", "unknown")}
        deps = [c.dep_view(d) for d in c.deps.values()]
        ready = sum(1 for d in deps if d["state"] == "ready")
        m1 = db.one("SELECT COUNT(*) n, COALESCE(SUM(out_tok),0) tok FROM req_log WHERE ts>=? AND kind='chat'", (t - 60,))
        m5 = db.one("SELECT AVG(CASE WHEN ttft_ms>0 THEN ttft_ms END) ttft, AVG(CASE WHEN status>=400 THEN 1.0 ELSE 0.0 END) err "
                    "FROM req_log WHERE ts>=? AND kind='chat'", (t - 300,))
        gw = ctx.gateway
        demand = {m["name"]: gw.demand(m["name"]) for m in c.models.values()}
        return {
            "ts": t, "demo": bool(ctx.sim),
            "kpi": {
                "nodes_total": len(nodes), "nodes_online": cnt["online"], "nodes_sleeping": cnt["sleeping"],
                "nodes_offline": cnt["offline"] + cnt.get("unknown", 0), "nodes_waking": cnt["waking"] + cnt["rebooting"],
                "replicas_ready": ready, "replicas_total": len(deps), "rpm": m1["n"], "tps": round(m1["tok"] / 60.0, 1),
                "ttft_ms": int(m5["ttft"] or 0), "err_rate": round((m5["err"] or 0) * 100, 1),
                "queue": sum(v["waiting"] for v in demand.values()), "inflight": sum(v["inflight"] for v in demand.values()),
                "power_w": round(sum((n["power_w"] or 0) for n in nodes)),
                "users_24h": db.one("SELECT COUNT(*) n FROM users WHERE last_seen>=?", (t - 86400,))["n"],
                "conversations": db.one("SELECT COUNT(*) n FROM conversations")["n"],
            },
            "nodes": nodes, "deployments": deps, "demand": demand,
            "events": db.q("SELECT * FROM events ORDER BY id DESC LIMIT 14"),
            "series": {"req": series_requests(30), "temp": series_temps(30)},
            "autoscaler": {"enabled": bool(db.get("auto.enabled")), "dry_run": bool(db.get("auto.dry_run")),
                           "mode": db.get("auto.mode"), "unmet": list(ctx.autoscaler.unmet.values())},
            "distill": ctx.distill.status(),
            "uplink": {k: ctx.uplink.st.get(k) for k in ("mode", "state", "rtt_ms", "public_url", "error")},
            "net": {"modem": ctx.network.modem, "internet": ctx.network.internet},
            "transfers": sum(1 for x in ctx.fabric.xfers.values() if x.state in ("active", "queued")),
        }

    # ======================================================================= düğümler
    @r.get("/live")
    async def live(window: int = 300, since: float = 0.0):
        return c.live(min(3600, max(30, window)), since)

    @r.get("/nodes")
    async def nodes_list():
        return [c.node_view(n) for n in sorted(c.nodes.values(), key=lambda x: x.name)]

    @r.post("/nodes")
    async def nodes_create(request: Request):
        b = await request.json()
        v = clean(b, NODE_SPEC)
        if not v.get("name") or not v.get("host"):
            raise HTTPException(400, "Ad ve IP/ana makine adı gerekli")
        if db.one("SELECT id FROM nodes WHERE name=?", (v["name"],)):
            raise HTTPException(409, "Bu ad zaten kullanılıyor")
        v["mac"] = norm_mac(v.get("mac", ""))
        v.setdefault("agent_token", "") if v.get("agent_token") else v.update(agent_token=rand_token(16))
        role = v.get("role", "laptop")
        v.setdefault("allow_sleep", role == "laptop")
        if v["host"] in LOCAL_IPS:  # kontrol sunucusunun çalıştığı makine (ana PC) uyutulup yeniden başlatılmasın
            v.update(always_on=True, allow_sleep=False, allow_restart=False)
        if isinstance(b.get("rt_paths"), dict):
            v["rt_paths"] = {str(k): str(x).strip() for k, x in b["rt_paths"].items() if str(x).strip()}
        cols = to_db(v)
        cols["created_at"] = now()
        nid = db.x(f"INSERT INTO nodes({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
        c.reload()
        db.event("info", "node", f"Düğüm eklendi: {v['name']} ({v['host']})", nid)
        return {"node": c.node_view(c.nodes[nid]), "install_cmd": install_cmd(request, v["agent_token"])}

    def install_cmd(request: Request, token: str) -> str:
        cu = controller_url(request)
        return f"curl -fsSL {cu}/agent/install.sh | sudo bash -s -- --controller {cu} --token {token}"

    @r.get("/nodes/{nid}")
    async def node_get(nid: int, request: Request):
        n = node_or_404(nid)
        return {"node": c.node_view(n), "install_cmd": install_cmd(request, n.agent_token), "agent_token": n.agent_token}

    @r.patch("/nodes/{nid}")
    async def node_patch(nid: int, request: Request):
        n = node_or_404(nid)
        b = await request.json()
        v = clean(b, NODE_SPEC)
        if isinstance(b.get("rt_paths"), dict):
            v["rt_paths"] = {str(k): str(x).strip() for k, x in b["rt_paths"].items() if str(x).strip()}
        if "mac" in v:
            v["mac"] = norm_mac(v["mac"])
        if "name" in v and v["name"] != n.name and db.one("SELECT id FROM nodes WHERE name=?", (v["name"],)):
            raise HTTPException(409, "Bu ad zaten kullanılıyor")
        if v:
            cols = to_db(v)
            db.x(f"UPDATE nodes SET {','.join(k + '=?' for k in cols)} WHERE id=?", tuple(cols.values()) + (nid,))
            c.reload()
        return {"node": c.node_view(c.nodes[nid])}

    @r.delete("/nodes/{nid}")
    async def node_delete(nid: int):
        n = node_or_404(nid)
        for d in list(c.deps_on(nid)):
            await c.remove_deployment(d.id, "düğüm silindi")
        db.x("DELETE FROM nodes WHERE id=?", (nid,))
        c.reload()
        db.event("info", "node", f"Düğüm silindi: {n.name}")
        return {"ok": True}

    @r.post("/nodes/{nid}/test")
    async def node_test(nid: int):
        n = node_or_404(nid)
        try:
            info = await c.client(n).info()
        except Exception as e:
            raise HTTPException(502, f"Ajana ulaşılamadı: {type(e).__name__}: {str(e)[:160]}")
        c._apply_info(n, info)
        return {"ok": True, "info": info}

    @r.post("/nodes/{nid}/power")
    async def node_power(nid: int, request: Request):
        n = node_or_404(nid)
        b = await request.json()
        action = str(b.get("action", ""))
        if action == "enable":
            db.x("UPDATE nodes SET enabled=1 WHERE id=?", (nid,))
            c.reload()
        elif action == "disable":
            db.x("UPDATE nodes SET enabled=0 WHERE id=?", (nid,))
            c.reload()
            db.event("info", "node", f"{n.name} bakım moduna alındı", nid)
        elif action == "drain":
            n.draining = True
            c.kick.set()
        elif action == "undrain":
            n.draining = False
            c.kick.set()
        else:
            try:
                await c.power(n, action, "elle", force=bool(b.get("force")))
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception as e:
                raise HTTPException(502, f"{type(e).__name__}: {str(e)[:200]}")
        return {"node": c.node_view(n)}

    @r.get("/nodes/{nid}/history")
    async def node_history(nid: int, range: str = "1h"):
        node_or_404(nid)
        span, bucket = RANGES.get(range, RANGES["1h"])
        rows = db.q(f"SELECT (ts/{bucket})*{bucket} b, AVG(cpu) cpu, AVG(mem) mem, AVG(cpu_temp) cpu_temp, AVG(gpu_util) gpu_util, "
                    "AVG(gpu_mem) gpu_mem, AVG(gpu_temp) gpu_temp, AVG(power) power, AVG(net) net, MAX(inflight) inflight "
                    "FROM metrics WHERE node_id=? AND ts>=? GROUP BY b ORDER BY b", (nid, int(now()) - span))
        keys = ("cpu", "mem", "cpu_temp", "gpu_util", "gpu_mem", "gpu_temp", "power", "net", "inflight")
        out = {"ts": [x["b"] for x in rows], "bucket": bucket, "span": span}
        for k in keys:
            out[k] = [None if x[k] is None else round(x[k], 1) for x in rows]
        return out

    @r.get("/nodes/{nid}/logs")
    async def node_logs(nid: int, proc: str, tail: int = 200):
        try:
            return {"text": await c.logs(nid, proc, min(1000, max(10, tail)))}
        except Exception as e:
            raise HTTPException(502, f"{type(e).__name__}: {str(e)[:200]}")

    # ======================================================================= modeller
    def model_view(m: dict) -> dict:
        rt = c.runtimes.get(m["runtime"]) or {}
        deps = [c.dep_view(d) for d in c.deps_of(m["id"])]
        man = jload(m.get("manifest"), None) or {}
        return {**{k: v for k, v in m.items() if k != "manifest"}, "prefer_tags": jload(m["prefer_tags"], []), "exclude_tags": jload(m["exclude_tags"], []),
                "files_gb": round((man.get("total") or 0) / 2**30, 2), "file_count": len(man.get("files") or []),
                "size_gb": round(rtm.model_size_gb(m), 1), "runtime_label": rt.get("label", m["runtime"]),
                "deployments": deps, "demand": ctx.gateway.demand(m["name"]),
                "ready": sum(1 for d in deps if d["state"] == "ready")}

    def validate_model(v: dict, partial: bool = False) -> None:
        if not partial or "name" in v:
            if not re.match(r"^[A-Za-z0-9._:/@+-]{1,80}$", v.get("name", "")):
                raise HTTPException(400, "Model adı: harf, rakam ve . _ : / @ + - karakterleri (en fazla 80)")
        if not partial and not v.get("repo"):
            raise HTTPException(400, "HuggingFace yolu / model adresi gerekli")
        if "runtime" in v and v["runtime"] not in c.runtimes:
            raise HTTPException(400, f"Bilinmeyen runtime: {v['runtime']}")
        if "kind" in v and v["kind"] not in ("llm", "stt", "tts", "embedding"):
            raise HTTPException(400, "tür: llm | stt | tts | embedding")
        if "source" in v and v["source"] not in ("hf", "runtime"):
            raise HTTPException(400, "kaynak: hf | runtime")
        for k, lo, hi in (("min_replicas", 0, 64), ("max_replicas", 0, 64), ("priority", 1, 10), ("max_concurrency", 1, 512),
                          ("gpu_count", 0, 16), ("ctx", 0, 2_000_000), ("idle_unload_min", 0, 100000), ("startup_timeout_s", 30, 86400)):
            if k in v and not lo <= v[k] <= hi:
                raise HTTPException(400, f"{k}: {lo}–{hi} aralığında olmalı")

    @r.get("/models")
    async def models_list():
        return [model_view(m) for m in sorted(c.models.values(), key=lambda x: x["name"])]

    @r.post("/models")
    async def models_create(request: Request):
        v = clean(await request.json(), MODEL_SPEC)
        v.setdefault("runtime", "llamacpp")
        v.setdefault("kind", "llm")
        validate_model(v)
        if db.one("SELECT id FROM models WHERE name=?", (v["name"],)):
            raise HTTPException(409, "Bu model adı zaten var")
        if v.get("min_replicas", 0) > v.get("max_replicas", 1):
            v["max_replicas"] = v["min_replicas"]
        cols = to_db(v)
        cols["created_at"] = now()
        mid = db.x(f"INSERT INTO models({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
        c.reload()
        db.event("info", "model", f"Model eklendi: {v['name']} ({v['repo']})")
        ctx.autoscaler.kick.set()
        if v.get("source") == "hf":
            ctx.fabric._start_manifest(c.models[mid])
        return model_view(c.models[mid])

    @r.patch("/models/{mid}")
    async def models_patch(mid: int, request: Request):
        m = model_or_404(mid)
        v = clean(await request.json(), MODEL_SPEC)
        validate_model(v, partial=True)
        if "name" in v and v["name"] != m["name"] and db.one("SELECT id FROM models WHERE name=?", (v["name"],)):
            raise HTTPException(409, "Bu model adı zaten var")
        if v:
            cols = to_db(v)
            if any(k in v and v[k] != m.get(k) for k in ("repo", "file_glob", "source")):
                cols["manifest"] = ""  # dosya listesi yeniden alınsın
            db.x(f"UPDATE models SET {','.join(k + '=?' for k in cols)} WHERE id=?", tuple(cols.values()) + (mid,))
            c.reload()
            ctx.autoscaler.kick.set()
        return model_view(c.models[mid])

    @r.delete("/models/{mid}")
    async def models_delete(mid: int):
        m = model_or_404(mid)
        for d in list(c.deps_of(mid)):
            await c.remove_deployment(d.id, "model silindi")
        db.x("DELETE FROM models WHERE id=?", (mid,))
        c.reload()
        ctx.gateway.reload_services()
        db.event("info", "model", f"Model silindi: {m['name']}")
        return {"ok": True}

    @r.get("/estimate")
    async def estimate(repo: str, extra: str = ""):
        return {"size_gb": rtm.estimate_vram_gb(repo, extra)}

    @r.post("/models/{mid}/deploy")
    async def model_deploy(mid: int, request: Request):
        m = model_or_404(mid)
        b = await request.json()
        nid = b.get("node_id")
        if nid:
            n = node_or_404(int(nid))
            rt = c.runtimes[m["runtime"]]
            fit = c._fit(n, m, rt, db.get("auto.mode")) if n.hw else None
            if fit is None and not b.get("force"):
                raise HTTPException(409, "Bu düğümde yeterli VRAM/RAM görünmüyor (ya da donanım bilgisi yok). 'Zorla' ile yine de deneyebilirsiniz.")
            gpus = fit[0] if fit else [g["index"] for g in (n.hw.get("gpus") or [])][: max(0, int(m["gpu_count"]))]
            if n.status == "sleeping":
                await c.wake(n, f"{m['name']} dağıtımı")
            d = c.create_deployment(mid, n.id, "manual", True, gpus)
        else:
            p = c.plan_placement(m, allow_wake=True, allow_evict=True)
            if p is None:
                raise HTTPException(409, "Uygun düğüm / yeterli kapasite bulunamadı")
            d = await c.deploy(m, p, "manual", True)
        return c.dep_view(d)

    @r.get("/deployments")
    async def deployments_list():
        return [c.dep_view(d) for d in c.deps.values()]

    @r.post("/deployments/{did}/action")
    async def deployment_action(did: int, request: Request):
        d = c.deps.get(did)
        if not d:
            raise HTTPException(404, "Dağıtım bulunamadı")
        a = (await request.json()).get("action")
        if a == "stop":
            c.set_desired(did, "stopped")
        elif a == "start":
            c.set_desired(did, "running")
        elif a == "restart":
            await c.restart_deployment(did)
        elif a == "remove":
            await c.remove_deployment(did, "elle")
        elif a in ("pin", "unpin"):
            d.pinned = a == "pin"
            db.x("UPDATE deployments SET pinned=? WHERE id=?", (1 if d.pinned else 0, did))
        else:
            raise HTTPException(400, "Bilinmeyen işlem")
        return {"ok": True}

    # ----------------------------------------------------------------------- runtime'lar
    def rt_view(x: dict) -> dict:
        return {**x, "args": jload(x["args"], []), "env": jload(x["env"], {}), "builtin": bool(x["builtin"]),
                "needs_gpu": bool(x["needs_gpu"]), "paths": jload(x.get("paths"), {}) or {}}

    @r.get("/runtimes")
    async def runtimes_list():
        return [rt_view(x) for x in c.runtimes.values()]

    def rt_vals(b: dict) -> dict:
        args = b.get("args")
        if isinstance(args, str):
            import shlex
            args = shlex.split(args)
        env = b.get("env")
        if isinstance(env, str):
            env = dict(line.split("=", 1) for line in env.splitlines() if "=" in line)
        out = {}
        for k in ("label", "exe", "health", "hook", "notes"):
            if k in b:
                out[k] = str(b[k]).strip()
        if args is not None:
            out["args"] = jdump([str(a) for a in args])
        if env is not None:
            out["env"] = jdump({str(k).strip(): str(v) for k, v in env.items()})
        if "needs_gpu" in b:
            out["needs_gpu"] = 1 if conv(b["needs_gpu"], bool) else 0
        if isinstance(b.get("paths"), dict):
            out["paths"] = jdump({str(k): str(x).strip() for k, x in b["paths"].items() if str(x).strip()})
        return out

    @r.put("/runtimes/{key}")
    async def runtime_update(key: str, request: Request):
        if key not in c.runtimes:
            raise HTTPException(404, "Runtime bulunamadı")
        v = rt_vals(await request.json())
        if v:
            db.x(f"UPDATE runtimes SET {','.join(k + '=?' for k in v)} WHERE key=?", tuple(v.values()) + (key,))
            c.reload()
        return rt_view(c.runtimes[key])

    @r.post("/runtimes")
    async def runtime_create(request: Request):
        b = await request.json()
        key = str(b.get("key", "")).strip().lower()
        if not re.match(r"^[a-z0-9_-]{2,30}$", key):
            raise HTTPException(400, "Anahtar: küçük harf, rakam, - ve _ (2–30)")
        if key in c.runtimes:
            raise HTTPException(409, "Bu anahtar zaten var")
        v = rt_vals(b)
        if not v.get("exe"):
            raise HTTPException(400, "Çalıştırılabilir dosya gerekli")
        db.x("INSERT INTO runtimes(key,label,exe,args,env,health,needs_gpu,hook,notes,paths,builtin) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
             (key, v.get("label", key), v["exe"], v.get("args", "[]"), v.get("env", "{}"), v.get("health", "/health"),
              v.get("needs_gpu", 0), v.get("hook", ""), v.get("notes", ""), v.get("paths", "{}")))
        c.reload()
        return rt_view(c.runtimes[key])

    @r.delete("/runtimes/{key}")
    async def runtime_delete(key: str):
        x = c.runtimes.get(key)
        if not x:
            raise HTTPException(404, "Runtime bulunamadı")
        if x["builtin"]:
            raise HTTPException(400, "Yerleşik runtime silinemez (düzenleyebilirsiniz)")
        if db.one("SELECT id FROM models WHERE runtime=?", (key,)):
            raise HTTPException(409, "Bu runtime'ı kullanan modeller var")
        db.x("DELETE FROM runtimes WHERE key=?", (key,))
        c.reload()
        return {"ok": True}

    # ======================================================================= kullanıcılar ve sohbetler
    @r.get("/users")
    async def users_list(q: str = "", limit: int = 100, offset: int = 0):
        return chatlog.list_users(db, q, min(500, limit), offset)

    @r.get("/conversations")
    async def conv_list(user: str = "", model: str = "", q: str = "", sort: str = "recent", limit: int = 50, offset: int = 0):
        return chatlog.list_conversations(db, user, model, q, sort, min(200, limit), offset)

    @r.get("/conversations/{cid}")
    async def conv_get(cid: str):
        x = chatlog.get_conversation(db, cid)
        if not x:
            raise HTTPException(404, "Konuşma bulunamadı")
        return x

    @r.delete("/conversations/{cid}")
    async def conv_delete(cid: str):
        db.x("DELETE FROM chat_turns WHERE conv_id=?", (cid,))
        db.x("DELETE FROM conversations WHERE id=?", (cid,))
        return {"ok": True}

    @r.get("/chats/stats")
    async def chats_stats():
        t = now()
        return {
            "users": db.one("SELECT COUNT(*) n FROM users")["n"],
            "conversations": db.one("SELECT COUNT(*) n FROM conversations")["n"],
            "turns": db.one("SELECT COUNT(*) n FROM chat_turns")["n"],
            "today_turns": db.one("SELECT COUNT(*) n FROM chat_turns WHERE ts>=?", (t - 86400,))["n"],
            "by_model": db.q("SELECT model, COUNT(*) n FROM chat_turns GROUP BY model ORDER BY n DESC LIMIT 10"),
            "pending_export": db.one("SELECT COUNT(*) n FROM conversations WHERE turns>exported_turns")["n"],
        }

    @r.get("/export/chats.jsonl")
    async def export_chats(anonymize: int = 1):
        recs, _ = chatlog.export_pending(db, 0, bool(anonymize), db.get("hf.salt") or "", bool(db.get("hf.scrub_pii")),
                                         limit=200000, only_pending=False)
        import json

        def gen():
            for x in recs:
                yield json.dumps(x, ensure_ascii=False) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": "attachment; filename=orkestra-chats.jsonl"})

    # ======================================================================= otomasyon
    @r.get("/autoscaler")
    async def autoscaler_status():
        return ctx.autoscaler.status()

    @r.post("/autoscaler/tick")
    async def autoscaler_tick():
        await ctx.autoscaler.tick()
        return ctx.autoscaler.status()

    # ======================================================================= distillation
    @r.get("/distill/status")
    async def distill_status():
        return {**ctx.distill.status(), "sets": ctx.distill.list_sets(), "jobs": ctx.distill.list_jobs()}

    @r.post("/distill/sets")
    async def distill_set_add(request: Request):
        b = await request.json()
        try:
            sid = ctx.distill.add_set(str(b.get("name", "")).strip(), str(b.get("text", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": sid}

    @r.delete("/distill/sets/{sid}")
    async def distill_set_del(sid: int):
        ctx.distill.delete_set(sid)
        return {"ok": True}

    @r.post("/distill/jobs")
    async def distill_job_add(request: Request):
        b = await request.json()
        cfg = {"sets": [int(x) for x in b.get("sets", [])], "use_daily": bool(b.get("use_daily")),
               "daily_hours": float(b.get("daily_hours", 24)), "limit": int(b.get("limit") or 0),
               "teacher": str(b.get("teacher", "")), "forced": bool(b.get("forced", True))}
        for k, f in (("temperature", float), ("max_tokens", int), ("concurrency", int)):
            if b.get(k) not in (None, ""):
                cfg[k] = f(b[k])
        if b.get("system"):
            cfg["system"] = str(b["system"])
        try:
            jid = ctx.distill.create_job(str(b.get("name", "")).strip() or "Distillation işi", cfg)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": jid}

    @r.post("/distill/jobs/{jid}/action")
    async def distill_job_action(jid: int, request: Request):
        try:
            ctx.distill.job_action(jid, str((await request.json()).get("action", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @r.get("/distill/jobs/{jid}/items")
    async def distill_items(jid: int, status: str = "", limit: int = 50, offset: int = 0):
        return ctx.distill.job_items(jid, status, min(500, limit), offset)

    # ======================================================================= HuggingFace
    @r.get("/hf/status")
    async def hf_status():
        return ctx.hf.status()

    @r.post("/hf/sync")
    async def hf_sync():
        return await ctx.hf.run("elle")

    @r.post("/hf/test")
    async def hf_test():
        return await ctx.hf.test()

    # ----------------------------------------------------------------------- HuggingFace'te model ara / depo ayrıntısı
    hf_cache: dict = {}
    PIPE = {"llm": "text-generation", "stt": "automatic-speech-recognition", "tts": "text-to-speech", "embedding": "feature-extraction"}

    async def hf_get(path: str, params: dict | None = None, ttl: float = 90.0):
        key = (path, jdump(params or {}))
        hit = hf_cache.get(key)
        if hit and now() - hit[0] < ttl:
            return hit[1]
        try:
            resp = await ctx.fabric.http.get(ctx.fabric.hf_base + path, params=params, headers=ctx.fabric._hf_headers(), timeout=20.0)
        except Exception as e:
            raise HTTPException(502, f"HuggingFace'e ulaşılamadı: {type(e).__name__}: {str(e)[:120]}")
        if resp.status_code == 404:
            raise HTTPException(404, "Depo bulunamadı")
        if resp.status_code in (401, 403):
            raise HTTPException(403, "Bu depo kısıtlı ya da özel: HuggingFace sayfasından erişim isteyin ve token girin")
        if resp.status_code >= 400:
            raise HTTPException(502, f"HuggingFace HTTP {resp.status_code}")
        data = resp.json()
        if len(hf_cache) > 200:
            hf_cache.clear()
        hf_cache[key] = (now(), data)
        return data

    @r.get("/hf/search")
    async def hf_search(q: str = "", kind: str = "llm", gguf: int = 0, limit: int = 20):
        params = {"search": q.strip(), "sort": "downloads", "direction": "-1", "limit": str(min(40, max(5, limit)))}
        if kind in PIPE:
            params["pipeline_tag"] = PIPE[kind]
        if gguf:
            params["filter"] = "gguf"
        data = await hf_get("/api/models", params)
        return [{"id": x.get("id") or x.get("modelId"), "downloads": x.get("downloads", 0), "likes": x.get("likes", 0),
                 "pipeline": x.get("pipeline_tag"), "gguf": "gguf" in (x.get("tags") or []), "updated": x.get("lastModified"),
                 "gated": bool(x.get("gated"))} for x in data if x.get("id") or x.get("modelId")]

    @r.get("/hf/repo")
    async def hf_repo(id: str):
        if not re.match(r"^[\w.-]+/[\w.-]+$", id):
            raise HTTPException(400, "Geçersiz depo adı (kullanici/depo biçiminde olmalı)")
        d = await hf_get(f"/api/models/{id}", {"blobs": "true"})
        files = [{"path": s["rfilename"], "size": s.get("size") or (s.get("lfs") or {}).get("size") or 0} for s in d.get("siblings", []) if s.get("rfilename")]
        weights = sum(f["size"] for f in files if f["path"].endswith(".safetensors")) / 1024 ** 3
        st = d.get("safetensors") or {}
        return {"id": d.get("id"), "sha": d.get("sha"), "pipeline": d.get("pipeline_tag"), "tags": (d.get("tags") or [])[:25], "downloads": d.get("downloads", 0),
                "likes": d.get("likes", 0), "gated": bool(d.get("gated")), "license": (d.get("cardData") or {}).get("license"),
                "params_b": round(st.get("total", 0) / 1e9, 2) or None, "weights_gb": round(weights, 2), "file_count": len(files),
                "variants": repo_variants(files)}

    # ======================================================================= ağ üzerinden dağıtım
    f = ctx.fabric

    @r.get("/fabric")
    async def fabric_view():
        return f.view()

    @r.put("/fabric/upload")
    async def fabric_upload(request: Request, name: str):
        """Ham dosya yükleme (derleme paketi): panelden sürükle bırak, ilerleme tarayıcıda izlenir."""
        name = Path(name.replace("\\", "/")).name[:160] or "paket"
        tmp = f.store.tmp / f"up-{rand_token(6)}.bin"
        fh = await asyncio.to_thread(open, tmp, "wb")
        try:
            async for chunk in request.stream():
                await asyncio.to_thread(fh.write, chunk)
        finally:
            await asyncio.to_thread(fh.close)
        sha, size = await asyncio.to_thread(f.store.add_file, tmp, name, "runtime")
        return {"sha256": sha, "size": size, "name": name}

    @r.post("/fabric/builds")
    async def fabric_build_add(request: Request):
        b = await request.json()
        rt = str(b.get("runtime", ""))
        if rt not in c.runtimes:
            raise HTTPException(400, "Bilinmeyen runtime")
        fmt = "src" if b.get("format") == "src" else "bin"
        sha, size, filename = str(b.get("sha256") or ""), 0, str(b.get("filename") or "")
        try:
            if b.get("git_url"):
                fmt = "src"
                sha, size, filename = await f.add_git_source(str(b["git_url"]).strip(), str(b.get("git_ref") or "").strip())
            elif b.get("path"):
                p = Path(str(b["path"]).strip())
                if p.is_dir():
                    sha, size, filename = await asyncio.to_thread(f.pack_dir, p, p.name)
                elif p.is_file():
                    sha, size = await asyncio.to_thread(f.store.add_file, p, p.name, "runtime", False)
                    filename = p.name
                else:
                    raise ValueError(f"Bu bilgisayarda bulunamadı: {p}")
            elif sha:
                row = db.one("SELECT size,name FROM blobs WHERE sha256=?", (sha,))
                if not row:
                    raise ValueError("Yüklenen dosya bulunamadı")
                size, filename = row["size"], filename or row["name"]
            else:
                raise ValueError("Dosya, klasör yolu ya da git adresi gerekli")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return f.add_build(rt, sha, size, filename, version=str(b.get("version") or "").strip(), platform=str(b.get("platform") or "linux-x86_64"),
                           backend=str(b.get("backend") or "cuda"), fmt=fmt, entry=str(b.get("entry") or "").strip(), cmake=str(b.get("cmake") or "").strip(),
                           notes=str(b.get("notes") or "").strip())

    @r.post("/fabric/builds/{bid}/action")
    async def fabric_build_action(bid: str, request: Request):
        row = db.one("SELECT * FROM runtime_builds WHERE id=?", (bid,))
        if not row:
            raise HTTPException(404, "Derleme bulunamadı")
        a = (await request.json()).get("action")
        if a == "activate":
            db.x("UPDATE runtime_builds SET active=0 WHERE runtime=? AND platform=? AND backend=? AND format=?", (row["runtime"], row["platform"], row["backend"], row["format"]))
            db.x("UPDATE runtime_builds SET active=1 WHERE id=?", (bid,))
        elif a == "deactivate":
            db.x("UPDATE runtime_builds SET active=0 WHERE id=?", (bid,))
        elif a == "delete":
            db.x("DELETE FROM runtime_builds WHERE id=?", (bid,))
            if not db.one("SELECT 1 x FROM runtime_builds WHERE sha256=?", (row["sha256"],)):
                f.store.remove(row["sha256"])
        else:
            raise HTTPException(400, "Bilinmeyen işlem")
        c.kick.set()
        return {"ok": True}

    @r.post("/fabric/rollout")
    async def fabric_rollout(request: Request):
        """Çalışan replikaları sırayla yeni derlemeye geçirir; her adımda diğerlerinin hazır kalmasını bekler."""
        key = str((await request.json()).get("runtime", ""))
        rt = c.runtimes.get(key)
        if not rt:
            raise HTTPException(404, "Runtime bulunamadı")
        todo = []
        for d in c.deps.values():
            m = c.models.get(d.model_id)
            n = c.nodes.get(d.node_id)
            if not m or not n or m["runtime"] != key or d.desired != "running" or d.state != "ready":
                continue
            b = f.pick_build(n, rt, not d.gpus)
            if b and d.build != b["id"]:
                todo.append(d.id)

        async def run():
            for did in todo:
                d = c.deps.get(did)
                if not d:
                    continue
                await c.restart_deployment(did)
                t0 = now()
                while now() - t0 < 600 and c.deps.get(did) is d and d.state != "ready":
                    await asyncio.sleep(1)
        f.spawn(run())
        db.event("info", "fabric", f"{rt['label']}: {len(todo)} replika sırayla yeni derlemeye geçiriliyor")
        return {"count": len(todo)}

    @r.get("/fabric/nodes/{nid}/log")
    async def fabric_log(nid: int, job: str):
        try:
            return {"text": await c.client(node_or_404(nid)).fabric_log(job, 400)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, str(e)[:200])

    @r.post("/fabric/models/{mid}/download")
    async def fabric_download(mid: int):
        m = model_or_404(mid)
        if m.get("source") != "hf":
            raise HTTPException(400, "Bu model ağdan dağıtılmıyor")
        t = f.start_download(m)
        return {k: v for k, v in t.items() if k != "task"}

    @r.post("/fabric/models/{mid}/refresh")
    async def fabric_refresh(mid: int):
        m = model_or_404(mid)
        try:
            man = await f.refresh_manifest(m)
        except Exception as e:
            raise HTTPException(502, str(e)[:240])
        return {"files": len(man["files"]), "total": man["total"], "main": man.get("main", "")}

    @r.post("/fabric/models/{mid}/push")
    async def fabric_push(mid: int, request: Request):
        m = model_or_404(mid)
        b = await request.json()
        ids = [int(x) for x in b.get("node_ids") or []] or [n.id for n in c.nodes.values() if n.status == "online"]
        try:
            return {"queued": f.push(m, ids)}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @r.post("/fabric/nodes/{nid}/clean")
    async def fabric_clean(nid: int, request: Request):
        n = node_or_404(nid)
        b = await request.json()
        try:
            res = await c.client(n).fabric_remove({"models": b.get("models") or [], "runtimes": b.get("runtimes") or [], "blobs": b.get("blobs") or []})
            f.ns[n.id] = await c.client(n).fabric_state()
            return res
        except Exception as e:
            raise HTTPException(502, str(e)[:200])

    @r.post("/fabric/nodes/{nid}/bwtest")
    async def fabric_bw(nid: int):
        try:
            return await f.bwtest(node_or_404(nid))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Ölçülemedi: {str(e)[:160]}")

    @r.post("/fabric/nodes/{nid}/update-agent")
    async def fabric_agent(nid: int):
        await f.update_agent(node_or_404(nid))
        return {"ok": True}

    # ======================================================================= yerel ağ ve kurulum
    @r.get("/net")
    async def net_view():
        out = ctx.network.status()
        out["bw"] = f.bw
        out["uplink"] = {k: v for k, v in ctx.uplink.status().items() if k != "hist"}
        out["wan"] = {"hf_down": f.wan_down, "tunnel_up": ctx.uplink.up_bytes, "tunnel_down": ctx.uplink.down_bytes}
        return out

    @r.post("/net/scan")
    async def net_scan(request: Request):
        b = await request.json()
        try:
            return await ctx.network.scan(str(b.get("cidr") or ""), int(b.get("ssh_port") or 22), int(b.get("agent_port") or 7777))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @r.post("/provision")
    async def provision_start(request: Request):
        try:
            return ctx.provision.start(await request.json())
        except ValueError as e:
            raise HTTPException(400, str(e))

    @r.get("/provision")
    async def provision_list():
        return ctx.provision.list()

    @r.get("/provision/{jid}")
    async def provision_get(jid: str):
        j = ctx.provision.jobs.get(jid)
        if not j:
            raise HTTPException(404, "Kurulum işi bulunamadı")
        return ctx.provision.view(j, 800)

    @r.post("/nodes/self")
    async def node_self(request: Request):
        b = await request.json()
        try:
            nid = ctx.provision.add_self(int(b.get("port") or 7777))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": nid}

    # ======================================================================= site bağlantısı ve servis
    def uplink_view() -> dict:
        s = ctx.uplink.status()
        return {**s, "config": {"mode": db.get("uplink.mode"), "relay_url": db.get("uplink.relay_url"), "public_url": db.get("uplink.public_url"),
                                "cf_path": db.get("uplink.cf_path"), "has_secret": bool(db.get("uplink.secret")), "has_cf_token": bool(db.get("uplink.cf_token"))},
                "base_url": ctx.uplink.public_base()}

    def ensure_secret() -> str:
        s = db.get("uplink.secret") or ""
        if not s:
            s = rand_token(32)
            db.set("uplink.secret", s)
        return s

    @r.get("/uplink")
    async def uplink_get():
        return uplink_view()

    @r.put("/uplink")
    async def uplink_put(request: Request):
        b = await request.json()
        mode = str(b.get("mode") or "off")
        if mode not in ("off", "relay", "cloudflare", "direct"):
            raise HTTPException(400, "Bilinmeyen bağlantı türü")
        vals = {"uplink.mode": mode, "uplink.relay_url": str(b.get("relay_url") or "").strip(), "uplink.public_url": str(b.get("public_url") or "").strip(),
                "uplink.cf_path": str(b.get("cf_path") or "cloudflared").strip()}
        if b.get("cf_token"):
            vals["uplink.cf_token"] = str(b["cf_token"]).strip()
        for k in ("uplink.relay_url", "uplink.public_url"):
            if vals[k] and not re.match(r"^https?://", vals[k]):
                raise HTTPException(400, "Adres http:// ya da https:// ile başlamalı")
        db.set_many(vals)
        if mode == "relay":
            ensure_secret()
        await ctx.uplink.apply()
        return uplink_view()

    @r.post("/uplink/secret")
    async def uplink_rotate():
        db.set("uplink.secret", rand_token(32))
        db.event("warn", "uplink", "Tünel anahtarı değiştirildi; röle dosyasını yeniden yükleyin")
        await ctx.uplink.apply()
        return {"ok": True}

    @r.get("/uplink/relay/{kind}")
    async def uplink_relay(kind: str):
        """Tünel anahtarı içine yazılmış röle dosyası. Yalnızca yöneticiye verilir."""
        if kind not in ("php", "node", "python"):
            raise HTTPException(404, "Bilinmeyen röle türü")
        name, data = relay_source(kind, ensure_secret())
        if kind == "node":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("relay.mjs", data)
                z.writestr("package.json", json.dumps({"name": "llm-relay", "version": "1.0.0", "private": True, "type": "module", "main": "relay.mjs",
                                                       "scripts": {"start": "node relay.mjs"}, "engines": {"node": ">=18"}}, indent=2))
            name, data = "llm-relay-node.zip", buf.getvalue()
        return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @r.post("/uplink/test")
    async def uplink_test():
        """Siteden gelecek isteğin aynısını dış adres üzerinden yapar: röle → tünel → ağ geçidi → model."""
        base = ctx.uplink.public_base()
        if not base:
            raise HTTPException(400, "Dış adres yok: önce bağlantıyı kurun")
        if ctx.sim:
            return {"ok": True, "base": base, "steps": [{"name": "Dış adres", "ok": True, "ms": 41, "note": base}, {"name": "Tünel ve ağ geçidi", "ok": True, "ms": 88, "note": "demo"},
                                                        {"name": "Model yanıtı (ilk token)", "ok": True, "ms": 240, "note": "demo"}]}
        kid, key = auth.create_key("__baglanti_sinama__", 0, [], False)
        steps, h = [], {"Authorization": f"Bearer {key}", "X-User-Id": "baglanti-sinama"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=120.0), follow_redirects=True) as cl:
                t0 = now()
                resp = await cl.get(base + "/v1/models", headers=h)
                steps.append({"name": "Dış adres → tünel → ağ geçidi", "ok": resp.status_code == 200, "ms": int((now() - t0) * 1000), "note": f"HTTP {resp.status_code}"})
                svc = next((s for s in ctx.gateway.services.values() if s["kind"] == "llm" and s["enabled"] and s["is_default"]), None) \
                    or next((s for s in ctx.gateway.services.values() if s["kind"] == "llm" and s["enabled"]), None)
                name = svc["name"] if svc else next((m["name"] for m in c.models.values() if m["kind"] == "llm" and m["enabled"] and ctx.gateway._ready_count(m)), "")
                if name and resp.status_code == 200:
                    t0 = now()
                    first = None
                    async with cl.stream("POST", base + "/v1/chat/completions", headers=h, json={"model": name, "stream": True, "max_tokens": 8, "messages": [{"role": "user", "content": "Merhaba"}]}) as s:
                        ok = s.status_code == 200
                        async for line in s.aiter_lines():
                            if line.startswith("data:") and first is None:
                                first = int((now() - t0) * 1000)
                    steps.append({"name": f"Model yanıtı ({name})", "ok": ok and first is not None, "ms": first or int((now() - t0) * 1000), "note": "ilk token" if first else f"HTTP {s.status_code}"})
                else:
                    steps.append({"name": "Model yanıtı", "ok": False, "ms": 0, "note": "Hazır sohbet modeli yok"})
        except Exception as e:
            steps.append({"name": "Bağlantı", "ok": False, "ms": 0, "note": f"{type(e).__name__}: {str(e)[:160]}"})
        finally:
            db.x("DELETE FROM api_keys WHERE id=?", (kid,))
        return {"ok": bool(steps) and all(x["ok"] for x in steps), "base": base, "steps": steps}

    def svc_row(name: str):
        return db.one("SELECT * FROM services WHERE name=?", (name,))

    @r.get("/services")
    async def services_list():
        return [ctx.gateway.service_view(s) for s in sorted(ctx.gateway.services.values(), key=lambda x: (x["kind"], x["name"]))]

    @r.put("/services/{name}")
    async def service_put(name: str, request: Request):
        if not re.match(r"^[A-Za-z0-9._-]{1,60}$", name):
            raise HTTPException(400, "Servis adı: harf, rakam, . _ - (en fazla 60)")
        b = await request.json()
        kind = str(b.get("kind") or "llm")
        if kind not in ("llm", "stt", "tts", "embedding"):
            raise HTTPException(400, "Geçersiz tür")
        targets = [str(x) for x in b.get("targets") or []]
        for t in targets:
            m = ctx.gateway.by_name(t)
            if not m:
                raise HTTPException(400, f"Model bulunamadı: {t}")
            if m["kind"] != kind:
                raise HTTPException(400, f"{t} bu servis türüne uygun değil")
        if not targets:
            raise HTTPException(400, "En az bir model seçin")
        default = bool(b.get("is_default"))
        with db.tx():
            if default:
                db.x("UPDATE services SET is_default=0 WHERE kind=?", (kind,))
            db.x("INSERT INTO services(name,kind,targets,enabled,is_default,options,notes,created_at) VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,targets=excluded.targets,enabled=excluded.enabled,is_default=excluded.is_default,options=excluded.options,notes=excluded.notes",
                 (name, kind, jdump(targets), 1 if b.get("enabled", True) else 0, 1 if default else 0, jdump(b.get("options") or {}), str(b.get("notes") or ""), now()))
        ctx.gateway.reload_services()
        return ctx.gateway.service_view(ctx.gateway.services[name])

    @r.delete("/services/{name}")
    async def service_del(name: str):
        if not svc_row(name):
            raise HTTPException(404, "Servis bulunamadı")
        db.x("DELETE FROM services WHERE name=?", (name,))
        ctx.gateway.reload_services()
        return {"ok": True}

    @r.get("/usage")
    async def usage(days: int = 30):
        since = time.strftime("%Y-%m-%d", time.localtime(now() - max(1, min(366, days)) * 86400))
        sel = "COALESCE(SUM(requests),0) requests, COALESCE(SUM(in_tok),0) in_tok, COALESCE(SUM(out_tok),0) out_tok, COALESCE(SUM(audio_s),0) audio_s, COALESCE(SUM(chars),0) chars"
        return {"days": db.q(f"SELECT day, {sel}, COUNT(DISTINCT user_id) users FROM usage_daily WHERE day>=? GROUP BY day ORDER BY day", (since,)),
                "plans": db.q(f"SELECT plan, {sel}, COUNT(DISTINCT user_id) users FROM usage_daily WHERE day>=? GROUP BY plan ORDER BY out_tok DESC", (since,)),
                "services": db.q(f"SELECT service, kind, {sel} FROM usage_daily WHERE day>=? GROUP BY service, kind ORDER BY requests DESC", (since,)),
                "users": db.q(f"SELECT user_id, MAX(plan) plan, {sel} FROM usage_daily WHERE day>=? GROUP BY user_id ORDER BY (SUM(in_tok)+SUM(out_tok)) DESC LIMIT 25", (since,))}

    # ======================================================================= ayarlar, anahtarlar, olaylar
    @r.get("/settings")
    async def settings_get():
        return db.all_settings(mask=True)

    @r.put("/settings")
    async def settings_put(request: Request):
        b = await request.json()
        upd = {}
        for k, v in b.items():
            if k not in SETTINGS_DEFAULTS or k == "admin.password_hash":
                continue
            if k in SECRET_KEYS and (v is None or v is True or v is False or v == ""):
                continue  # boş gelen gizli alan = değişiklik yok
            upd[k] = v
        db.set_many(upd)
        ctx.autoscaler.kick.set()
        return db.all_settings(mask=True)

    @r.post("/settings/clear")
    async def settings_clear(request: Request):
        k = (await request.json()).get("key")
        if k not in SECRET_KEYS or k == "admin.password_hash":
            raise HTTPException(400, "Bu anahtar temizlenemez")
        db.set(k, "")
        return {"ok": True}

    @r.post("/settings/password")
    async def settings_password(request: Request):
        b = await request.json()
        from .auth import verify_password
        if not verify_password(str(b.get("current", "")), db.get("admin.password_hash") or ""):
            raise HTTPException(403, "Mevcut parola hatalı")
        try:
            auth.set_password(str(b.get("new", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "message": "Parola değişti; tüm oturumlar kapatıldı"}

    @r.get("/keys")
    async def keys_list():
        rows = db.q("SELECT id,name,prefix,rpm,models,log_chats,created_at,last_used_at,revoked FROM api_keys ORDER BY id DESC")
        for x in rows:
            x["models"] = jload(x["models"], [])
        return rows

    @r.post("/keys")
    async def keys_create(request: Request):
        b = await request.json()
        name = str(b.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "Anahtar için bir ad girin")
        kid, plain = auth.create_key(name, int(b.get("rpm") if b.get("rpm") is not None else 600), [str(x) for x in b.get("models", [])],
                                     bool(b.get("log_chats", True)))
        return {"id": kid, "key": plain, "sign_secret": auth.sign_secret(kid)}

    @r.delete("/keys/{kid}")
    async def keys_revoke(kid: int):
        db.x("UPDATE api_keys SET revoked=1 WHERE id=?", (kid,))
        return {"ok": True}

    @r.get("/events")
    async def events(limit: int = 100, level: str = "", source: str = "", before: int = 0):
        where, args = [], []
        if level:
            where.append("level=?")
            args.append(level)
        if source:
            where.append("source=?")
            args.append(source)
        if before:
            where.append("id<?")
            args.append(before)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        return db.q(sql, tuple(args) + (min(500, limit),))

    @r.get("/system")
    async def system(request: Request):
        size = Path(db.path).stat().st_size if Path(db.path).exists() else 0
        return {
            "version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "uptime_s": int(now() - ctx.started), "data_dir": str(ctx.data_dir), "db_mb": round(size / 1048576, 2),
            "controller_url": controller_url(request), "admin_port": boot.admin_port, "lan_ip": lan_ip(),
            "public_port": boot.public_port if boot.public else None, "fabric_port": boot.fabric_port, "demo": bool(ctx.sim),
            "hf_hub": _has("huggingface_hub"),
        }

    # ======================================================================= simülatör
    if ctx.sim:
        @r.get("/sim/state")
        async def sim_state():
            return ctx.sim.state()

        @r.post("/sim/inject")
        async def sim_inject(request: Request):
            b = await request.json()
            try:
                ctx.sim.inject(int(b["node_id"]), str(b["kind"]))
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e))
            return {"ok": True}

        @r.post("/sim/traffic")
        async def sim_traffic(request: Request):
            b = await request.json()
            ctx.sim.traffic.enabled = bool(b.get("enabled"))
            return ctx.sim.state()["traffic"]

        @r.post("/sim/speed")
        async def sim_speed(request: Request):
            b = await request.json()
            ctx.sim.time_scale = max(0.2, min(10.0, float(b.get("time_scale", 1.0))))
            return {"time_scale": ctx.sim.time_scale}

    root = APIRouter()
    root.include_router(open_r)
    root.include_router(r)
    return root


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None
