"""HuggingFace veri seti senkronu.

Belirlenen saatlerde (hf.schedule) sohbet kayıtlarını ve damıtılmış veri setini JSONL olarak hazırlar ve
HuggingFace Hub'daki dataset deposuna tek bir commit ile yükler.

* Gönderim başarılı olmadan hiçbir kayıt "gönderildi" işaretlenmez; hata olursa bir sonraki çalıştırmada tekrar denenir.
* `hf.dry_run` açıkken (ya da token/depo yoksa) dosyalar yalnızca data/hf_outbox altında hazırlanır.
* Gizlilik: depo varsayılan olarak özel (private) oluşturulur; kullanıcı kimlikleri varsayılan olarak anonimleştirilir
  ve metinlerdeki e-posta/telefon/IBAN/T.C. kimlik no benzeri veriler maskelenir.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path

from . import chatlog, distill
from .util import log, now, rand_token


class HFSync:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.running = False
        self._task: asyncio.Task | None = None
        self.outbox = Path(ctx.data_dir) / "hf_outbox"

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ---- zamanlama ----------------------------------------------------------
    def _due(self) -> str | None:
        """Zamanı gelmiş bir gönderim var mı? Kaçırılan çalıştırma yalnızca 3 saat içinde telafi edilir."""
        t = datetime.now()
        last = self.db.get("hf.last_key", "") or ""
        for hhmm in sorted(self.db.get("hf.schedule") or []):
            try:
                h, m = (int(x) for x in hhmm.split(":"))
            except ValueError:
                continue
            key = f"{t.strftime('%Y-%m-%d')} {hhmm}"
            delta = (t - t.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds()
            if 0 <= delta <= 3 * 3600 and key > last:
                return key
        return None

    def next_run(self) -> str:
        t = datetime.now()
        sched = sorted(self.db.get("hf.schedule") or [])
        last = self.db.get("hf.last_key", "") or ""
        for hhmm in sched:
            if f"{t.strftime('%Y-%m-%d')} {hhmm}" > last and hhmm > t.strftime("%H:%M"):
                return f"bugün {hhmm}"
        return f"yarın {sched[0]}" if sched else "—"

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                if self.db.get("hf.auto"):
                    key = self._due()
                    if key:
                        await self.run("zamanlanmış", key)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hfsync")

    # ---- çalıştırma ---------------------------------------------------------
    async def run(self, trigger: str = "elle", key: str | None = None) -> dict:
        if self.running:
            return {"status": "busy", "message": "Başka bir senkron çalışıyor"}
        self.running = True
        try:
            res = await asyncio.to_thread(self._run_blocking, trigger)
        finally:
            self.running = False
        if key:
            self.db.set("hf.last_key", key)
        return res

    def _salt(self) -> str:
        s = self.db.get("hf.salt")
        if not s:
            s = rand_token(12)
            self.db.set("hf.salt", s)
        return s

    def _record(self, trigger: str, status: str, repo: str, records: int, message: str) -> dict:
        self.db.x("INSERT INTO hf_syncs(ts,trigger,status,repo,records,message) VALUES(?,?,?,?,?,?)",
                  (now(), trigger, status, repo, records, message[:500]))
        lvl = "error" if status == "error" else "info"
        self.db.event(lvl, "hf", f"HF senkron [{trigger}] {status}: {records} kayıt — {message[:160]}")
        return {"status": status, "records": records, "message": message, "repo": repo}

    def _write_jsonl(self, path: Path, recs: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _run_blocking(self, trigger: str) -> dict:
        db = self.db
        token = db.get("hf.token") or ""
        chat_repo = (db.get("hf.chat_repo") or "").strip()
        dist_repo = (db.get("hf.distill_repo") or "").strip() or chat_repo
        anonymize, scrub, salt = bool(db.get("hf.anonymize")), bool(db.get("hf.scrub_pii")), self._salt()
        dry = bool(db.get("hf.dry_run")) or not token or not (chat_repo or dist_repo)
        crecs, cmarks = chatlog.export_pending(db, int(db.get("hf.idle_min")), anonymize, salt, scrub)
        drecs, dids = distill.export_pending(db, anonymize, salt, scrub)
        total = len(crecs) + len(drecs)
        if total == 0:
            return self._record(trigger, "boş", chat_repo, 0, "Gönderilecek yeni kayıt yok")
        day, ts = datetime.now().strftime("%Y-%m-%d"), datetime.now().strftime("%H%M%S")
        plan: list[tuple[Path, str, str]] = []  # yerel dosya, depodaki yol, depo
        if dry:
            if crecs:
                self._write_jsonl(self.outbox / "chats" / "dry-run-latest.jsonl", crecs)
            if drecs:
                self._write_jsonl(self.outbox / "distill" / "dry-run-latest.jsonl", drecs)
            why = "dry-run açık" if db.get("hf.dry_run") else "token/depo tanımlı değil"
            return self._record(trigger, "dry-run", chat_repo, total, f"{why}: dosyalar {self.outbox} altında hazırlandı")
        if crecs:
            p = self.outbox / "chats" / f"{day}_{ts}.jsonl"
            self._write_jsonl(p, crecs)
            plan.append((p, f"chats/{day}/{ts}.jsonl", chat_repo))
        if drecs:
            p = self.outbox / "distill" / f"{day}_{ts}.jsonl"
            self._write_jsonl(p, drecs)
            plan.append((p, f"distill/{day}/{ts}.jsonl", dist_repo))
        try:
            from huggingface_hub import CommitOperationAdd, HfApi
            api = HfApi(token=token)
            by_repo: dict[str, list] = {}
            for path, in_repo, repo in plan:
                by_repo.setdefault(repo, []).append(CommitOperationAdd(path_in_repo=in_repo, path_or_fileobj=str(path)))
            for repo, ops in by_repo.items():
                api.create_repo(repo_id=repo, repo_type="dataset", private=bool(db.get("hf.private")), exist_ok=True)
                api.create_commit(repo_id=repo, repo_type="dataset", operations=ops,
                                  commit_message=f"Orkestra {trigger} {day} {ts}: {len(ops)} dosya")
        except Exception as e:
            msg = str(e).replace(token, "***")
            return self._record(trigger, "error", chat_repo, 0, f"{type(e).__name__}: {msg}"[:400])
        if cmarks:
            chatlog.mark_exported(db, cmarks)
        if dids:
            distill.mark_exported(db, dids)
        sent = self.outbox / "sent"
        sent.mkdir(parents=True, exist_ok=True)
        for path, _, _ in plan:
            try:
                shutil.move(str(path), str(sent / path.name))
            except Exception:
                pass
        return self._record(trigger, "ok", chat_repo, total, f"{len(plan)} dosya yüklendi ({', '.join(sorted({p[2] for p in plan}))})")

    async def test(self) -> dict:
        token = self.db.get("hf.token") or ""
        if not token:
            return {"ok": False, "error": "Token tanımlı değil"}

        def _t():
            from huggingface_hub import HfApi
            return HfApi(token=token).whoami()

        try:
            who = await asyncio.to_thread(_t)
            return {"ok": True, "user": who.get("name"), "type": who.get("type")}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e).replace(token, '***')[:200]}"}

    def status(self) -> dict:
        db = self.db
        pend_conv = db.one("SELECT COUNT(*) n FROM conversations WHERE turns>exported_turns")["n"]
        files = []
        if self.outbox.exists():
            files = [str(p.relative_to(self.outbox)) for p in sorted(self.outbox.rglob("*.jsonl")) if "sent" not in p.parts][-20:]
        return {
            "token_set": bool(db.get("hf.token")), "chat_repo": db.get("hf.chat_repo"), "distill_repo": db.get("hf.distill_repo"),
            "private": bool(db.get("hf.private")), "dry_run": bool(db.get("hf.dry_run")), "auto": bool(db.get("hf.auto")),
            "schedule": db.get("hf.schedule"), "next_run": self.next_run(), "running": self.running,
            "pending_conversations": pend_conv,
            "pending_distill": db.one("SELECT COUNT(*) n FROM distill_items WHERE status='done' AND exported=0")["n"],
            "history": db.q("SELECT * FROM hf_syncs ORDER BY id DESC LIMIT 15"),
            "outbox_files": files, "outbox": str(self.outbox),
        }
