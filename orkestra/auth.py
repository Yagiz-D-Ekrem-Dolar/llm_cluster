"""Yönetici oturumu ve API anahtarları."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from .util import jdump, jload, now, rand_token, sha256


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        scheme, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


class Principal:
    """İstek sahibi: yönetici oturumu ya da API anahtarı."""

    __slots__ = ("kind", "name", "key_id", "rpm", "models", "log_chats", "user_id", "plan")

    def __init__(self, kind: str, name: str, key_id: int = 0, rpm: int = 0, models=None, log_chats: bool = True,
                 user_id: str = "", plan: str = ""):
        self.user_id, self.plan = user_id, plan
        self.kind = kind
        self.name = name
        self.key_id = key_id
        self.rpm = rpm
        self.models = models or []
        self.log_chats = log_chats

    def allows(self, model: str) -> bool:
        return not self.models or model in self.models


class RateLimited(Exception):
    pass


class Auth:
    COOKIE = "orkestra_session"
    TTL = 12 * 3600

    def __init__(self, db):
        self.db = db
        self.sessions: dict[str, float] = {}
        self.fails: dict[str, list[float]] = {}
        self.buckets: dict[int, list[float]] = {}

    # ---- yönetici parolası --------------------------------------------------
    def configured(self) -> bool:
        return bool(self.db.get("admin.password_hash"))

    def set_password(self, pw: str) -> None:
        if len(pw) < 8:
            raise ValueError("Parola en az 8 karakter olmalı")
        self.db.set("admin.password_hash", hash_password(pw))
        self.sessions.clear()

    def login(self, pw: str, ip: str) -> str | None:
        t = now()
        recent = [x for x in self.fails.get(ip, []) if t - x < 300]
        self.fails[ip] = recent
        if len(recent) >= 8:
            raise RateLimited("Çok fazla başarısız deneme; 5 dakika sonra tekrar deneyin")
        if verify_password(pw, self.db.get("admin.password_hash") or ""):
            tok = rand_token(32)
            self.sessions[tok] = t + self.TTL
            self.fails.pop(ip, None)
            return tok
        recent.append(t)
        return None

    def session_valid(self, tok: str | None) -> bool:
        if not tok:
            return False
        exp = self.sessions.get(tok)
        t = now()
        if exp and exp > t:
            self.sessions[tok] = t + self.TTL
            return True
        self.sessions.pop(tok, None)
        return False

    def logout(self, tok: str | None) -> None:
        if tok:
            self.sessions.pop(tok, None)

    # ---- API anahtarları ----------------------------------------------------
    def create_key(self, name: str, rpm: int = 60, models=None, log_chats: bool = True) -> tuple[int, str]:
        plain = "sk-ork-" + rand_token(30)
        kid = self.db.x(
            "INSERT INTO api_keys(name,key_hash,prefix,rpm,models,log_chats,sign_secret,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (name, sha256(plain), plain[:12], int(rpm), jdump(models or []), 1 if log_chats else 0, rand_token(32), now()),
        )
        return kid, plain

    def sign_secret(self, key_id: int) -> str:
        r = self.db.one("SELECT sign_secret FROM api_keys WHERE id=? AND revoked=0", (key_id,))
        return (r or {}).get("sign_secret") or ""

    def make_user_token(self, key_id: int, user: str, plan: str = "", ttl: int = 3600) -> str:
        """Tarayıcıya verilecek kısa ömürlü jeton (sitenizin arka ucunda da aynı şekilde üretilebilir)."""
        payload = base64.urlsafe_b64encode(json.dumps({"k": key_id, "u": user, "p": plan, "exp": int(now() + ttl)}, separators=(",", ":")).encode()).rstrip(b"=").decode()
        sig = base64.urlsafe_b64encode(hmac.new(self.sign_secret(key_id).encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        return f"ut1.{payload}.{sig}"

    def verify_user_token(self, tok: str) -> Principal | None:
        """ut1.<yük>.<imza>: API anahtarını tarayıcıya vermeden kullanıcının doğrudan bağlanmasını sağlar."""
        try:
            _, payload, sig = tok.split(".")
            d = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            r = self.db.one("SELECT * FROM api_keys WHERE id=? AND revoked=0", (int(d["k"]),))
            if not r or not r["sign_secret"] or float(d["exp"]) < now() or not d.get("u"):
                return None
            want = base64.urlsafe_b64encode(hmac.new(r["sign_secret"].encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
            if not hmac.compare_digest(want, sig):
                return None
        except Exception:
            return None
        return Principal("user", r["name"], r["id"], 0, jload(r["models"], []), bool(r["log_chats"]), user_id=str(d["u"])[:128], plan=str(d.get("p") or "")[:32])

    def lookup_key(self, plain: str) -> Principal | None:
        r = self.db.one("SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (sha256(plain),))
        if not r:
            return None
        if now() - r["last_used_at"] > 60:
            self.db.x("UPDATE api_keys SET last_used_at=? WHERE id=?", (now(), r["id"]))
        return Principal("key", r["name"], r["id"], r["rpm"], jload(r["models"], []), bool(r["log_chats"]))

    def rate_ok(self, p: Principal) -> bool:
        """Anahtar başına dakikalık istek sınırı (token bucket)."""
        if p.kind != "key" or p.rpm <= 0:
            return True
        t = now()
        b = self.buckets.setdefault(p.key_id, [float(p.rpm), t])
        b[0] = min(float(p.rpm), b[0] + (t - b[1]) * p.rpm / 60.0)
        b[1] = t
        if b[0] >= 1:
            b[0] -= 1
            return True
        return False
