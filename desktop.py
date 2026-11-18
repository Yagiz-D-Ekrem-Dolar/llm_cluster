"""Masaüstü uygulaması: kontrol sunucusunu başlatır, paneli kendi penceresinde açar."""
from __future__ import annotations

import argparse
import asyncio
import os
import runpy
import sys
import threading
import time
import urllib.request
from pathlib import Path


def main() -> None:
    # Paketli uygulamada Python yoktur: ajan ve runtime betikleri yine bu exe ile çalışır
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".py"):
        script = sys.argv[1]
        sys.argv = sys.argv[1:]
        runpy.run_path(script, run_name="__main__")
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--public-port", type=int, default=8081)
    ap.add_argument("--fabric-port", type=int, default=8079)
    ap.add_argument("--data-dir", default=str(Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "KumeYonetimi"))
    ap.add_argument("--no-window", action="store_true", help="pencere açmadan çalış")
    a = ap.parse_args()
    data = Path(a.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # pencereli exe'de konsol yok
        sys.stdout = sys.stderr = open(data / "uygulama.log", "a", encoding="utf-8", buffering=1)

    from orkestra.app import Boot, serve
    boot = Boot(data_dir=data, admin_host="0.0.0.0", admin_port=a.port, public_host="127.0.0.1", public_port=a.public_port, fabric_port=a.fabric_port)
    threading.Thread(target=lambda: asyncio.run(serve(boot)), daemon=True).start()
    url = f"http://127.0.0.1:{a.port}/"
    for _ in range(150):
        try:
            urllib.request.urlopen(url + "api/auth/state", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    if a.no_window:
        threading.Event().wait()
    import webview
    webview.create_window("Mercan", url, width=1440, height=900, min_size=(1000, 640))
    webview.start()
    os._exit(0)


if __name__ == "__main__":
    main()
