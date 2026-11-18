"""Simülatör: gerçek donanım olmadan tüm akışı denemek için sahte düğümler.

* SimNode        : sahte makine (güç durumları, sıcaklık/güç fiziği, süreçler)
* SimAgentClient : cluster.py'nin beklediği ajan arayüzü
* build_router   : /_sim/<düğüm>/<port>/v1/...  OpenAI uyumlu sahte çıkarım uçları (yalnızca loopback)
* DemoTraffic    : arayüzü canlandıran sahte kullanıcı trafiği
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import random
import struct
import time
import uuid
import wave
from collections import deque

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .util import clamp, estimate_vram_gb, jdump, jload, log, now

AMBIENT = 33.0

DEMO_NODES = [
    # name, host, role, tags, spec, extra
    ("ana-pc", "192.168.50.10", "main", ["main", "gpu"], dict(
        cpu_model="AMD Ryzen 9 7900X", cores=12, threads=24, ram_gb=32, speed=1.3, cpu_speed=0.35, cpu_tdp=120,
        gpus=[dict(name="NVIDIA GeForce RTX 4070", vram_gb=12, tdp=200)], initial="on", mac="02:00:00:00:00:10"),
        dict(always_on=1, allow_sleep=0)),
    ("laptop-1", "192.168.50.11", "laptop", ["laptop", "gpu"], dict(
        cpu_model="Intel Core i7-12700H", cores=14, threads=20, ram_gb=16, speed=0.7, cpu_speed=0.18, cpu_tdp=45, laptop=True,
        gpus=[dict(name="NVIDIA GeForce RTX 3060 Laptop", vram_gb=6, tdp=80)], initial="on", mac="02:00:00:00:00:11"), {}),
    ("laptop-2", "192.168.50.12", "laptop", ["laptop", "gpu"], dict(
        cpu_model="Intel Core i7-12700H", cores=14, threads=20, ram_gb=16, speed=0.7, cpu_speed=0.18, cpu_tdp=45, laptop=True,
        gpus=[dict(name="NVIDIA GeForce RTX 3060 Laptop", vram_gb=6, tdp=80)], initial="on", mac="02:00:00:00:00:12"), {}),
    ("laptop-3", "192.168.50.13", "laptop", ["laptop", "gpu"], dict(
        cpu_model="Intel Core i7-11800H", cores=8, threads=16, ram_gb=16, speed=0.55, cpu_speed=0.16, cpu_tdp=45, laptop=True,
        gpus=[dict(name="NVIDIA GeForce RTX 3050 Ti Laptop", vram_gb=4, tdp=60)], initial="suspended", mac="02:00:00:00:00:13"), {}),
    ("laptop-4", "192.168.50.14", "laptop", ["laptop", "gpu"], dict(
        cpu_model="AMD Ryzen 7 5800H", cores=8, threads=16, ram_gb=16, speed=0.7, cpu_speed=0.17, cpu_tdp=45, laptop=True,
        gpus=[dict(name="NVIDIA GeForce RTX 3060 Laptop", vram_gb=6, tdp=80)], initial="suspended", mac="02:00:00:00:00:14"), {}),
    ("laptop-5", "192.168.50.15", "laptop", ["laptop", "cpu"], dict(
        cpu_model="Intel Core i5-1135G7", cores=4, threads=8, ram_gb=16, speed=0.15, cpu_speed=0.15, cpu_tdp=28, laptop=True,
        gpus=[], initial="suspended", mac="02:00:00:00:00:15"), {}),
    ("laptop-6", "192.168.50.16", "laptop", ["laptop", "cpu"], dict(
        cpu_model="Intel Core i5-1135G7", cores=4, threads=8, ram_gb=8, speed=0.12, cpu_speed=0.12, cpu_tdp=28, laptop=True,
        gpus=[], initial="suspended", mac="02:00:00:00:00:16", link_mbps=100), {}),
]

# Demo kataloğu: dosyalar ana PC'de durur, switch üzerinden makinelere dağıtılır
DEMO_MODELS = [
    dict(name="qwen2.5-1.5b-instruct", repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M", runtime="llamacpp", kind="llm", ctx=8192,
         vram_gb=2.2, gpu_count=1, cpu_ok=1, min_replicas=1, max_replicas=3, priority=3, max_concurrency=4, idle_unload_min=20, file_gb=1.1, stored=1),
    dict(name="qwen2.5-7b-instruct", repo="Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M", runtime="llamacpp", kind="llm", ctx=8192,
         vram_gb=5.3, gpu_count=1, cpu_ok=1, min_replicas=0, max_replicas=4, priority=5, max_concurrency=4, idle_unload_min=10, file_gb=4.4, stored=1),
    dict(name="llama-3.1-8b-instruct", repo="bartowski/Llama-3.1-8B-Instruct-GGUF:Q4_K_M", runtime="llamacpp", kind="llm", ctx=16384,
         vram_gb=5.8, gpu_count=1, cpu_ok=0, min_replicas=0, max_replicas=3, priority=6, max_concurrency=4, idle_unload_min=10, file_gb=4.7, stored=0,
         prefer_tags=["main"]),
    dict(name="qwen2.5-14b-instruct", repo="Qwen/Qwen2.5-14B-Instruct-GGUF:Q4_K_M", runtime="llamacpp", kind="llm", ctx=8192,
         vram_gb=10.2, gpu_count=1, cpu_ok=0, min_replicas=0, max_replicas=1, priority=4, max_concurrency=2, idle_unload_min=5, file_gb=8.9, stored=1,
         prefer_tags=["main"], notes="Distillation öğretmen modeli"),
    dict(name="whisper-large-v3", repo="ggerganov/whisper.cpp", file_glob="ggml-large-v3-turbo-q5_0.bin", runtime="whispercpp", kind="stt", ctx=0,
         vram_gb=1.8, gpu_count=1, cpu_ok=1, min_replicas=0, max_replicas=1, priority=5, max_concurrency=2, idle_unload_min=10, file_gb=0.55, stored=1),
    dict(name="tts-tr", repo="rhasspy/piper-voices", file_glob="tr/tr_TR/fettah/medium/*", runtime="piper", kind="tts", ctx=0,
         vram_gb=0.0, ram_gb=0.8, gpu_count=0, cpu_ok=1, min_replicas=0, max_replicas=1, priority=4, max_concurrency=4, idle_unload_min=10, file_gb=0.06, stored=1),
]
DEMO_SERVICES = [("sohbet", "llm", ["qwen2.5-7b-instruct", "qwen2.5-1.5b-instruct"], 1, {}), ("sohbet-hizli", "llm", ["qwen2.5-1.5b-instruct"], 0, {}),
                 ("konusma-metin", "stt", ["whisper-large-v3"], 1, {"language": "tr"}), ("metin-konusma", "tts", ["tts-tr"], 1, {"response_format": "wav"})]


def _fake_sha(s: str) -> str:
    return hashlib.sha256(("sim:" + s).encode()).hexdigest()


def demo_manifest(m: dict) -> dict:
    size = int(float(m.get("file_gb") or 0.5) * 2**30)
    if m["runtime"] == "piper":
        base = "tr/tr_TR/fettah/medium/tr_TR-fettah-medium.onnx"
        files = [{"path": base, "size": size, "sha256": _fake_sha(base)}, {"path": base + ".json", "size": 5120, "sha256": _fake_sha(base + ".json")}]
        main = base
    elif m["runtime"] == "whispercpp":
        files, main = [{"path": m["file_glob"], "size": size, "sha256": _fake_sha(m["file_glob"])}], m["file_glob"]
    else:
        p = m["repo"].split("/")[-1].split(":")[0].replace("-GGUF", "").lower() + "-q4_k_m.gguf"
        files, main = [{"path": p, "size": size, "sha256": _fake_sha(p)}], p
    sig = hashlib.sha256(m["name"].encode()).hexdigest()[:10]
    return {"repo": m["repo"].split(":")[0], "revision": "a1b2c3d4e5f6", "files": files, "main": main, "total": sum(f["size"] for f in files), "key": f"{m['name'][:48]}-{sig}"}


_OPEN = ["Elbette!", "Tabii,", "Memnuniyetle.", "Şöyle özetleyebilirim:", "Kısaca:", "Güzel soru."]
_FILL = [
    "Bu yanıt simülatör tarafından üretildi; gerçek bir model bağlandığında burada modelin cevabı görünecek.",
    "İstek yönlendirme, kuyruklama, yük dengeleme ve kayıt akışları bu metinle sınanıyor.",
    "Düğümün sıcaklık ve GPU kullanımı bu sırada canlı olarak panoda izlenebilir.",
    "Aynı soru yoğun saatlerde başka bir replikaya yönlendirilebilir.",
    "Konuşma geçmişi kullanıcı kimliğiyle birlikte kaydedilir ve zamanlanmış görevle HuggingFace'e gönderilir.",
    "Yoğunluk düşünce boştaki makineler uykuya alınır, talep gelince otomatik uyandırılır.",
]

DEMO_PROMPTS = [
    "Türkiye'nin başkenti neresidir ve kaç yıldır başkenttir?", "Python'da liste ile demet arasındaki fark nedir?",
    "Bana 3 maddelik kısa bir hafta sonu planı yaz.", "Fotosentez nasıl çalışır, basitçe anlatır mısın?",
    "SQL'de INNER JOIN ile LEFT JOIN farkı nedir?", "Evde yapabileceğim kolay bir tatlı tarifi ver.",
    "REST ile GraphQL arasında nasıl seçim yaparım?", "Bir iş görüşmesinde kendimi nasıl tanıtmalıyım?",
    "Kuantum bilgisayarlar neden önemli?", "Linux'ta bir portu hangi süreç kullanıyor, nasıl bulurum?",
    "Docker ile sanal makine arasındaki fark nedir?", "İngilizce 'serendipity' kelimesi ne demek?",
    "Bir e-ticaret sitesi için SEO ipuçları verir misin?", "Git'te rebase ile merge farkını örnekle anlat.",
    "Uyku düzenimi nasıl düzeltebilirim?", "Transformer mimarisinde dikkat mekanizması ne işe yarar?",
    "Excel'de DÜŞEYARA yerine hangi fonksiyon kullanılabilir?", "Kısa bir motivasyon konuşması yazar mısın?",
    "Küçük bir işletme için bütçe nasıl hazırlanır?", "Ağ anahtarında (switch) VLAN ne işe yarar?",
]


def compose(prompt: str, model: str, node: str) -> str:
    h = int(hashlib.md5(prompt.encode("utf-8", "ignore")).hexdigest(), 16)
    q = " ".join(str(prompt).split())[:120]
    parts = [_OPEN[h % len(_OPEN)], f"“{q}” isteğini {model} modeliyle {node} düğümünde yanıtlıyorum."]
    for i in range(2 + h % 3):
        parts.append(_FILL[(h >> (i * 4 + 3)) % len(_FILL)])
    return " ".join(parts)


def make_wav(text: str, sr: int = 16000) -> bytes:
    n = max(1, min(len(text), 160))
    dur = min(9.0, 0.5 + n * 0.055)
    total = int(sr * dur)
    frames = bytearray()
    for i in range(total):
        t = i / sr
        syl = int(t * 6)
        ch = ord(text[syl % len(text)]) if text else 65
        f = 170 + 35 * ((syl * 7 + ch) % 6)
        env = 0.5 * (1 - math.cos(2 * math.pi * ((t * 6) % 1)))
        frames += struct.pack("<h", int(8000 * env * math.sin(2 * math.pi * f * t)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# --------------------------------------------------------------------------- sahte düğüm
class SimProc:
    def __init__(self, spec: dict, node: "SimNode"):
        self.id = spec["id"]
        self.argv = spec["argv"]
        self.port = int(spec["port"])
        self.env = spec.get("env", {})
        self.started = now()
        self.state = "running"
        self.rc = None
        repo = ""
        for flag in ("-hf", "-m", "--model", "serve", "--model-path", "--model-id"):
            if flag in self.argv:
                i = self.argv.index(flag)
                if i + 1 < len(self.argv):
                    repo = self.argv[i + 1]
                    break
        declared = (spec.get("meta") or {}).get("size_gb")
        self.size_gb = float(declared) if declared else (estimate_vram_gb(repo) if repo else 2.0)
        cv = self.env.get("CUDA_VISIBLE_DEVICES")
        self.gpus = [int(x) for x in cv.split(",") if x.strip().isdigit()] if cv else []
        self.load_s = clamp(self.size_gb * 0.9 / max(node.speed if self.gpus else node.cpu_speed, 0.2), 4.0, 26.0)
        self.inflight = 0
        self.served = 0
        self.fail_at = 0.0
        self.log: deque = deque(maxlen=300)
        self.log.append(f"{time.strftime('%H:%M:%S')} başlatıldı: {' '.join(self.argv)[:200]}")
        # bellek yetmezse kısa süre sonra çökmeyi simüle et
        vram_total = sum(node.spec["gpus"][g]["vram_gb"] for g in self.gpus if g < len(node.spec["gpus"]))
        if self.gpus and self.size_gb > vram_total + 0.3:
            self.fail_at = self.started + 3.0
        elif not self.gpus and self.size_gb > node.spec["ram_gb"] - 2:
            self.fail_at = self.started + 3.0

    def ready(self) -> bool:
        return self.state == "running" and now() - self.started >= self.load_s

    def progress(self) -> float:
        return clamp((now() - self.started) / max(0.1, self.load_s), 0.0, 1.0)


class SimNode:
    def __init__(self, world: "SimWorld", nid: int, name: str, spec: dict):
        self.world, self.id, self.name, self.spec = world, nid, name, spec
        self.speed = float(spec.get("speed", 1.0))
        self.cpu_speed = float(spec.get("cpu_speed", 0.2))
        self.state = spec.get("initial", "on")  # on | suspended | booting | off
        self.boot_until = 0.0
        self.boot_ts = now() - random.uniform(3600, 86400 * 5)
        self.pending: tuple | None = None
        self.procs: dict[str, SimProc] = {}
        n_g = len(spec["gpus"])
        self.gpu_t = [AMBIENT + 4] * n_g
        self.cpu_t = AMBIENT + 8
        self.bias = 0.0
        self.bias_until = 0.0
        self.gpu_lost = False
        self._phys_t = now()
        self._pid = random.randint(2000, 9000)
        # ajan deposunun karşılığı
        self.f_blobs: dict[str, int] = {}
        self.f_rt: dict[str, dict] = {}
        self.f_models: dict[str, dict] = {}
        self.f_jobs: dict[str, dict] = {}
        self.f_rev = 1

    # ---- durum --------------------------------------------------------------
    def is_on(self) -> bool:
        self._tick()
        return self.state == "on"

    def _tick(self) -> None:
        t = now()
        if self.pending and t >= self.pending[1]:
            action = self.pending[0]
            self.pending = None
            self.procs.clear()
            if action == "suspend":
                self.state = "suspended"
            elif action == "poweroff":
                self.state = "off"
            elif action == "reboot":
                self.state, self.boot_until = "booting", t + self.spec.get("boot_s", 7.0)
        if self.state == "booting" and t >= self.boot_until:
            self.state, self.boot_ts = "on", t
            self.cpu_t = AMBIENT + 6
            self.gpu_t = [AMBIENT + 3] * len(self.gpu_t)
        if self.bias and t >= self.bias_until:
            self.bias = 0.0
        for p in self.procs.values():
            if p.state == "running" and p.fail_at and t >= p.fail_at:
                p.state, p.rc = "exited", 1
                p.log.append("CUDA error: out of memory (simülasyon)" if p.gpus else "Cannot allocate memory (simülasyon)")

    def require_on(self) -> None:
        if not self.is_on():
            raise ConnectionError(f"{self.name}: ulaşılamıyor ({self.state})")

    def proc_by_port(self, port: int) -> SimProc | None:
        for p in self.procs.values():
            if p.port == port:
                return p
        return None

    # ---- ajan deposu ----------------------------------------------------------
    def f_tick(self) -> None:
        t = now()
        rate = float(self.spec.get("link_mbps", 1000)) * 110000 * self.world.time_scale
        for j in self.f_jobs.values():
            if j["state"] != "running":
                continue
            if j["kind"] == "fetch":
                j["bytes"] = min(j["total"], j["bytes"] + int((t - j["_t"]) * rate))
                j["bps"] = int(rate)
                j["_t"] = t
                if j["bytes"] >= j["total"]:
                    self.f_blobs[j["_sha"]] = j["total"]
                    self._f_done(j, t)
            elif t >= j["_until"]:
                j["_fn"]()
                self._f_done(j, t)

    def _f_done(self, j: dict, t: float) -> None:
        j.update(state="done", ended=t, bps=0)
        self.f_rev += 1

    def f_view(self, j: dict) -> dict:
        return {k: v for k, v in j.items() if not k.startswith("_")}

    def f_state(self) -> dict:
        self.require_on()
        self.f_tick()
        used = sum(self.f_blobs.values()) / 2**30
        return {"root": "/var/lib/orkestra/store", "sep": "/", "free_gb": round(420.0 - used, 2), "total_gb": 512.0, "blobs": dict(self.f_blobs),
                "runtimes": self.f_rt, "models": self.f_models, "jobs": [self.f_view(j) for j in self.f_jobs.values()], "rev": self.f_rev}

    def f_start(self, jid: str, kind: str, total: int, secs: float, fn) -> dict:
        t = now()
        j = {"id": jid, "kind": kind, "state": "running", "bytes": 0, "total": total, "bps": 0, "error": "", "code": "", "src": "", "step": "", "started": t, "ended": 0,
             "_t": t, "_until": t + secs / max(0.2, self.world.time_scale), "_fn": fn}
        self.f_jobs[jid] = j
        return j

    def f_fetch(self, spec: dict) -> dict:
        self.require_on()
        self.f_tick()
        sha = spec["sha256"]
        jid = "fetch:" + sha[:16]
        if sha in self.f_blobs:
            return {"id": jid, "state": "done", "bytes": spec["size"], "total": spec["size"]}
        cur = self.f_jobs.get(jid)
        if cur and cur["state"] == "running":
            return self.f_view(cur)
        j = self.f_start(jid, "fetch", int(spec["size"]), 0, None)
        j["_sha"] = sha
        return self.f_view(j)

    def f_install(self, spec: dict, build: bool = False) -> dict:
        self.require_on()
        rid = spec["id"]
        exe = spec.get("exe_name") or "llama-server"

        def fin():
            self.f_rt[rid] = {"dir": f"/var/lib/orkestra/store/runtimes/{rid}", "entry": f"/var/lib/orkestra/store/runtimes/{rid}/{exe}", "sha256": spec["sha256"], "at": now()}
        return self.f_view(self.f_start(("build:" if build else "install:") + rid, "build" if build else "install", 0, 25.0 if build else 1.0, fin))

    def f_link(self, spec: dict) -> dict:
        self.require_on()
        d = f"/var/lib/orkestra/store/models/{spec['key']}"
        self.f_models[spec["key"]] = {"dir": d, "files": [f["sha256"] for f in spec["files"]], "at": now()}
        self.f_rev += 1
        return {"ok": True, "dir": d}

    def f_remove(self, spec: dict) -> dict:
        self.require_on()
        for k in spec.get("models") or []:
            self.f_models.pop(k, None)
        for k in spec.get("runtimes") or []:
            self.f_rt.pop(k, None)
        for k in spec.get("blobs") or []:
            self.f_blobs.pop(k, None)
        self.f_rev += 1
        return {"ok": True, "removed": [], "kept": []}

    # ---- ajan işlevleri -----------------------------------------------------
    def info(self) -> dict:
        s = self.spec
        rts = {"llama-server": "/usr/local/bin/llama-server", "ollama": "/usr/local/bin/ollama", "docker": "/usr/bin/docker",
               "python3": "/usr/bin/python3"}
        if s["gpus"]:
            rts["vllm"] = "/opt/venv/bin/vllm"
        return {
            "agent": "0.3.0", "node_id": hashlib.sha256(self.name.encode()).hexdigest()[:20], "platform": "linux-x86_64", "python_exe": "/usr/bin/python3",
            "laptop": bool(s.get("laptop")), "tools": {"cmake": True, "gcc": True, "ffmpeg": True, "git": True, "ninja": True},
            "gpu_caps": {"nvidia": bool(s["gpus"]), "driver": "550.120" if s["gpus"] else "", "cuda": "12.4" if s["gpus"] else "", "vulkan": False,
                         "backends": (["cuda"] if s["gpus"] else []) + ["cpu"]},
            "hostname": self.name, "os": s.get("os", "Ubuntu 24.04 LTS"),
            "cpu": {"model": s["cpu_model"], "cores": s["cores"], "threads": s["threads"]},
            "ram_gb": s["ram_gb"],
            "gpus": [{"index": i, "name": g["name"], "vram_gb": g["vram_gb"]} for i, g in enumerate(s["gpus"])],
            "runtimes": rts, "macs": {"eno1": s["mac"]}, "primary_mac": s["mac"], "boot_ts": self.boot_ts,
        }

    def tps(self, p: SimProc) -> float:
        base = 40.0 * (self.speed if p.gpus else self.cpu_speed)
        return max(1.5, base / (1 + 0.1 * max(0, p.inflight - 1))) * self.world.time_scale

    def metrics(self) -> dict:
        self.require_on()
        t = now()
        dt = clamp(t - self._phys_t, 0.0, 15.0)
        self._phys_t = t
        k = 1 - math.exp(-dt * max(0.2, self.world.time_scale) / 22.0)
        s = self.spec
        running = [p for p in self.procs.values() if p.state == "running"]
        gpus = []
        gpu_util_all = []
        for i, g in enumerate(s["gpus"]):
            if self.gpu_lost and i == len(s["gpus"]) - 1:
                continue
            util, mem = 0.0, 0.4
            for p in running:
                if i in p.gpus:
                    prog = p.progress()
                    mem += p.size_gb / max(1, len(p.gpus)) * prog
                    util += (12 + 30 * p.inflight) if p.ready() else 20 * (1 - prog) + 6
            util = clamp(util + random.uniform(-2, 3) if util else random.uniform(0, 2), 0, 99)
            target = AMBIENT + util * (0.5 if s.get("laptop") else 0.42) + (8 if s.get("laptop") else 0) + self.bias
            self.gpu_t[i] += (target - self.gpu_t[i]) * k
            tdp = g.get("tdp", 150)
            idle = tdp * 0.07
            gpus.append({
                "index": i, "name": g["name"], "util": round(util, 1), "mem_used_gb": round(min(mem, g["vram_gb"]), 2),
                "mem_total_gb": g["vram_gb"], "temp": round(self.gpu_t[i] + random.uniform(-0.4, 0.4), 1),
                "power_w": round(idle + (tdp - idle) * util / 100 + random.uniform(-2, 2), 1),
            })
            gpu_util_all.append(util)
        cpu = 3 + random.uniform(0, 3)
        ram = 1.6 + random.uniform(0, 0.3)
        for p in running:
            if p.gpus:
                cpu += 4 * p.inflight + 2
                ram += 1.2
            else:
                cpu += (22 + 28 * p.inflight) if p.ready() else 45
                ram += p.size_gb * p.progress()
        cpu = clamp(cpu, 0, 99)
        target = AMBIENT + 6 + cpu * 0.32 + (8 if s.get("laptop") else 0) + self.bias * 0.6
        self.cpu_t += (target - self.cpu_t) * k
        infl = sum(p.inflight for p in running)
        disk_total = 512.0
        return {
            "ts": t, "cpu": round(cpu, 1), "load1": round(cpu / 100 * s["threads"] * 0.5, 2),
            "mem": {"total_gb": s["ram_gb"], "used_gb": round(min(ram, s["ram_gb"]), 2),
                    "pct": round(min(ram, s["ram_gb"]) / s["ram_gb"] * 100, 1)},
            "cpu_temp": round(self.cpu_t + random.uniform(-0.5, 0.5), 1),
            "gpus": gpus, "gpu_error": "GPU has fallen off the bus (simülasyon)" if self.gpu_lost else "",
            "disk": {"total_gb": disk_total, "used_gb": 118.0, "pct": round(118 / disk_total * 100, 1)},
            "net": {"rx_bps": round(infl * 18000 + random.uniform(0, 3000) + sum(j["bps"] for j in self.f_jobs.values() if j["state"] == "running")),
                    "tx_bps": round(infl * 42000 + random.uniform(0, 3000)), "ip": self.world.ctx.cluster.nodes[self.id].host if self.id in self.world.ctx.cluster.nodes else "",
                    "iface": "enp3s0", "speed_mbps": s.get("link_mbps", 1000), "duplex": "full", "wifi": False, "up": True, "mtu": 1500, "errors": 0, "drops": 0},
            "power": {"ac": True, "battery_pct": 100 if s.get("laptop") else None, "status": "Full" if s.get("laptop") else ""},
            "fabric": self._f_summary(),
            "uptime_s": int(t - self.boot_ts),
            "cpu_power_w": round(6 + cpu * s.get("cpu_tdp", 45) / 100 * 0.8, 1),
            "procs": [{"id": p.id, "pid": self._fake_pid(p), "state": p.state, "rc": p.rc, "started": p.started, "port": p.port}
                      for p in self.procs.values()],
        }

    def _f_summary(self) -> dict:
        self.f_tick()
        return {"rev": self.f_rev, "free_gb": round(420.0 - sum(self.f_blobs.values()) / 2**30, 2), "serving": 0, "served": 0,
                "jobs": [self.f_view(j) for j in self.f_jobs.values() if j["state"] == "running"]}

    def _fake_pid(self, p: SimProc) -> int:
        return 2000 + (hash(p.id) % 7000)

    def launch(self, spec: dict) -> dict:
        self.require_on()
        pid = spec["id"]
        if pid in self.procs and self.procs[pid].state == "running":
            raise RuntimeError("süreç zaten çalışıyor")
        port = int(spec["port"])
        for p in self.procs.values():
            if p.port == port and p.state == "running":
                raise RuntimeError(f"port {port} kullanımda")
        self.procs[pid] = SimProc(spec, self)
        return {"ok": True, "id": pid}

    def stop(self, pid: str) -> dict:
        p = self.procs.pop(pid, None)
        if p:
            p.state = "exited"
        return {"ok": True}

    def crash_all(self) -> None:
        for p in self.procs.values():
            if p.state == "running":
                p.state, p.rc = "exited", 137
                p.log.append("Killed (simülasyon: ani çökme)")


class SimAgentClient:
    def __init__(self, world: "SimWorld", nid: int):
        self.world, self.nid = world, nid

    def _n(self) -> SimNode:
        n = self.world.nodes.get(self.nid)
        if n is None:
            raise ConnectionError("simüle edilmiş düğüm yok")
        return n

    async def info(self):
        n = self._n()
        n.require_on()
        return n.info()

    async def metrics(self):
        await asyncio.sleep(random.uniform(0.0, 0.03))
        return self._n().metrics()

    async def launch(self, spec):
        return self._n().launch(spec)

    async def stop(self, proc_id, grace=15):
        n = self._n()
        n.require_on()
        return n.stop(proc_id)

    async def logs(self, proc_id, tail=200):
        n = self._n()
        n.require_on()
        p = n.procs.get(proc_id)
        return "\n".join(list(p.log)[-tail:]) if p else "(süreç yok)"

    async def power(self, action):
        n = self._n()
        n.require_on()
        n.pending = (action, now() + 1.5)
        return {"ok": True}

    async def fabric_state(self):
        return self._n().f_state()

    async def fabric_jobs(self):
        n = self._n()
        n.require_on()
        n.f_tick()
        return {"jobs": [n.f_view(j) for j in n.f_jobs.values()], "rev": n.f_rev}

    async def fabric_log(self, jid, tail=200):
        return "(simülasyon: derleme günlüğü yok)"

    async def fabric_fetch(self, spec):
        return self._n().f_fetch(spec)

    async def fabric_install(self, spec):
        return self._n().f_install(spec)

    async def fabric_build(self, spec):
        return self._n().f_install(spec, build=True)

    async def fabric_link(self, spec):
        return self._n().f_link(spec)

    async def fabric_remove(self, spec):
        return self._n().f_remove(spec)

    async def fabric_cancel(self, jid):
        return {"ok": True}

    async def fabric_update(self, spec):
        return {"ok": True}

    async def fabric_bwtest(self, spec):
        n = self._n()
        n.require_on()
        await asyncio.sleep(0.6)
        mbit = float(n.spec.get("link_mbps", 1000)) * random.uniform(0.91, 0.95)
        return {"bytes": int(mbit * 1e6 / 8 * 2), "seconds": 2.0, "bps": int(mbit * 1e6 / 8), "mbit": round(mbit, 1)}

    def runtime_base(self, port: int) -> str:
        return f"http://127.0.0.1:{self.world.admin_port}/_sim/{self.nid}/{port}"


# --------------------------------------------------------------------------- dünya
class SimWorld:
    def __init__(self, ctx):
        self.ctx = ctx
        self.nodes: dict[int, SimNode] = {}
        self.admin_port = 8080
        self.time_scale = 1.0
        self.traffic = DemoTraffic(ctx)

    def seed(self, db) -> None:
        """Veritabanı boşsa demo düğümlerini ve model kataloğunu oluşturur."""
        if db.one("SELECT id FROM nodes LIMIT 1"):
            return
        from .util import rand_token
        for name, host, role, tags, spec, extra in DEMO_NODES:
            db.x(
                "INSERT INTO nodes(name,host,agent_port,agent_token,mac,role,tags,enabled,allow_sleep,allow_restart,always_on,"
                "weight,warn_c,crit_c,sim,sim_spec,last_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, host, 7777, rand_token(16), spec["mac"], role, jdump(tags), 1, extra.get("allow_sleep", 1), 1,
                 extra.get("always_on", 0), extra.get("weight", 1.0), 80 if role != "laptop" else 85, 90 if role != "laptop" else 95,
                 1, jdump(spec), "sleeping" if spec["initial"] == "suspended" else "unknown", now()),
            )
        for m in DEMO_MODELS:
            man = demo_manifest(m)
            db.x(
                "INSERT INTO models(name,repo,runtime,kind,ctx,vram_gb,ram_gb,gpu_count,cpu_ok,min_replicas,max_replicas,priority,"
                "max_concurrency,idle_unload_min,prefer_tags,notes,source,file_glob,manifest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m["name"], m["repo"], m["runtime"], m["kind"], m["ctx"], m["vram_gb"], m.get("ram_gb", 0), m["gpu_count"], m["cpu_ok"],
                 m["min_replicas"], m["max_replicas"], m["priority"], m["max_concurrency"], m["idle_unload_min"],
                 jdump(m.get("prefer_tags", [])), m.get("notes", ""), "hf", m.get("file_glob", ""), jdump(man), now()),
            )
            if m.get("stored"):
                for f in man["files"]:
                    db.x("INSERT OR REPLACE INTO blobs(sha256,size,name,kind,created_at) VALUES(?,?,?,?,?)", (f["sha256"], f["size"], f"{man['repo']}/{f['path']}", "model", now()))
        for rt, ver, backend, plat, size, name in (("llamacpp", "b6210-yamali", "cuda", "linux-x86_64", 86, "llama-yamali-cuda.tar.gz"), ("llamacpp", "b6210-yamali", "cpu", "linux-x86_64", 31, "llama-yamali-cpu.tar.gz"),
                                                   ("whispercpp", "1.8.1", "cuda", "linux-x86_64", 42, "whisper-cuda.tar.gz"), ("whispercpp", "1.8.1", "cpu", "linux-x86_64", 18, "whisper-cpu.tar.gz"),
                                                   ("piper", "yerleşik", "any", "any", 1, "piper_server.py")):
            sha = _fake_sha(name)
            db.x("INSERT OR REPLACE INTO blobs(sha256,size,name,kind,created_at) VALUES(?,?,?,?,?)", (sha, size << 20, name, "runtime", now()))
            db.x("INSERT INTO runtime_builds(id,runtime,version,platform,backend,format,sha256,size,filename,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                 (f"{rt}-{sha[:12]}", rt, ver, plat, backend, "bin", sha, size << 20, name, now()))
        for name, kind, targets, default, opts in DEMO_SERVICES:
            db.x("INSERT INTO services(name,kind,targets,enabled,is_default,options,notes,created_at) VALUES(?,?,?,1,?,?,'',?)", (name, kind, jdump(targets), default, jdump(opts), now()))
        db.set("uplink.mode", "relay")
        db.set("uplink.relay_url", "https://siteniz.com/llm/relay.php")
        db.event("info", "sim", "Demo kümesi oluşturuldu (7 makine, 6 model)")

    def sync(self, db) -> None:
        for r in db.q("SELECT id,name,sim_spec FROM nodes WHERE sim=1"):
            if r["id"] not in self.nodes:
                spec = jload(r["sim_spec"], {})
                if spec:
                    sn = self.nodes[r["id"]] = SimNode(self, r["id"], r["name"], spec)
                    for row in db.q("SELECT name,manifest FROM models WHERE source='hf'"):  # bazı dosyalar makinelerde hazır
                        man = jload(row["manifest"], {})
                        small = row["name"] in ("qwen2.5-1.5b-instruct", "qwen2.5-7b-instruct") and spec["initial"] == "on"
                        if small or (r["name"] == "ana-pc" and row["name"] != "llama-3.1-8b-instruct"):
                            sn.f_blobs.update({f["sha256"]: f["size"] for f in man.get("files", [])})

    def client_for(self, n) -> SimAgentClient:
        if n.id not in self.nodes:
            self.sync(self.ctx.db)
        return SimAgentClient(self, n.id)

    def manifest_for(self, m: dict) -> dict:
        spec = next((x for x in DEMO_MODELS if x["name"] == m["name"]), None)
        return demo_manifest(spec or {**m, "file_gb": max(0.5, float(m.get("vram_gb") or 2.0) * 0.85)})

    async def fake_download(self, f: dict, t: dict, base: int) -> str:
        """Modemden indirmenin karşılığı (~40 MB/sn)."""
        done, size = 0, int(f["size"])
        while done < size:
            await asyncio.sleep(0.5)
            step = int(40_000_000 * 0.5 * self.time_scale)
            done = min(size, done + step)
            t["bytes"], t["bps"] = base + done, int(step / 0.5)
            self.ctx.fabric.wan_down += step
        sha = f.get("sha256") or _fake_sha(f["path"])
        self.ctx.fabric.store.register(sha, size, f["path"], "model")
        return sha

    def wake(self, nid: int) -> bool:
        sn = self.nodes.get(nid)
        if sn and sn.state in ("suspended", "off"):
            sn.state, sn.boot_until = "booting", now() + sn.spec.get("boot_s", 7.0)
            return True
        return False

    def inject(self, nid: int, kind: str) -> str:
        sn = self.nodes.get(nid)
        if not sn:
            raise ValueError("simüle düğüm bulunamadı")
        if kind == "overheat":
            sn.bias, sn.bias_until = 66.0, now() + 150
        elif kind == "crash":
            sn.crash_all()
        elif kind == "offline":
            sn.state, sn.procs = "off", {}
        elif kind == "gpu_lost":
            sn.gpu_lost = bool(sn.spec["gpus"])
        elif kind == "recover":
            sn.bias, sn.gpu_lost = 0.0, False
        else:
            raise ValueError("bilinmeyen arıza türü")
        self.ctx.db.event("warn", "sim", f"Arıza enjekte edildi: {sn.name} → {kind}", nid)
        return kind

    def state(self) -> dict:
        return {
            "nodes": [{"id": n.id, "name": n.name, "state": n.state, "bias": n.bias, "gpu_lost": n.gpu_lost,
                       "procs": [{"id": p.id, "port": p.port, "state": p.state, "ready": p.ready(), "inflight": p.inflight,
                                  "served": p.served} for p in n.procs.values()]} for n in self.nodes.values()],
            "traffic": {"enabled": self.traffic.enabled, "rpm": round(self.traffic.rpm, 1)},
            "time_scale": self.time_scale,
        }

    async def start(self) -> None:
        self.sync(self.ctx.db)
        self.traffic.start()

    async def stop(self) -> None:
        await self.traffic.stop()


# --------------------------------------------------------------------------- sahte çıkarım uçları
def _sse(obj) -> str:
    return "data: " + jdump(obj) + "\n\n"


def build_router(world: SimWorld) -> APIRouter:
    r = APIRouter(prefix="/_sim/{nid}/{port}")

    def find(request: Request, nid: int, port: int):
        if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "yalnızca loopback")
        node = world.nodes.get(nid)
        if not node or not node.is_on():
            raise HTTPException(503, "düğüm kapalı")
        p = node.proc_by_port(port)
        if not p or p.state != "running":
            raise HTTPException(503, "süreç yok")
        return node, p

    @r.get("/health")
    async def health(request: Request, nid: int, port: int):
        node, p = find(request, nid, port)
        if p.ready():
            return {"status": "ok"}
        return JSONResponse({"status": "loading", "progress": round(p.progress(), 2)}, status_code=503)

    @r.post("/v1/chat/completions")
    async def chat(request: Request, nid: int, port: int):
        node, p = find(request, nid, port)
        if not p.ready():
            raise HTTPException(503, "model yükleniyor")
        body = await request.json()
        msgs = body.get("messages") or []
        last = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                last = m.get("content") or ""
                if isinstance(last, list):
                    last = " ".join(x.get("text", "") for x in last if isinstance(x, dict))
                break
        model = body.get("model", "sim")
        words = compose(str(last), model, node.name).split(" ")
        mt = body.get("max_tokens") or body.get("max_completion_tokens")
        if mt:
            words = words[: max(1, int(mt))]
        ptoks = sum(len(str(m.get("content", ""))) for m in msgs) // 4 + 1
        cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(now())
        usage = {"prompt_tokens": ptoks, "completion_tokens": len(words), "total_tokens": ptoks + len(words)}

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        p.inflight += 1
        if not body.get("stream"):
            try:
                await asyncio.sleep(len(words) / node.tps(p))
                return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": " ".join(words)},
                                     "finish_reason": "stop"}], "usage": usage}
            finally:
                p.inflight -= 1
                p.served += 1

        async def gen():
            try:
                yield _sse(chunk({"role": "assistant", "content": ""}))
                for w in words:
                    await asyncio.sleep(1.0 / node.tps(p))
                    yield _sse(chunk({"content": w + " "}))
                yield _sse(chunk({}, "stop"))
                if (body.get("stream_options") or {}).get("include_usage"):
                    yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                                "choices": [], "usage": usage})
                yield "data: [DONE]\n\n"
            finally:
                p.inflight -= 1
                p.served += 1

        return StreamingResponse(gen(), media_type="text/event-stream")

    @r.post("/v1/completions")
    async def completions(request: Request, nid: int, port: int):
        node, p = find(request, nid, port)
        if not p.ready():
            raise HTTPException(503, "model yükleniyor")
        body = await request.json()
        text = compose(str(body.get("prompt", "")), body.get("model", "sim"), node.name)
        await asyncio.sleep(0.3)
        return {"id": "cmpl-sim", "object": "text_completion", "model": body.get("model", "sim"),
                "choices": [{"index": 0, "text": text, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": len(text.split()), "total_tokens": 5 + len(text.split())}}

    @r.post("/v1/embeddings")
    async def embeddings(request: Request, nid: int, port: int):
        find(request, nid, port)
        body = await request.json()
        inp = body.get("input", "")
        items = inp if isinstance(inp, list) else [inp]
        out = []
        for i, s in enumerate(items):
            h = hashlib.sha256(str(s).encode()).digest()
            out.append({"object": "embedding", "index": i, "embedding": [(b - 128) / 128 for b in h[:16]]})
        return {"object": "list", "data": out, "model": body.get("model", "sim"), "usage": {"prompt_tokens": 1, "total_tokens": 1}}

    @r.post("/v1/audio/transcriptions")
    async def stt(request: Request, nid: int, port: int):
        node, p = find(request, nid, port)
        if not p.ready():
            raise HTTPException(503, "model yükleniyor")
        form = await request.form()
        f = form.get("file")
        size = 0
        if f is not None and hasattr(f, "read"):
            size = len(await f.read())
        p.inflight += 1
        try:
            await asyncio.sleep(0.8 + min(3.0, size / 200000))
        finally:
            p.inflight -= 1
            p.served += 1
        return {"text": f"Simülasyon: {size} baytlık ses kaydı {node.name} üzerinde çözümlendi. Merhaba, bu bir deneme metnidir."}

    @r.post("/v1/audio/speech")
    async def tts(request: Request, nid: int, port: int):
        node, p = find(request, nid, port)
        if not p.ready():
            raise HTTPException(503, "model yükleniyor")
        body = await request.json()
        text = str(body.get("input", ""))
        p.inflight += 1
        try:
            await asyncio.sleep(0.4)
            wav = await asyncio.to_thread(make_wav, text)
        finally:
            p.inflight -= 1
            p.served += 1
        return Response(content=wav, media_type="audio/wav")

    return r


# --------------------------------------------------------------------------- demo trafiği
class DemoTraffic:
    def __init__(self, ctx):
        self.ctx = ctx
        self.enabled = False
        self.rpm = 0.0
        self._task: asyncio.Task | None = None
        self._active = 0
        self._convs: dict[str, tuple[str, list]] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for t in list(self._tasks):
            t.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if not self.enabled:
                self.rpm = 0.0
                continue
            ph = time.time() / 75.0
            self.rpm = 10 + 42 * (0.5 + 0.5 * math.sin(ph)) + (55 if random.random() < 0.015 else 0)
            if self._active < 24 and random.random() < self.rpm / 60.0:
                t = asyncio.create_task(self._one())
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def _one(self) -> None:
        self._active += 1
        try:
            gw = self.ctx.gateway
            models = [m for m in self.ctx.cluster.models.values() if m["kind"] == "llm" and m["enabled"] and m["name"] != "qwen2.5-14b-instruct"]
            if not models:
                return
            weights = [{"qwen2.5-7b-instruct": 6, "llama-3.1-8b-instruct": 2, "qwen2.5-1.5b-instruct": 3}.get(m["name"], 1) for m in models]
            model = random.choices(models, weights)[0]["name"]
            uid = f"demo-user-{random.randint(1, 18):02d}"
            conv_id, msgs = self._convs.get(uid, ("", []))
            if not conv_id or random.random() < 0.4 or len(msgs) > 8:
                conv_id, msgs = f"demo-{uid}-{uuid.uuid4().hex[:6]}", []
            msgs = msgs + [{"role": "user", "content": random.choice(DEMO_PROMPTS)}]
            res = await gw.internal_chat(model, msgs, user_id=uid, conv_id=conv_id, source="demo", max_tokens=200)
            if res.get("ok"):
                self._convs[uid] = (conv_id, msgs + [{"role": "assistant", "content": res["text"]}])
                u = res.get("usage") or {}
                gw.add_usage(uid, ["free", "free", "plus", "pro"][hash(uid) % 4], "sohbet", "chat", u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # demo trafiği hiçbir zaman sistemi bozmamalı
            log.debug("demo trafik: %s", e)
        finally:
            self._active -= 1
