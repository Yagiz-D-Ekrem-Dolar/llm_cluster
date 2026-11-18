"""Distillation modu.

Soru listelerinden ve/veya gün içinde kullanıcıların sorduğu sorulardan bir "öğretmen" modele yanıt ürettirip
(soru, yanıt) çiftlerinden bir veri seti hazırlar. Veri seti HuggingFace senkronu ile hedef depoya gönderilir.

Not: Bu, yanıt (sequence-level) distillation için veri üretimidir; öğrenci modelin eğitimi bu uygulamanın
kapsamı dışındadır (üretilen veri seti ile ayrıca ince ayar yapılabilir).

Otomatik modda yalnızca küme `distill.idle_min` dakikadır boştaysa çalışır ve gerçek trafik gelir gelmez duraklar.
İstekler düşük öncelikle gönderilir: etkileşimli isteklerin önüne geçmez.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import httpx

from . import chatlog
from .util import jdump, jload, log, now, scrub_pii, sha256

_NUM = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*•])\s+")


def parse_questions(text: str) -> list[str]:
    """Düz metin (satır başına bir soru) ya da JSONL ({"prompt": ...}) içeriğini soru listesine çevirir."""
    out, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        q = ""
        if line.startswith("{"):
            try:
                j = json.loads(line)
                if isinstance(j, dict):
                    q = next((str(j[k]) for k in ("prompt", "question", "instruction", "input", "text", "soru") if j.get(k)), "")
                    if not q and isinstance(j.get("messages"), list):
                        q = next((chatlog.msg_text(m) for m in reversed(j["messages"]) if m.get("role") == "user"), "")
            except Exception:
                q = line
        else:
            q = _NUM.sub("", line)
        q = " ".join(q.split())
        if len(q) >= 3 and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


class Distiller:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.state = "idle"  # idle | running | waiting
        self.reason = ""
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._running_items: set[int] = set()
        self.attempts: dict[int, int] = {}
        self._ext = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0))

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._bg):
            t.cancel()
        await asyncio.gather(*(([self._task] if self._task else []) + list(self._bg)), return_exceptions=True)
        await self._ext.aclose()

    # ---- soru setleri -------------------------------------------------------
    def add_set(self, name: str, text: str) -> int:
        items = parse_questions(text)
        if not items:
            raise ValueError("Geçerli soru bulunamadı (satır başına bir soru ya da JSONL)")
        return self.db.x("INSERT INTO question_sets(name,items,created_at) VALUES(?,?,?)", (name or "Soru listesi", jdump(items), now()))

    def list_sets(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"], "count": len(jload(r["items"], [])), "created_at": r["created_at"]}
                for r in self.db.q("SELECT id,name,items,created_at FROM question_sets ORDER BY id DESC")]

    def delete_set(self, sid: int) -> None:
        self.db.x("DELETE FROM question_sets WHERE id=?", (sid,))

    # ---- işler --------------------------------------------------------------
    def create_job(self, name: str, cfg: dict) -> int:
        prompts: list[str] = []
        for sid in cfg.get("sets", []):
            row = self.db.one("SELECT items FROM question_sets WHERE id=?", (int(sid),))
            if row:
                prompts += jload(row["items"], [])
        if cfg.get("use_daily"):
            prompts += chatlog.last_user_prompts(self.db, now() - float(cfg.get("daily_hours", 24)) * 3600,
                                                 int(cfg.get("daily_limit", self.db.get("distill.daily_limit"))),
                                                 int(self.db.get("distill.min_len")))
        done = {r["prompt_hash"] for r in self.db.q("SELECT DISTINCT prompt_hash FROM distill_items WHERE status='done'")}
        seen, final = set(), []
        for p in prompts:
            h = sha256(p.strip().lower())
            if h in seen or h in done:
                continue
            seen.add(h)
            final.append((p, h))
        lim = int(cfg.get("limit") or 0)
        if lim > 0:
            final = final[:lim]
        if not final:
            raise ValueError("Bu işte işlenecek yeni soru yok (hepsi daha önce damıtılmış ya da liste boş)")
        cfg = {k: v for k, v in cfg.items() if k != "sets" or v}
        status = "running" if cfg.get("forced") else "queued"
        with self.db.tx():
            jid = self.db.x("INSERT INTO distill_jobs(name,status,config,total,created_at) VALUES(?,?,?,?,?)",
                            (name or "Distillation işi", status, jdump(cfg), len(final), now()))
            self.db.xmany("INSERT INTO distill_items(job_id,prompt,prompt_hash) VALUES(?,?,?)", [(jid, p, h) for p, h in final])
        self.db.event("info", "distill", f"İş oluşturuldu: {name} ({len(final)} soru)")
        return jid

    def job_action(self, jid: int, action: str) -> None:
        job = self.db.one("SELECT * FROM distill_jobs WHERE id=?", (jid,))
        if not job:
            raise ValueError("iş bulunamadı")
        cfg = jload(job["config"], {})
        if action == "start":
            cfg["forced"] = True
            self.db.x("UPDATE distill_jobs SET status='running', config=? WHERE id=? AND status IN ('queued','paused','error')",
                      (jdump(cfg), jid))
        elif action == "queue":
            cfg["forced"] = False
            self.db.x("UPDATE distill_jobs SET status='queued', config=? WHERE id=? AND status IN ('running','paused','error')",
                      (jdump(cfg), jid))
        elif action == "pause":
            self.db.x("UPDATE distill_jobs SET status='paused' WHERE id=? AND status IN ('queued','running')", (jid,))
        elif action == "cancel":
            self.db.x("UPDATE distill_jobs SET status='cancelled', finished_at=? WHERE id=? AND status NOT IN ('done','cancelled')",
                      (now(), jid))
            self.db.x("UPDATE distill_items SET status='skipped' WHERE job_id=? AND status IN ('pending','running')", (jid,))
        elif action == "delete":
            self.db.x("DELETE FROM distill_items WHERE job_id=?", (jid,))
            self.db.x("DELETE FROM distill_jobs WHERE id=?", (jid,))
        else:
            raise ValueError("bilinmeyen işlem")

    def list_jobs(self) -> list[dict]:
        out = []
        for j in self.db.q("SELECT * FROM distill_jobs ORDER BY id DESC LIMIT 100"):
            j["config"] = jload(j["config"], {})
            out.append(j)
        return out

    def job_items(self, jid: int, status: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        if status:
            return self.db.q("SELECT id,prompt,response,status,tokens,teacher,ts,error FROM distill_items WHERE job_id=? AND status=? "
                             "ORDER BY id DESC LIMIT ? OFFSET ?", (jid, status, limit, offset))
        return self.db.q("SELECT id,prompt,response,status,tokens,teacher,ts,error FROM distill_items WHERE job_id=? "
                         "ORDER BY id DESC LIMIT ? OFFSET ?", (jid, limit, offset))

    def status(self) -> dict:
        pend = self.db.one("SELECT COUNT(*) n FROM distill_items WHERE status IN ('pending','running')")["n"]
        done = self.db.one("SELECT COUNT(*) n FROM distill_items WHERE status='done'")["n"]
        unexp = self.db.one("SELECT COUNT(*) n FROM distill_items WHERE status='done' AND exported=0")["n"]
        return {"state": self.state, "reason": self.reason, "pending": pend, "done": done, "unexported": unexp,
                "idle_min": round(self.ctx.gateway.idle_minutes(), 1), "enabled": bool(self.db.get("distill.enabled"))}

    # ---- çalıştırıcı --------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("distill")

    def _refresh_counts(self, jid: int) -> None:
        self.db.x("UPDATE distill_jobs SET done=(SELECT COUNT(*) FROM distill_items WHERE job_id=? AND status='done'), "
                  "failed=(SELECT COUNT(*) FROM distill_items WHERE job_id=? AND status='failed') WHERE id=?", (jid, jid, jid))

    async def _step(self) -> None:
        db, gw = self.db, self.ctx.gateway
        job = db.one("SELECT * FROM distill_jobs WHERE status IN ('running','queued') "
                     "ORDER BY (status='running') DESC, id LIMIT 1")
        if job is None:
            self.state, self.reason = "idle", ""
            await self._maybe_auto_job()
            return
        cfg = jload(job["config"], {})
        forced = bool(cfg.get("forced"))
        if not forced:
            if not db.get("distill.enabled"):
                self.state, self.reason = "waiting", "otomatik distillation kapalı (işi elle başlatabilirsiniz)"
                return
            idle = gw.idle_minutes()
            need = float(db.get("distill.idle_min"))
            if idle < need:
                self.state, self.reason = "waiting", f"küme meşgul/yeni boşta: {idle:.1f} / {need:.0f} dk"
                return
        elif gw.busy() and not cfg.get("ignore_busy"):
            pass  # zorunlu iş: düşük öncelikle devam eder, ağ geçidi etkileşimli istekleri öne alır
        ok, why = await self._ensure_teacher(cfg, forced)
        if not ok:
            self.state, self.reason = "waiting", why
            return
        conc = max(1, int(cfg.get("concurrency") or db.get("distill.concurrency")))
        slots = conc - len(self._running_items)
        if slots > 0:
            rows = db.q("SELECT * FROM distill_items WHERE job_id=? AND status='pending' ORDER BY id LIMIT ?", (job["id"], slots))
            for it in rows:
                db.x("UPDATE distill_items SET status='running' WHERE id=?", (it["id"],))
                self._running_items.add(it["id"])
                t = asyncio.create_task(self._do_item(job, cfg, it))
                self._bg.add(t)
                t.add_done_callback(self._bg.discard)
            if rows:
                db.x("UPDATE distill_jobs SET started_at=COALESCE(started_at,?), status='running' WHERE id=?", (now(), job["id"]))
        self.state, self.reason = "running", ""
        left = db.one("SELECT COUNT(*) n FROM distill_items WHERE job_id=? AND status IN ('pending','running')", (job["id"],))["n"]
        if left == 0:
            self._refresh_counts(job["id"])
            db.x("UPDATE distill_jobs SET status='done', finished_at=? WHERE id=?", (now(), job["id"]))
            db.event("info", "distill", f"İş tamamlandı: {job['name']}")

    async def _maybe_auto_job(self) -> None:
        """Otomatik mod: kümede yeterince boşta kalındığında günün sorularından bir iş oluşturur."""
        db, gw = self.db, self.ctx.gateway
        if not (db.get("distill.enabled") and db.get("distill.use_daily")):
            return
        if gw.idle_minutes() < float(db.get("distill.idle_min")):
            return
        day = datetime.now().strftime("%Y-%m-%d")
        if db.get("distill.last_auto_day", "") == day:
            return
        db.set("distill.last_auto_day", day)
        try:
            self.create_job(f"otomatik-{day}", {"use_daily": True, "daily_hours": 24, "forced": False})
        except ValueError:
            pass  # yeni soru yok

    async def _ensure_teacher(self, cfg: dict, forced: bool) -> tuple[bool, str]:
        db, c = self.db, self.ctx.cluster
        teacher = cfg.get("teacher") or db.get("distill.teacher")
        if not teacher and db.get("distill.teacher_url"):
            return True, ""
        if not teacher:
            return False, "öğretmen model seçilmedi (Ayarlar → Distillation)"
        m = self.ctx.gateway.by_name(teacher)
        if not m or not m["enabled"]:
            return False, f"öğretmen model bulunamadı: {teacher}"
        reps = [d for d in c.deps_of(m["id"]) if d.desired == "running"]
        if any(d.state == "ready" for d in reps):
            return True, ""
        if reps:
            return False, f"öğretmen model yükleniyor: {teacher}"
        p = c.plan_placement(m, allow_wake=bool(db.get("distill.allow_wake")), allow_evict=forced)
        if p is None:
            return False, f"öğretmen için boş kapasite yok: {teacher}"
        await c.deploy(m, p, "autoscaler", False)
        db.event("info", "distill", f"Öğretmen model başlatılıyor: {teacher} → {p.node.name}")
        return False, f"öğretmen model başlatılıyor: {teacher}"

    async def _do_item(self, job: dict, cfg: dict, it: dict) -> None:
        db = self.db
        iid = it["id"]
        try:
            msgs = []
            system = cfg.get("system", db.get("distill.system"))
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": it["prompt"]})
            temp = float(cfg.get("temperature", db.get("distill.temperature")))
            mt = int(cfg.get("max_tokens", db.get("distill.max_tokens")))
            teacher = cfg.get("teacher") or db.get("distill.teacher")
            if not teacher and db.get("distill.teacher_url"):
                text, tokens, label = await self._external(msgs, temp, mt)
            else:
                res = await self.ctx.gateway.internal_chat(teacher, msgs, user_id="distill", source="distill", priority="low",
                                                           max_tokens=mt, temperature=temp, log_turn=False)
                if not res["ok"]:
                    raise RuntimeError(res["error"])
                text = res["text"]
                tokens = (res.get("usage") or {}).get("completion_tokens") or len(text) // 3
                label = teacher
            if not text.strip():
                raise RuntimeError("boş yanıt")
            db.x("UPDATE distill_items SET response=?, status='done', tokens=?, teacher=?, ts=?, error='' WHERE id=?",
                 (text, int(tokens), label, now(), iid))
        except asyncio.CancelledError:
            db.x("UPDATE distill_items SET status='pending' WHERE id=? AND status='running'", (iid,))
            raise
        except Exception as e:
            n = self.attempts.get(iid, 0) + 1
            self.attempts[iid] = n
            if n >= 3:
                db.x("UPDATE distill_items SET status='failed', error=? WHERE id=?", (str(e)[:300], iid))
            else:
                db.x("UPDATE distill_items SET status='pending', error=? WHERE id=?", (str(e)[:300], iid))
                await asyncio.sleep(5)
        finally:
            self._running_items.discard(iid)
            self._refresh_counts(job["id"])

    async def _external(self, msgs: list, temp: float, mt: int) -> tuple[str, int, str]:
        db = self.db
        url = db.get("distill.teacher_url").rstrip("/") + "/chat/completions"
        model = db.get("distill.teacher_model")
        hdr = {"Authorization": f"Bearer {db.get('distill.teacher_key')}"} if db.get("distill.teacher_key") else {}
        r = await self._ext.post(url, headers=hdr, json={"model": model, "messages": msgs, "temperature": temp, "max_tokens": mt})
        if r.status_code != 200:
            raise RuntimeError(f"harici öğretmen HTTP {r.status_code}: {r.text[:150]}")
        j = r.json()
        text = j["choices"][0]["message"]["content"] or ""
        return text, (j.get("usage") or {}).get("completion_tokens") or len(text) // 3, f"ext:{model}"


def export_pending(db, anonymize: bool, salt: str, scrub: bool, limit: int = 20000):
    """Gönderilmemiş damıtılmış çiftleri kayıt listesine çevirir. -> (kayıtlar, [item_id])"""
    rows = db.q("SELECT i.*, j.name AS job_name FROM distill_items i JOIN distill_jobs j ON j.id=i.job_id "
                "WHERE i.status='done' AND i.exported=0 ORDER BY i.id LIMIT ?", (limit,))
    recs, ids = [], []
    for r in rows:
        prompt, resp = r["prompt"], r["response"]
        if scrub:
            prompt, resp = scrub_pii(prompt), scrub_pii(resp)
        recs.append({
            "id": f"d{r['id']}", "job": r["job_name"], "teacher": r["teacher"], "prompt": prompt, "response": resp,
            "messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": resp}],
            "tokens": r["tokens"], "created": datetime.fromtimestamp(r["ts"] or now()).isoformat(timespec="seconds"),
        })
        ids.append(r["id"])
    return recs, ids


def mark_exported(db, ids: list[int]) -> None:
    db.xmany("UPDATE distill_items SET exported=1 WHERE id=?", [(i,) for i in ids])
