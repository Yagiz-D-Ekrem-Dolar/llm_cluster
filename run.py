#!/usr/bin/env python3
"""Orkestra başlatıcı:  python run.py [--demo] [--port 8080]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orkestra.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
