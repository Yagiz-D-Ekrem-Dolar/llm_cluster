"""Demo (simülatör) uçtan uca sınamasını tek komutla çalıştırır: kendi geçici sunucusunu başlatır, sınar, kapatır.

Kullanım:  python tests/run_e2e_demo.py
(Çalışan bir panelinize dokunmaz: 8782/8783 portlarını ve geçici veri klasörünü kullanır.)
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="demo-e2e-"))
rc = 1
srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--demo", "--reset-demo", "--data-dir", str(tmp / "d"),
                        "--port", "8782", "--public-port", "8783"], stdout=open(tmp / "srv.log", "w"), stderr=subprocess.STDOUT)
try:
    t0 = time.time()
    while True:
        try:
            if httpx.get("http://127.0.0.1:8782/api/auth/state", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        if time.time() - t0 > 40:
            raise SystemExit("sunucu açılmadı:\n" + (tmp / "srv.log").read_text(encoding="utf-8", errors="ignore")[-1500:])
        time.sleep(0.5)
    rc = subprocess.call([sys.executable, str(ROOT / "tests" / "e2e_demo.py"), "http://127.0.0.1:8782", "http://127.0.0.1:8783"])
finally:
    srv.terminate()
    try:
        srv.wait(10)
    except Exception:
        srv.kill()
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(rc)
