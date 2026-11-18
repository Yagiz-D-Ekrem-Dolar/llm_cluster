#!/usr/bin/env python3
"""Orkestra düğüm ajanı: tek dosya, yalnızca standart kütüphane (Python 3.8+).

Her Ubuntu makinede çalışır ve kontrol sunucusuna şunları sağlar:
  GET  /v1/info      donanım özeti (CPU, RAM, GPU'lar, kurulu runtime'lar, MAC)
  GET  /v1/metrics   CPU/RAM/sıcaklık/GPU/disk/ağ ölçümleri + yönetilen süreçler
  POST /v1/launch    bir çıkarım sunucusu süreci başlat (izinli çalıştırılabilirler)
  POST /v1/stop      süreci durdur
  GET  /v1/logs      süreç günlüğünün son satırları
  POST /v1/power     suspend | reboot | poweroff
  GET  /healthz      kimlik doğrulamasız canlılık

Ağ üzerinden dağıtım (switch üzerinden, internete çıkmadan):
  POST /v1/fabric/fetch    bir dosyayı (sha256) ana PC'den ya da başka bir makineden çek, doğrula
  GET  /v1/fabric/blob/X   elindeki dosyayı başka makinelere ver (süreli imzalı bilet ile)
  POST /v1/fabric/install  runtime paketini aç      POST /v1/fabric/build  kaynaktan bu makinede derle
  POST /v1/fabric/link     model klasörünü oluştur  POST /v1/fabric/remove yer aç
  POST /v1/fabric/update   ajanın kendisini güncelle (çalışan çıkarım süreçleri kesilmez)

Güvenlik: tüm /v1 uçları Bearer token ister. Süreçler `shell` olmadan, argüman listesiyle başlatılır ve
yalnızca izinli çalıştırılabilir dosyalar ile kontrol sunucusunun bu makineye kurduğu runtime'lar çalıştırılabilir.
Token = bu makinede uzaktan komut çalıştırma yetkisi demektir: gizli tutun, ağ erişimini kontrol sunucusuyla sınırlayın.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

VERSION = "0.3.0"
IS_WIN = os.name == "nt"

try:  # isteğe bağlı: Windows'ta ve /proc olmayan yerlerde yedek
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

DEFAULT_ALLOW_EXE = ["llama-server", "llama-cpp-server", "vllm", "ollama", "docker", "python3", "python",
                     "text-generation-launcher", "piper", "whisper-server"]
DEFAULT_ALLOW_MODULES = ["sglang.launch_server", "vllm.entrypoints.openai.api_server", "uvicorn", "speaches"]

CFG = {"token": "", "allow_ips": set(), "dry_power": False, "allow_exe": set(), "allow_modules": set(),
       "allow_paths": set(), "allow_paths_real": set(), "allow_dirs": [], "node_id": ""}


def code_sha() -> str:
    """Çalışan ajan dosyasının özeti: kontrol sunucusu sürüm farkını buradan anlar."""
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def path_allowed(p: str) -> bool:
    """Tam yolla verilen çalıştırılabilir dosya açıkça izinli mi (dosya ya da izinli klasör altında)?"""
    rp = os.path.realpath(p)
    if p in CFG["allow_paths"] or rp in CFG["allow_paths_real"] or (FAB is not None and FAB.is_managed(rp)):
        return True
    return any(os.path.dirname(rp) == d or rp.startswith(d.rstrip(os.sep) + os.sep) for d in CFG["allow_dirs"])


def discover_runtimes() -> dict:
    """Bu makinede bulunan ve ajanda izinli çalıştırılabilir dosyalar (ad ya da tam yol -> çözümlenen yol)."""
    out = {}
    for n in sorted(CFG["allow_exe"]):
        p = shutil.which(n)
        if p:
            out[n] = p
    for p in sorted(CFG["allow_paths"]):
        if os.path.isfile(p) and (IS_WIN or os.access(p, os.X_OK)):
            out[p] = p
    skip = (".so", ".dll", ".py", ".txt", ".md", ".json", ".h", ".c", ".cpp", ".cu", ".sh", ".a", ".o", ".log", ".yml", ".yaml")
    for d in CFG["allow_dirs"]:
        n = 0
        for root, dirs, files in os.walk(d):
            if root[len(d):].count(os.sep) >= 3:
                dirs[:] = []
                continue
            for f in sorted(files):
                fp = os.path.join(root, f)
                if f.lower().endswith(skip) or not (os.access(fp, os.X_OK) if not IS_WIN else f.lower().endswith(".exe")):
                    continue
                out[fp] = fp
                n += 1
                if n >= 150:
                    break
    return out


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def read_file(path: str) -> str:
    try:
        with open(path, "r", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


# ------------------------------------------------------------------------------------ ölçümler
_cpu_prev = None
_cpu_lock = threading.Lock()


def read_cpu():
    global _cpu_prev
    txt = read_file("/proc/stat")
    if txt:
        try:
            vals = [int(x) for x in txt.splitlines()[0].split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            with _cpu_lock:
                prev, _cpu_prev = _cpu_prev, (idle, total)
            if not prev:
                return 0.0
            di, dt = idle - prev[0], total - prev[1]
            return round(100.0 * (1 - di / dt), 1) if dt > 0 else 0.0
        except Exception:
            pass
    if psutil:
        return psutil.cpu_percent(interval=None)
    return None


def read_mem():
    txt = read_file("/proc/meminfo")
    total = avail = None
    if txt:
        m = dict(re.findall(r"^(\w+):\s+(\d+)", txt, re.M))
        if "MemTotal" in m:
            total = int(m["MemTotal"]) * 1024
            avail = int(m.get("MemAvailable", m.get("MemFree", 0))) * 1024
    if total is None and psutil:
        vm = psutil.virtual_memory()
        total, avail = vm.total, vm.available
    if total is None:
        return {"total_gb": 0, "used_gb": 0, "pct": 0}
    used = total - avail
    return {"total_gb": round(total / 2**30, 2), "used_gb": round(used / 2**30, 2), "pct": round(used / total * 100, 1)}


CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "k8temp")


def read_cpu_temp():
    best, fallback = None, None
    base = "/sys/class/hwmon"
    try:
        for h in sorted(os.listdir(base)):
            name = read_file(f"{base}/{h}/name").strip()
            if name not in CPU_HWMON and name != "acpitz":
                continue
            for f in os.listdir(f"{base}/{h}"):
                if f.startswith("temp") and f.endswith("_input"):
                    try:
                        v = int(read_file(f"{base}/{h}/{f}").strip()) / 1000.0
                    except ValueError:
                        continue
                    if name == "acpitz":
                        fallback = max(fallback or v, v)
                    else:
                        best = max(best or v, v)
    except Exception:
        pass
    if best is None:
        try:
            for z in sorted(os.listdir("/sys/class/thermal")):
                if z.startswith("thermal_zone"):
                    t = read_file(f"/sys/class/thermal/{z}/type").strip()
                    if "pkg" in t or "cpu" in t.lower():
                        best = max(best or 0, int(read_file(f"/sys/class/thermal/{z}/temp").strip()) / 1000.0)
        except Exception:
            pass
    return best if best is not None else fallback


_gpu_cache = {"t": 0.0, "gpus": [], "err": ""}
_gpu_lock = threading.Lock()


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def nvidia_query():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], ""
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                              "--format=csv,noheader,nounits"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=6)
    except Exception as e:
        return [], f"nvidia-smi: {e}"
    if out.returncode != 0:
        return [], ((out.stdout or "") + (out.stderr or "")).strip()[:200] or "nvidia-smi hata verdi"
    gpus = []
    for line in out.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 7:
            continue
        used, tot = _f(p[3]), _f(p[4])
        gpus.append({"index": int(p[0]), "name": p[1], "util": _f(p[2]),
                     "mem_used_gb": round(used / 1024, 2) if used is not None else None,
                     "mem_total_gb": round(tot / 1024, 2) if tot is not None else None,
                     "temp": _f(p[5]), "power_w": _f(p[6])})
    return gpus, ""


def amd_query():
    out, base = [], "/sys/class/drm"
    try:
        for card in sorted(os.listdir(base)):
            if not re.fullmatch(r"card\d+", card):
                continue
            dev = f"{base}/{card}/device"
            if not os.path.exists(dev + "/gpu_busy_percent"):
                continue
            used = _f(read_file(dev + "/mem_info_vram_used").strip())
            tot = _f(read_file(dev + "/mem_info_vram_total").strip())
            temp = power = None
            hw = [d for d in os.listdir(dev + "/hwmon")] if os.path.isdir(dev + "/hwmon") else []
            if hw:
                t = _f(read_file(f"{dev}/hwmon/{hw[0]}/temp1_input").strip())
                temp = t / 1000.0 if t is not None else None
                p = _f(read_file(f"{dev}/hwmon/{hw[0]}/power1_average").strip())
                power = p / 1e6 if p is not None else None
            out.append({"index": len(out), "name": read_file(dev + "/product_name").strip() or f"AMD GPU {card}",
                        "util": _f(read_file(dev + "/gpu_busy_percent").strip()),
                        "mem_used_gb": round(used / 2**30, 2) if used is not None else None,
                        "mem_total_gb": round(tot / 2**30, 2) if tot is not None else None, "temp": temp, "power_w": power})
    except Exception:
        pass
    return out


def _gpu_refresh():
    gpus, err = nvidia_query()
    if not gpus and not err:
        gpus = amd_query()
    with _gpu_lock:
        _gpu_cache.update(t=time.time(), gpus=gpus, err=err)


def probe_gpu_caps() -> dict:
    """Bu makinede hangi hızlandırma arka uçları kullanılabilir? (runtime derlemesi seçmek için)"""
    out = {"nvidia": False, "driver": "", "cuda": "", "amd": False, "vulkan": False, "backends": []}
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            r = subprocess.run([exe], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10)
            if r.returncode == 0:
                out["nvidia"] = True
                m = re.search(r"Driver Version:\s*([\d.]+)", r.stdout)
                out["driver"] = m.group(1) if m else ""
                m = re.search(r"CUDA Version:\s*([\d.]+)", r.stdout)
                out["cuda"] = m.group(1) if m else ""
        except Exception:
            pass
    try:
        for card in os.listdir("/sys/class/drm"):
            if re.fullmatch(r"card\d+", card) and read_file(f"/sys/class/drm/{card}/device/vendor").strip() == "0x1002":
                out["amd"] = True
    except Exception:
        pass
    vk = bool(shutil.which("vulkaninfo"))
    if not vk:
        try:
            import ctypes.util
            vk = bool(ctypes.util.find_library("vulkan") or ctypes.util.find_library("vulkan-1"))
        except Exception:
            vk = False
    out["vulkan"] = vk
    out["backends"] = (["cuda"] if out["nvidia"] else []) + (["vulkan"] if vk else []) + ["cpu"]
    return out


def _gpu_loop():
    """GPU ölçümlerini arka planda tazeler: hibrit grafikli laptoplarda nvidia-smi saniyeler sürebilir
    ve /v1/metrics yanıtını geciktirmemelidir."""
    next_caps = 0.0
    while True:
        try:
            if time.time() >= next_caps:  # ilk bilgi isteğinden önce hazır olsun
                caps = probe_gpu_caps()
                with _gpu_lock:
                    _gpu_cache["caps"] = caps
                next_caps = time.time() + 60
            _gpu_refresh()
        except Exception:
            pass
        time.sleep(2.5)


def read_gpus():
    with _gpu_lock:
        if _gpu_cache.get("bg"):
            stale = _gpu_cache["t"] and time.time() - _gpu_cache["t"] > 25
            return _gpu_cache["gpus"], _gpu_cache["err"] or ("GPU ölçümü güncel değil (nvidia-smi yanıt vermiyor?)" if stale else "")
        if time.time() - _gpu_cache["t"] < 2.0:
            return _gpu_cache["gpus"], _gpu_cache["err"]
    _gpu_refresh()
    with _gpu_lock:
        return _gpu_cache["gpus"], _gpu_cache["err"]


def read_disk():
    try:
        u = shutil.disk_usage(os.path.abspath(os.sep))
        return {"total_gb": round(u.total / 2**30, 1), "used_gb": round(u.used / 2**30, 1), "pct": round(u.used / u.total * 100, 1)}
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "pct": 0}


_net_prev = None
_net_lock = threading.Lock()


def read_net():
    global _net_prev
    rx = tx = None
    txt = read_file("/proc/net/dev")
    if txt:
        rx = tx = 0
        for line in txt.splitlines()[2:]:
            if ":" not in line:
                continue
            name, data = line.split(":", 1)
            name = name.strip()
            if name == "lo" or name.startswith(("docker", "veth", "br-")):
                continue
            f = data.split()
            rx += int(f[0])
            tx += int(f[8])
    elif psutil:
        c = psutil.net_io_counters()
        rx, tx = c.bytes_recv, c.bytes_sent
    if rx is None:
        return {"rx_bps": 0, "tx_bps": 0}
    t = time.time()
    with _net_lock:
        prev, _net_prev = _net_prev, (t, rx, tx)
    if not prev or t <= prev[0]:
        return {"rx_bps": 0, "tx_bps": 0}
    return {"rx_bps": round((rx - prev[1]) / (t - prev[0])), "tx_bps": round((tx - prev[2]) / (t - prev[0]))}


def read_uptime():
    txt = read_file("/proc/uptime")
    if txt:
        try:
            return int(float(txt.split()[0]))
        except Exception:
            pass
    if psutil:
        return int(time.time() - psutil.boot_time())
    return 0


def default_iface():
    for line in read_file("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000":
            return f[0]
    return None


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


_link_cache = [0.0, None]


def read_link() -> dict:
    """Varsayılan ağ arayüzü: switch bağlantı hızı, Wi-Fi mi, hata sayaçları."""
    if _link_cache[1] and time.time() - _link_cache[0] < 10:
        return _link_cache[1]
    ip = local_ip()
    out = {"ip": ip, "iface": None, "speed_mbps": None, "duplex": None, "wifi": False, "up": None, "mtu": None, "errors": 0, "drops": 0}
    i = default_iface()
    base = f"/sys/class/net/{i}" if i else ""
    if base and os.path.isdir(base):
        speed = _f(read_file(base + "/speed").strip())
        out.update(iface=i, speed_mbps=int(speed) if speed and speed > 0 else None, duplex=read_file(base + "/duplex").strip() or None,
                   wifi=os.path.isdir(base + "/wireless"), up=read_file(base + "/operstate").strip() == "up",
                   mtu=int(_f(read_file(base + "/mtu").strip()) or 0) or None)
        for k, names in (("errors", ("rx_errors", "tx_errors")), ("drops", ("rx_dropped", "tx_dropped"))):
            out[k] = int(sum(_f(read_file(f"{base}/statistics/{n}").strip()) or 0 for n in names))
    elif psutil:
        try:
            for name, addrs in psutil.net_if_addrs().items():
                if any(a.address == ip for a in addrs):
                    st = psutil.net_if_stats().get(name)
                    out.update(iface=name, wifi=bool(re.search(r"wi-?fi|wlan|kablosuz", name, re.I)))
                    if st:
                        out.update(speed_mbps=st.speed or None, up=st.isup, mtu=st.mtu or None)
                    break
        except Exception:
            pass
    _link_cache[0], _link_cache[1] = time.time(), out
    return out


def read_power() -> dict:
    """Dizüstülerde şebeke/pil durumu: pille çalışan makineye iş verilmez."""
    ac, pct, status = None, None, ""
    base = "/sys/class/power_supply"
    try:
        for d in sorted(os.listdir(base)):
            t = read_file(f"{base}/{d}/type").strip()
            if t in ("Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP"):
                v = read_file(f"{base}/{d}/online").strip()
                if v in ("0", "1"):
                    ac = bool(ac) or v == "1"
            elif t == "Battery" and read_file(f"{base}/{d}/scope").strip().lower() != "device":
                c = _f(read_file(f"{base}/{d}/capacity").strip())
                if c is not None:
                    pct, status = c, read_file(f"{base}/{d}/status").strip()
    except Exception:
        pass
    if pct is None and psutil:
        try:
            b = psutil.sensors_battery()
            if b:
                pct, ac = b.percent, bool(b.power_plugged)
        except Exception:
            pass
    return {"ac": ac, "battery_pct": pct, "status": status}


def is_laptop() -> bool:
    t = read_file("/sys/class/dmi/id/chassis_type").strip()
    if t:
        return t in ("8", "9", "10", "11", "14", "30", "31", "32")
    return read_power()["battery_pct"] is not None


def machine_id() -> str:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        v = read_file(p).strip()
        if v:
            return v
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except Exception:
            pass
    return socket.gethostname()


def platform_key() -> str:
    m = platform.machine().lower()
    m = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)
    return ("windows" if IS_WIN else sys.platform.split("-")[0]) + "-" + m


def read_macs():
    macs = {}
    try:
        for i in sorted(os.listdir("/sys/class/net")):
            if i == "lo" or i.startswith(("docker", "veth", "br-", "virbr", "tun", "tap")):
                continue
            m = read_file(f"/sys/class/net/{i}/address").strip()
            if m and m != "00:00:00:00:00:00":
                macs[i] = m
    except Exception:
        pass
    if not macs and psutil:
        try:
            for i, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if getattr(a.family, "name", "") == "AF_LINK" and a.address and not i.lower().startswith("loopback"):
                        macs[i] = a.address.replace("-", ":").lower()
        except Exception:
            pass
    di = default_iface()
    primary = macs.get(di) if di else None
    if not primary and macs:
        primary = next(iter(macs.values()))
    return macs, primary


def os_name():
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)', read_file("/etc/os-release"), re.M)
    return m.group(1) if m else platform.platform()


def cpu_model():
    m = re.search(r"^model name\s*:\s*(.+)$", read_file("/proc/cpuinfo"), re.M)
    return m.group(1).strip() if m else (platform.processor() or platform.machine())


def cpu_cores():
    threads = os.cpu_count() or 1
    if psutil:
        try:
            return psutil.cpu_count(logical=False) or threads, threads
        except Exception:
            pass
    txt = read_file("/proc/cpuinfo")
    pairs = set(zip(re.findall(r"^physical id\s*:\s*(\d+)", txt, re.M), re.findall(r"^core id\s*:\s*(\d+)", txt, re.M)))
    return (len(pairs) or max(1, threads // 2)), threads


def read_info():
    if _gpu_cache.get("bg"):  # donanım özetinde GPU'lar eksik görünmesin: ilk örneği (en çok ~12 sn) bekle
        for _ in range(60):
            if _gpu_cache["t"]:
                break
            time.sleep(0.2)
    gpus, _ = read_gpus()
    mem = read_mem()
    macs, primary = read_macs()
    runtimes = discover_runtimes()
    try:
        import importlib.util
        if importlib.util.find_spec("sglang"):
            runtimes["sglang"] = "python-module"
    except Exception:
        pass
    cores, threads = cpu_cores()
    if FAB is not None:
        for rid, r in FAB.state()["runtimes"].items():
            if r.get("entry"):
                runtimes["fabric:" + rid] = r["entry"]
    return {
        "agent": VERSION, "code_sha": code_sha(), "node_id": CFG["node_id"], "platform": platform_key(), "python": platform.python_version(), "python_exe": sys.executable,
        "laptop": is_laptop(), "gpu_caps": _gpu_cache.get("caps") or {"backends": ["cpu"]},
        "tools": {k: bool(shutil.which(k)) for k in ("cmake", "gcc", "g++", "make", "ninja", "git", "ffmpeg", "nvcc", "docker")},
        "store": FAB.root if FAB is not None else "",
        "hostname": socket.gethostname(), "os": os_name(),
        "cpu": {"model": cpu_model(), "cores": cores, "threads": threads},
        "ram_gb": mem["total_gb"],
        "gpus": [{"index": g["index"], "name": g["name"], "vram_gb": g["mem_total_gb"]} for g in gpus],
        "runtimes": {k: v for k, v in runtimes.items() if v}, "macs": macs, "primary_mac": primary,
        "allow_exe": sorted(CFG["allow_exe"]) + sorted(CFG["allow_paths"]) + [d + os.sep + "*" for d in CFG["allow_dirs"]],
        "boot_ts": time.time() - read_uptime(),
    }


def read_metrics(procs):
    gpus, err = read_gpus()
    la = None
    try:
        la = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    return {
        "ts": time.time(), "cpu": read_cpu(), "load1": la, "mem": read_mem(), "cpu_temp": read_cpu_temp(),
        "gpus": gpus, "gpu_error": err, "disk": read_disk(), "net": {**read_net(), **read_link()}, "uptime_s": read_uptime(),
        "power": read_power(), "fabric": FAB.summary() if FAB is not None else None, "procs": procs.list(),
    }


# ------------------------------------------------------------------------------------ süreç yönetimi
def _pid_alive(pid: int) -> bool:
    if IS_WIN:
        return bool(psutil and psutil.pid_exists(pid))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ProcMgr:
    def __init__(self, state_path: str, log_dir: str):
        self.state_path, self.log_dir = state_path, log_dir
        self.lock = threading.RLock()
        self.items = {}
        os.makedirs(log_dir, exist_ok=True)
        self._adopt()

    def _adopt(self):
        """Ajan yeniden başlasa da çalışan çıkarım süreçlerini yeniden sahiplen."""
        try:
            with open(self.state_path) as f:
                saved = json.load(f)
        except Exception:
            return
        for pid, it in saved.items():
            p = it.get("pid")
            if not p or not _pid_alive(p):
                continue
            if not IS_WIN:
                cmd = read_file(f"/proc/{p}/cmdline")
                if cmd and os.path.basename(it["argv"][0]) not in cmd:
                    continue
            it["popen"], it["rc"] = None, None
            self.items[pid] = it
            log(f"süreç yeniden sahiplenildi: {pid} (pid {p})")

    def _save(self):
        try:
            data = {k: {kk: vv for kk, vv in v.items() if kk != "popen"} for k, v in self.items.items()}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            log(f"durum yazılamadı: {e}")

    @staticmethod
    def validate(argv):
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv bir metin listesi olmalı")
        exe0 = argv[0]
        exe = os.path.basename(exe0).lower()
        exe = exe[:-4] if exe.endswith(".exe") else exe
        if os.path.isabs(exe0):
            # kontrol sunucusunun kurduğu paketteki betik (ör. server.py) bu ajanın Python'uyla çalışır
            if FAB is not None and len(argv) > 1 and os.path.isabs(argv[1]) and FAB.is_managed(os.path.realpath(argv[1])) \
                    and os.path.normcase(os.path.realpath(exe0)) == os.path.normcase(os.path.realpath(sys.executable)):
                return
            if not path_allowed(exe0):
                raise PermissionError(f"çalıştırılabilir dosya izinli değil: {exe0} (makinede: sudo orkestra-agent-allow {exe0})")
        else:
            if os.path.basename(exe0) != exe0:
                raise PermissionError("göreli yol kullanılamaz: yalnızca ad ya da tam yol")
            if exe not in CFG["allow_exe"]:
                raise PermissionError(f"çalıştırılabilir dosya izinli değil: {exe} (makinede: sudo orkestra-agent-allow {exe})")
        if FAB is not None and os.path.isabs(exe0) and FAB.is_managed(os.path.realpath(exe0)):
            return
        if exe in ("python", "python3"):
            if len(argv) < 3 or argv[1] != "-m" or argv[2] not in CFG["allow_modules"]:
                raise PermissionError("python yalnızca izinli modüllerle (-m <modül>) çalıştırılabilir")

    def _state(self, it):
        p = it.get("popen")
        if p is not None:
            rc = p.poll()
            if rc is None:
                return "running", None
            return "exited", rc
        return ("running", None) if _pid_alive(it["pid"]) else ("exited", it.get("rc"))

    def list(self):
        out = []
        with self.lock:
            for pid, it in list(self.items.items()):
                st, rc = self._state(it)
                it["rc"] = rc
                if st == "exited" and time.time() - it.get("ended", time.time()) > 900:
                    self.items.pop(pid, None)
                    continue
                if st == "exited":
                    it.setdefault("ended", time.time())
                out.append({"id": pid, "pid": it["pid"], "state": st, "rc": rc, "started": it["started"], "port": it.get("port")})
        return out

    def launch(self, spec):
        pid = str(spec.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", pid):
            raise ValueError("geçersiz süreç kimliği")
        argv = spec.get("argv")
        self.validate(argv)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
        env["PYTHONUNBUFFERED"] = "1"
        with self.lock:
            cur = self.items.get(pid)
            if cur and self._state(cur)[0] == "running":
                raise RuntimeError("süreç zaten çalışıyor")
            lp = os.path.join(self.log_dir, pid + ".log")
            logf = open(lp, "ab", buffering=0)
            logf.write(f"\n--- {time.strftime('%F %T')} başlatıldı: {' '.join(argv)}\n".encode())
            cwd = spec.get("cwd") or None
            if cwd and not os.path.isdir(cwd):
                cwd = None
            kw = dict(stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=cwd)
            if IS_WIN:
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            try:
                p = subprocess.Popen(argv, **kw)
            except FileNotFoundError:
                raise ValueError(f"komut bulunamadı: {argv[0]} (PATH'te yok; ORKESTRA_EXTRA_PATH ile ekleyebilirsiniz)")
            self.items[pid] = {"id": pid, "popen": p, "pid": p.pid, "argv": argv, "port": spec.get("port"),
                               "started": time.time(), "log": lp, "rc": None}
            self._save()
            log(f"başlatıldı {pid} pid={p.pid}: {' '.join(argv)[:160]}")
            return {"ok": True, "id": pid, "pid": p.pid}

    def stop(self, pid, grace=15):
        with self.lock:
            it = self.items.get(pid)
        if not it:
            return {"ok": True, "note": "süreç yok"}
        p = it.get("popen")
        try:
            if IS_WIN:
                if p is not None:
                    p.terminate()
                elif psutil:  # ajan yeniden başladıktan sonra sahiplenilmiş süreç
                    psutil.Process(it["pid"]).terminate()
            else:
                os.killpg(os.getpgid(it["pid"]), signal.SIGTERM)
        except Exception:
            pass
        end = time.time() + max(1, grace)
        while time.time() < end and self._state(it)[0] == "running":
            time.sleep(0.2)
        if self._state(it)[0] == "running":
            try:
                if IS_WIN and p is not None:
                    p.kill()
                elif IS_WIN and psutil:
                    psutil.Process(it["pid"]).kill()
                elif not IS_WIN:
                    os.killpg(os.getpgid(it["pid"]), signal.SIGKILL)
            except Exception:
                pass
            time.sleep(0.5)
        if p is not None:
            try:
                p.wait(timeout=3)
            except Exception:
                pass
        with self.lock:
            self.items.pop(pid, None)
            self._save()
        log(f"durduruldu {pid}")
        return {"ok": True}

    def stop_all(self, grace=10):
        for pid in list(self.items):
            self.stop(pid, grace)

    def logs(self, pid, tail):
        it = self.items.get(pid)
        path = it["log"] if it else os.path.join(self.log_dir, re.sub(r"[^A-Za-z0-9._-]", "", pid) + ".log")
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 200000))
                data = f.read().decode("utf-8", "replace")
            return "\n".join(data.splitlines()[-tail:])
        except Exception:
            return "(günlük yok)"


# ------------------------------------------------------------------------------------ ağ üzerinden dağıtım
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,120}")


class Cancelled(Exception):
    pass


class NoSpace(Exception):
    pass


def make_ticket(secret: str, name: str, ttl: float = 3600) -> str:
    """Dosya almak için süreli bilet: makineler birbirinin token'ını öğrenmeden veri alışverişi yapar."""
    exp = int(time.time() + ttl)
    sig = hmac.new(secret.encode(), f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def ticket_ok(secret: str, name: str, ticket: str) -> bool:
    try:
        exp_s, sig = ticket.split(".", 1)
        if int(exp_s) < time.time():
            return False
    except Exception:
        return False
    want = hmac.new(secret.encode(), f"{name}|{exp_s}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def bearer(headers) -> str:
    h = headers.get("Authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


def serve_file(h, path: str) -> None:
    """BaseHTTPRequestHandler üzerinden Range destekli, sendfile ile dosya gönderimi."""
    size = os.path.getsize(path)
    start, end, code = 0, size - 1, 200
    m = re.fullmatch(r"bytes=(\d+)-(\d*)", (h.headers.get("Range") or "").strip())
    if m:
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        if start >= size or start > end:
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{size}")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        code = 206
    length = max(0, end - start + 1)
    h.send_response(code)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", str(length))
    h.send_header("Accept-Ranges", "bytes")
    if code == 206:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.end_headers()
    if h.command == "HEAD" or not length:
        return
    h.close_connection = True
    with open(path, "rb") as f:
        h.connection.sendfile(f, start, length)


def serve_zero(h, mb: int) -> None:
    """Ağ hızı ölçümü için veri akışı (diske dokunmaz)."""
    block = os.urandom(1 << 20)
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", str(mb << 20))
    h.end_headers()
    h.close_connection = True
    for _ in range(mb):
        h.wfile.write(block)


def safe_join(root: str, rel: str) -> str:
    rel = str(rel).replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError(f"güvensiz yol: {rel}")
    return os.path.join(root, *parts)


def _common_root(names) -> str:
    roots, nested = set(), False
    for n in names:
        p = [x for x in n.replace("\\", "/").split("/") if x not in ("", ".")]
        if not p:
            continue
        roots.add(p[0])
        nested = nested or len(p) > 1
    return next(iter(roots)) if len(roots) == 1 and nested else ""


def _strip(name: str, root: str) -> str:
    p = [x for x in name.replace("\\", "/").split("/") if x not in ("", ".")]
    if root and p and p[0] == root:
        p = p[1:]
    return "/".join(p)


def extract_archive(path: str, dest: str) -> bool:
    """tar.* ya da zip paketini güvenli biçimde açar (yol kaçışı yok). Arşiv değilse False."""
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as t:
            members = t.getmembers()
            root = _common_root(m.name for m in members)
            links = []
            for m in members:
                name = _strip(m.name, root)
                if not name:
                    continue
                target = safe_join(dest, name)
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.isreg():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with t.extractfile(m) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
                    os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
                elif m.issym() or m.islnk():
                    links.append((m, target, name))
            for m, target, name in links:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src_rel = _strip(m.linkname, root) if m.islnk() else os.path.normpath(os.path.join(os.path.dirname(name), m.linkname)).replace("\\", "/")
                source = safe_join(dest, src_rel)  # paketin dışını gösteren bağlantı reddedilir
                if m.issym() and not IS_WIN:
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(m.linkname, target)
                elif os.path.isfile(source):
                    shutil.copy2(source, target)
        return True
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            root = _common_root(i.filename for i in infos)
            for i in infos:
                name = _strip(i.filename, root)
                if not name:
                    continue
                target = safe_join(dest, name)
                if i.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(i) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                mode = (i.external_attr >> 16) & 0o777
                os.chmod(target, 0o755 if (mode & 0o111 or not mode) else 0o644)
        return True
    return False


def find_entry(root: str, entry: str, exe_name: str) -> str:
    """Paketteki çalıştırılacak dosyayı bulur: açık yol, ad eşleşmesi ya da tek çalıştırılabilir."""
    if entry:
        p = safe_join(root, entry)
        if not os.path.isfile(p):
            raise ValueError(f"pakette giriş dosyası yok: {entry}")
        return p
    want = {exe_name, exe_name + ".exe", exe_name + ".py", exe_name + ".sh"} if exe_name else set()
    hits, execs = [], []
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            if f in want:
                hits.append(p)
            elif not f.lower().endswith((".so", ".dll", ".txt", ".md", ".json", ".h", ".hpp", ".a", ".o", ".lib")) \
                    and (f.lower().endswith(".exe") or (not IS_WIN and os.access(p, os.X_OK))):
                execs.append(p)
    if hits:
        return sorted(hits, key=lambda x: (x.count(os.sep), len(x)))[0]
    if len(execs) == 1:
        return execs[0]
    raise ValueError(f"pakette '{exe_name}' bulunamadı" + (f"; çalıştırılabilirler: {', '.join(os.path.basename(x) for x in execs[:6])}" if execs else ""))


class Fabric:
    """Bu makinedeki dosya deposu: içerik adresli dosyalar, runtime paketleri, model klasörleri."""

    CMAKE_BASE = ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF", "-DGGML_NATIVE=ON", "-DLLAMA_BUILD_TESTS=OFF",
                  "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_SERVER=ON", "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=ON",
                  "-DWHISPER_BUILD_EXAMPLES=ON", "-DWHISPER_SDL2=OFF"]
    BACKEND = {"cuda": ["-DGGML_CUDA=ON"], "vulkan": ["-DGGML_VULKAN=ON"], "cpu": []}

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.d = {k: os.path.join(self.root, k) for k in ("blobs", "tmp", "runtimes", "models", "build", "meta")}
        for p in self.d.values():
            os.makedirs(p, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.rev = 1
        self.fetch_sem = threading.BoundedSemaphore(2)
        self.build_sem = threading.BoundedSemaphore(1)
        self.serving = 0
        self.served = 0
        self._managed = (os.path.realpath(self.d["runtimes"]) + os.sep).lower() if IS_WIN else os.path.realpath(self.d["runtimes"]) + os.sep

    # ---- yardımcılar --------------------------------------------------------
    def is_managed(self, real_path: str) -> bool:
        p = real_path.lower() if IS_WIN else real_path
        return p.startswith(self._managed)

    def touch(self) -> None:
        with self.lock:
            self.rev += 1

    def blob_path(self, sha: str) -> str:
        if not HEX64.fullmatch(sha or ""):
            raise ValueError("geçersiz sha256")
        return os.path.join(self.d["blobs"], sha)

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def _meta(self, kind: str, key: str) -> str:
        return os.path.join(self.d["meta"], f"{kind}-{key}.json")

    def _read_meta(self, kind: str, key: str) -> dict:
        try:
            with open(self._meta(kind, key), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_meta(self, kind: str, key: str, data: dict) -> None:
        tmp = self._meta(kind, key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._meta(kind, key))

    # ---- durum --------------------------------------------------------------
    def state(self) -> dict:
        blobs = {}
        for n in os.listdir(self.d["blobs"]):
            if HEX64.fullmatch(n):
                try:
                    blobs[n] = os.path.getsize(os.path.join(self.d["blobs"], n))
                except OSError:
                    pass
        rts = {}
        for n in os.listdir(self.d["runtimes"]):
            p = os.path.join(self.d["runtimes"], n)
            meta = self._read_meta("rt", n)
            if os.path.isdir(p) and meta.get("entry"):
                rts[n] = {"dir": p, "entry": os.path.join(p, *meta["entry"].split("/")), "sha256": meta.get("sha256"), "at": meta.get("at")}
        models = {}
        for n in os.listdir(self.d["models"]):
            p = os.path.join(self.d["models"], n)
            meta = self._read_meta("model", n)
            if os.path.isdir(p) and meta:
                models[n] = {"dir": p, "files": meta.get("files", []), "at": meta.get("at")}
        du = shutil.disk_usage(self.root)
        return {"root": self.root, "sep": os.sep, "free_gb": round(du.free / 2**30, 2), "total_gb": round(du.total / 2**30, 2),
                "blobs": blobs, "runtimes": rts, "models": models, "jobs": self.job_list(), "rev": self.rev}

    def view(self, j: dict) -> dict:
        return {k: v for k, v in j.items() if not k.startswith("_")}

    def job_list(self) -> list:
        with self.lock:
            return [self.view(j) for j in self.jobs.values()]

    def summary(self) -> dict:
        with self.lock:
            active = [self.view(j) for j in self.jobs.values() if j["state"] in ("queued", "running")]
            return {"rev": self.rev, "free_gb": round(self.free_bytes() / 2**30, 2), "jobs": active, "serving": self.serving, "served": self.served}

    # ---- işler --------------------------------------------------------------
    def _job(self, jid: str, kind: str, total: int, target) -> dict:
        with self.lock:
            j = self.jobs.get(jid)
            if j and j["state"] in ("queued", "running"):
                return self.view(j)
            j = {"id": jid, "kind": kind, "state": "queued", "bytes": 0, "total": int(total or 0), "bps": 0, "error": "", "code": "",
                 "src": "", "step": "", "started": time.time(), "ended": 0, "_cancel": False}
            self.jobs[jid] = j
            done = sorted((x for x in self.jobs.values() if x["ended"]), key=lambda x: x["ended"])
            for x in done[:-40]:
                self.jobs.pop(x["id"], None)
        threading.Thread(target=self._run, args=(j, target), daemon=True).start()
        return self.view(j)

    def _run(self, j: dict, target) -> None:
        try:
            target(j)
            j["state"] = "done"
        except Cancelled:
            j["state"], j["error"] = "cancelled", "iptal edildi"
        except NoSpace as e:
            j["state"], j["code"], j["error"] = "failed", "no_space", str(e)
        except Exception as e:
            j["state"], j["error"] = "failed", f"{e}"[:600]
            log(f"iş başarısız {j['id']}: {e}")
        finally:
            j["ended"], j["bps"] = time.time(), 0
            self.touch()

    def cancel(self, jid: str) -> dict:
        with self.lock:
            j = self.jobs.get(jid)
            if j:
                j["_cancel"] = True
        return {"ok": True}

    # ---- dosya çekme --------------------------------------------------------
    def fetch(self, spec: dict) -> dict:
        sha = str(spec.get("sha256", ""))
        final = self.blob_path(sha)
        size = int(spec.get("size") or 0)
        sources = spec.get("sources") or []
        limit = float(spec.get("limit_bps") or 0)
        reserve = int(float(spec.get("reserve_gb", 2)) * 2**30)
        jid = "fetch:" + sha[:16]
        if os.path.isfile(final) and os.path.getsize(final) == size:
            with self.lock:
                return {"id": jid, "kind": "fetch", "state": "done", "bytes": size, "total": size, "bps": 0, "error": "", "code": "", "src": "", "step": "", "started": time.time(), "ended": time.time()}
        if not sources:
            raise ValueError("kaynak verilmedi")

        def run(j):
            with self.fetch_sem:
                j["state"] = "running"
                part = os.path.join(self.d["tmp"], sha + ".part")
                have = os.path.getsize(part) if os.path.exists(part) else 0
                if have > size:
                    os.remove(part)
                    have = 0
                if self.free_bytes() < (size - have) + reserve:
                    raise NoSpace(f"diskte yer yok: {(size - have) / 2**30:.1f} GB gerekli, {self.free_bytes() / 2**30:.1f} GB boş")
                errors = []
                for src in sources:
                    if j["_cancel"]:
                        raise Cancelled()
                    try:
                        j["src"] = urlsplit(src["url"]).hostname or ""
                        self._pull(j, src, part, sha, size, limit)
                        break
                    except Cancelled:
                        raise
                    except Exception as e:
                        errors.append(f"{j['src']}: {e}")
                else:
                    raise IOError("; ".join(errors)[:500] or "kaynak yok")
                os.replace(part, final)
                try:
                    os.chmod(final, 0o644)
                except OSError:
                    pass

        return self._job(jid, "fetch", size, run)

    def _pull(self, j: dict, src: dict, part: str, sha: str, size: int, limit: float) -> None:
        u = urlsplit(src["url"])
        have = os.path.getsize(part) if os.path.exists(part) else 0
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=40)
        try:
            headers = {"Authorization": "Bearer " + src.get("ticket", "")}
            if have:
                headers["Range"] = f"bytes={have}-"
            conn.request("GET", u.path + (("?" + u.query) if u.query else ""), headers=headers)
            r = conn.getresponse()
            if r.status == 416:
                have = 0
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise IOError("aralık geçersiz, baştan denenecek")
            if r.status not in (200, 206):
                raise IOError(f"HTTP {r.status}")
            if r.status == 200:
                have = 0
            h = hashlib.sha256() if have == 0 else None
            j["bytes"] = have
            buf = bytearray(1 << 20)
            view = memoryview(buf)
            t0, b0, tick, btick = time.time(), have, time.time(), have
            with open(part, "r+b" if have else "wb") as f:
                f.seek(have)
                while True:
                    n = r.readinto(buf)
                    if not n:
                        break
                    if j["_cancel"]:
                        raise Cancelled()
                    f.write(view[:n])
                    if h:
                        h.update(view[:n])
                    have += n
                    j["bytes"] = have
                    t = time.time()
                    if t - tick >= 0.5:
                        j["bps"] = int((have - btick) / (t - tick))
                        tick, btick = t, have
                    if limit > 0:
                        wait = (have - b0) / limit - (t - t0)
                        if wait > 0:
                            time.sleep(min(wait, 1.0))
        finally:
            conn.close()
        if have != size:
            raise IOError(f"eksik veri: {have}/{size} bayt")
        j["step"] = "doğrulanıyor"
        digest = h.hexdigest() if h else self._hash_file(part, j)
        if digest != sha:
            os.remove(part)
            raise ValueError("sha256 uyuşmuyor (dosya bozuk, silindi)")

    def _hash_file(self, path: str, j: dict) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 22)
                if not b:
                    return h.hexdigest()
                if j["_cancel"]:
                    raise Cancelled()
                h.update(b)

    # ---- runtime paketi ------------------------------------------------------
    def _finish_runtime(self, rid: str, tmp: str, sha: str, entry_path: str) -> None:
        final = os.path.join(self.d["runtimes"], rid)
        rel = os.path.relpath(entry_path, tmp).replace(os.sep, "/")
        if not IS_WIN:
            os.chmod(entry_path, os.stat(entry_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if os.path.exists(final):
            shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        self._write_meta("rt", rid, {"sha256": sha, "entry": rel, "at": time.time()})

    def install(self, spec: dict) -> dict:
        rid, sha = str(spec.get("id", "")), str(spec.get("sha256", ""))
        if not SAFE_ID.fullmatch(rid):
            raise ValueError("geçersiz paket kimliği")
        blob = self.blob_path(sha)
        entry, exe_name, filename = str(spec.get("entry") or ""), str(spec.get("exe_name") or ""), os.path.basename(str(spec.get("filename") or ""))

        def run(j):
            j["state"], j["step"] = "running", "paket açılıyor"
            if not os.path.isfile(blob):
                raise IOError("paket dosyası bu makinede yok")
            tmp = os.path.join(self.d["runtimes"], "." + rid + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            try:
                if not extract_archive(blob, tmp):  # arşiv değil: tek çalıştırılabilir dosya
                    name = exe_name or filename or "runtime"
                    if filename.lower().endswith((".py", ".exe", ".sh")) and not name.lower().endswith((".py", ".exe", ".sh")):
                        name += os.path.splitext(filename)[1]
                    shutil.copyfile(blob, os.path.join(tmp, name))
                    os.chmod(os.path.join(tmp, name), 0o755)
                self._finish_runtime(rid, tmp, sha, find_entry(tmp, entry, exe_name))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return self._job("install:" + rid, "install", 0, run)

    def build(self, spec: dict) -> dict:
        """Kaynak paketini bu makinede, bu makinenin işlemcisi ve GPU'su için derler."""
        rid, sha = str(spec.get("id", "")), str(spec.get("sha256", ""))
        if not SAFE_ID.fullmatch(rid):
            raise ValueError("geçersiz paket kimliği")
        blob = self.blob_path(sha)
        backend, exe_name = str(spec.get("backend") or "cpu"), str(spec.get("exe_name") or "")
        extra = [str(x) for x in (spec.get("cmake") or [])]
        entry = str(spec.get("entry") or "")

        def run(j):
            with self.build_sem:
                j["state"], j["step"] = "running", "kaynak açılıyor"
                wd = os.path.join(self.d["build"], rid)
                shutil.rmtree(wd, ignore_errors=True)
                src, out = os.path.join(wd, "src"), os.path.join(wd, "out")
                os.makedirs(src)
                os.makedirs(out)
                logp = os.path.join(self.d["build"], rid + ".log")
                if not os.path.isfile(blob):
                    raise IOError("kaynak paketi bu makinede yok")
                if not extract_archive(blob, src):
                    raise ValueError("kaynak paketi tar/zip değil")
                env = dict(os.environ, OUT=out, BACKEND=backend, JOBS=str(os.cpu_count() or 2), EXE_NAME=exe_name)
                with open(logp, "wb", buffering=0) as lg:
                    custom = [p for p in ("orkestra-build.py", "orkestra-build.sh") if os.path.isfile(os.path.join(src, p))]
                    if custom:
                        j["step"] = "özel derleme betiği"
                        cmd = [sys.executable, custom[0]] if custom[0].endswith(".py") else ["bash", custom[0]]
                        self._cmd(j, cmd, src, env, lg)
                    elif os.path.isfile(os.path.join(src, "CMakeLists.txt")):
                        if not shutil.which("cmake"):
                            raise IOError("cmake kurulu değil (kurulumda 'derleme araçları' seçeneğini açın)")
                        j["step"] = "yapılandırılıyor"
                        gen = ["-G", "Ninja"] if shutil.which("ninja") else []
                        self._cmd(j, ["cmake", "-S", ".", "-B", "_b"] + gen + self.CMAKE_BASE + self.BACKEND.get(backend, []) + extra, src, env, lg)
                        j["step"] = "derleniyor"
                        tgt = ["--target", exe_name] if exe_name else []
                        self._cmd(j, ["cmake", "--build", "_b", "--config", "Release", "-j", env["JOBS"]] + tgt, src, env, lg)
                        for d, _, files in os.walk(os.path.join(src, "_b")):
                            if os.path.basename(d) in ("bin", "Release"):
                                for f in files:
                                    p = os.path.join(d, f)
                                    if os.access(p, os.X_OK) or f.endswith((".so", ".dll")) or ".so." in f:
                                        shutil.copy2(p, os.path.join(out, f))
                    else:
                        raise ValueError("pakette CMakeLists.txt ya da orkestra-build.sh yok")
                j["step"] = "kuruluyor"
                self._finish_runtime(rid, out, sha, find_entry(out, entry, exe_name))
                shutil.rmtree(wd, ignore_errors=True)

        return self._job("build:" + rid, "build", 0, run)

    def _cmd(self, j, argv, cwd, env, lg) -> None:
        lg.write(("$ " + " ".join(argv) + "\n").encode())
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=lg, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        while p.poll() is None:
            if j["_cancel"]:
                p.kill()
                p.wait()
                raise Cancelled()
            time.sleep(0.3)
        if p.returncode != 0:
            raise IOError(f"{os.path.basename(argv[0])} başarısız (çıkış {p.returncode}): " + self.job_log(j["id"], 6)[-300:])

    def job_log(self, jid: str, tail: int = 200) -> str:
        rid = jid.split(":", 1)[-1]
        if not SAFE_ID.fullmatch(rid):
            return ""
        try:
            with open(os.path.join(self.d["build"], rid + ".log"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 200000))
                return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-tail:])
        except OSError:
            return ""

    # ---- model klasörü --------------------------------------------------------
    def link(self, spec: dict) -> dict:
        key = str(spec.get("key", ""))
        if not SAFE_ID.fullmatch(key):
            raise ValueError("geçersiz model anahtarı")
        files = spec.get("files") or []
        d = os.path.join(self.d["models"], key)
        with self.lock:
            os.makedirs(d, exist_ok=True)
            wanted = set()
            for f in files:
                target, blob = safe_join(d, f["path"]), self.blob_path(f["sha256"])
                if not os.path.isfile(blob):
                    raise IOError(f"dosya bu makinede yok: {f['path']}")
                wanted.add(os.path.normcase(target))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.exists(target):
                    try:
                        if os.path.samefile(target, blob):
                            continue
                    except OSError:
                        pass
                    os.remove(target)
                try:
                    os.link(blob, target)  # aynı dosya sisteminde ek yer kaplamaz
                except OSError:
                    if IS_WIN:
                        shutil.copy2(blob, target)
                    else:
                        os.symlink(blob, target)
            for root, _, names in os.walk(d):
                for n in names:
                    p = os.path.join(root, n)
                    if os.path.normcase(p) not in wanted:
                        os.remove(p)
            self._write_meta("model", key, {"files": [f["sha256"] for f in files], "at": time.time()})
            self.rev += 1
        return {"ok": True, "dir": d}

    def _in_use(self, path: str) -> bool:
        needle = os.path.normcase(path)
        for it in PROCS.items.values() if PROCS else ():
            if PROCS._state(it)[0] == "running" and any(needle in os.path.normcase(str(a)) for a in it.get("argv", [])):
                return True
        return False

    def remove(self, spec: dict) -> dict:
        removed, kept = [], []
        for kind, base, meta in (("models", self.d["models"], "model"), ("runtimes", self.d["runtimes"], "rt")):
            for key in spec.get(kind) or []:
                if not SAFE_ID.fullmatch(str(key)):
                    continue
                p = os.path.join(base, key)
                if self._in_use(p):
                    kept.append(key)
                    continue
                shutil.rmtree(p, ignore_errors=True)
                try:
                    os.remove(self._meta(meta, key))
                except OSError:
                    pass
                removed.append(key)
        for sha in spec.get("blobs") or []:
            try:
                os.remove(self.blob_path(str(sha)))
                removed.append(str(sha)[:12])
            except (OSError, ValueError):
                pass
        self.touch()
        return {"ok": True, "removed": removed, "kept": kept, "free_gb": round(self.free_bytes() / 2**30, 2)}

    def bwtest(self, spec: dict) -> dict:
        """Verilen kaynaktan veri çekip hızı ölçer (diske yazmaz)."""
        u = urlsplit(str(spec["url"]))
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=20)
        t0 = time.time()
        n = 0
        try:
            conn.request("GET", u.path + (("?" + u.query) if u.query else ""), headers={"Authorization": "Bearer " + str(spec.get("ticket", ""))})
            r = conn.getresponse()
            if r.status != 200:
                raise IOError(f"HTTP {r.status}")
            buf = bytearray(1 << 20)
            limit_s = float(spec.get("max_s", 12))
            while time.time() - t0 < limit_s:
                k = r.readinto(buf)
                if not k:
                    break
                n += k
        finally:
            conn.close()
        dt = max(1e-6, time.time() - t0)
        return {"bytes": n, "seconds": round(dt, 3), "bps": int(n / dt), "mbit": round(n * 8 / dt / 1e6, 1)}


def self_update(code_b64: str, sha: str) -> dict:
    code = base64.b64decode(code_b64)
    if hashlib.sha256(code).hexdigest() != sha:
        raise ValueError("ajan kodu özeti uyuşmuyor")
    compile(code, "node_agent.py", "exec")
    path = os.path.abspath(__file__)
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(code)
    try:
        os.chmod(tmp, 0o755)
    except OSError:
        pass
    os.replace(tmp, path)

    def restart():
        time.sleep(0.4)
        log("ajan yeni sürümle yeniden başlıyor (çıkarım süreçleri çalışmaya devam eder)")
        if IS_WIN:
            subprocess.Popen([sys.executable] + sys.argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                             stdout=sys.stdout.fileno() if hasattr(sys.stdout, "fileno") else None, stderr=subprocess.STDOUT)
            os._exit(0)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=restart, daemon=True).start()
    return {"ok": True, "restarting": True}


# ------------------------------------------------------------------------------------ güç
def do_power(action: str, procs: ProcMgr):
    cmds = {"suspend": ["systemctl", "suspend"], "reboot": ["systemctl", "reboot"], "poweroff": ["systemctl", "poweroff"]}
    cmd = cmds[action]

    def run():
        time.sleep(1.5)
        try:
            procs.stop_all(8)
        except Exception:
            pass
        if CFG["dry_power"]:
            log(f"[dry-power] çalıştırılacaktı: sudo -n {' '.join(cmd)}")
            return
        full = cmd if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo", "-n"] + cmd
        log("güç komutu: " + " ".join(full))
        r = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode != 0:
            log(f"güç komutu başarısız ({r.returncode}): {r.stdout.strip()[:200]}")

    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------------------------ HTTP
PROCS: ProcMgr = None  # type: ignore
FAB: Fabric = None  # type: ignore


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def handle_error(self, request, client_address):  # kopan bağlantılar normaldir
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, socket.timeout)):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrkestraAgent/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if CFG["allow_ips"] and self.client_address[0] not in CFG["allow_ips"]:
            return False
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return bool(CFG["token"]) and hmac.compare_digest(tok.encode(), CFG["token"].encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8_000_000:
            raise ValueError("gövde çok büyük")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/healthz":
                return self._send(200, {"status": "ok", "agent": VERSION, "id": CFG["node_id"]})
            q = parse_qs(u.query)
            if u.path.startswith("/v1/fabric/blob/"):
                return self._blob(u.path.rsplit("/", 1)[1], q)
            if not self._authorized():
                return self._send(401, {"error": "yetkisiz"})
            if u.path == "/v1/info":
                return self._send(200, read_info())
            if u.path == "/v1/metrics":
                return self._send(200, read_metrics(PROCS))
            if u.path == "/v1/logs":
                return self._send(200, {"text": PROCS.logs(q.get("id", [""])[0], int(q.get("tail", ["200"])[0]))})
            if u.path == "/v1/fabric/state":
                return self._send(200, FAB.state())
            if u.path == "/v1/fabric/jobs":
                return self._send(200, {"jobs": FAB.job_list(), "rev": FAB.rev})
            if u.path == "/v1/fabric/log":
                return self._send(200, {"text": FAB.job_log(q.get("id", [""])[0], int(q.get("tail", ["200"])[0]))})
            return self._send(404, {"error": "bulunamadı"})
        except (ConnectionError, socket.timeout):
            self.close_connection = True
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _blob(self, name, q):
        """Başka makinelere dosya verir: IP kısıtı yoktur, süreli bilet (ya da ajan token'ı) ister."""
        tok = bearer(self.headers)
        if not (ticket_ok(CFG["token"], name, tok) or (tok and hmac.compare_digest(tok.encode(), CFG["token"].encode()))):
            return self._send(401, {"error": "geçersiz bilet"})
        if name == "zero":
            return serve_zero(self, max(1, min(8192, int(q.get("mb", ["256"])[0]))))
        p = FAB.blob_path(name)
        if not os.path.isfile(p):
            return self._send(404, {"error": "dosya yok"})
        FAB.serving += 1
        try:
            serve_file(self, p)
            FAB.served += 1
        finally:
            FAB.serving -= 1

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authorized():
            return self._send(401, {"error": "yetkisiz"})
        try:
            b = self._body()
            if u.path == "/v1/launch":
                return self._send(200, PROCS.launch(b))
            if u.path == "/v1/stop":
                return self._send(200, PROCS.stop(str(b.get("id", "")), int(b.get("grace_s", 15))))
            if u.path == "/v1/power":
                a = str(b.get("action", ""))
                if a not in ("suspend", "reboot", "poweroff"):
                    return self._send(400, {"error": "geçersiz işlem"})
                if IS_WIN and not CFG["dry_power"]:
                    return self._send(501, {"error": "Windows'ta güç komutları desteklenmiyor"})
                do_power(a, PROCS)
                return self._send(200, {"ok": True, "action": a, "in_s": 1.5})
            if u.path == "/v1/fabric/fetch":
                return self._send(200, FAB.fetch(b))
            if u.path == "/v1/fabric/install":
                return self._send(200, FAB.install(b))
            if u.path == "/v1/fabric/build":
                return self._send(200, FAB.build(b))
            if u.path == "/v1/fabric/link":
                return self._send(200, FAB.link(b))
            if u.path == "/v1/fabric/remove":
                return self._send(200, FAB.remove(b))
            if u.path == "/v1/fabric/cancel":
                return self._send(200, FAB.cancel(str(b.get("id", ""))))
            if u.path == "/v1/fabric/bwtest":
                return self._send(200, FAB.bwtest(b))
            if u.path == "/v1/fabric/update":
                return self._send(200, self_update(str(b.get("code", "")), str(b.get("sha256", ""))))
            return self._send(404, {"error": "bulunamadı"})
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e)})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except RuntimeError as e:
            return self._send(409, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    global PROCS, FAB
    ap = argparse.ArgumentParser(description="Orkestra düğüm ajanı")
    ap.add_argument("--host", default=os.environ.get("ORKESTRA_AGENT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ORKESTRA_AGENT_PORT", "7777")))
    ap.add_argument("--token", default=os.environ.get("ORKESTRA_AGENT_TOKEN", ""))
    ap.add_argument("--allow-from", default=os.environ.get("ORKESTRA_ALLOW_FROM", ""), help="izinli IP'ler (virgülle)")
    ap.add_argument("--allow-exe", action="append", default=[], help="ek izinli çalıştırılabilir: ad ya da TAM YOL (tekrarlanabilir)")
    ap.add_argument("--allow-dir", action="append", default=[], help="altındaki çalıştırılabilirlere izin veren klasör (tekrarlanabilir)")
    ap.add_argument("--allow-module", action="append", default=[], help="python -m için ek izinli modül")
    ap.add_argument("--state-dir", default=os.environ.get("ORKESTRA_STATE_DIR", os.path.join(os.path.expanduser("~"), ".orkestra-agent")))
    ap.add_argument("--store-dir", default=os.environ.get("ORKESTRA_STORE_DIR", ""), help="model ve runtime deposu (varsayılan: <state-dir>/store)")
    ap.add_argument("--dry-power", action="store_true", help="güç komutlarını gerçekten çalıştırma (sınama)")
    a = ap.parse_args()
    if not a.token:
        sys.exit("Token gerekli (--token ya da ORKESTRA_AGENT_TOKEN). Güvenlik için tokensiz başlatılmaz.")
    extra = os.environ.get("ORKESTRA_EXTRA_PATH", "")
    if extra:
        os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")
    CFG["token"] = a.token
    CFG["allow_ips"] = {x.strip() for x in a.allow_from.split(",") if x.strip()}
    CFG["dry_power"] = a.dry_power
    CFG["allow_exe"] = set(DEFAULT_ALLOW_EXE)
    extra_exe = list(a.allow_exe) + [x.strip() for x in os.environ.get("ORKESTRA_ALLOW_EXE", "").split(",") if x.strip()]
    for x in extra_exe:
        if os.path.isabs(x):
            CFG["allow_paths"].add(x)
            CFG["allow_paths_real"].add(os.path.realpath(x))
        else:
            x = x.lower()
            CFG["allow_exe"].add(x[:-4] if x.endswith(".exe") else x)
    CFG["allow_dirs"] = [os.path.realpath(d) for d in list(a.allow_dir) + [x.strip() for x in os.environ.get("ORKESTRA_ALLOW_DIRS", "").split(",") if x.strip()]]
    CFG["allow_modules"] = set(DEFAULT_ALLOW_MODULES) | set(a.allow_module)
    os.makedirs(a.state_dir, exist_ok=True)
    CFG["node_id"] = hashlib.sha256(f"{machine_id()}|{os.path.abspath(a.state_dir)}".encode()).hexdigest()[:20]
    FAB = Fabric(a.store_dir or os.path.join(a.state_dir, "store"))
    PROCS = ProcMgr(os.path.join(a.state_dir, "procs.json"), os.path.join(a.state_dir, "logs"))
    read_cpu()  # ilk ölçüm referansı
    read_net()
    _gpu_cache["bg"] = True  # GPU ölçümü arka planda; açılışı bekletmez
    threading.Thread(target=_gpu_loop, daemon=True).start()
    srv = Server((a.host, a.port), Handler)

    def bye(*_):
        log("kapatılıyor (çıkarım süreçleri çalışmaya devam eder)")
        threading.Thread(target=srv.shutdown, daemon=True).start()

    if not IS_WIN:
        signal.signal(signal.SIGTERM, bye)
    log(f"Ajan {VERSION} dinliyor: {a.host}:{a.port} — izinli adlar: {', '.join(sorted(CFG['allow_exe']))}"
        + (f"; tam yollar: {', '.join(sorted(CFG['allow_paths']))}" if CFG["allow_paths"] else "")
        + (f"; klasörler: {', '.join(CFG['allow_dirs'])}" if CFG["allow_dirs"] else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
