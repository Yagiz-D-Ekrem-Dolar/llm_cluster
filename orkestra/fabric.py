"""Ağ üzerinden dağıtım merkezi: kod, runtime ve model dosyaları switch üzerinden makinelere gider.

* Store     : ana PC'deki içerik adresli depo (sha256 -> dosya)
* Fabric    : HuggingFace'ten tek sefer indirme (modemden), LAN'a dağıtım, runtime kurulumu/derleme, disk temizliği
* Blob sunucu: makinelerin dosya çektiği ayrı iş parçacıklı HTTP sunucusu (ağ geçidi akışlarını yavaşlatmaz)

Model dosyası modemden yalnızca bir kez iner. Sonra makineler birbirinden de çekebildiği için
kopya sayısı arttıkça dağıtım hızlanır; ana PC'nin tek gigabit bağlantısı darboğaz olmaz.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import importlib.util
import ntpath
import os
import posixpath
import re
import shutil
import socket
import tarfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .util import jdump, jload, log, now, rand_token

ROOT = Path(__file__).resolve().parent.parent
AGENT_FILE = ROOT / "agent" / "node_agent.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("orkestra_node_agent", AGENT_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AG = _load_agent()
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SPLIT = re.compile(r"-\d{5}-of-\d{5}")
QUANT = re.compile(r"(IQ\d_[A-Z0-9_]+|Q\d_[A-Z0-9_]+|Q\d|BF16|F16|F32)", re.I)
NOISE = (".gitattributes", "*.md", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.pdf", "*.ipynb", "*.mp3", "*.wav")
ALT_WEIGHTS = ("*.bin", "*.pt", "*.pth", "*.h5", "*.msgpack", "*.ot", "*.tflite", "*.onnx", "*.onnx_data", "onnx/*", "openvino/*", "*.gguf")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(s).lower()).strip("-.")[:60] or "x"


def _match(path: str, patterns) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p) for p in patterns)


def gguf_groups(files: list[dict]) -> dict:
    out: dict = {}
    for f in files:
        n = f["path"]
        if not n.lower().endswith(".gguf") or "mmproj" in n.lower():
            continue
        base = SPLIT.sub("", n)
        m = QUANT.search(base.rsplit("/", 1)[-1])
        e = out.setdefault(base, {"base": base, "tag": m.group(1).upper() if m else base.rsplit("/", 1)[-1][:-5], "files": [], "size": 0})
        e["files"].append(f)
        e["size"] += int(f.get("size") or 0)
    return out


def select_files(files: list[dict], tag: str = "", glob: str = "") -> tuple[list[dict], str]:
    """Depodaki dosyalardan bu model için gerekenleri ve ana dosyayı seçer."""
    if glob.strip():
        pats = [p for p in re.split(r"[,\s]+", glob.strip()) if p]
        chosen = [f for f in files if _match(f["path"], pats)]
    else:
        groups = gguf_groups(files)
        chosen = []
        if groups:
            g = None
            if tag:
                g = next((x for x in groups.values() if x["tag"].upper() == tag.upper()), None) \
                    or next((x for x in groups.values() if tag.upper() in x["base"].upper()), None)
                if g is None:
                    return [], ""
            else:
                g = next((x for x in groups.values() if x["tag"] == "Q4_K_M"), None) or min(groups.values(), key=lambda x: x["size"])
            chosen = list(g["files"])
        else:
            chosen = [f for f in files if not _match(f["path"], NOISE)]
            if any(f["path"].endswith(".safetensors") for f in chosen):
                chosen = [f for f in chosen if not _match(f["path"], ALT_WEIGHTS)]
    chosen.sort(key=lambda f: f["path"])
    main = ""
    weights = [f for f in chosen if f["path"].lower().endswith((".gguf", ".onnx")) or re.search(r"ggml.*\.bin$", f["path"], re.I)]
    if weights and not any(f["path"].endswith(".safetensors") for f in chosen):
        main = weights[0]["path"]
    return chosen, main


def repo_variants(files: list[dict]) -> list[dict]:
    """Panelde seçtirilecek seçenekler: GGUF nicemlemeleri, whisper.cpp dosyaları, Piper sesleri ya da tüm depo."""
    out = []
    for g in sorted(gguf_groups(files).values(), key=lambda x: x["size"]):
        out.append({"id": "gguf:" + g["tag"], "type": "gguf", "label": g["tag"], "tag": g["tag"], "glob": "", "size": g["size"], "parts": len(g["files"])})
    names = {f["path"]: f for f in files}
    for f in files:
        n = f["path"]
        if re.search(r"(^|/)ggml-[^/]+\.bin$", n):
            out.append({"id": n, "type": "ggml", "label": n.rsplit("/", 1)[-1], "tag": "", "glob": n, "size": f.get("size") or 0, "parts": 1})
        elif n.endswith(".onnx") and n + ".json" in names:
            out.append({"id": n, "type": "voice", "label": n, "tag": "", "glob": f"{n},{n}.json", "size": (f.get("size") or 0) + (names[n + ".json"].get("size") or 0), "parts": 2})
    if not out:
        chosen, _ = select_files(files)
        out.append({"id": "repo", "type": "repo", "label": "Tüm model dosyaları", "tag": "", "glob": "", "size": sum(int(f.get("size") or 0) for f in chosen), "parts": len(chosen)})
    return out


def _lan_ip_for(host: str) -> str:
    """Verilen makineye giden yolda bu bilgisayarın kullandığı adres."""
    if host in ("127.0.0.1", "localhost", "::1") or host.startswith("127."):
        return "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def W(step: str, pct: float | None = None) -> dict:
    return {"state": "working", "step": step, "pct": None if pct is None else round(pct, 1)}


def F(error: str) -> dict:
    return {"state": "failed", "error": error}


# --------------------------------------------------------------------------- depo
class Store:
    def __init__(self, root: Path, db, fake: bool = False):
        self.root, self.db, self.fake = Path(root), db, fake
        self.blobs, self.tmp = self.root / "blobs", self.root / "tmp"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str) -> Path:
        if not HEX64.match(sha or ""):
            raise ValueError("geçersiz sha256")
        return self.blobs / sha

    def has(self, sha: str | None) -> bool:
        if not sha or not self.db.one("SELECT 1 x FROM blobs WHERE sha256=?", (sha,)):
            return False
        return self.fake or self.path(sha).is_file()

    def register(self, sha: str, size: int, name: str, kind: str) -> None:
        self.db.x("INSERT INTO blobs(sha256,size,name,kind,created_at) VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET name=excluded.name",
                  (sha, int(size), name[:200], kind, now()))

    def add_file(self, src: Path, name: str, kind: str, move: bool = True) -> tuple[str, int]:
        h, size = hashlib.sha256(), 0
        with open(src, "rb") as f:
            while True:
                b = f.read(1 << 22)
                if not b:
                    break
                h.update(b)
                size += len(b)
        sha = h.hexdigest()
        dst = self.path(sha)
        if not dst.exists():
            if move:
                os.replace(src, dst)
            else:
                shutil.copyfile(src, dst)
        elif move:
            os.remove(src)
        self.register(sha, size, name, kind)
        return sha, size

    def remove(self, sha: str) -> None:
        self.db.x("DELETE FROM blobs WHERE sha256=?", (sha,))
        if not self.fake:
            try:
                self.path(sha).unlink()
            except OSError:
                pass

    def usage(self) -> dict:
        r = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM blobs")
        try:
            du = shutil.disk_usage(self.root)
            free, total = du.free, du.total
        except OSError:
            free, total = 0, 0
        return {"files": r["n"], "bytes": r["s"], "free_bytes": free, "total_bytes": total, "path": str(self.root)}


# --------------------------------------------------------------------------- aktarım kaydı
class Xfer:
    __slots__ = ("nid", "sha", "size", "name", "prio", "state", "src", "bytes", "bps", "tries", "next_try", "error", "t0", "job")

    def __init__(self, nid: int, sha: str, size: int, name: str, prio: int):
        self.nid, self.sha, self.size, self.name, self.prio = nid, sha, int(size), name, prio
        self.state, self.src, self.bytes, self.bps, self.tries = "queued", None, 0, 0, 0
        self.next_try, self.error, self.t0, self.job = 0.0, "", now(), "fetch:" + sha[:16]


def _blob_handler(fab: "Fabric"):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "blob"

        def log_message(self, *a):
            pass

        def _err(self, code: int, msg: str) -> None:
            b = msg.encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlsplit(self.path)
            p = [x for x in u.path.split("/") if x]
            if p == ["healthz"]:
                return self._err(200, "ok")
            if len(p) != 2 or p[0] != "blob":
                return self._err(404, "yok")
            name = p[1]
            if not AG.ticket_ok(fab.secret, name, AG.bearer(self.headers)):
                return self._err(401, "geçersiz bilet")
            try:
                if name == "zero":
                    return AG.serve_zero(self, max(1, min(8192, int(parse_qs(u.query).get("mb", ["256"])[0]))))
                fp = fab.store.path(name)
                if not fp.is_file():
                    return self._err(404, "dosya yok")
                fab.serving += 1
                try:
                    AG.serve_file(self, str(fp))
                finally:
                    fab.serving -= 1
            except (ConnectionError, socket.timeout, BrokenPipeError):
                self.close_connection = True

        do_HEAD = do_GET

    return H


# --------------------------------------------------------------------------- merkez
class Fabric:
    def __init__(self, ctx):
        self.ctx, self.db = ctx, ctx.db
        self.store = Store(Path(ctx.data_dir) / "store", ctx.db, fake=bool(ctx.sim))
        self.secret = self.db.get("fabric.secret") or ""
        if not self.secret:
            self.secret = rand_token(32)
            self.db.set("fabric.secret", self.secret)
        self.port = ctx.boot.fabric_port
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True,
                                      limits=httpx.Limits(max_connections=16))
        self.ns: dict[int, dict] = {}        # makine -> ajan deposu durumu
        self.xfers: dict[tuple, Xfer] = {}
        self.ops: dict[tuple, dict] = {}     # (makine, anahtar) -> kurulum/derleme işi
        self.dl: dict[int, dict] = {}        # model -> HuggingFace indirme görevi
        self.man_tasks: dict[int, asyncio.Task] = {}
        self.man_err: dict[int, tuple] = {}
        self._refreshing: set[int] = set()
        self._agent_tried: dict[int, float] = {}
        self.bw: dict[int, dict] = {}        # son ağ hızı ölçümleri
        self.serving = 0
        self.wan_down = 0                    # modemden inen bayt
        self.lan_bytes = 0
        self._srv = None
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        code = AGENT_FILE.read_bytes()
        self.agent_sha, self.agent_b64 = hashlib.sha256(code).hexdigest(), base64.b64encode(code).decode()

    @property
    def c(self):
        return self.ctx.cluster

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    # ---- yaşam döngüsü ------------------------------------------------------
    def register_bundled(self) -> None:
        """Uygulamayla gelen köprüler (Piper sunucusu) derleme olarak kayıtlı olsun."""
        src = ROOT / "agent" / "piper_server.py"
        if self.ctx.sim or not src.is_file():
            return
        sha, size = self.store.add_file(src, "piper_server.py", "runtime", move=False)
        if not self.db.one("SELECT id FROM runtime_builds WHERE runtime='piper' AND sha256=?", (sha,)):
            self.db.x("UPDATE runtime_builds SET active=0 WHERE runtime='piper'")
            self.db.x("INSERT OR REPLACE INTO runtime_builds(id,runtime,version,platform,backend,format,sha256,size,filename,entry,cmake,active,notes,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)", (f"piper-{sha[:12]}", "piper", "yerleşik", "any", "any", "bin", sha, size, "piper_server.py", "", "", "OpenAI uyumlu Piper köprüsü", now()))

    def start(self) -> None:
        if self.port and not self.ctx.sim:
            try:
                self._srv = AG.Server((self.ctx.boot.admin_host, self.port), _blob_handler(self))
                threading.Thread(target=self._srv.serve_forever, daemon=True, name="blob-server").start()
            except OSError as e:
                self.db.event("error", "fabric", f"Dosya dağıtım portu {self.port} açılamadı: {e}")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._bg) + list(self.man_tasks.values()):
            t.cancel()
        for t in self.dl.values():
            if t.get("task"):
                t["task"].cancel()
        await asyncio.gather(*([self._task] if self._task else []), *self._bg, return_exceptions=True)
        if self._srv:
            threading.Thread(target=self._srv.shutdown, daemon=True).start()
            self._srv.server_close()
        await self.http.aclose()

    # ---- HuggingFace --------------------------------------------------------
    @property
    def hf_base(self) -> str:
        return (os.environ.get("HF_ENDPOINT") or self.db.get("fabric.hf_endpoint") or "https://huggingface.co").rstrip("/")

    def _hf_headers(self) -> dict:
        h = {"User-Agent": "kume-yoneticisi"}
        tok = self.db.get("hf.token")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    async def hf_tree(self, repo: str, rev: str) -> list[dict]:
        url = f"{self.hf_base}/api/models/{repo}/tree/{quote(rev)}?recursive=true&expand=false"
        out: list[dict] = []
        while url:
            r = await self.http.get(url, headers=self._hf_headers())
            if r.status_code in (401, 403):
                raise ValueError("Depo kısıtlı ya da özel: HuggingFace'te erişim izni alıp token girin")
            if r.status_code == 404:
                raise ValueError(f"Depo bulunamadı: {repo}")
            r.raise_for_status()
            for x in r.json():
                if x.get("type") == "file":
                    lfs = x.get("lfs") or {}
                    out.append({"path": x["path"], "size": int(lfs.get("size") or x.get("size") or 0), "sha256": lfs.get("oid") or None, "oid": x.get("oid")})
            url = (r.links.get("next") or {}).get("url")
        return out

    async def hf_revision(self, repo: str) -> str:
        r = await self.http.get(f"{self.hf_base}/api/models/{repo}", headers=self._hf_headers())
        if r.status_code in (401, 403):
            raise ValueError("Depo kısıtlı ya da özel: HuggingFace'te erişim izni alıp token girin")
        if r.status_code == 404:
            raise ValueError(f"Depo bulunamadı: {repo}")
        r.raise_for_status()
        return r.json().get("sha") or "main"

    def model_row(self, mid: int) -> dict | None:
        return self.c.models.get(mid)

    def manifest(self, m: dict) -> dict | None:
        return jload(m.get("manifest"), None) or None

    def _save_manifest(self, m: dict, man: dict) -> None:
        self.db.x("UPDATE models SET manifest=? WHERE id=?", (jdump(man), m["id"]))
        m["manifest"] = jdump(man)
        cur = self.c.models.get(m["id"])
        if cur is not None:
            cur["manifest"] = m["manifest"]

    async def refresh_manifest(self, m: dict) -> dict:
        repo, _, tag = str(m["repo"]).partition(":")
        if self.ctx.sim:
            man = self.ctx.sim.manifest_for(m)
        else:
            rev = await self.hf_revision(repo)
            files = await self.hf_tree(repo, rev)
            chosen, main = select_files(files, tag, m.get("file_glob") or "")
            if not chosen:
                raise ValueError(f"'{m['repo']}' için eşleşen dosya bulunamadı" + (f" (desen: {m.get('file_glob')})" if m.get("file_glob") else ""))
            man = {"repo": repo, "revision": rev, "files": chosen, "main": main}
        man["total"] = sum(int(f.get("size") or 0) for f in man["files"])
        man["key"] = self._model_key(m, man)
        self._save_manifest(m, man)
        return man

    def _model_key(self, m: dict, man: dict) -> str:
        sig = hashlib.sha256(jdump([[f["path"], f.get("sha256") or f.get("oid")] for f in man["files"]]).encode()).hexdigest()[:10]
        return f"{slug(m['name'])[:48]}-{sig}"

    def _start_manifest(self, m: dict) -> None:
        mid = m["id"]
        t = self.man_tasks.get(mid)
        if t and not t.done():
            return
        async def run():
            try:
                await self.refresh_manifest(m)
                self.man_err.pop(mid, None)
                self.c.kick.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.man_err[mid] = (now(), f"{e}"[:300])
        self.man_tasks[mid] = self.spawn(run())

    # ---- modeli ana PC'ye indirme ---------------------------------------------
    def start_download(self, m: dict) -> dict:
        t = self.dl.get(m["id"])
        if t and t["state"] == "running":
            return t
        man = self.manifest(m)
        total = int((man or {}).get("total") or 0)
        t = {"model": m["name"], "model_id": m["id"], "state": "running", "bytes": 0, "total": total, "bps": 0, "file": "", "error": "", "t0": now()}
        self.dl[m["id"]] = t
        t["task"] = self.spawn(self._download(m, t))
        return t

    async def _download(self, m: dict, t: dict) -> None:
        try:
            man = self.manifest(m) or await self.refresh_manifest(m)
            t["total"] = man["total"]
            done = 0
            for f in man["files"]:
                if f.get("sha256") and self.store.has(f["sha256"]):
                    done += int(f["size"])
                    t["bytes"] = done
                    continue
                t["file"] = f["path"]
                if self.ctx.sim:
                    sha = await self.ctx.sim.fake_download(f, t, done)
                else:
                    sha = await self._download_file(man, f, t, done)
                if not f.get("sha256"):
                    f["sha256"] = sha
                    self._save_manifest(m, man)
                done += int(f["size"])
                t["bytes"] = done
            man["key"] = self._model_key(m, man)
            self._save_manifest(m, man)
            t["state"], t["bps"] = "done", 0
            self.db.event("info", "fabric", f"{m['name']} ana PC'ye indirildi ({man['total'] / 2**30:.1f} GB)")
            self.c.kick.set()
            if self.db.get("fabric.preseed"):
                self.preseed(self.c.models.get(m["id"]) or m)
        except asyncio.CancelledError:
            t["state"] = "cancelled"
            raise
        except Exception as e:
            t["state"], t["error"], t["bps"] = "failed", f"{e}"[:300], 0
            self.db.event("error", "fabric", f"{m['name']} indirilemedi: {e}")

    async def _download_file(self, man: dict, f: dict, t: dict, base: int) -> str:
        size = int(f["size"])
        key = f.get("sha256") or hashlib.sha1(f["path"].encode()).hexdigest()
        part = self.store.tmp / f"hf-{key}.part"
        h = hashlib.sha256()
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have:
            await asyncio.to_thread(self._hash_prefix, part, h)
        url = f"{self.hf_base}/{man['repo']}/resolve/{man['revision']}/{quote(f['path'])}"
        headers = self._hf_headers()
        if have:
            headers["Range"] = f"bytes={have}-"
        for attempt in range(6):
            try:
                async with self.http.stream("GET", url, headers=headers) as r:
                    if r.status_code in (401, 403):
                        raise ValueError("Bu depo için HuggingFace izni/token gerekiyor")
                    if r.status_code == 416:
                        break
                    if r.status_code == 200 and have:
                        part.unlink(missing_ok=True)
                        have = 0
                        h = hashlib.sha256()
                    elif r.status_code not in (200, 206):
                        raise IOError(f"HTTP {r.status_code}")
                    fh = await asyncio.to_thread(open, part, "ab")
                    try:
                        t0, b0, tick, btick = now(), have, now(), have
                        async for chunk in r.aiter_bytes(1 << 20):
                            await asyncio.to_thread(self._write, fh, h, chunk)
                            have += len(chunk)
                            self.wan_down += len(chunk)
                            t["bytes"] = base + have
                            tn = now()
                            if tn - tick >= 1:
                                t["bps"] = int((have - btick) / (tn - tick))
                                tick, btick = tn, have
                            lim = float(self.db.get("fabric.hf_limit_mbps") or 0) * 125000
                            if lim > 0:
                                wait = (have - b0) / lim - (tn - t0)
                                if wait > 0:
                                    await asyncio.sleep(min(wait, 2.0))
                    finally:
                        await asyncio.to_thread(fh.close)
                break
            except (httpx.TransportError, IOError) as e:
                if isinstance(e, ValueError) or attempt == 5:
                    raise
                log.warning("HF indirme yeniden denenecek (%s): %s", f["path"], e)
                await asyncio.sleep(min(30, 2 ** attempt))
                have = part.stat().st_size if part.exists() else 0
                h = hashlib.sha256()
                if have:
                    await asyncio.to_thread(self._hash_prefix, part, h)
                    headers["Range"] = f"bytes={have}-"
                else:
                    headers.pop("Range", None)
        if have != size:
            raise IOError(f"{f['path']}: eksik indirme ({have}/{size})")
        sha = h.hexdigest()
        if f.get("sha256") and sha != f["sha256"]:
            part.unlink(missing_ok=True)
            raise IOError(f"{f['path']}: sha256 uyuşmuyor, dosya silindi")
        dst = self.store.path(sha)
        os.replace(part, dst)
        self.store.register(sha, size, f"{man['repo']}/{f['path']}", "model")
        return sha

    @staticmethod
    def _write(fh, h, chunk) -> None:
        fh.write(chunk)
        h.update(chunk)

    @staticmethod
    def _hash_prefix(path: Path, h) -> None:
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 22)
                if not b:
                    return
                h.update(b)

    # ---- runtime derlemeleri --------------------------------------------------
    def builds_of(self, key: str) -> list[dict]:
        return self.db.q("SELECT * FROM runtime_builds WHERE runtime=? AND active=1 ORDER BY created_at DESC", (key,))

    def add_build(self, runtime: str, sha: str, size: int, filename: str, *, version: str = "", platform: str = "linux-x86_64",
                  backend: str = "cuda", fmt: str = "bin", entry: str = "", cmake: str = "", notes: str = "") -> dict:
        if fmt == "src":
            platform, backend = "any", "any"
        bid = f"{slug(runtime)[:18]}-{sha[:12]}"
        self.db.x("UPDATE runtime_builds SET active=0 WHERE runtime=? AND platform=? AND backend=? AND format=?", (runtime, platform, backend, fmt))
        self.db.x("INSERT OR REPLACE INTO runtime_builds(id,runtime,version,platform,backend,format,sha256,size,filename,entry,cmake,active,notes,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                  (bid, runtime, version, platform, backend, fmt, sha, int(size), filename, entry, cmake, notes, now()))
        self.db.event("info", "fabric", f"{runtime} için yeni derleme eklendi: {version or sha[:12]} ({platform}/{backend}/{fmt})")
        self.c.kick.set()
        return self.db.one("SELECT * FROM runtime_builds WHERE id=?", (bid,))

    def pack_dir(self, src: Path, name: str) -> tuple[str, int, str]:
        """Yerel klasörü (kaynak ağacı ya da derleme çıktısı) paketler."""
        tmp = self.store.tmp / f"pack-{rand_token(6)}.tar.gz"
        skip = {".git", "build", "_b", "__pycache__", "node_modules", ".cache"}
        with tarfile.open(tmp, "w:gz", compresslevel=3) as t:
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d not in skip]
                for f in files:
                    p = Path(root) / f
                    t.add(p, arcname=str(p.relative_to(src)).replace(os.sep, "/"))
        sha, size = self.store.add_file(tmp, name, "runtime")
        return sha, size, f"{name}.tar.gz"

    async def add_git_source(self, url: str, ref: str) -> tuple[str, int, str]:
        tmp = self.store.tmp / f"git-{rand_token(6)}"
        args = ["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules"] + (["--branch", ref] if ref else []) + [url, str(tmp)]
        try:
            p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                     env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            out, _ = await asyncio.wait_for(p.communicate(), timeout=1800)
            if p.returncode != 0:
                raise ValueError("git clone başarısız: " + out.decode("utf-8", "replace")[-300:])
            name = slug(url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")) + ("-" + slug(ref) if ref else "")
            return await asyncio.to_thread(self.pack_dir, tmp, name)
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, True)

    def _hw(self, n) -> dict:
        return n.hw or {}

    def pick_build(self, n, rt: dict, cpu: bool) -> dict | None:
        rows = self.builds_of(rt["key"])
        if not rows:
            return None
        hw = self._hw(n)
        plat = hw.get("platform") or "linux-x86_64"
        caps = (hw.get("gpu_caps") or {}).get("backends") or ["cpu"]
        order = (["cpu"] + [b for b in caps if b != "cpu"]) if cpu else list(caps)
        # önce hazır derleme, sonra kaynak (bu makinede derlenir)
        for be in order:
            for r in rows:
                if r["format"] == "bin" and r["platform"] in (plat, "any") and r["backend"] == be:
                    return r
        for r in rows:
            if r["format"] == "bin" and r["platform"] in (plat, "any") and r["backend"] == "any":
                return r
        for r in rows:
            if r["format"] == "src":
                return r
        return None

    def build_backend(self, n, b: dict, cpu: bool) -> str:
        caps = (self._hw(n).get("gpu_caps") or {}).get("backends") or ["cpu"]
        return "cpu" if cpu else caps[0]

    def instance_id(self, n, b: dict, cpu: bool) -> str:
        return b["id"] if b["format"] == "bin" else f"{b['id']}-{self.build_backend(n, b, cpu)}"

    def runtime_ok(self, n, rt: dict) -> bool:
        return bool(self.pick_build(n, rt, False) or self.pick_build(n, rt, True))

    # ---- dağıtım kuyruğu ------------------------------------------------------
    def available(self, sha: str | None) -> bool:
        if not sha:
            return False
        return self.store.has(sha) or any(sha in (st.get("blobs") or {}) for nid, st in self.ns.items() if self._online(nid))

    def _online(self, nid: int) -> bool:
        n = self.c.nodes.get(nid)
        return bool(n and n.status == "online")

    def want(self, n, sha: str, size: int, name: str, prio: int = 0) -> Xfer:
        key = (n.id, sha)
        x = self.xfers.get(key)
        if x is None or x.state in ("done", "cancelled"):
            x = Xfer(n.id, sha, size, name, prio)
            self.xfers[key] = x
        elif prio < x.prio:
            x.prio = prio
        if x.state == "failed" and now() >= x.next_try:
            x.state = "queued"
        return x

    def _progress(self, n, files) -> tuple[float, int]:
        total = sum(int(f["size"]) for f in files) or 1
        have = 0
        for f in files:
            if f["sha256"] in (self.ns.get(n.id, {}).get("blobs") or {}):
                have += int(f["size"])
            else:
                x = self.xfers.get((n.id, f["sha256"]))
                have += x.bytes if x else 0
        return min(100.0, have * 100.0 / total), total - have

    def _source_url(self, kind, sha: str, node) -> dict:
        if kind == 0:
            host = _lan_ip_for(node.host)
            return {"url": f"http://{host}:{self.port}/blob/{sha}", "ticket": AG.make_ticket(self.secret, sha, 6 * 3600)}
        p = self.c.nodes[kind]
        return {"url": f"http://{p.host}:{p.agent_port}/v1/fabric/blob/{sha}", "ticket": AG.make_ticket(p.agent_token, sha, 6 * 3600)}

    def _pick_sources(self, x: Xfer, up: dict) -> list:
        n = self.c.nodes[x.nid]
        slots = max(1, int(self.db.get("fabric.source_slots")))
        peers = [nid for nid, st in self.ns.items() if nid != x.nid and self._online(nid) and x.sha in (st.get("blobs") or {})
                 and self.c.nodes[nid].sim == n.sim]
        peers.sort(key=lambda p: up.get(p, 0))
        order, has_store = [], self.store.has(x.sha)
        free_peers = [p for p in peers if up.get(p, 0) < 1]
        if free_peers:
            order = free_peers + ([0] if has_store else [])
        elif has_store and up.get(0, 0) < slots:
            order = [0] + peers
        return order

    async def _tick(self) -> None:
        act = [x for x in self.xfers.values() if x.state == "active"]
        # etkin aktarımların ilerlemesi
        for nid in {x.nid for x in act}:
            n = self.c.nodes.get(nid)
            if not n or n.status != "online":
                for x in act:
                    if x.nid == nid:
                        x.state, x.src = "queued", None
                continue
            try:
                jobs = {j["id"]: j for j in (await self.c.client(n).fabric_jobs())["jobs"]}
            except Exception:
                continue
            changed = False
            for x in [y for y in act if y.nid == nid]:
                j = jobs.get(x.job)
                if not j:
                    x.state, x.src = "queued", None
                    continue
                self.lan_bytes += max(0, int(j.get("bytes", 0)) - x.bytes)
                x.bytes, x.bps = int(j.get("bytes", 0)), int(j.get("bps", 0))
                if j["state"] == "done":
                    x.state, x.bps, x.bytes = "done", 0, x.size
                    x.src = None
                    self.ns.setdefault(nid, {}).setdefault("blobs", {})[x.sha] = x.size
                    changed = True
                elif j["state"] in ("failed", "cancelled"):
                    x.tries += 1
                    x.error, x.src, x.bps = j.get("error", ""), None, 0
                    if j.get("code") == "no_space":
                        await self._make_room(n, x.size)
                    x.state = "failed" if x.tries >= 4 else "queued"
                    x.next_try = now() + min(120, 5 * 2 ** x.tries)
                    if x.state == "failed":
                        self.db.event("error", "fabric", f"{x.name} → {n.name} aktarılamadı: {x.error}", nid)
            if changed:
                self._refresh_soon(n)
                self.c.kick.set()
        # kuyruktakileri başlat
        up: dict = {}
        per_node: dict = {}
        for x in self.xfers.values():
            if x.state == "active":
                per_node[x.nid] = per_node.get(x.nid, 0) + 1
                if x.src is not None:
                    up[x.src] = up.get(x.src, 0) + 1
        t = now()
        queued = sorted((x for x in self.xfers.values() if x.state == "queued" and x.next_try <= t), key=lambda x: (x.prio, x.t0))
        for x in queued:
            n = self.c.nodes.get(x.nid)
            if not n or n.status != "online" or per_node.get(x.nid, 0) >= 2:
                continue
            if x.sha in (self.ns.get(x.nid, {}).get("blobs") or {}):
                x.state, x.bytes = "done", x.size
                continue
            order = self._pick_sources(x, up)
            if not order:
                continue
            src = order[0]
            spec = {"sha256": x.sha, "size": x.size, "sources": [self._source_url(s, x.sha, n) for s in order],
                    "limit_bps": float(self.db.get("fabric.lan_limit_mbps") or 0) * 125000, "reserve_gb": float(self.db.get("fabric.reserve_gb"))}
            try:
                j = await self.c.client(n).fabric_fetch(spec)
            except Exception as e:
                x.tries += 1
                x.error, x.next_try = str(e)[:200], now() + 5 * x.tries
                continue
            x.state, x.src, x.job = "active", src, j.get("id", x.job)
            x.bytes = int(j.get("bytes", 0))
            per_node[x.nid] = per_node.get(x.nid, 0) + 1
            up[src] = up.get(src, 0) + 1
        # eski kayıtlar
        for k, x in list(self.xfers.items()):
            if x.state == "done" and t - x.t0 > 900:
                self.xfers.pop(k, None)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fabric")

    # ---- makine durumu --------------------------------------------------------
    def on_metrics(self, n, m: dict) -> None:
        fm = m.get("fabric")
        if not fm:
            return
        st = self.ns.get(n.id)
        if st is None or st.get("rev") != fm.get("rev"):
            self._refresh_soon(n)
        elif st is not None:
            st["free_gb"] = fm.get("free_gb")

    def on_info(self, n, info: dict) -> None:
        sha = info.get("code_sha")
        if n.sim or not sha or sha == self.agent_sha or not self.db.get("fabric.auto_update_agent"):
            return
        if now() - self._agent_tried.get(n.id, 0) < 300:
            return
        self._agent_tried[n.id] = now()
        self.spawn(self.update_agent(n))

    async def update_agent(self, n) -> None:
        try:
            await self.c.client(n).fabric_update({"code": self.agent_b64, "sha256": self.agent_sha})
            n.last_info = 0.0
            self.db.event("info", "fabric", f"{n.name}: ajan güncellendi", n.id)
        except Exception as e:
            self.db.event("warn", "fabric", f"{n.name}: ajan güncellenemedi: {e}", n.id)

    def _refresh_soon(self, n) -> None:
        if n.id in self._refreshing:
            return
        self._refreshing.add(n.id)
        async def run():
            try:
                self.ns[n.id] = await self.c.client(n).fabric_state()
                self.c.kick.set()
            except Exception as e:
                log.debug("fabric state %s: %s", n.name, e)
            finally:
                self._refreshing.discard(n.id)
        self.spawn(run())

    def has_model(self, n, m: dict) -> bool:
        man = self.manifest(m)
        if not man:
            return False
        st = self.ns.get(n.id) or {}
        return man.get("key") in (st.get("models") or {}) or all(f.get("sha256") in (st.get("blobs") or {}) for f in man["files"])

    # ---- kurulum işleri ------------------------------------------------------
    def _op(self, n, key: str, label: str, starter, poll_id: str | None):
        """Ajandaki bir işi (paket açma, derleme, bağlantı) başlatır; bitince durum tazelenir."""
        k = (n.id, key)
        op = self.ops.get(k)
        if op is None:
            op = self.ops[k] = {"state": "running", "error": "", "label": label, "t0": now(), "step": ""}
            self.spawn(self._run_op(n, k, op, starter, poll_id))
        if op["state"] == "failed":
            self.ops.pop(k, None)
            return F(f"{label}: {op['error']}")
        if op["state"] == "done":
            self.ops.pop(k, None)
            return None
        return W(op.get("step") or label)

    async def _run_op(self, n, k, op: dict, starter, poll_id: str | None) -> None:
        cl = self.c.client(n)
        try:
            await starter(cl)
            while poll_id:
                await asyncio.sleep(1.0)
                jobs = {j["id"]: j for j in (await cl.fabric_jobs())["jobs"]}
                j = jobs.get(poll_id)
                if not j:
                    raise IOError("iş kayboldu")
                op["step"] = j.get("step") or op["step"]
                if j["state"] == "done":
                    break
                if j["state"] in ("failed", "cancelled"):
                    raise IOError(j.get("error") or j["state"])
            self.ns[n.id] = await cl.fabric_state()
            op["state"] = "done"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            op["state"], op["error"] = "failed", f"{e}"[:400]
        self.c.kick.set()

    async def _make_room(self, n, need: int, keep_keys=()) -> bool:
        """Kullanılmayan modelleri eskiden yeniye silerek yer açar."""
        st = self.ns.get(n.id) or {}
        keep_blobs, keep_models = set(), set(keep_keys)
        for d in self.c.deps_on(n.id):
            m = self.c.models.get(d.model_id)
            man = self.manifest(m) if m and d.desired == "running" else None
            if man:
                keep_models.add(man.get("key"))
                keep_blobs.update(f.get("sha256") for f in man["files"])
        for b in self.db.q("SELECT sha256 FROM runtime_builds WHERE active=1"):
            keep_blobs.add(b["sha256"])
        models = st.get("models") or {}
        blobs = st.get("blobs") or {}
        free = float(st.get("free_gb") or 0) * 2**30
        reserve = float(self.db.get("fabric.reserve_gb")) * 2**30
        rm_models, rm_blobs = [], []
        for key, info in sorted(models.items(), key=lambda kv: kv[1].get("at") or 0):
            if free >= need + reserve:
                break
            if key in keep_models:
                continue
            rm_models.append(key)
            others = {s for k, v in models.items() if k != key and k not in rm_models for s in v.get("files", [])}
            for s in info.get("files", []):
                if s not in others and s not in keep_blobs and s in blobs and s not in rm_blobs:
                    rm_blobs.append(s)
                    free += blobs[s]
        if not rm_models:
            return free >= need + reserve
        try:
            await self.c.client(n).fabric_remove({"models": rm_models, "blobs": rm_blobs})
            self.db.event("info", "fabric", f"{n.name}: yer açıldı ({len(rm_models)} model silindi)", n.id)
            self.ns[n.id] = await self.c.client(n).fabric_state()
        except Exception as e:
            log.warning("yer açılamadı %s: %s", n.name, e)
            return False
        return float(self.ns[n.id].get("free_gb") or 0) * 2**30 >= need + reserve

    # ---- bir dağıtım için gerekenler -------------------------------------------
    def ensure(self, n, d, m: dict, rt: dict) -> dict:
        """Dağıtımın ihtiyaçları makinede hazır mı? Hazır değilse eksikleri başlatır (bloklamaz)."""
        out = {"state": "ready", "exe": "", "cwd": "", "env": {}, "model_path": "", "model_dir": ""}
        builds = self.builds_of(rt["key"])
        managed = m.get("source") == "hf"
        if not builds and not managed:
            return out
        st = self.ns.get(n.id)
        if st is None:
            self._refresh_soon(n)
            return W("makine deposu okunuyor")
        path = ntpath if st.get("sep") == "\\" else posixpath
        cpu = not d.gpus
        if builds:
            b = self.pick_build(n, rt, cpu)
            hw = self._hw(n)
            if b is None:
                return F(f"{rt['label']}: bu makineye uygun derleme yok ({hw.get('platform', '?')}, {'/'.join((hw.get('gpu_caps') or {}).get('backends', ['cpu']))})")
            rid = self.instance_id(n, b, cpu)
            r = (st.get("runtimes") or {}).get(rid)
            if not r:
                if b["sha256"] not in (st.get("blobs") or {}):
                    if not self.available(b["sha256"]):
                        return F(f"derleme dosyası kayıp: {b['filename']}")
                    x = self.want(n, b["sha256"], b["size"], b["filename"] or rid, 0)
                    if x.state == "failed":
                        return F(f"runtime aktarılamadı: {x.error}")
                    return W("runtime ağdan aktarılıyor", x.bytes * 100.0 / max(1, x.size))
                if b["format"] == "src":
                    spec = {"id": rid, "sha256": b["sha256"], "backend": self.build_backend(n, b, cpu), "exe_name": posixpath.basename(rt["exe"]),
                            "entry": b["entry"], "cmake": (b["cmake"] or "").split()}
                    res = self._op(n, "rt:" + rid, "derleniyor", lambda cl: cl.fabric_build(spec), "build:" + rid)
                else:
                    spec = {"id": rid, "sha256": b["sha256"], "entry": b["entry"], "exe_name": posixpath.basename(rt["exe"]), "filename": b["filename"]}
                    res = self._op(n, "rt:" + rid, "runtime kuruluyor", lambda cl: cl.fabric_install(spec), "install:" + rid)
                if res is not None:
                    return res
                r = (self.ns.get(n.id, {}).get("runtimes") or {}).get(rid)
                if not r:
                    return W("runtime kuruluyor")
            out.update(exe=r["entry"], cwd=r["dir"], build=b["id"])
            libdirs = [path.dirname(r["entry"]), r["dir"], path.join(r["dir"], "lib")]
            out["env"] = {"LD_LIBRARY_PATH": ":".join(dict.fromkeys(libdirs))}
        if managed:
            man = self.manifest(m)
            if not man:
                err = self.man_err.get(m["id"])
                if err and now() - err[0] < 30:
                    return F(err[1])
                self._start_manifest(m)
                return W("HuggingFace depo bilgisi alınıyor")
            files = man["files"]
            if any(not self.available(f.get("sha256")) for f in files):
                t = self.dl.get(m["id"])
                if t and t["state"] == "failed" and now() - t.get("t0", 0) < 30:
                    return F(f"indirilemedi: {t['error']}")
                t = self.start_download(m)
                return W("HuggingFace'ten ana PC'ye iniyor", t["bytes"] * 100.0 / max(1, t["total"]))
            key = man.get("key") or self._model_key(m, man)
            blobs = st.get("blobs") or {}
            miss = [f for f in files if f["sha256"] not in blobs]
            if miss:
                pct, left = self._progress(n, files)
                free = float(st.get("free_gb") or 0) * 2**30
                reserve = float(self.db.get("fabric.reserve_gb")) * 2**30
                need = sum(int(f["size"]) for f in miss)
                active = any((self.xfers.get((n.id, f["sha256"])) or Xfer(0, "0" * 64, 0, "", 0)).state == "active" for f in miss)
                if not active and free < need + reserve:
                    self.spawn(self._make_room(n, need, (key,)))
                    if not (st.get("models") or {}):
                        return F(f"diskte yer yok: {need / 2**30:.1f} GB gerekli, {free / 2**30:.1f} GB boş")
                    return W("diskte yer açılıyor", pct)
                for f in miss:
                    x = self.want(n, f["sha256"], f["size"], f["path"], 0)
                    if x.state == "failed":
                        return F(f"aktarım başarısız: {x.error}")
                return W("model ağdan aktarılıyor", pct)
            if key not in (st.get("models") or {}):
                spec = {"key": key, "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files]}
                res = self._op(n, "m:" + key, "model hazırlanıyor", lambda cl: cl.fabric_link(spec), None)
                if res is not None:
                    return res
            info = (self.ns.get(n.id, {}).get("models") or {}).get(key)
            if not info:
                return W("model hazırlanıyor")
            mdir = info["dir"]
            out["model_dir"] = mdir
            out["model_path"] = path.join(mdir, *man["main"].split("/")) if man.get("main") else mdir
        return out

    def preseed(self, m: dict) -> int:
        """Modeli çalıştırabilecek makinelere önceden kopyalar: talep gelince beklenmez."""
        man = self.manifest(m)
        if not man:
            return 0
        rt = self.c.runtimes.get(m["runtime"])
        count = 0
        for n in self.c.nodes.values():
            st = self.ns.get(n.id)
            if not st or n.status != "online" or not n.enabled or not rt or not self.c.can_host(n, m, rt):
                continue
            miss = [f for f in man["files"] if f.get("sha256") and f["sha256"] not in (st.get("blobs") or {})]
            need = sum(int(f["size"]) for f in miss)
            if not miss or float(st.get("free_gb") or 0) * 2**30 < need + float(self.db.get("fabric.reserve_gb")) * 2**30:
                continue
            for f in miss:
                self.want(n, f["sha256"], f["size"], f["path"], 1)
            count += 1
        return count

    def push(self, m: dict, node_ids: list[int]) -> int:
        man = self.manifest(m)
        if not man:
            raise ValueError("Önce model dosya listesi alınmalı")
        cnt = 0
        for nid in node_ids:
            n = self.c.nodes.get(nid)
            if not n or n.status != "online":
                continue
            for f in man["files"]:
                if f.get("sha256") and f["sha256"] not in (self.ns.get(nid, {}).get("blobs") or {}):
                    self.want(n, f["sha256"], f["size"], f["path"], 0)
                    cnt += 1
        return cnt

    async def bwtest(self, n, mb: int = 256) -> dict:
        ticket = AG.make_ticket(self.secret, "zero", 120)
        res = await self.c.client(n).fabric_bwtest({"url": f"http://{_lan_ip_for(n.host)}:{self.port}/blob/zero?mb={int(mb)}", "ticket": ticket, "max_s": 10})
        res["ts"] = now()
        self.bw[n.id] = res
        return res

    # ---- panel görünümü --------------------------------------------------------
    def view(self) -> dict:
        nodes = []
        for n in sorted(self.c.nodes.values(), key=lambda x: x.name):
            st = self.ns.get(n.id) or {}
            hw = self._hw(n)
            nodes.append({"id": n.id, "name": n.name, "status": n.status, "free_gb": st.get("free_gb"), "total_gb": st.get("total_gb"),
                          "platform": hw.get("platform"), "backends": (hw.get("gpu_caps") or {}).get("backends"),
                          "agent_ok": (not hw.get("code_sha")) and n.sim or hw.get("code_sha") == self.agent_sha,
                          "agent": hw.get("agent"), "runtimes": sorted((st.get("runtimes") or {}).keys()),
                          "models": sorted((st.get("models") or {}).keys()), "tools": hw.get("tools") or {}})
        models = []
        for m in sorted(self.c.models.values(), key=lambda x: x["name"]):
            man = self.manifest(m)
            if m.get("source") != "hf":
                continue
            files = (man or {}).get("files") or []
            total = int((man or {}).get("total") or 0)
            in_store = sum(int(f["size"]) for f in files if self.store.has(f.get("sha256")))
            per = {}
            for n in self.c.nodes.values():
                st = self.ns.get(n.id) or {}
                b = st.get("blobs") or {}
                have = sum(int(f["size"]) for f in files if f.get("sha256") in b)
                xf = [self.xfers[(n.id, f["sha256"])] for f in files if f.get("sha256") and (n.id, f["sha256"]) in self.xfers]
                got = have + sum(x.bytes for x in xf if x.state == "active")
                per[n.id] = {"pct": round(min(100.0, got * 100.0 / total), 1) if total else 0, "complete": bool(files) and have >= total,
                             "active": any(x.state == "active" for x in xf), "bps": sum(x.bps for x in xf if x.state == "active"),
                             "queued": any(x.state == "queued" for x in xf), "error": next((x.error for x in xf if x.state == "failed"), "")}
            t = self.dl.get(m["id"])
            models.append({"id": m["id"], "name": m["name"], "kind": m["kind"], "repo": m["repo"], "total": total, "files": len(files),
                           "main": (man or {}).get("main", ""), "revision": ((man or {}).get("revision") or "")[:10], "in_store": in_store,
                           "error": (self.man_err.get(m["id"]) or (0, ""))[1],
                           "download": {k: v for k, v in t.items() if k != "task"} if t else None, "nodes": per})
        builds = self.db.q("SELECT * FROM runtime_builds ORDER BY created_at DESC")
        for b in builds:
            b["in_store"] = self.store.has(b["sha256"])
        xf = [{"node_id": x.nid, "node": (self.c.nodes.get(x.nid).name if self.c.nodes.get(x.nid) else "?"), "name": x.name, "size": x.size, "bytes": x.bytes, "bps": x.bps,
               "state": x.state, "error": x.error, "tries": x.tries,
               "src": "ana PC" if x.src == 0 else (self.c.nodes[x.src].name if x.src in self.c.nodes else "")} for x in self.xfers.values() if x.state != "done"]
        return {"store": self.store.usage(), "nodes": nodes, "models": models, "builds": builds, "transfers": xf, "agent_sha": self.agent_sha,
                "ops": [{"node": self.c.nodes[k[0]].name if k[0] in self.c.nodes else "?", "key": k[1], **v} for k, v in self.ops.items() if v["state"] == "running"],
                "wan_down": self.wan_down, "lan_bytes": self.lan_bytes, "bw": self.bw, "port": self.port}
