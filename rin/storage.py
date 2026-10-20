"""Состояние моста: модели и разговоры по чатам, чаты-поручения, блок/стоп для чужих."""
import json
import os
import sqlite3

from .config import DB_FILE, DEFAULT_MODEL, STATE_FILE


def _load():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


STATE = _load()   # "<chat_id>": {"model": "iq", "gen": 0};  "_task_chats": [id, ...]
TASK_CHATS = set(STATE.get("_task_chats", []))   # люди, которым Рин писала по поручению владельца


def save():
    STATE["_task_chats"] = sorted(TASK_CHATS)
    STATE_FILE.write_text(json.dumps(STATE, indent=1))
    os.chmod(STATE_FILE, 0o600)


def chat_state(chat_id):
    return STATE.setdefault(str(chat_id), {"model": DEFAULT_MODEL, "gen": 0})


def owner_session_id(chat_id):
    """Сессия Hermes для пути runs: одна на чат, /new и сброс меняют gen."""
    return f"userbot-{chat_id}-{chat_state(chat_id)['gen']}"


def pending_approvals():
    return list(STATE.get("_pending_approvals", []))


def add_pending_approval(run_id, request_id):
    STATE.setdefault("_pending_approvals", []).append({"run_id": run_id, "request_id": request_id})
    save()


def clear_pending_approval(run_id):
    STATE["_pending_approvals"] = [p for p in STATE.get("_pending_approvals", []) if p["run_id"] != run_id]
    save()


def add_task_chat(user_id):
    TASK_CHATS.add(user_id)
    save()


def drop_task_chat(user_id):
    TASK_CHATS.discard(user_id)
    save()


# --- чужие люди: только id и флаги, без имён и текстов
_db = sqlite3.connect(str(DB_FILE), check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS people (user_id INTEGER PRIMARY KEY, blocked INTEGER DEFAULT 0, stopped INTEGER DEFAULT 0, gifs INTEGER DEFAULT 0)")
try:  # старые базы без колонки gifs
    _db.execute("ALTER TABLE people ADD COLUMN gifs INTEGER DEFAULT 0")
    _db.commit()
except sqlite3.OperationalError:
    pass
os.chmod(DB_FILE, 0o600)


def person(user_id):
    r = _db.execute("SELECT blocked, stopped, gifs FROM people WHERE user_id=?", (user_id,)).fetchone() or (0, 0, 0)
    return {"blocked": bool(r[0]), "stopped": bool(r[1]), "gifs": bool(r[2])}


def set_flag(user_id, flag, value):
    assert flag in ("blocked", "stopped", "gifs")
    _db.execute("INSERT OR IGNORE INTO people(user_id) VALUES (?)", (user_id,))
    _db.execute(f"UPDATE people SET {flag}=? WHERE user_id=?", (int(value), user_id))
    _db.commit()


def blocked_count():
    return _db.execute("SELECT count(*) FROM people WHERE blocked=1").fetchone()[0]
