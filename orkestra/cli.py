"""Komut satırı girişi."""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from .app import Boot, serve


def main(argv=None) -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="orkestra", description="Orkestra – LLM küme yönetimi (kontrol sunucusu)")
    ap.add_argument("--demo", action="store_true", help="Simülatör: sahte düğümlerle denemek için (ayrı veri klasörü)")
    ap.add_argument("--host", default=None, help="Panel adresi (varsayılan: 0.0.0.0, demo'da 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8080, help="Panel portu")
    ap.add_argument("--public-port", type=int, default=8081, help="İnternete açılacak ağ geçidi portu")
    ap.add_argument("--public-host", default="127.0.0.1", help="Ağ geçidi adresi (varsayılan yalnızca bu makine; site tünelle gelir)")
    ap.add_argument("--fabric-port", type=int, default=8079, help="Makinelerin model/runtime dosyası çektiği port")
    ap.add_argument("--no-public", action="store_true", help="Herkese açık ağ geçidini başlatma")
    ap.add_argument("--data-dir", default=None, help="Veri klasörü (varsayılan: ./data/main, demo: ./data/demo)")
    ap.add_argument("--log-level", default="info")
    ap.add_argument("--reset-demo", action="store_true", help="Demo veri klasörünü silip sıfırdan başlat (yalnızca --demo ile)")
    a = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    data = Path(a.data_dir) if a.data_dir else root / "data" / ("demo" if a.demo else "main")
    if a.reset_demo:
        if not a.demo:
            ap.error("--reset-demo yalnızca --demo ile kullanılabilir (gerçek veriyi silmemek için)")
        shutil.rmtree(data, ignore_errors=True)
    host = a.host or ("127.0.0.1" if a.demo else "0.0.0.0")
    boot = Boot(data_dir=data, admin_host=host, admin_port=a.port, public=not a.no_public, public_host=a.public_host,
                public_port=a.public_port, fabric_port=a.fabric_port, demo=a.demo, log_level=a.log_level)
    try:
        asyncio.run(serve(boot))
    except KeyboardInterrupt:
        pass
