"""SQLite katmanı: şema, ayarlar ve küçük sorgu yardımcıları."""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .util import jdump, jload, now

log = logging.getLogger("orkestra")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  host TEXT NOT NULL,
  agent_port INTEGER NOT NULL DEFAULT 7777,
  agent_token TEXT NOT NULL DEFAULT '',
  mac TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'laptop',
  tags TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  allow_sleep INTEGER NOT NULL DEFAULT 1,
  allow_restart INTEGER NOT NULL DEFAULT 1,
  always_on INTEGER NOT NULL DEFAULT 0,
  weight REAL NOT NULL DEFAULT 1.0,
  warn_c REAL NOT NULL DEFAULT 80,
  crit_c REAL NOT NULL DEFAULT 90,
  sim INTEGER NOT NULL DEFAULT 0,
  sim_spec TEXT NOT NULL DEFAULT '{}',
  hw TEXT NOT NULL DEFAULT '{}',
  rt_paths TEXT NOT NULL DEFAULT '{}',
  last_status TEXT NOT NULL DEFAULT 'unknown',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtimes(
  key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  exe TEXT NOT NULL,
  args TEXT NOT NULL DEFAULT '[]',
  env TEXT NOT NULL DEFAULT '{}',
  health TEXT NOT NULL DEFAULT '/health',
  needs_gpu INTEGER NOT NULL DEFAULT 0,
  hook TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  paths TEXT NOT NULL DEFAULT '{}',
  builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS models(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  repo TEXT NOT NULL,
  runtime TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'llm',
  upstream TEXT NOT NULL DEFAULT '',
  ctx INTEGER NOT NULL DEFAULT 8192,
  vram_gb REAL NOT NULL DEFAULT 0,
  ram_gb REAL NOT NULL DEFAULT 0,
  gpu_count INTEGER NOT NULL DEFAULT 1,
  cpu_ok INTEGER NOT NULL DEFAULT 0,
  min_replicas INTEGER NOT NULL DEFAULT 0,
  max_replicas INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 5,
  max_concurrency INTEGER NOT NULL DEFAULT 4,
  idle_unload_min INTEGER NOT NULL DEFAULT 15,
  startup_timeout_s INTEGER NOT NULL DEFAULT 900,
  prefer_tags TEXT NOT NULL DEFAULT '[]',
  exclude_tags TEXT NOT NULL DEFAULT '[]',
  extra_args TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  notes TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  port INTEGER NOT NULL,
  desired TEXT NOT NULL DEFAULT 'running',
  origin TEXT NOT NULL DEFAULT 'manual',
  pinned INTEGER NOT NULL DEFAULT 0,
  gpus TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  last_used_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics(
  node_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  cpu REAL, mem REAL, cpu_temp REAL, gpu_util REAL, gpu_mem REAL, gpu_temp REAL,
  power REAL, net REAL, inflight INTEGER
);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics(node_id, ts);

CREATE TABLE IF NOT EXISTS req_log(
  ts REAL NOT NULL, kind TEXT, model TEXT, node_id INTEGER, user_id TEXT,
  status INTEGER, ms INTEGER, ttft_ms INTEGER, in_tok INTEGER, out_tok INTEGER, prio TEXT
);
CREATE INDEX IF NOT EXISTS ix_req_ts ON req_log(ts);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT NOT NULL,
  source TEXT NOT NULL, node_id INTEGER, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
  prefix TEXT NOT NULL, rpm INTEGER NOT NULL DEFAULT 60, models TEXT NOT NULL DEFAULT '[]',
  log_chats INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
  last_used_at REAL NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  turns INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, model TEXT, title TEXT NOT NULL DEFAULT '',
  first_ts REAL NOT NULL, last_ts REAL NOT NULL, turns INTEGER NOT NULL DEFAULT 0,
  exported_turns INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_conv_user ON conversations(user_id, last_ts);
CREATE INDEX IF NOT EXISTS ix_conv_last ON conversations(last_ts);

CREATE TABLE IF NOT EXISTS chat_turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, conv_id TEXT NOT NULL, user_id TEXT NOT NULL,
  model TEXT, node TEXT, messages TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', finish TEXT,
  in_tok INTEGER, out_tok INTEGER, ttft_ms INTEGER, total_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'ok', key_id INTEGER, source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_turn_conv ON chat_turns(conv_id, id);
CREATE INDEX IF NOT EXISTS ix_turn_ts ON chat_turns(ts);

CREATE TABLE IF NOT EXISTS question_sets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, items TEXT NOT NULL, created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS distill_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, status TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}', total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS distill_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, prompt TEXT NOT NULL,
  prompt_hash TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
  tokens INTEGER NOT NULL DEFAULT 0, teacher TEXT NOT NULL DEFAULT '', ts REAL,
  exported INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_di_job ON distill_items(job_id, status);
CREATE INDEX IF NOT EXISTS ix_di_hash ON distill_items(prompt_hash);

CREATE TABLE IF NOT EXISTS blobs(
  sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_builds(
  id TEXT PRIMARY KEY, runtime TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT 'linux-x86_64',
  backend TEXT NOT NULL DEFAULT 'cuda', format TEXT NOT NULL DEFAULT 'bin', sha256 TEXT NOT NULL, size INTEGER NOT NULL,
  filename TEXT NOT NULL DEFAULT '', entry TEXT NOT NULL DEFAULT '', cmake TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_builds_rt ON runtime_builds(runtime, active);

CREATE TABLE IF NOT EXISTS services(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, targets TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0, options TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_daily(
  day TEXT NOT NULL, user_id TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '', service TEXT NOT NULL, kind TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0, in_tok INTEGER NOT NULL DEFAULT 0, out_tok INTEGER NOT NULL DEFAULT 0,
  audio_s REAL NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, user_id, service, kind)
);
CREATE INDEX IF NOT EXISTS ix_usage_user ON usage_daily(user_id, day);

CREATE TABLE IF NOT EXISTS hf_syncs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL,
  repo TEXT, records INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
);
"""

# Varsayılan ayarlar. Anahtar -> varsayılan değer (tür buradan çıkarılır).
SETTINGS_DEFAULTS: dict = {
    # ağ
    "net.controller_url": "",
    "net.public_url": "",
    "net.admin_host": "0.0.0.0",
    "net.admin_port": 8080,
    "net.public_host": "0.0.0.0",
    "net.public_port": 8081,
    # otomasyon
    "auto.enabled": True,
    "auto.dry_run": False,
    "auto.mode": "balanced",  # performance | balanced | eco
    "auto.interval_s": 5,
    "auto.target_concurrency": 3,
    "auto.scale_up_cooldown_s": 20,
    "auto.allow_wake": True,
    "auto.allow_sleep": True,
    "auto.allow_reboot": True,
    "auto.sleep_after_idle_min": 15,
    "auto.min_awake_nodes": 1,
    "auto.wake_timeout_s": 240,
    "auto.max_reboots_per_hour": 1,
    # ağ geçidi (gateway)
    "gw.cold_start_timeout_s": 300,
    "gw.queue_timeout_s": 120,
    "gw.max_queue": 500,
    "gw.affinity": True,
    "gw.log_chats": True,
    "gw.public_enabled": True,
    "gw.cors_origins": [],
    "gw.max_upload_mb": 25,
    # satılan hizmet: paketler ve kullanıcı sınırları (0 = sınırsız; rank küçük olan kuyrukta önce gelir)
    "svc.default_plan": "free",
    "svc.plans": {
        "free": {"label": "Ücretsiz", "rank": 2, "rpm": 10, "day_tokens": 50000, "max_conc": 1, "max_tokens": 1024},
        "plus": {"label": "Plus", "rank": 1, "rpm": 40, "day_tokens": 500000, "max_conc": 2, "max_tokens": 4096},
        "pro": {"label": "Pro", "rank": 0, "rpm": 120, "day_tokens": 0, "max_conc": 4, "max_tokens": 8192},
    },
    # siteye bağlantı (modem CGNAT arkasında olabilir: küme dışarı doğru bağlanır)
    "uplink.mode": "off",
    "uplink.relay_url": "",
    "uplink.secret": "",
    "uplink.cf_token": "",
    "uplink.cf_path": "cloudflared",
    "uplink.public_url": "",
    "uplink.max_streams": 200,
    # ağ üzerinden dağıtım
    "fabric.secret": "",
    "fabric.hf_endpoint": "",
    "fabric.hf_limit_mbps": 0,
    "fabric.lan_limit_mbps": 0,
    "fabric.source_slots": 2,
    "fabric.preseed": True,
    "fabric.reserve_gb": 5,
    "fabric.auto_update_agent": True,
    "deploy.smoke_test": True,
    "deploy.avoid_battery": True,
    "net.modem_ip": "",
    "net.modem_name": "Superbox",
    # HuggingFace
    "hf.token": "",
    "hf.chat_repo": "",
    "hf.distill_repo": "",
    "hf.private": True,
    "hf.schedule": ["03:00"],
    "hf.auto": True,
    "hf.dry_run": True,
    "hf.anonymize": True,
    "hf.scrub_pii": True,
    "hf.idle_min": 30,
    "hf.salt": "",
    # distillation
    "distill.enabled": False,
    "distill.idle_min": 10,
    "distill.teacher": "",
    "distill.teacher_url": "",
    "distill.teacher_key": "",
    "distill.teacher_model": "",
    "distill.temperature": 0.7,
    "distill.max_tokens": 1024,
    "distill.concurrency": 2,
    "distill.system": "",
    "distill.use_daily": True,
    "distill.daily_limit": 500,
    "distill.min_len": 12,
    "distill.allow_wake": False,
    # sistem
    "sys.retention_days": 30,
    "admin.password_hash": "",
}

SECRET_KEYS = {"hf.token", "distill.teacher_key", "admin.password_hash", "hf.salt", "fabric.secret", "uplink.secret", "uplink.cf_token"}


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=8000")
            self.conn.executescript(SCHEMA)
        self._migrate()
        self._cache: dict = {}
        for r in self.q("SELECT key, value FROM settings"):
            self._cache[r["key"]] = jload(r["value"])

    def _migrate(self) -> None:
        """Eski veritabanlarına sonradan eklenen sütunları ekler."""
        for table, col, ddl in (("runtimes", "paths", "TEXT NOT NULL DEFAULT '{}'"),
                                ("nodes", "rt_paths", "TEXT NOT NULL DEFAULT '{}'"),
                                ("nodes", "ssh_user", "TEXT NOT NULL DEFAULT ''"),
                                ("models", "source", "TEXT NOT NULL DEFAULT 'runtime'"),
                                ("models", "file_glob", "TEXT NOT NULL DEFAULT ''"),
                                ("models", "manifest", "TEXT NOT NULL DEFAULT ''"),
                                ("api_keys", "sign_secret", "TEXT NOT NULL DEFAULT ''")):
            if col not in {r["name"] for r in self.q(f"PRAGMA table_info({table})")}:
                self.x(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # ---- sorgu yardımcıları -------------------------------------------------
    def q(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            cur = self.conn.execute(sql, params)
            r = cur.fetchone()
            return dict(r) if r else None

    def x(self, sql: str, params=()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid

    def xmany(self, sql: str, seq) -> None:
        with self._lock:
            self.conn.executemany(sql, seq)

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---- ayarlar ------------------------------------------------------------
    def get(self, key: str, default=None):
        if key in self._cache:
            return self._cache[key]
        if key in SETTINGS_DEFAULTS:
            return SETTINGS_DEFAULTS[key]
        return default

    def set(self, key: str, value) -> None:
        self._cache[key] = value
        self.x(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, jdump(value)),
        )

    def set_many(self, values: dict) -> None:
        for k, v in values.items():
            if k in SETTINGS_DEFAULTS:
                self.set(k, coerce_like(SETTINGS_DEFAULTS[k], v))

    def all_settings(self, mask: bool = True) -> dict:
        out = {}
        for k, d in SETTINGS_DEFAULTS.items():
            v = self.get(k, d)
            if mask and k in SECRET_KEYS:
                out[k] = bool(v)  # yalnızca "tanımlı mı" bilgisi
            else:
                out[k] = v
        return out

    # ---- olaylar ------------------------------------------------------------
    def event(self, level: str, source: str, message: str, node_id: int | None = None) -> None:
        lv = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        log.log(lv, "[%s] %s", source, message)
        try:
            self.x(
                "INSERT INTO events(ts,level,source,node_id,message) VALUES(?,?,?,?,?)",
                (now(), level, source, node_id, message),
            )
        except Exception:  # olay yazımı hiçbir zaman akışı bozmamalı
            pass

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def coerce_like(default, v):
    """Gelen değeri varsayılanın türüne zorlar."""
    if isinstance(default, bool):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "evet")
        return bool(v)
    if isinstance(default, int):
        try:
            return int(float(v))
        except Exception:
            return default
    if isinstance(default, float):
        try:
            return float(v)
        except Exception:
            return default
    if isinstance(default, dict):
        if isinstance(v, str):
            return jload(v, default)
        return dict(v) if isinstance(v, dict) else default
    if isinstance(default, list):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return list(v)
    return "" if v is None else str(v)
