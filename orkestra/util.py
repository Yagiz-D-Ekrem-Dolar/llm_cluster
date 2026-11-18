"""Küçük ortak yardımcılar."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time

log = logging.getLogger("orkestra")


def now() -> float:
    return time.time()


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)


def jload(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def rand_token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


def hash_user(uid: str, salt: str) -> str:
    """Kullanıcı kimliğini geri döndürülemez biçimde anonimleştirir."""
    return "u_" + hashlib.sha256(f"{salt}:{uid}".encode()).hexdigest()[:16]


_PII = [
    (re.compile(r"\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", re.I), "[iban]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[e-posta]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "[kart-no]"),
    (re.compile(r"(?<!\d)(?:\+?90[\s-]?|0)?\(?5\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)"), "[telefon]"),
    (re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)"), "[kimlik-no]"),
]


def scrub_pii(text: str) -> str:
    """E-posta, telefon, IBAN, kart ve T.C. kimlik numarası benzeri verileri maskeler."""
    if not text:
        return text
    for rx, rep in _PII:
        text = rx.sub(rep, text)
    return text


def estimate_vram_gb(repo: str, extra: str = "") -> float:
    """Model adından kaba VRAM (GB) tahmini. Kullanıcı elle girmediyse kullanılır."""
    s = f"{repo} {extra}".lower()
    m = re.search(r"(?<![\w.])e?(\d+(?:\.\d+)?)\s*b(?![a-z])", s)
    params = float(m.group(1)) if m else 7.0
    if re.search(r"iq2|q2", s):
        bpp = 0.35
    elif re.search(r"iq3|q3", s):
        bpp = 0.46
    elif re.search(r"iq4|q4|int4|awq|gptq|4bit|4-bit|nf4|mxfp4", s):
        bpp = 0.58
    elif re.search(r"q5", s):
        bpp = 0.70
    elif re.search(r"q6", s):
        bpp = 0.82
    elif re.search(r"q8|int8|fp8|8bit|8-bit", s):
        bpp = 1.06
    elif "gguf" in s:
        bpp = 0.58
    else:
        bpp = 2.05
    return round(params * bpp + 1.2, 1)


class EMA:
    """Üstel hareketli ortalama."""

    def __init__(self, alpha: float = 0.3, value: float | None = None):
        self.alpha = alpha
        self.v = value

    def add(self, x: float) -> float:
        self.v = x if self.v is None else self.v * (1 - self.alpha) + x * self.alpha
        return self.v

    def get(self, default: float = 0.0) -> float:
        return default if self.v is None else self.v


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x
