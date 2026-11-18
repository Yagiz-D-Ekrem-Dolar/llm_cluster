"""Runtime (çıkarım motoru) şablonları ve başlatma komutu üretimi.

Her runtime bir çalıştırılabilir dosya + argüman şablonudur. Şablonlar arayüzden düzenlenebilir;
buradakiler yalnızca başlangıç değerleridir. Komutlar `shell` kullanılmadan, argüman listesi olarak
node ajanına gönderilir.
"""
from __future__ import annotations

import shlex

from .util import estimate_vram_gb, jdump, jload

BUILTIN: list[dict] = [
    dict(
        key="llamacpp",
        label="llama.cpp (llama-server)",
        exe="llama-server",
        args=["-hf", "{repo}", "--host", "0.0.0.0", "--port", "{port}", "-c", "{ctx_total}", "-ngl", "{ngl}",
              "--alias", "{name}", "-np", "{parallel}", "--jinja", "--metrics"],
        env={},
        health="/health",
        needs_gpu=0,
        notes="GGUF modeller. Yamalı derlemenizi buraya yükleyin: paket switch üzerinden makinelere gider, model dosyası da "
              "ana PC'den dağıtılır (makineler internetten indirmez). Bağlam, sohbet başına değerdir; toplam bellek otomatik hesaplanır.",
    ),
    dict(
        key="whispercpp",
        label="whisper.cpp (konuşma → metin)",
        exe="whisper-server",
        args=["-m", "{repo}", "--host", "0.0.0.0", "--port", "{port}", "-t", "{threads}", "-l", "auto", "--convert",
              "--inference-path", "/v1/audio/transcriptions"],
        env={},
        health="/health",
        needs_gpu=0,
        notes="OpenAI uyumlu /v1/audio/transcriptions. Model: ggml-*.bin (ör. ggerganov/whisper.cpp). "
              "webm/mp3 gibi biçimler için makinede ffmpeg gerekir (kurulumda seçilebilir).",
    ),
    dict(
        key="piper",
        label="Piper (metin → konuşma)",
        exe="piper_server.py",
        args=["--model", "{repo}", "--host", "0.0.0.0", "--port", "{port}", "--threads", "{threads}"],
        env={},
        health="/health",
        needs_gpu=0,
        notes="OpenAI uyumlu /v1/audio/speech, Türkçe sesler dahil (rhasspy/piper-voices). Sunucu köprüsü panelden otomatik dağıtılır; "
              "makinede Piper'ı kurulum seçeneği hazırlar. CPU'da gerçek zamanlıdır, GPU'yu sohbete bırakır.",
    ),
    dict(
        key="vllm",
        label="vLLM",
        exe="vllm",
        args=["serve", "{repo}", "--host", "0.0.0.0", "--port", "{port}", "--max-model-len", "{ctx}",
              "--gpu-memory-utilization", "{gpu_util}", "--served-model-name", "{name}", "--max-num-seqs", "{parallel}"],
        env={},
        health="/health",
        needs_gpu=1,
        notes="NVIDIA GPU gerekir. Sürekli batching ile en yüksek verim. Whisper gibi STT modelleri de "
              "(tür: stt) bu runtime ile servis edilebilir.",
    ),
    dict(
        key="sglang",
        label="SGLang",
        exe="python3",
        args=["-m", "sglang.launch_server", "--model-path", "{repo}", "--host", "0.0.0.0", "--port", "{port}",
              "--context-length", "{ctx}", "--served-model-name", "{name}"],
        env={},
        health="/health",
        needs_gpu=1,
        notes="NVIDIA GPU gerekir.",
    ),
    dict(
        key="ollama",
        label="Ollama",
        exe="ollama",
        args=["serve"],
        env={"OLLAMA_HOST": "0.0.0.0:{port}", "OLLAMA_KEEP_ALIVE": "-1"},
        health="/api/tags",
        needs_gpu=0,
        hook="ollama",
        notes="Depo alanına Ollama model adı yazın (ör. qwen2.5:7b ya da hf.co/kullanici/depo:Q4_K_M). "
              "Model ilk başlatmada indirilir ve belleğe yüklenir.",
    ),
    dict(
        key="tgi",
        label="TGI (Docker)",
        exe="docker",
        args=["run", "--rm", "--name", "orkestra-dep-{id}", "--gpus", "all", "-p", "{port}:80",
              "-v", "/var/lib/orkestra/hf:/data", "-e", "HF_TOKEN",
              "ghcr.io/huggingface/text-generation-inference:latest",
              "--model-id", "{repo}", "--max-total-tokens", "{ctx}"],
        env={},
        health="/health",
        needs_gpu=1,
        notes="NVIDIA Container Toolkit gerekir.",
    ),
    dict(
        key="speaches",
        label="Speaches (Whisper STT + TTS, Docker)",
        exe="docker",
        args=["run", "--rm", "--name", "orkestra-dep-{id}", "--gpus", "all", "-p", "{port}:8000",
              "ghcr.io/speaches-ai/speaches:latest-cuda"],
        env={},
        health="/health",
        needs_gpu=0,
        notes="OpenAI uyumlu /v1/audio/transcriptions ve /v1/audio/speech. Depo alanı istemcinin gönderdiği "
              "model adıdır (ör. Systran/faster-whisper-small). Komutu kendi kurulumunuza göre düzenleyin.",
    ),
    dict(
        key="custom",
        label="Özel komut",
        exe="python3",
        args=["server.py", "--port", "{port}"],
        env={},
        health="/health",
        needs_gpu=0,
        notes="Kendi çıkarım sunucunuz. OpenAI uyumlu /v1/* uçlarını sunmalıdır. "
              "{repo} {name} {port} {ctx} {ngl} {parallel} {gpu_util} değişkenleri kullanılabilir.",
    ),
]

# Üst akımda model adı olarak depo yolunu bekleyen runtime'lar
UPSTREAM_IS_REPO = {"ollama", "speaches"}
CPU_FLAG = {"whispercpp": ["--no-gpu"]}


def seed(db) -> None:
    for r in BUILTIN:
        if db.one("SELECT key FROM runtimes WHERE key=?", (r["key"],)):
            continue
        db.x(
            "INSERT INTO runtimes(key,label,exe,args,env,health,needs_gpu,hook,notes,builtin) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (r["key"], r["label"], r["exe"], jdump(r["args"]), jdump(r["env"]), r["health"],
             r["needs_gpu"], r.get("hook", ""), r.get("notes", "")),
        )


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(s: str, variables: dict) -> str:
    return str(s).format_map(_Safe(variables))


def model_size_gb(model: dict) -> float:
    """Ağırlıklar + çalışma payı (kullanıcı girdiyse o, yoksa addan tahmin)."""
    if model.get("vram_gb"):
        return float(model["vram_gb"])
    return estimate_vram_gb(model["repo"], model.get("extra_args", ""))


def model_vram(model: dict) -> float:
    """GPU'da gereken toplam VRAM (gpu_count=0 ise yalnızca CPU modeli)."""
    if not model.get("gpu_count"):
        return 0.0
    return model_size_gb(model)


def ram_for_gpu_run(model: dict) -> float:
    return float(model["ram_gb"]) if model.get("ram_gb") else 3.0


def ram_for_cpu_run(model: dict) -> float:
    return float(model["ram_gb"]) if model.get("ram_gb") else model_size_gb(model)


def upstream_model(rt: dict, model: dict) -> str:
    if model.get("upstream"):
        return model["upstream"]
    if rt["key"] in UPSTREAM_IS_REPO:
        return model["repo"]
    return model["name"]


def rt_path(rt: dict | None, key: str, default: str) -> str:
    """Runtime'ın API yolu geçersiz kılması (ör. yamalı llama.cpp'de farklı ses uç noktası)."""
    if not rt:
        return default
    return (jload(rt.get("paths"), {}) or {}).get(key) or default


def build_spec(rt: dict, model: dict, dep: dict, gpu_total_gb: float, hf_token: str = "", exe_override: str = "",
               local: dict | None = None, hw: dict | None = None) -> dict:
    """Node ajanına gönderilecek başlatma tanımı.

    exe_override: bu makineye özel çalıştırılabilir yol.
    local: ağdan dağıtılmış dosyalar (exe, cwd, env, model_path). Varsa makine internetten hiçbir şey indirmez.
    """
    local = local or {}
    hw = hw or {}
    gpus = dep.get("gpus") or []
    need = model_vram(model)
    util = 0.9
    if gpu_total_gb and need:
        util = max(0.2, min(0.92, round(need / max(1.0, gpu_total_gb * max(1, len(gpus))) * 1.05, 2)))
    cores = int(((hw.get("cpu") or {}).get("cores")) or 4)
    parallel = max(1, int(model["max_concurrency"]))
    model_ref = local.get("model_path") or model["repo"]
    variables = {
        "repo": model_ref,
        "model_path": model_ref,
        "model_dir": local.get("model_dir") or model_ref,
        "name": model["name"],
        "port": dep["port"],
        "ctx": model["ctx"],
        "ctx_total": int(model["ctx"] or 0) * parallel,  # llama.cpp bağlamı yuvalara böler: her sohbet tam bağlam alsın
        "ngl": 999 if gpus else 0,
        "parallel": parallel,
        "gpu_util": util,
        "threads": max(2, min(cores, 4)) if gpus else max(1, cores),
        "id": dep["id"],
        "host": "0.0.0.0",
    }
    args = [render(a, variables) for a in jload(rt["args"], [])]
    if local.get("model_path") and "-hf" in args:
        args[args.index("-hf")] = "-m"  # yerel dosya: llama.cpp internete çıkmaz
    if rt["key"] == "vllm" and len(gpus) > 1:
        args += ["--tensor-parallel-size", str(len(gpus))]
    if not gpus:
        args += CPU_FLAG.get(rt["key"], [])
    if model.get("extra_args"):
        args += shlex.split(model["extra_args"])
    env = {k: render(v, variables) for k, v in jload(rt["env"], {}).items()}
    env.update(local.get("env") or {})
    if gpus:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in gpus)
    elif rt["needs_gpu"] == 0 and rt["key"] != "custom":
        env["CUDA_VISIBLE_DEVICES"] = ""  # GPU'suz düğümde/CPU modunda GPU'ya dokunma
    if local.get("model_path"):
        env["HF_HUB_OFFLINE"] = "1"
    elif hf_token:
        env["HF_TOKEN"] = hf_token
    exe = local.get("exe") or exe_override or rt["exe"]
    argv = [exe] + args
    if exe.lower().endswith(".py") and hw.get("python_exe"):
        argv = [hw["python_exe"], exe] + args
    return {
        "id": f"dep-{dep['id']}",
        "argv": argv,
        "env": env,
        "cwd": local.get("cwd") or None,
        "port": dep["port"],
        "health_path": rt["health"],
        "meta": {"model": model["name"], "size_gb": model_size_gb(model)},  # ajan yok sayar; simülatör kullanır
    }
