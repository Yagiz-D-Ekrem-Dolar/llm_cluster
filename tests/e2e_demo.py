"""Demo (simülatör) modunda çalışan sunucuya karşı uçtan uca sınama.

Kullanım:  python tests/e2e_demo.py [panel_url] [herkese_acik_url]
Önce:      python run.py --demo --port 8780 --public-port 8781
"""
import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8780"
PUB = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8781"
DEMO_PASSWORD = "demo1234"  # yalnızca --demo modunda tohumlanan sınama parolası
FAILS: list[str] = []
cl = httpx.Client(base_url=BASE, timeout=180, headers={"X-Orkestra": "1"})
pub = httpx.Client(base_url=PUB, timeout=180)


def step(name):
    print(f"\n=== {name}", flush=True)


def ok(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)
    return cond


def wait_for(fn, timeout=90, every=1.0):
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


def overview():
    return cl.get("/api/overview").json()


def node(name):
    return next(n for n in overview()["nodes"] if n["name"] == name)


def model_id(name):
    return next(m["id"] for m in cl.get("/api/models").json() if m["name"] == name)


def chat(model, text, stream=False, **kw):
    body = {"model": model, "messages": [{"role": "user", "content": text}], "stream": stream, **kw}
    return cl.post("/v1/chat/completions", json=body, headers={"X-User-Id": "e2e-user"})


# --------------------------------------------------------------------------------------
step("kimlik doğrulama")
ok(cl.get("/api/overview").status_code == 401, "oturumsuz /api/overview reddedildi (401)")
r = cl.post("/api/auth/login", json={"password": "yanlış-parola"})
ok(r.status_code == 401, "yanlış parola reddedildi")
r = cl.post("/api/auth/login", json={"password": DEMO_PASSWORD})
ok(r.status_code == 200, "demo parolasıyla giriş")
r = httpx.post(BASE + "/api/auth/login", json={"password": DEMO_PASSWORD})
ok(r.status_code == 403, "X-Orkestra başlığı olmadan istek reddedildi (CSRF koruması)")

step("genel bakış")
o = overview()
k = o["kpi"]
ok(k["nodes_total"] == 7, f"7 makine kayıtlı: ana PC + 6 laptop (bulunan {k['nodes_total']})")
ok(all("superbox" not in n["name"] for n in o["nodes"]), "Superbox makine listesinde değil (modem)")
wait_for(lambda: overview()["kpi"]["nodes_online"] >= 3, 30)
o = overview()
ok(o["kpi"]["nodes_online"] == 3 and o["kpi"]["nodes_sleeping"] == 4,
   f"3 çevrimiçi / 4 uykuda (bulunan {o['kpi']['nodes_online']}/{o['kpi']['nodes_sleeping']})")
sb = node("ana-pc")
ok(len(sb["gpus"]) == 1 and sb["cpu_temp"] is not None, "ana PC GPU ve sıcaklık bildiriyor")

step("min_replicas: otomatik yerleştirme")
ready = wait_for(lambda: [d for d in overview()["deployments"] if d["model"] == "qwen2.5-1.5b-instruct" and d["state"] == "ready"], 90)
ok(bool(ready), f"qwen2.5-1.5b-instruct otomatik başlatıldı ve hazır → {ready[0]['node'] if ready else '-'}")

step("sohbet (akışsız)")
r = chat("qwen2.5-1.5b-instruct", "Merhaba, kendini tanıt.")
ok(r.status_code == 200, f"HTTP {r.status_code}")
j = r.json()
txt = j["choices"][0]["message"]["content"]
ok("simülatörü" in txt or "yanıtlıyorum" in txt, "yanıt metni geldi: " + txt[:70])
ok(bool(r.headers.get("x-orkestra-node")), "yanıt başlığında düğüm adı var: " + r.headers.get("x-orkestra-node", ""))

step("sohbet (akış / SSE)")
chunks, usage, t0, ttft = 0, None, time.time(), None
with cl.stream("POST", "/v1/chat/completions", headers={"X-User-Id": "e2e-user"},
               json={"model": "qwen2.5-1.5b-instruct", "stream": True, "messages": [{"role": "user", "content": "Akış testi"}]}) as r:
    ok(r.status_code == 200 and "text/event-stream" in r.headers.get("content-type", ""), "SSE başladı")
    for line in r.iter_lines():
        if line.startswith("data:") and "[DONE]" not in line:
            d = json.loads(line[5:])
            if d.get("usage"):
                usage = d["usage"]
            elif d["choices"] and d["choices"][0]["delta"].get("content"):
                chunks += 1
                ttft = ttft or time.time() - t0
ok(chunks > 10, f"{chunks} parça alındı, ilk parça {ttft:.2f} sn")
ok(usage is not None, "usage bilgisi akış sonunda geldi")

step("istemci erken koparsa kaynak sızıntısı olmamalı")
with cl.stream("POST", "/v1/chat/completions", json={"model": "qwen2.5-1.5b-instruct", "stream": True,
               "messages": [{"role": "user", "content": "Kes beni"}]}) as r:
    n = 0
    for line in r.iter_lines():
        n += 1
        if n >= 3:
            break
time.sleep(2.0)
infl = overview()["kpi"]["inflight"]
ok(infl == 0, f"kopan istek sonrası inflight={infl}")

step("soğuk başlatma: hiç replikası olmayan model")
t0 = time.time()
r = chat("qwen2.5-7b-instruct", "Soğuk başlatma denemesi")
dt = time.time() - t0
ok(r.status_code == 200, f"HTTP {r.status_code} ({dt:.1f} sn bekledi) → düğüm: {r.headers.get('x-orkestra-node')}")
ok(float(r.headers.get("x-orkestra-wait-ms", 0)) > 1000, "istek replika hazır olana kadar bekletildi")
dec = cl.get("/api/autoscaler").json()["decisions"]
ok(any(x["kind"] == "scale_up" and "qwen2.5-7b" in x["label"] for x in dec), "otomatik ölçekleyici kararı kaydetti")

step("servis adı: site sabit ad kullanır, model arkada değişebilir")
r = chat("sohbet", "Servis adıyla istek")
ok(r.status_code == 200 and r.json()["model"] == "sohbet" and r.headers.get("x-orkestra-model") in ("qwen2.5-7b-instruct", "qwen2.5-1.5b-instruct"),
   f"'sohbet' servisi → {r.headers.get('x-orkestra-model')}")

step("yönetici olmayan istek / anahtar yönetimi")
ok(pub.post("/v1/chat/completions", json={"model": "x", "messages": []}).status_code == 401, "anahtarsız genel uç reddedildi")
ok(pub.get("/api/overview").status_code == 404, "genel portta yönetim API'si yok")
kr = cl.post("/api/keys", json={"name": "e2e", "rpm": 3}).json()
key = kr["key"]
H = {"Authorization": f"Bearer {key}", "X-User-Id": "musteri-42"}
r = pub.post("/v1/chat/completions", headers=H, json={"model": "qwen2.5-1.5b-instruct", "messages": [{"role": "user", "content": "Selam"}]})
ok(r.status_code == 200, f"geçerli anahtar kabul edildi (HTTP {r.status_code})")
codes = [pub.post("/v1/chat/completions", headers=H, json={"model": "qwen2.5-1.5b-instruct", "messages": [{"role": "user", "content": "Selam"}]}).status_code for _ in range(4)]
ok(429 in codes, f"hız sınırı çalıştı: {codes}")
lst = pub.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
ok(lst.status_code == 200 and {"sohbet", "konusma-metin", "metin-konusma"} <= {x["id"] for x in lst.json()["data"]}, "site yalnızca servis adlarını görüyor: " + ", ".join(x["id"] for x in lst.json()["data"]))
key2 = cl.post("/api/keys", json={"name": "paket", "rpm": 0}).json()
H2 = {"Authorization": f"Bearer {key2['key']}", "X-User-Plan": "free", "X-User-Id": "ucretsiz-1"}
codes = [pub.post("/v1/chat/completions", headers=H2, json={"model": "sohbet-hizli", "messages": [{"role": "user", "content": str(i)}], "max_tokens": 3}).status_code for i in range(12)]
ok(codes.count(200) <= 10 and 429 in codes, f"ücretsiz paketin dakikalık sınırı çalıştı: {codes}")
u = pub.get("/v1/usage", params={"user": "ucretsiz-1"}, headers=H2).json()
ok(u["plan"] == "free" and u["today"]["tokens"] > 0, f"kullanım sitede okunabiliyor: {u['today']}")
import base64, hashlib, hmac
payload = base64.urlsafe_b64encode(json.dumps({"k": key2["id"], "u": "tarayici-1", "p": "pro", "exp": int(time.time()) + 60}).encode()).rstrip(b"=").decode()
sig = base64.urlsafe_b64encode(hmac.new(key2["sign_secret"].encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
r = pub.post("/v1/chat/completions", headers={"Authorization": f"Bearer ut1.{payload}.{sig}"}, json={"model": "sohbet-hizli", "messages": [{"role": "user", "content": "jeton"}]})
ok(r.status_code == 200, f"imzalı kullanıcı jetonu kabul edildi (HTTP {r.status_code})")
ok(pub.post("/v1/chat/completions", headers={"Authorization": f"Bearer ut1.{payload}.yanlis"}, json={"model": "sohbet-hizli", "messages": [{"role": "user", "content": "x"}]}).status_code == 401, "imzası bozuk jeton reddedildi")

step("sohbet kaydı")
us = cl.get("/api/users").json()
ok(any(u["id"] == "musteri-42" for u in us), "kullanıcı 'musteri-42' kaydedildi")
cv = cl.get("/api/conversations", params={"user": "musteri-42"}).json()
ok(len(cv) >= 1, f"konuşma kaydı var ({len(cv)})")
full = cl.get(f"/api/conversations/{cv[0]['id']}").json() if cv else {}
ok(bool(full.get("messages")) and full["messages"][-1]["role"] == "assistant", "konuşma metni asistan yanıtıyla birlikte okunuyor")

step("güç yönetimi: uyandır / uyut")
l3 = node("laptop-3")
ok(l3["status"] == "sleeping", f"laptop-3 başlangıçta uykuda ({l3['status']})")
r = cl.post(f"/api/nodes/{l3['id']}/power", json={"action": "wake"})
ok(r.status_code == 200, "wake komutu kabul edildi")
up = wait_for(lambda: node("laptop-3")["status"] == "online", 60)
ok(bool(up), "laptop-3 uyandı ve çevrimiçi oldu (WoL simülasyonu)")
r = cl.post(f"/api/nodes/{l3['id']}/power", json={"action": "sleep"})
ok(r.status_code == 200, "sleep komutu kabul edildi")
sl = wait_for(lambda: node("laptop-3")["status"] == "sleeping", 40)
ok(bool(sl), "laptop-3 uykuya geçti")

step("hata enjeksiyonu: süreç çökmesi")
dep = next(d for d in overview()["deployments"] if d["model"] == "qwen2.5-1.5b-instruct")
nid = next(n["id"] for n in overview()["nodes"] if n["name"] == dep["node"])
cl.post("/api/sim/inject", json={"node_id": nid, "kind": "crash"})
bad = wait_for(lambda: any(d["fail"] >= 1 or d["state"] != "ready" for d in overview()["deployments"] if d["id"] == dep["id"]), 20)
ok(bool(bad), "çöken süreç fark edildi")
back = wait_for(lambda: next(d for d in overview()["deployments"] if d["id"] == dep["id"])["state"] == "ready", 90)
ok(bool(back), "uzlaştırıcı süreci yeniden başlattı (kendi kendini onarma)")

step("ses: STT ve TTS")
r = cl.post("/v1/audio/speech", json={"model": "metin-konusma", "input": "Merhaba dünya, bu bir deneme."})
ok(r.status_code == 200 and r.content[:4] == b"RIFF", f"TTS WAV üretti ({len(r.content)} bayt)")
r = cl.post("/v1/audio/transcriptions", data={"model": "konusma-metin"}, files={"file": ("a.webm", b"\x1a\x45\xdf\xa3" * 5000, "audio/webm")})
ok(r.status_code == 200 and "text" in r.json(), "STT metin döndürdü: " + str(r.json())[:80])

step("distillation")
sid = cl.post("/api/distill/sets", json={"name": "deneme", "text": "1. Türkiye'nin başkenti neresi?\n2. Python nedir?\n- Fotosentez nedir?\nGit rebase nedir?"}).json()["id"]
jid = cl.post("/api/distill/jobs", json={"name": "e2e", "sets": [sid], "teacher": "qwen2.5-7b-instruct", "forced": True,
                                          "max_tokens": 80, "concurrency": 2}).json()["id"]
done = wait_for(lambda: next(j for j in cl.get("/api/distill/status").json()["jobs"] if j["id"] == jid)["status"] == "done", 90)
ok(bool(done), "distillation işi tamamlandı")
items = cl.get(f"/api/distill/jobs/{jid}/items").json()
ok(len(items) == 4 and all(i["status"] == "done" for i in items), f"4 çift üretildi: {[i['status'] for i in items]}")

step("HuggingFace senkronu (dry-run)")
cl.put("/api/settings", json={"hf.chat_repo": "ornek/sohbet", "hf.dry_run": True, "hf.idle_min": 0})
r = cl.post("/api/hf/sync").json()
ok(r["status"] == "dry-run" and r["records"] > 0, f"dry-run: {r['records']} kayıt hazırlandı → {r['message'][:60]}")
st = cl.get("/api/hf/status").json()
ok(len(st["outbox_files"]) >= 1, f"outbox dosyaları: {st['outbox_files']}")

step("uyuyan düğüm talep gelince otomatik uyandırılır")
online = [n for n in overview()["nodes"] if n["status"] == "online"]
for n in online:  # yalnızca uyuyan düğümler aday kalsın
    cl.post(f"/api/nodes/{n['id']}/power", json={"action": "disable"})
time.sleep(4)
t0 = time.time()
r = chat("qwen2.5-1.5b-instruct", "uyandırma testi")
woke = [n for n in overview()["nodes"] if n["status"] == "online" and n["enabled"]]
ok(r.status_code == 200 and bool(woke), f"HTTP {r.status_code}; {time.time() - t0:.0f} sn sonra uyanan düğüm: {[n['name'] for n in woke]}")
ok(r.headers.get("x-orkestra-node") in [n["name"] for n in woke], "istek uyanan düğüm tarafından karşılandı: " + r.headers.get("x-orkestra-node", ""))
for n in online:
    cl.post(f"/api/nodes/{n['id']}/power", json={"action": "enable"})

step("ısı koruması")
cl.post("/api/sim/speed", json={"time_scale": 6})
target = next(n for n in overview()["nodes"] if n["status"] == "online" and n["gpus"])
cl.post("/api/sim/inject", json={"node_id": target["id"], "kind": "overheat"})
hot = wait_for(lambda: node(target["name"])["thermal"] == "crit", 60)
ok(bool(hot), f"{target['name']} aşırı ısındı ve 'crit' oldu ({node(target['name'])['max_temp']}°C)")
time.sleep(4)
stopped = [d for d in node(target["name"])["deployments"] if d["desired"] == "running" and d["state"] == "ready"]
ok(not stopped, "ısınan düğümde hazır replika kalmadı (korumaya alındı)")
cl.post("/api/sim/inject", json={"node_id": target["id"], "kind": "recover"})
cool = wait_for(lambda: node(target["name"])["thermal"] == "ok", 90)
ok(bool(cool), "soğuyunca düğüm tekrar kullanıma açıldı")
cl.post("/api/sim/speed", json={"time_scale": 1})

step("olaylar")
ev = cl.get("/api/events", params={"limit": 8}).json()
for e in ev:
    print("   ", time.strftime("%H:%M:%S", time.localtime(e["ts"])), e["level"], e["source"], e["message"][:110])

print("\n" + ("TÜM KONTROLLER GEÇTİ" if not FAILS else f"{len(FAILS)} BAŞARISIZ KONTROL:"))
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)
