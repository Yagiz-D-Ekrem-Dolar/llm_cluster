"""Sohbet kaydı: kullanıcılar, konuşmalar, turlar ve HuggingFace dışa aktarımı için sorgular."""
from __future__ import annotations

from datetime import datetime

from .util import hash_user, jdump, jload, now, scrub_pii, sha256


def msg_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return "" if c is None else str(c)


def derive_conv_id(user_id: str, messages: list) -> str:
    sysm = next((msg_text(m) for m in messages if m.get("role") == "system"), "")
    first = next((msg_text(m) for m in messages if m.get("role") == "user"), "")
    return "c_" + sha256(f"{user_id}|{sysm}|{first}")[:16]


def record_turn(db, *, conv_id: str, user_id: str, model: str, node: str, messages: list, reply: str,
                finish: str | None, in_tok: int, out_tok: int, ttft_ms: int, total_ms: int, status: str,
                key_id: int, source: str) -> None:
    t = now()
    first = next((msg_text(m) for m in messages if m.get("role") == "user"), "")
    title = " ".join(first.split())[:80]
    with db.tx():
        db.x(
            "INSERT INTO chat_turns(ts,conv_id,user_id,model,node,messages,reply,finish,in_tok,out_tok,ttft_ms,total_ms,"
            "status,key_id,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, conv_id, user_id, model, node, jdump(messages), reply, finish, in_tok, out_tok, ttft_ms, total_ms,
             status, key_id, source),
        )
        db.x(
            "INSERT INTO conversations(id,user_id,model,title,first_ts,last_ts,turns,source) VALUES(?,?,?,?,?,?,1,?) "
            "ON CONFLICT(id) DO UPDATE SET last_ts=excluded.last_ts, turns=conversations.turns+1, model=excluded.model",
            (conv_id, user_id, model, title, t, t, source),
        )
        db.x(
            "INSERT INTO users(id,first_seen,last_seen,turns,tokens) VALUES(?,?,?,1,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, turns=users.turns+1, tokens=users.tokens+?",
            (user_id, t, t, (in_tok or 0) + (out_tok or 0), (in_tok or 0) + (out_tok or 0)),
        )


# ---- sorgular ---------------------------------------------------------------
def list_users(db, q: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    if q:
        return db.q("SELECT * FROM users WHERE id LIKE ? OR label LIKE ? ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                    (f"%{q}%", f"%{q}%", limit, offset))
    return db.q("SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?", (limit, offset))


def list_conversations(db, user_id: str = "", model: str = "", q: str = "", sort: str = "recent", limit: int = 50,
                       offset: int = 0) -> list[dict]:
    where, args = [], []
    if user_id:
        where.append("c.user_id=?")
        args.append(user_id)
    if model:
        where.append("c.model=?")
        args.append(model)
    if q:
        where.append("(c.title LIKE ? OR c.id LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    sql = "SELECT c.* FROM conversations c"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY RANDOM()" if sort == "random" else " ORDER BY c.last_ts DESC"
    sql += " LIMIT ? OFFSET ?"
    return db.q(sql, tuple(args) + (limit, offset))


def get_conversation(db, conv_id: str) -> dict | None:
    c = db.one("SELECT * FROM conversations WHERE id=?", (conv_id,))
    if not c:
        return None
    turns = db.q("SELECT id,ts,model,node,in_tok,out_tok,ttft_ms,total_ms,status,finish FROM chat_turns WHERE conv_id=? ORDER BY id",
                 (conv_id,))
    last = db.one("SELECT messages, reply FROM chat_turns WHERE conv_id=? AND reply!='' ORDER BY id DESC LIMIT 1", (conv_id,))
    msgs = []
    if last:
        msgs = jload(last["messages"], []) + [{"role": "assistant", "content": last["reply"]}]
    return {**c, "messages": msgs, "turn_list": turns}


def last_user_prompts(db, since_ts: float, limit: int, min_len: int) -> list[str]:
    """Son sohbet turlarındaki kullanıcı istemleri (tekilleştirilmiş)."""
    rows = db.q("SELECT messages FROM chat_turns WHERE ts>=? AND source!='distill' ORDER BY id DESC LIMIT ?",
                (since_ts, limit * 6))
    seen, out = set(), []
    for r in rows:
        msgs = jload(r["messages"], [])
        text = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                text = " ".join(msg_text(m).split())
                break
        if len(text) < min_len or len(text) > 4000:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


# ---- HuggingFace dışa aktarımı ---------------------------------------------
def export_pending(db, idle_min: int, anonymize: bool, salt: str, scrub: bool, limit: int = 5000,
                   only_pending: bool = True):
    """Boşta kalmış (idle_min dk) ve henüz tam gönderilmemiş konuşmaları kayıt listesine çevirir.

    Dönüş: (kayıtlar, [(conv_id, turns)])  -- gönderim başarılı olunca mark_exported çağrılmalı.
    """
    cutoff = now() - idle_min * 60
    cond = "AND turns>exported_turns" if only_pending else ""
    rows = db.q(f"SELECT * FROM conversations WHERE last_ts<=? {cond} ORDER BY last_ts LIMIT ?", (cutoff, limit))
    recs, marks = [], []
    for c in rows:
        last = db.one("SELECT messages, reply, node, model FROM chat_turns WHERE conv_id=? AND status='ok' AND reply!='' "
                      "ORDER BY id DESC LIMIT 1", (c["id"],))
        marks.append((c["id"], c["turns"]))
        if not last:
            continue
        msgs = jload(last["messages"], []) + [{"role": "assistant", "content": last["reply"]}]
        out_msgs = []
        for m in msgs:
            txt = msg_text(m)
            if scrub:
                txt = scrub_pii(txt)
            out_msgs.append({"role": m.get("role", "user"), "content": txt})
        agg = db.one("SELECT COALESCE(SUM(in_tok),0) i, COALESCE(SUM(out_tok),0) o FROM chat_turns WHERE conv_id=?", (c["id"],))
        rec = {
            "id": ("c_" + sha256(salt + c["id"])[:16]) if anonymize else c["id"],
            "user_id": hash_user(c["user_id"], salt) if anonymize else c["user_id"],
            "model": last["model"],
            "started": datetime.fromtimestamp(c["first_ts"]).isoformat(timespec="seconds"),
            "ended": datetime.fromtimestamp(c["last_ts"]).isoformat(timespec="seconds"),
            "turns": c["turns"],
            "source": c["source"],
            "in_tokens": agg["i"], "out_tokens": agg["o"],
            "messages": out_msgs,
        }
        recs.append(rec)
    return recs, marks


def mark_exported(db, marks: list[tuple[str, int]]) -> None:
    db.xmany("UPDATE conversations SET exported_turns=? WHERE id=?", [(t, cid) for cid, t in marks])
