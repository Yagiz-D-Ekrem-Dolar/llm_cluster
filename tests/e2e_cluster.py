"""Gerçek akış sınaması: simülatör YOK.

Gerçek ajan süreçleri ayrı makineler gibi farklı IP'lerde (127.0.1.x) çalışır, dosyalar gerçek TCP ile aktarılır:

  HuggingFace taklidi → ana PC deposu → (LAN) → makineler → süreç başlatma → ağ geçidi → tünel → PHP/Node/Python rölesi → site istemcisi

Kullanım:  python tests/e2e_cluster.py
"""
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
CTRL, PUB, FAB, HF_PORT, AG_PORT, RELAY_BASE, SSH_PORT = 8791, 8792, 8793, 8794, 7791, 8800, 2299
PW = secrets.token_urlsafe(12)
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else "/bin/bash"
FAILS, procs = [], []
tmp = Path(tempfile.mkdtemp(prefix="kume-e2e-"))


def step(s):
    print(f"\n=== {s}", flush=True)


def ok(c, msg):
    print(("  OK   " if c else "  FAIL ") + msg, flush=True)
    if not c:
        FAILS.append(msg)
    return c


def wait_for(fn, timeout=60, every=0.3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    return None


def spawn(args, env=None, log=None, cwd=None):
    f = open(log, "ab") if log else subprocess.DEVNULL
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})}, cwd=cwd)
    procs.append(p)
    return p


# ------------------------------------------------------------------ HuggingFace taklidi
rnd = random.Random(7)
def blob(n):
    return rnd.randbytes(n)


FILES = {
    "acme/tiny-GGUF": {"tiny-Q4_K_M.gguf": blob(12 << 20), "tiny-Q8_0.gguf": blob(3 << 20), "README.md": b"# tiny"},
    "acme/split-GGUF": {"split-Q4_K_M-00001-of-00002.gguf": blob(4 << 20), "split-Q4_K_M-00002-of-00002.gguf": blob(4 << 20)},
    "acme/big-GGUF": {"big-Q4_K_M.gguf": blob(48 << 20)},
    "acme/whisper": {"ggml-tiny.bin": blob(2 << 20), "ggml-base.bin": blob(3 << 20)},
    "acme/voices": {"tr/tr_TR/x/medium/tr_TR-x-medium.onnx": blob(1 << 20), "tr/tr_TR/x/medium/tr_TR-x-medium.onnx.json": b'{"audio":{"sample_rate":22050}}'},
}
HF_SERVED = {"bytes": 0, "files": {}}


class HF(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, o, code=200):
        b = json.dumps(o).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlsplit(self.path)
        p = unquote(u.path)
        m = re.match(r"^/api/models/([^/]+/[^/]+)/tree/([^/]+)$", p)
        if m and m.group(1) in FILES:
            return self._json([{"type": "file", "path": k, "size": len(v), "oid": hashlib.sha1(v).hexdigest(),
                                **({"lfs": {"oid": hashlib.sha256(v).hexdigest(), "size": len(v)}} if len(v) > 4096 else {})} for k, v in FILES[m.group(1)].items()])
        m = re.match(r"^/api/models/([^/]+/[^/]+)$", p)
        if m and m.group(1) in FILES:
            return self._json({"id": m.group(1), "sha": "abc123def456", "pipeline_tag": "text-generation", "tags": ["gguf"],
                               "siblings": [{"rfilename": k, "size": len(v)} for k, v in FILES[m.group(1)].items()]})
        m = re.match(r"^/([^/]+/[^/]+)/resolve/[^/]+/(.+)$", p)
        if m and m.group(1) in FILES and m.group(2) in FILES[m.group(1)]:
            data = FILES[m.group(1)][m.group(2)]
            start, code = 0, 200
            r = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
            if r:
                start, code = int(r.group(1)), 206
            part = data[start:]
            self.send_response(code)
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            self.wfile.write(part)
            HF_SERVED["bytes"] += len(part)
            HF_SERVED["files"][m.group(2)] = HF_SERVED["files"].get(m.group(2), 0) + 1
            return
        self._json({"error": "yok"}, 404)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


hf_srv = QuietServer(("127.0.0.1", HF_PORT), HF)
threading.Thread(target=hf_srv.serve_forever, daemon=True).start()


# ------------------------------------------------------------------ PHP için çok işçili barındırma taklidi
class Balancer:
    """`php -S` tek istek işler: önüne bağlantıyı boş işçiye veren küçük TCP dengeleyici konur (PHP-FPM/LiteSpeed gibi)."""

    def __init__(self, port, workers):
        self.port, self.workers, self.busy = port, workers, {w: 0 for w in workers}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self.start(), self.loop)

    async def start(self):
        self.srv = await asyncio.start_server(self.conn, "127.0.0.1", self.port)

    async def pump(self, r, w):
        try:
            while True:
                b = await r.read(65536)
                if not b:
                    break
                w.write(b)
                await w.drain()
        except Exception:
            pass
        try:
            w.close()
        except Exception:
            pass

    async def conn(self, r, w):
        while True:
            free = [x for x, n in self.busy.items() if n == 0]
            if free:
                break
            await asyncio.sleep(0.01)
        port = free[0]
        self.busy[port] = 1
        try:
            r2, w2 = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.gather(self.pump(r, w2), self.pump(r2, w))
        finally:
            self.busy[port] = 0


def agent_cmd(i, code):
    st = tmp / f"m{i}"
    return [sys.executable, str(code), "--host", f"127.0.1.{i}", "--port", str(AG_PORT), "--token=" + TOKENS[i], "--dry-power", "--state-dir", str(st)]


def start_agent(i):
    code = tmp / f"m{i}" / "node_agent.py"
    return spawn(agent_cmd(i, code), env={"TEST_BIND": f"127.0.1.{i}", "ORKESTRA_BIND_HOST": f"127.0.1.{i}", "PYTHONPATH": str(TESTS / "fake_piper")}, log=tmp / f"agent{i}.log")


def kill_agent(i):
    """Ajan kendini güncellediğinde süreç numarası değişir: komut satırından bulunur."""
    needle = str(tmp / f"m{i}" / "node_agent.py")
    for pr in psutil.process_iter(["pid", "cmdline"]):
        if needle in " ".join(pr.info.get("cmdline") or []):
            pr.kill()
            pr.wait(5)


TOKENS = {i: secrets.token_urlsafe(16) for i in (1, 2, 3)}
AG = {}
cl = httpx.Client(base_url=f"http://127.0.0.1:{CTRL}", timeout=60, headers={"X-Orkestra": "1"})
pub = httpx.Client(base_url=f"http://127.0.0.1:{PUB}", timeout=60)


def ag(i):
    return httpx.Client(base_url=f"http://127.0.1.{i}:{AG_PORT}", timeout=10, headers={"Authorization": f"Bearer {TOKENS[i]}"})


def dep(did):
    return next((d for d in cl.get("/api/deployments").json() if d["id"] == did), None)


def dep_ready(did, timeout=90):
    return wait_for(lambda: (dep(did) or {}).get("state") == "ready", timeout)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def bundle(files, top="paket"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, (data, mode) in files.items():
            ti = tarfile.TarInfo(f"{top}/{name}" if top else name)
            ti.size, ti.mode = len(data), mode
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def upload(name, data):
    r = cl.put("/api/fabric/upload", params={"name": name}, content=data)
    assert r.status_code == 200, r.text
    return r.json()


FAKE = (TESTS / "fake_runtime.py").read_bytes()
rc = 1
try:
    step("kurulum: ana PC + 3 ayrı makine (gerçek süreçler, ayrı IP'ler)")
    for i in TOKENS:
        (tmp / f"m{i}").mkdir()
        # ajanlar bilerek eski kodla başlar: ana PC onları kendisi güncellemeli
        (tmp / f"m{i}" / "node_agent.py").write_text((ROOT / "agent" / "node_agent.py").read_text(encoding="utf-8") + "\n# eski sürüm\n", encoding="utf-8")
        AG[i] = start_agent(i)
    spawn([sys.executable, str(ROOT / "run.py"), "--host", "127.0.0.1", "--port", str(CTRL), "--public-port", str(PUB), "--fabric-port", str(FAB), "--data-dir", str(tmp / "data")],
          env={"ORKESTRA_ADMIN_PASSWORD": PW, "HF_ENDPOINT": f"http://127.0.0.1:{HF_PORT}"}, log=tmp / "ctrl.log")
    ok(bool(wait_for(lambda: cl.get("/api/auth/state").status_code == 200, 30)), "kontrol sunucusu ayakta")
    ok(cl.post("/api/auth/login", json={"password": PW}).status_code == 200, "panele giriş")
    cl.put("/api/settings", json={"fabric.reserve_gb": 0, "fabric.preseed": False, "auto.allow_sleep": False, "fabric.source_slots": 1})
    ids = {}
    for i in TOKENS:
        ids[i] = cl.post("/api/nodes", json={"name": f"m{i}", "host": f"127.0.1.{i}", "agent_port": AG_PORT, "agent_token": TOKENS[i], "role": "laptop"}).json()["node"]["id"]
    ok(bool(wait_for(lambda: all(n["status"] == "online" and n["hw"]["ram_gb"] for n in cl.get("/api/nodes").json()), 40)), "3 makine çevrimiçi")

    step("ajan güncelleme: eski ajan kendini ana PC'den yeniler")
    want = sha((ROOT / "agent" / "node_agent.py").read_bytes())
    ok(bool(wait_for(lambda: all(ag(i).get("/v1/info").json()["code_sha"] == want for i in TOKENS), 60, 1)), "tüm ajanlar ana PC'deki sürüme güncellendi")
    ok(bool(wait_for(lambda: all(n["status"] == "online" for n in cl.get("/api/nodes").json()), 30)), "güncellemeden sonra makineler yine çevrimiçi")

    step("runtime: yamalı derleme paketi switch üzerinden dağıtılır")
    pkg = bundle({"bin/llama-server.py": (FAKE, 0o755), "bin/libllama.so": (b"lib", 0o644), "VERSION": (b"yamali-1", 0o644)})
    up = upload("llama-yamali.tar.gz", pkg)
    ok(up["sha256"] == sha(pkg), "paket yüklendi, sha256 doğru")
    plat = ag(1).get("/v1/info").json()["platform"]
    b = cl.post("/api/fabric/builds", json={"runtime": "llamacpp", "sha256": up["sha256"], "version": "yamali-1", "platform": plat, "backend": "cpu", "format": "bin"})
    ok(b.status_code == 200, f"derleme kaydedildi ({plat}/cpu)")
    r = cl.post("/api/fabric/builds", json={"runtime": "whispercpp", "sha256": upload("whisper.tar.gz", bundle({"whisper-server.py": (FAKE, 0o755)}))["sha256"], "platform": plat, "backend": "cpu"})
    ok(r.status_code == 200, "whisper.cpp derlemesi kaydedildi")

    step("model: HuggingFace'ten yalnızca bir kez iner, makineler ağdan alır")
    r = cl.post("/api/models", json={"name": "tiny", "repo": "acme/tiny-GGUF:Q4_K_M", "runtime": "llamacpp", "source": "hf", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.1, "max_replicas": 3, "max_concurrency": 4, "ctx": 2048})
    ok(r.status_code == 200, "model kataloğa eklendi")
    mid = r.json()["id"]
    wait_for(lambda: next(m for m in cl.get("/api/models").json() if m["id"] == mid)["file_count"] == 1, 15)
    m = next(m for m in cl.get("/api/models").json() if m["id"] == mid)
    ok(m["file_count"] == 1 and m["files_gb"] == round(12 / 1024, 2), "yalnızca seçilen nicemleme listelendi (Q8 ve README alınmadı)")
    d1 = cl.post(f"/api/models/{mid}/deploy", json={"node_id": ids[1]}).json()["id"]
    states = set()
    wait_for(lambda: states.add((dep(d1) or {}).get("state")) or (dep(d1) or {}).get("state") == "ready", 60, 0.05)
    ok("syncing" in states and (dep(d1) or {}).get("state") == "ready", f"dağıtım: {' → '.join(s for s in ('pending', 'syncing', 'starting', 'ready') if s in states)}")
    ok(HF_SERVED["files"].get("tiny-Q4_K_M.gguf") == 1 and "tiny-Q8_0.gguf" not in HF_SERVED["files"], "dosya HuggingFace'ten bir kez indi")
    want_sha = sha(FILES["acme/tiny-GGUF"]["tiny-Q4_K_M.gguf"])
    ok(want_sha in ag(1).get("/v1/fabric/state").json()["blobs"], "dosya m1 deposunda, sha256 eşleşti")
    d2 = cl.post(f"/api/models/{mid}/deploy", json={"node_id": ids[2]}).json()["id"]
    d3 = cl.post(f"/api/models/{mid}/deploy", json={"node_id": ids[3]}).json()["id"]
    ok(bool(dep_ready(d2) and dep_ready(d3)), "m2 ve m3 de hazır")
    ok(HF_SERVED["files"]["tiny-Q4_K_M.gguf"] == 1, "m2 ve m3 için internete çıkılmadı (modem kotası harcanmadı)")
    served = sum(ag(i).get("/v1/metrics").json()["fabric"]["served"] for i in TOKENS)
    ok(served >= 1, f"makineler dosyayı birbirinden de aldı (eşler arası aktarım: {served})")
    cmd = ag(1).get("/v1/logs", params={"id": f"dep-{d1}"}).json()["text"]
    ok(re.search(r"llama-server\.py .*-m \S+tiny-Q4_K_M\.gguf", cmd) is not None and " -hf " not in cmd, "süreç yerel dosyayla başlatıldı (-m, internetsiz)")
    ok("ctx=8192 np=4" in cmd, "bağlam yuva sayısıyla çarpıldı: her sohbet tam bağlam alıyor (2048 × 4)")

    step("çıkarım: istek ağ geçidinden makinelere dağılır")
    key = cl.post("/api/keys", json={"name": "site", "rpm": 0}).json()
    H = {"Authorization": f"Bearer {key['key']}", "X-User-Id": "kisi-1", "X-User-Plan": "pro"}
    seen = set()
    for i in range(9):
        r = pub.post("/v1/chat/completions", headers=H, json={"model": "tiny", "messages": [{"role": "user", "content": f"soru {i}"}]})
        assert r.status_code == 200, r.text
        seen.add(re.search(r"\|(127\.0\.1\.\d)\]", r.json()["choices"][0]["message"]["content"]).group(1))
    ok(len(seen) >= 2, f"istekler birden çok makineye gitti: {sorted(seen)}")
    ok(FILES and sha(FILES["acme/tiny-GGUF"]["tiny-Q4_K_M.gguf"][:1 << 20])[:12] in r.text, "yanıt, makineye aktarılan gerçek model dosyasından üretildi")

    step("güvenlik ve bütünlük")
    a1 = httpx.Client(base_url=f"http://127.0.1.1:{AG_PORT}", timeout=10)
    ok(a1.get(f"/v1/fabric/blob/{want_sha}").status_code == 401, "biletsiz dosya isteği reddedildi")
    ok(a1.get(f"/v1/fabric/blob/{want_sha}", headers={"Authorization": "Bearer 9999999999.yanlis"}).status_code == 401, "sahte bilet reddedildi")
    ok(httpx.get(f"http://127.0.0.1:{FAB}/blob/{want_sha}").status_code == 401, "ana PC deposu da bilet istiyor")
    fake_sha = sha(b"baska")
    j = ag(2).post("/v1/fabric/fetch", json={"sha256": fake_sha, "size": 12 << 20, "reserve_gb": 0, "sources": [{"url": f"http://127.0.1.1:{AG_PORT}/v1/fabric/blob/{want_sha}", "ticket": TOKENS[1]}]}).json()
    bad = wait_for(lambda: next((x for x in ag(2).get("/v1/fabric/jobs").json()["jobs"] if x["id"] == j["id"] and x["state"] == "failed"), None), 15)
    ok(bool(bad) and "sha256" in bad["error"] and fake_sha not in ag(2).get("/v1/fabric/state").json()["blobs"], "bozuk içerik kabul edilmedi: " + (bad or {}).get("error", ""))
    spec = importlib.util.spec_from_file_location("ajan", ROOT / "agent" / "node_agent.py")
    agent_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_mod)
    (tmp / "kotu.tgz").write_bytes(bundle({"../../kacis.txt": (b"x", 0o644), "normal.txt": (b"y", 0o644)}, top=""))
    (tmp / "kutu").mkdir()
    try:
        agent_mod.extract_archive(str(tmp / "kotu.tgz"), str(tmp / "kutu"))
        escaped = True
    except ValueError:
        escaped = False
    ok(not escaped and not (tmp / "kacis.txt").exists(), "paket dışına yazmaya çalışan arşiv reddedildi")
    r = ag(1).post("/v1/launch", json={"id": "x1", "argv": [sys.executable, "-c", "print(1)"], "port": 1})
    ok(r.status_code == 403, "paket dışı komut çalıştırılamıyor")

    step("kopan aktarım kaldığı yerden devam eder")
    cl.put("/api/settings", json={"fabric.lan_limit_mbps": 200})
    r = cl.post("/api/models", json={"name": "big", "repo": "acme/big-GGUF", "runtime": "llamacpp", "source": "hf", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.1, "ctx": 512})
    bid = r.json()["id"]
    d4 = cl.post(f"/api/models/{bid}/deploy", json={"node_id": ids[3]}).json()["id"]
    def part_size():
        p = tmp / "m3" / "store" / "tmp" / (sha(FILES["acme/big-GGUF"]["big-Q4_K_M.gguf"]) + ".part")
        return p.stat().st_size if p.exists() else 0
    ok(bool(wait_for(lambda: part_size() > 5 << 20, 40, 0.02)), "aktarım başladı")
    kill_agent(3)
    before = part_size()
    ok(0 < before < 48 << 20, f"makine aktarım ortasında kapatıldı ({before >> 20} MB alınmıştı)")
    cl.put("/api/settings", json={"fabric.lan_limit_mbps": 0})
    AG[3] = start_agent(3)
    ok(bool(dep_ready(d4, 90)), "makine dönünce aktarım tamamlandı, model hazır")
    logs = ag(3).get("/v1/fabric/state").json()
    ok(sha(FILES["acme/big-GGUF"]["big-Q4_K_M.gguf"]) in logs["blobs"], "tamamlanan dosya sha256 doğrulamasından geçti")
    ok(HF_SERVED["files"]["big-Q4_K_M.gguf"] == 1, "yeniden deneme için internete çıkılmadı")
    ok(bool(wait_for(lambda: dep(d3)["state"] == "ready", 30)), "kapanıp açılan makinedeki eski model yeniden devrede")

    step("parçalı GGUF ve kaynaktan derleme")
    builder = b"import os,shutil,sys\nout=os.environ['OUT']\nshutil.copy('server.py',os.path.join(out,'yamali-server.py'))\nprint('derlendi',os.environ['BACKEND'])\n"
    src = bundle({"orkestra-build.py": (builder, 0o644), "server.py": (FAKE, 0o644)}, top="llama.cpp-yamali")
    cl.post("/api/runtimes", json={"key": "yamali", "label": "Yamalı", "exe": "yamali-server", "health": "/health", "args": ["-m", "{model_path}", "--host", "0.0.0.0", "--port", "{port}", "--alias", "{name}", "-np", "{parallel}"]})
    r = cl.post("/api/fabric/builds", json={"runtime": "yamali", "sha256": upload("src.tar.gz", src)["sha256"], "format": "src", "version": "kaynak-1"})
    ok(r.status_code == 200 and r.json()["format"] == "src", "kaynak paketi kaydedildi")
    sid = cl.post("/api/models", json={"name": "split", "repo": "acme/split-GGUF:Q4_K_M", "runtime": "yamali", "source": "hf", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.1, "ctx": 512}).json()["id"]
    d5 = cl.post(f"/api/models/{sid}/deploy", json={"node_id": ids[2]}).json()["id"]
    ok(bool(dep_ready(d5, 60)), "kaynak makinede derlendi, parçalı model hazır")
    st = ag(2).get("/v1/fabric/state").json()
    mdl = next(v for k, v in st["models"].items() if k.startswith("split"))
    ok(sorted(os.listdir(mdl["dir"])) == ["split-Q4_K_M-00001-of-00002.gguf", "split-Q4_K_M-00002-of-00002.gguf"], "parçalar özgün adlarıyla aynı klasörde")
    r = pub.post("/v1/chat/completions", headers=H, json={"model": "split", "messages": [{"role": "user", "content": "x"}]})
    ok(r.status_code == 200, "kaynaktan derlenen runtime yanıt veriyor")

    step("site servisleri: sohbet, konuşma → metin, metin → konuşma")
    w = cl.post("/api/models", json={"name": "stt", "repo": "acme/whisper", "file_glob": "ggml-tiny.bin", "runtime": "whispercpp", "kind": "stt", "source": "hf", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.1, "min_replicas": 1, "ctx": 0}).json()
    v = cl.post("/api/models", json={"name": "tts", "repo": "acme/voices", "file_glob": "tr/tr_TR/x/medium/*", "runtime": "piper", "kind": "tts", "source": "hf", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.1, "min_replicas": 1, "ctx": 0}).json()
    ok(cl.put("/api/services/sohbet", json={"kind": "llm", "targets": ["tiny"], "is_default": True}).status_code == 200, "sohbet servisi tanımlandı")
    ok(cl.put("/api/services/stt", json={"kind": "stt", "targets": ["stt"], "is_default": True, "options": {"language": "tr"}}).status_code == 200, "konuşma → metin servisi tanımlandı")
    ok(cl.put("/api/services/tts", json={"kind": "tts", "targets": ["tts"], "is_default": True, "options": {"response_format": "wav"}}).status_code == 200, "metin → konuşma servisi tanımlandı")
    ok(cl.put("/api/services/x", json={"kind": "tts", "targets": ["tiny"]}).status_code == 400, "yanlış türde model servise eklenemez")
    ok(bool(wait_for(lambda: all(any(d["state"] == "ready" for d in cl.get("/api/models").json()[k]["deployments"]) for k in [i for i, m in enumerate(cl.get("/api/models").json()) if m["name"] in ("stt", "tts")]), 60)), "ses modelleri otomatik dağıtıldı ve hazır")
    audio = os.urandom(300000)
    r = pub.post("/v1/audio/transcriptions", headers=H, files={"file": ("a.webm", audio, "audio/webm")})
    ok(r.status_code == 200 and "bayt" in r.json()["text"] and r.json()["language"] == "tr", f"model adı verilmeden STT çalıştı, servis dili uygulandı: {r.text[:70]}")
    r = pub.post("/v1/audio/speech", headers=H, json={"input": "Merhaba, bu bir deneme cümlesi. İkinci cümle de burada."})
    ok(r.status_code == 200 and r.content[:4] == b"RIFF" and len(r.content) > 40000, f"TTS gerçek köprüden ses verdi ({len(r.content)} bayt)")
    u = pub.get("/v1/usage", params={"user": "kisi-1"}, headers=H).json()
    kinds = {x["kind"] for x in u["days"]}
    ok({"chat", "stt", "tts"} <= kinds and u["today"]["tokens"] > 0, f"kullanım kaydı: {sorted(kinds)}, bugün {u['today']['tokens']} token")
    r = pub.get("/v1/models", headers=H).json()["data"]
    ok({x["id"] for x in r} == {"sohbet", "stt", "tts"}, "site yalnızca servis adlarını görüyor")

    step("siteye bağlantı: küme dışarı bağlanır, üç röle türü")
    node = shutil.which("node")
    php = shutil.which("php")
    relays = []
    rel = tmp / "relay"
    rel.mkdir()
    for i, kind in enumerate(("python", "node", "php")):
        d = rel / kind
        d.mkdir()
        r = cl.get(f"/api/uplink/relay/{kind}")
        ok(r.status_code == 200 and b"__RELAY_SECRET__" not in r.content, f"{kind} rölesi panelden anahtarı içine yazılmış indirildi")
        port = RELAY_BASE + i * 20
        if kind == "python":
            (d / "relay.py").write_bytes(r.content)
            spawn([sys.executable, str(d / "relay.py"), "--port", str(port)], log=tmp / "relay-py.log")
            relays.append((kind, f"http://127.0.0.1:{port}"))
        elif kind == "node" and node:
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(d)
            spawn([node, str(d / "relay.mjs")], env={"PORT": str(port), "RELAY_HOST": "127.0.0.1"}, log=tmp / "relay-node.log")
            relays.append((kind, f"http://127.0.0.1:{port}"))
        elif kind == "php" and php:
            (d / "relay.php").write_bytes(r.content)
            workers = [port + 1 + k for k in range(12)]
            for w_p in workers:
                spawn([php, "-d", "output_buffering=0", "-d", "max_execution_time=0", "-d", "upload_max_filesize=32M", "-d", "post_max_size=40M", "-S", f"127.0.0.1:{w_p}", "-t", str(d)], log=tmp / "relay-php.log")
            Balancer(port, workers)
            relays.append((kind, f"http://127.0.0.1:{port}/relay.php"))
    for kind, url in ([] if os.environ.get("E2E_SKIP_RELAY") else relays):
        print(f"  --- {kind}", flush=True)
        c = httpx.Client(timeout=60)
        cl.put("/api/uplink", json={"mode": "off"})
        ok(bool(wait_for(lambda: c.get(url + "/healthz").status_code == 200, 15)), "röle ayakta")
        r = c.post(url + "/v1/chat/completions", headers={"Authorization": "Bearer x"}, json={})
        ok(r.status_code == 503 and r.json()["error"]["code"] == "cluster_offline", "küme bağlı değilken anlaşılır hata (503)")
        cl.put("/api/uplink", json={"mode": "relay", "relay_url": url})
        up_ok = wait_for(lambda: cl.get("/api/uplink").json()["state"] == "up", 25)
        ok(bool(up_ok), f"tünel kuruldu: {cl.get('/api/uplink').json()['state']} {cl.get('/api/uplink').json()['error']}")
        ok(c.get(url + "/healthz").json()["cluster"] is True, "röle kümeyi görüyor")
        ok(c.post(url + "/v1/chat/completions", json={}).status_code == 401, "anahtarsız istek rölede reddedildi")
        ok(c.post(url + "/v1/chat/completions", headers={"Authorization": "Bearer yanlis"}, json={"model": "sohbet", "messages": [{"role": "user", "content": "x"}]}).status_code == 401, "yanlış anahtar ağ geçidinde reddedildi")
        ok(c.post(url + "/__tunnel/poll", headers={"Authorization": "Bearer yanlis"}, json={}).status_code == 401, "tünel ucu anahtarsız kullanılamıyor")
        t0 = time.time()
        r = c.post(url + "/v1/chat/completions", headers=H, json={"model": "sohbet", "messages": [{"role": "user", "content": "röle ğüşiöç"}]})
        ok(r.status_code == 200 and "röle ğüşiöç" in r.json()["choices"][0]["message"]["content"], f"sohbet yanıtı geldi ({(time.time() - t0) * 1000:.0f} ms), Türkçe bozulmadı")
        times, text = [], ""
        with c.stream("POST", url + "/v1/chat/completions", headers=H, json={"model": "sohbet", "stream": True, "messages": [{"role": "user", "content": "akış sınaması bir iki üç dört beş altı yedi sekiz dokuz on"}]}) as s:
            ok(s.status_code == 200 and "text/event-stream" in s.headers.get("content-type", ""), "akış başladı")
            for line in s.iter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    d = json.loads(line[5:])
                    if d.get("choices") and d["choices"][0]["delta"].get("content"):
                        times.append(time.time())
                        text += d["choices"][0]["delta"]["content"]
        ok(len(times) >= 10 and times[-1] - times[0] > 0.15 and "dokuz on" in text, f"token token geldi: {len(times)} parça, {times[-1] - times[0]:.2f} sn aralıkla")
        audio2 = os.urandom(3 << 20)
        r = c.post(url + "/v1/audio/transcriptions", headers=H, files={"file": ("uzun.webm", audio2, "audio/webm")}, timeout=60)
        ok(r.status_code == 200 and "bayt" in r.text, "3 MB ses yüklemesi tünelden geçti")
        r = c.post(url + "/v1/audio/speech", headers=H, json={"input": "Tünelden ses. " * 10})
        ok(r.status_code == 200 and r.content[:4] == b"RIFF" and len(r.content) > 100000, f"ses yanıtı tünelden geldi ({len(r.content)} bayt)")
        with c.stream("POST", url + "/v1/chat/completions", headers=H, json={"model": "sohbet", "stream": True, "messages": [{"role": "user", "content": "yavas akış"}]}) as s:
            n = 0
            for line in s.iter_lines():
                n += line.startswith("data:")
                if n >= 3:
                    break
        ok(bool(wait_for(lambda: sum(x["inflight"] for x in cl.get("/api/deployments").json()) == 0, 20)), "kullanıcı koparınca model üretimi durdu (GPU boşa çalışmıyor)")
        t = cl.post("/api/uplink/test").json()
        ok(t["ok"], "panelden uçtan uca sınama: " + "; ".join(f"{x['name']} {x['ms']} ms ({x['note']})" for x in t["steps"]))
    cl.put("/api/uplink", json={"mode": "off"})

    step("ağ taraması ve SSH ile kurulum (gerçek SSH protokolü)")
    ssh_pw = secrets.token_urlsafe(10)
    box = tmp / "ubuntu"
    box.mkdir()
    spawn([sys.executable, str(TESTS / "fake_ssh_host.py"), "127.0.1.7", str(SSH_PORT), "operator", ssh_pw, str(box), BASH, "127.0.1.7"], log=tmp / "ssh.log")
    ok(bool(wait_for(lambda: "ssh hazır" in (tmp / "ssh.log").read_text(encoding="utf-8", errors="ignore"), 20)), "sahte Ubuntu makine ayakta")
    s = cl.post("/api/net/scan", json={"cidr": "127.0.1.0/28", "ssh_port": SSH_PORT, "agent_port": AG_PORT})
    hosts = {h["ip"]: h for h in s.json()["hosts"]} if s.status_code == 200 else {}
    ok(hosts.get("127.0.1.7", {}).get("ssh") is True and hosts.get("127.0.1.1", {}).get("kind") == "node", f"tarama: {[(k, v['kind']) for k, v in hosts.items()]}")
    r = cl.post("/api/provision", json={"host": "127.0.1.7", "user": "operator", "password": "yanlis-parola", "ssh_port": SSH_PORT})
    bad = r.json()["id"]
    f = wait_for(lambda: (lambda j: j if j["state"] != "running" else None)(cl.get(f"/api/provision/{bad}").json()), 30)
    ok(f and f["state"] == "failed" and "reddedildi" in f["error"], "yanlış SSH parolası: " + (f or {}).get("error", ""))
    r = cl.post("/api/provision", json={"host": "127.0.1.7", "user": "operator", "password": ssh_pw, "ssh_port": SSH_PORT, "agent_port": 7797, "name": "yeni-laptop", "ffmpeg": True})
    ok(r.status_code == 200, "kurulum başladı")
    jid = r.json()["id"]
    f = wait_for(lambda: (lambda j: j if j["state"] != "running" else None)(cl.get(f"/api/provision/{jid}").json()), 90)
    log_text = "\n".join((f or {}).get("log", []))
    ok(f and f["state"] == "done", f"SSH ile kuruldu: {(f or {}).get('state')} {(f or {}).get('error', '')}")
    if not f or f["state"] != "done":
        print(log_text[-1500:])
    ok(ssh_pw not in log_text, "parola günlüğe yazılmadı")
    env = (box / "root/etc/orkestra/agent.env").read_text(encoding="utf-8") if (box / "root/etc/orkestra/agent.env").exists() else ""
    ok("ORKESTRA_AGENT_TOKEN=" in env and (box / "root/etc/systemd/system/orkestra-agent.service").exists() and (box / "root/etc/sudoers.d/orkestra").exists(), "systemd servisi, yapılandırma ve sudo izni yazıldı")
    nn = next((n for n in cl.get("/api/nodes").json() if n["name"] == "yeni-laptop"), None)
    ok(bool(nn) and nn["status"] == "online", "yeni makine panelde çevrimiçi")
    if nn:
        d6 = cl.post(f"/api/models/{mid}/deploy", json={"node_id": nn["id"]}).json()["id"]
        ok(bool(dep_ready(d6, 60)), "yeni makineye model switch üzerinden gitti ve hazır")
        ok(HF_SERVED["files"]["tiny-Q4_K_M.gguf"] == 1, "yeni makine için de internete çıkılmadı")
        bw = cl.post(f"/api/fabric/nodes/{ids[1]}/bwtest")
        ok(bw.status_code == 200 and bw.json()["mbit"] > 50, f"ağ hızı ölçümü çalışıyor: {bw.json().get('mbit')} Mbit/sn")
    rc = 0
finally:
    print("\n=== temizlik", flush=True)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(6)
        except Exception:
            p.kill()
    try:
        for pr in psutil.process_iter(["pid", "cmdline"]):
            c = " ".join(pr.info.get("cmdline") or [])
            if str(tmp) in c or tmp.as_posix() in c:
                pr.kill()
    except Exception:
        pass
    if FAILS or rc:
        for name in ("ctrl.log", "agent1.log", "relay-py.log", "relay-node.log", "relay-php.log", "ssh.log"):
            p = tmp / name
            if p.exists():
                print(f"--- {name} (son satırlar) ---")
                print("\n".join(p.read_text(encoding="utf-8", errors="ignore").splitlines()[-14:]))
    time.sleep(0.5)
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("TÜM KONTROLLER GEÇTİ" if not FAILS and rc == 0 else f"{len(FAILS)} BAŞARISIZ KONTROL" + ("" if rc == 0 else " (sınama yarıda kesildi)")))
for x in FAILS:
    print("  -", x)
sys.exit(1 if FAILS or rc else 0)
