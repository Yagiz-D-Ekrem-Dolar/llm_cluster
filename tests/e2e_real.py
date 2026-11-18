"""Gerçek-mod sınaması: simülatör YOK. Kontrol sunucusu ↔ HTTP ajan ↔ süreç başlatma ↔ ağ geçidi.

Bu betik kendi ajanını ve kontrol sunucusunu alt süreç olarak başlatır (geçici veri klasörü, farklı portlar),
sahte çıkarım sunucusunu (tests/mock_runtime.py) ajana başlattırır ve tüm akışı doğrular.
Kullanım:  python tests/e2e_real.py
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
CTRL_PORT, AGENT_PORT = 8790, 7790
AGENT_TOKEN = secrets.token_urlsafe(16)
ADMIN_PW = secrets.token_urlsafe(12)
FAILS = []
procs = []


def ok(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)
    return cond


def wait_for(fn, timeout=60, every=0.5):
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


def spawn(args, env=None, log=None):
    f = open(log, "w", encoding="utf-8") if log else subprocess.DEVNULL
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})})
    procs.append(p)
    return p


def start_agent(state_dir, log):
    return spawn([sys.executable, str(ROOT / "agent" / "node_agent.py"), "--host", "127.0.0.1", "--port", str(AGENT_PORT),
                  "--token", AGENT_TOKEN, "--dry-power", "--allow-exe", sys.executable, "--allow-module", "mock_runtime",
                  "--state-dir", str(state_dir)], log=log)


tmp = Path(tempfile.mkdtemp(prefix="orkestra-real-"))
try:
    print("=== alt süreçler başlatılıyor")
    agent = start_agent(tmp / "agent", tmp / "agent.log")
    ctrl = spawn([sys.executable, str(ROOT / "run.py"), "--host", "127.0.0.1", "--port", str(CTRL_PORT), "--no-public",
                  "--data-dir", str(tmp / "data")], env={"ORKESTRA_ADMIN_PASSWORD": ADMIN_PW}, log=tmp / "ctrl.log")
    ag = httpx.Client(base_url=f"http://127.0.0.1:{AGENT_PORT}", timeout=10)
    cl = httpx.Client(base_url=f"http://127.0.0.1:{CTRL_PORT}", timeout=120, headers={"X-Orkestra": "1"})
    ok(bool(wait_for(lambda: ag.get("/healthz").status_code == 200, 20)), "ajan ayakta")
    ok(bool(wait_for(lambda: cl.get("/api/auth/state").status_code == 200, 30)), "kontrol sunucusu ayakta")

    print("\n=== ajan güvenliği")
    ok(ag.get("/v1/metrics").status_code == 401, "tokensiz /v1/metrics reddedildi")
    ok(ag.get("/v1/metrics", headers={"Authorization": "Bearer yanlis"}).status_code == 401, "yanlış token reddedildi")
    H = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    m = ag.get("/v1/metrics", headers=H).json()
    ok(m["mem"]["total_gb"] > 0 and "procs" in m, f"metrikler: RAM {m['mem']['total_gb']} GB, CPU {m['cpu']}")
    inf = ag.get("/v1/info", headers=H).json()
    ok(bool(inf["hostname"]) and inf["ram_gb"] > 0, f"info: {inf['hostname']} / {inf['os'][:30]} / GPU sayısı {len(inf['gpus'])}")
    r = ag.post("/v1/launch", headers=H, json={"id": "x1", "argv": ["bash", "-c", "echo pwn"], "port": 1})
    ok(r.status_code == 403, f"izinsiz çalıştırılabilir reddedildi: {r.json()}")
    r = ag.post("/v1/launch", headers=H, json={"id": "x2", "argv": [sys.executable, "-c", "print(1)"], "port": 1})
    ok(r.status_code == 403, "python -c reddedildi (yalnızca izinli -m modülleri)")
    r = ag.post("/v1/power", headers=H, json={"action": "format-c"})
    ok(r.status_code == 400, "geçersiz güç işlemi reddedildi")
    r = ag.post("/v1/launch", headers=H, json={"id": "x3", "argv": ["./llama-server", "-h"], "port": 1})
    ok(r.status_code == 403, "göreli yol reddedildi: " + r.json().get("error", ""))
    other = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "cmd.exe") if os.name == "nt" else "/bin/sh"
    r = ag.post("/v1/launch", headers=H, json={"id": "x4", "argv": [other, "-c", "echo"], "port": 1})
    ok(r.status_code == 403 and "orkestra-agent-allow" in r.text, "izinli olmayan tam yol reddedildi ve çözüm komutu önerildi")
    inf2 = ag.get("/v1/info", headers=H).json()
    ok(sys.executable in inf2["runtimes"] and sys.executable in inf2["allow_exe"], "ajan, izinli tam yolu ve bulunduğunu bildiriyor")

    print("\n=== kontrol sunucusu: kurulum")
    ok(cl.post("/api/auth/login", json={"password": ADMIN_PW}).status_code == 200, "ortam değişkeniyle ayarlanan parolayla giriş")
    ok(cl.post("/api/runtimes", json={"key": "mock", "label": "Sahte", "exe": sys.executable, "health": "/health",
                                      "args": ["-m", "mock_runtime", "--port", "{port}", "--name", "{name}", "--load", "2"],
                                      "env": {"PYTHONPATH": str(TESTS)}}).status_code == 200, "özel runtime tanımlandı")
    r = cl.post("/api/nodes", json={"name": "yerel", "host": "127.0.0.1", "agent_port": AGENT_PORT, "agent_token": AGENT_TOKEN,
                                    "role": "main", "tags": ["test"], "allow_sleep": True})
    ok(r.status_code == 200 and "install_cmd" in r.json(), "düğüm eklendi, kurulum komutu üretildi: " + r.json().get("install_cmd", "")[:70])
    nid = r.json()["node"]["id"]
    on = wait_for(lambda: next(n for n in cl.get("/api/nodes").json() if n["id"] == nid)["status"] == "online", 20)
    ok(bool(on), "düğüm çevrimiçi (HTTP ajan üzerinden yoklama)")
    wait_for(lambda: next(n for n in cl.get("/api/nodes").json() if n["id"] == nid)["hw"]["ram_gb"], 15)
    node = next(n for n in cl.get("/api/nodes").json() if n["id"] == nid)
    ok(node["mem"]["total_gb"] > 0 and (node["hw"]["ram_gb"] or 0) > 0, f"donanım bilgisi alındı: {(node['hw']['cpu'] or {}).get('model', '?')[:40]}")
    ok(bool(node["mac"]), f"MAC otomatik algılandı: {node['mac']}")

    print("\n=== model dağıtımı ve çıkarım")
    mid = cl.post("/api/models", json={"name": "mock-model", "repo": "mock/mock", "runtime": "mock", "kind": "llm",
                                       "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.3, "max_replicas": 1,
                                       "idle_unload_min": 1}).json()["id"]
    d = cl.post(f"/api/models/{mid}/deploy", json={"node_id": nid})
    ok(d.status_code == 200, f"dağıtım oluşturuldu: {d.text[:90]}")
    did = d.json()["id"]
    ready = wait_for(lambda: next(x for x in cl.get("/api/deployments").json() if x["id"] == did)["state"] == "ready", 40)
    ok(bool(ready), "ajan süreci başlattı ve sağlık kontrolü geçti (starting → ready)")
    cl.put("/api/settings", json={"hf.dry_run": True})
    r = cl.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "gerçek mod deneme"}]})
    ok(r.status_code == 200 and "gerçek-mod sahte yanıtı" in r.text, f"gateway → ajan çıkarım süreci (akışsız): {r.status_code} {r.text[:80]}")
    chunks = []
    with cl.stream("POST", "/v1/chat/completions", json={"model": "mock-model", "stream": True,
                   "messages": [{"role": "user", "content": "akış"}]}) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and "[DONE]" not in line:
                j = json.loads(line[5:])
                if j["choices"] and j["choices"][0]["delta"].get("content"):
                    chunks.append(j["choices"][0]["delta"]["content"])
    ok(len(chunks) > 3, f"akış parçaları alındı: {len(chunks)}")
    logs = cl.get(f"/api/nodes/{nid}/logs", params={"proc": f"dep-{did}"}).json()["text"]
    ok("mock_runtime mock-model" in logs, "süreç günlüğü ajan üzerinden okundu")
    turns = cl.get("/api/chats/stats").json()["turns"]
    ok(turns >= 2, f"sohbet turları kaydedildi ({turns})")

    print("\n=== ajan yeniden başlayınca süreç yeniden sahiplenilir")
    agent.terminate()
    agent.wait(10)
    gone = wait_for(lambda: next(n for n in cl.get("/api/nodes").json() if n["id"] == nid)["status"] != "online", 25)
    ok(bool(gone), "ajan kapanınca düğüm çevrimdışı görüldü")
    agent = start_agent(tmp / "agent", tmp / "agent2.log")
    back = wait_for(lambda: next(n for n in cl.get("/api/nodes").json() if n["id"] == nid)["status"] == "online", 30)
    ok(bool(back), "ajan dönünce düğüm tekrar çevrimiçi")
    st = wait_for(lambda: next(x for x in cl.get("/api/deployments").json() if x["id"] == did)["state"] == "ready", 40)
    ok(bool(st), "çıkarım süreci ajan yeniden başlasa da hayatta kaldı ve hazır")
    r = cl.post("/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "dönüş"}]})
    ok(r.status_code == 200, "çıkarım ajan yeniden başladıktan sonra da çalışıyor")

    print("\n=== izinsiz runtime ile dağıtım güvenli şekilde başarısız olur")
    cl.post("/api/runtimes", json={"key": "evil", "label": "kötü", "exe": "bash", "args": ["-c", "echo pwn"]})
    mid2 = cl.post("/api/models", json={"name": "evil-model", "repo": "x", "runtime": "evil", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.1}).json()["id"]
    d2 = cl.post(f"/api/models/{mid2}/deploy", json={"node_id": nid}).json()
    failed = wait_for(lambda: next(x for x in cl.get("/api/deployments").json() if x["id"] == d2["id"])["state"] == "failed", 20)
    err = next(x for x in cl.get("/api/deployments").json() if x["id"] == d2["id"])["error"]
    ok(bool(failed) and "izinli değil" in err, f"ajan reddetti, dağıtım 'failed': {err[:80]}")

    print("\n=== yamalı/özel runtime: makineye özel yol ve API yolu geçersiz kılma")
    cl.post("/api/runtimes", json={"key": "yamali", "label": "Yamalı", "exe": "yok-boyle-bir-derleme", "health": "/health",
                                   "args": ["-m", "mock_runtime", "--port", "{port}", "--name", "{name}", "--load", "1", "--chat-path", "/alt/sohbet"],
                                   "env": {"PYTHONPATH": str(TESTS)}, "paths": {"chat": "/alt/sohbet"}})
    mid3 = cl.post("/api/models", json={"name": "yamali-model", "repo": "x", "runtime": "yamali", "gpu_count": 0, "cpu_ok": True, "ram_gb": 0.2}).json()["id"]
    r = cl.post(f"/api/models/{mid3}/deploy", json={})
    ok(r.status_code == 409, "runtime hiçbir makinede kullanılabilir değilken otomatik yerleştirme reddedildi")
    r = cl.patch(f"/api/nodes/{nid}", json={"rt_paths": {"yamali": sys.executable}})
    ok(r.status_code == 200 and r.json()["node"]["rt_paths"].get("yamali") == sys.executable, "makineye özel yol kaydedildi")
    r = cl.post(f"/api/models/{mid3}/deploy", json={})
    ok(r.status_code == 200, "makineye özel yolla runtime kullanılabilir oldu, dağıtım oluştu: " + r.text[:80])
    d3 = r.json()["id"]
    up3 = wait_for(lambda: next(x for x in cl.get("/api/deployments").json() if x["id"] == d3)["state"] == "ready", 40)
    ok(bool(up3), "makineye özel yolla başlatılan süreç hazır")
    r = cl.post("/v1/chat/completions", json={"model": "yamali-model", "messages": [{"role": "user", "content": "yol testi"}]})
    ok(r.status_code == 200 and "gerçek-mod sahte yanıtı" in r.text, f"API yolu geçersiz kılma çalıştı (/alt/sohbet): HTTP {r.status_code}")

    print("\n=== güç komutu ve kontrol sunucusu koruması")
    node_now = next(n for n in cl.get("/api/nodes").json() if n["id"] == nid)
    ok(node_now["is_self"] and node_now["always_on"] and not node_now["allow_sleep"], "kontrol sunucusunun çalıştığı makine otomatik korumaya alındı (is_self, her zaman açık)")
    r = cl.post(f"/api/nodes/{nid}/power", json={"action": "sleep", "force": True})
    ok(r.status_code == 400 and "kontrol sunucusunu çalıştırıyor" in r.text, "o makine uykuya alınamaz: " + r.text[:80])
    r = ag.post("/v1/power", headers=H, json={"action": "suspend"})
    seen = wait_for(lambda: "dry-power" in (tmp / "agent2.log").read_text(encoding="utf-8", errors="ignore"), 20)
    ok(r.status_code == 200 and bool(seen), "ajan güç komutunu doğrudan aldı (dry-power: gerçekten çalıştırılmadı)")
    ok("mock-model" in cl.get("/api/models").text, "model kaydı yerinde")
finally:
    print("\n=== temizlik")
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(8)
        except Exception:
            p.kill()
    # ajanın başlattığı sahte çıkarım süreçleri ajanla birlikte ölmez; durdur
    try:
        import psutil
        for pr in psutil.process_iter(["pid", "cmdline"]):
            c = " ".join(pr.info.get("cmdline") or [])
            if "mock_runtime" in c and "--port" in c:
                pr.kill()
    except Exception:
        pass
    if FAILS:
        print("--- denetleyici günlüğü (son satırlar) ---")
        try:
            print("\n".join((tmp / "ctrl.log").read_text(encoding="utf-8", errors="ignore").splitlines()[-25:]))
        except Exception:
            pass

print("\n" + ("TÜM KONTROLLER GEÇTİ" if not FAILS else f"{len(FAILS)} BAŞARISIZ KONTROL:"))
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)
