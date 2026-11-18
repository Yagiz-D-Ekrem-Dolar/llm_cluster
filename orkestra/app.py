"""Uygulama kurulumu: bağlam (Ctx), yönetim paneli, herkese açık ağ geçidi ve sunucular."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api, gateway
from . import runtimes
from .auth import Auth
from .autoscaler import Autoscaler
from .cluster import Cluster
from .db import DB
from .distill import Distiller
from .fabric import Fabric
from .gateway import Gateway
from .hfsync import HFSync
from .network import Network
from .provision import Provisioner
from .sim import SimWorld, build_router as build_sim_router
from .uplink import Uplink
from .util import log, now, rand_token

# Windows kayıt defteri bazen .js dosyalarını yanlış türle eşler; tarayıcı bunları çalıştırmaz.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STATIC = BASE / "static"


@dataclass
class Boot:
    data_dir: Path
    admin_host: str = "0.0.0.0"
    admin_port: int = 8080
    public: bool = True
    public_host: str = "0.0.0.0"
    public_port: int = 8081
    fabric_port: int = 8079
    demo: bool = False
    log_level: str = "info"


class Ctx:
    """Tüm bileşenlerin paylaştığı bağlam."""

    def __init__(self, boot: Boot):
        self.boot = boot
        self.data_dir = Path(boot.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.started = now()
        self.db = DB(self.data_dir / "orkestra.db")
        runtimes.seed(self.db)
        if not self.db.get("hf.salt"):
            self.db.set("hf.salt", rand_token(12))
        self.auth = Auth(self.db)
        self.sim = None
        if boot.demo:
            self.sim = SimWorld(self)
            self.sim.admin_port = boot.admin_port
            self.sim.seed(self.db)
        self.fabric = Fabric(self)
        self.cluster = Cluster(self)
        self.network = Network(self)
        self.uplink = Uplink(self)
        self.provision = Provisioner(self)
        self.fabric.register_bundled()
        self.gateway = Gateway(self)
        self.autoscaler = Autoscaler(self)
        self.hf = HFSync(self)
        self.distill = Distiller(self)

    async def start(self) -> None:
        if self.sim:
            await self.sim.start()
        self.fabric.start()
        self.cluster.start()
        self.network.start()
        self.uplink.start()
        self.provision.resume_self()
        self.autoscaler.start()
        self.hf.start()
        self.distill.start()

    async def stop(self) -> None:
        for part in (self.distill, self.hf, self.autoscaler, self.uplink, self.network, self.provision):
            await part.stop()
        if self.sim:
            await self.sim.stop()
        await self.fabric.stop()
        await self.cluster.stop()
        await self.gateway.close()
        self.db.close()


def _handlers(app: FastAPI) -> None:
    @app.exception_handler(ValueError)
    async def _ve(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def _ke(request: Request, exc: KeyError):
        return JSONResponse({"detail": f"Bulunamadı: {exc}"}, status_code=404)


def build_admin_app(ctx: Ctx) -> FastAPI:
    app = FastAPI(title="Orkestra", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    _handlers(app)

    @app.middleware("http")
    async def secure_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(("/v1/", "/api/", "/_sim/", "/agent/")):
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        return resp

    app.include_router(api.make_router(ctx))
    app.include_router(gateway.make_router(ctx, admin=True))
    if ctx.sim:
        app.include_router(build_sim_router(ctx.sim))

    @app.get("/agent/install.sh")
    async def agent_install():
        return FileResponse(ROOT / "agent" / "install_agent.sh", media_type="text/x-shellscript")

    @app.get("/agent/node_agent.py")
    async def agent_py():
        return FileResponse(ROOT / "agent" / "node_agent.py", media_type="text/x-python")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def build_public_app(ctx: Ctx) -> FastAPI:
    """İnternete açılacak yüzey: yalnızca OpenAI uyumlu uçlar (API anahtarı zorunlu)."""
    app = FastAPI(title="Orkestra Gateway", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    _handlers(app)
    app.include_router(gateway.make_router(ctx, admin=False))

    @app.middleware("http")
    async def cors(request: Request, call_next):
        """Tarayıcıdan doğrudan çağrı için yalnızca ayarlarda izin verilen site adresleri."""
        origin = request.headers.get("origin", "")
        allowed = ctx.db.get("gw.cors_origins") or []
        ok = bool(origin) and ("*" in allowed or origin in allowed)
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            resp = Response(status_code=204 if ok else 403)
        else:
            resp = await call_next(request)
        if ok:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Api-Key, X-User-Id, X-User-Plan, X-Conversation-Id"
            resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    @app.get("/healthz")
    async def healthz():
        ready = sum(1 for d in ctx.cluster.deps.values() if d.state == "ready")
        return {"status": "ok", "ready_replicas": ready}

    return app


class _Server(uvicorn.Server):
    """Tek süreçte iki sunucu çalıştırırken sinyal yakalayıcıların çakışmasını önler."""

    def install_signal_handlers(self) -> None:  # eski sürümler
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # yeni sürümler
        yield


async def serve(boot: Boot) -> None:
    logging.basicConfig(level=getattr(logging, boot.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "asyncssh"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ctx = Ctx(boot)
    pw = os.environ.get("ORKESTRA_ADMIN_PASSWORD", "")
    if pw:
        ctx.auth.set_password(pw)
    elif boot.demo:
        ctx.auth.set_password("demo1234")
        log.info("Demo modu: yönetici parolası 'demo1234' (yalnızca demo için)")
    servers = [_Server(uvicorn.Config(build_admin_app(ctx), host=boot.admin_host, port=boot.admin_port, log_level="warning",
                                     lifespan="off", access_log=False))]
    if boot.public:
        servers.append(_Server(uvicorn.Config(build_public_app(ctx), host=boot.public_host, port=boot.public_port,
                                              log_level="warning", lifespan="off", access_log=False)))
    await ctx.start()
    log.info("Hazır → panel: http://%s:%s%s", "127.0.0.1" if boot.admin_host in ("0.0.0.0", "::") else boot.admin_host, boot.admin_port,
             f"  |  ağ geçidi: {boot.public_host}:{boot.public_port}" if boot.public else "")
    if not ctx.auth.configured():
        log.info("İlk kurulum: panele girip yönetici parolasını belirleyin")
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        await ctx.stop()
