"""Упрощённая Рин для чужих людей и для чатов-поручений: лимиты, история в памяти, ответы."""
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

from .config import (APIS_PY, PUBLIC_DAILY_PER_USER, PUBLIC_DAILY_TOTAL, PUBLIC_HISTORY_LEN, PUBLIC_SYSTEM,
                     TASK_HISTORY_LEN, TASK_REPLIES_PER_DAY, TASK_SYSTEM, log)
from .llm import openrouter_chat

HISTORY = {}          # chat_id -> последние сообщения (только в памяти, до перезапуска)
_count = {"day": None, "total": 0, "users": {}}
_limit_notified = set()
_task_count = {}      # (user_id, day) -> ответов в чате-поручении

# Ответная гифка: строка вида GIF:слово в конце ответа — мост скачает аниме-гифку и уберёт строку.
_GIF_RE = re.compile(r"(?im)^\s*GIF:\s*([a-zA-Z0-9_-]{1,24})\s*$")


def fetch_gif(word):
    """Скачать аниме-гифку-реакцию по слову (скрипт GIF_SCRIPT из .env). -> путь или None."""
    if APIS_PY is None:
        return None
    try:
        out = subprocess.run([sys.executable, str(APIS_PY), "gif", word], capture_output=True,
                             text=True, timeout=40)
        data = json.loads(out.stdout or "{}")
        path = data.get("file")
        if path and Path(path).is_file():
            return path
        log.warning("gif word %r unknown: %s", word, data.get("error", "?"))
    except Exception as e:  # noqa: BLE001
        log.warning("gif fetch failed %r: %s", word, type(e).__name__)
    return None


def _apply_gif(reply):
    """'…текст\nGIF:hug' -> ('…текст', /путь/к/гифке). Без слова-реакции возвращает как есть."""
    m = _GIF_RE.search(reply)
    if not m:
        return reply, None
    path = fetch_gif(m.group(1).lower())
    return _GIF_RE.sub("", reply).strip(), path


def _today():
    return datetime.date.today().isoformat()


def take_public_slot(user_id):
    """True, если лимиты позволяют ответить; заодно учитывает сообщение."""
    if _count["day"] != _today():
        _count.update(day=_today(), total=0, users={})
        _limit_notified.clear()
    if _count["total"] >= PUBLIC_DAILY_TOTAL or _count["users"].get(user_id, 0) >= PUBLIC_DAILY_PER_USER:
        return False
    _count["total"] += 1
    _count["users"][user_id] = _count["users"].get(user_id, 0) + 1
    return True


def first_limit_notice(chat_id):
    """True только для первого упора в лимит в этом чате за день."""
    if chat_id in _limit_notified:
        return False
    _limit_notified.add(chat_id)
    return True


def today_stats():
    if _count["day"] != _today():
        return 0, 0
    return len(_count["users"]), _count["total"]


def ask_public(chat_id, text, gifs=False):
    """gifs=True — персональная настройка: с этим человеком использовать аниме-гифки почти в каждом ответе."""
    system = PUBLIC_SYSTEM
    if gifs:
        system += ("\nДОПОЛНИТЕЛЬНО ДЛЯ ЭТОГО ЧАТА: с этим человеком используй аниме-гифки активно — "
                   "почти в каждом ответе добавляй отдельную строку GIF:слово (одна на ответ, слово из списка выше).")
    hist = HISTORY.setdefault(chat_id, [])
    hist.append({"role": "user", "content": text[:4000]})
    del hist[:-PUBLIC_HISTORY_LEN]
    try:
        answer = openrouter_chat(system, hist, max_tokens=800) or "…"
    except Exception as e:  # noqa: BLE001
        log.warning("public chat failed: %s", type(e).__name__)
        hist.pop()
        return "Не могу ответить прямо сейчас, попробуй позже.", None
    answer, gif_path = _apply_gif(answer)
    hist.append({"role": "assistant", "content": answer})
    return answer, gif_path


def take_task_slot(user_id):
    key = (user_id, _today())
    n = _task_count.get(key, 0)
    if n >= TASK_REPLIES_PER_DAY:
        return None
    _task_count[key] = n + 1
    return n + 1


def ask_task(thread):
    """thread — последние сообщения переписки ([{role, content}]) — в ней видно и само поручение."""
    try:
        answer = openrouter_chat(PUBLIC_SYSTEM + TASK_SYSTEM, thread[-TASK_HISTORY_LEN:], max_tokens=600) or "…"
    except Exception as e:  # noqa: BLE001
        log.warning("task reply failed: %s", type(e).__name__)
        return "...секунду, что-то со связью. Напиши ещё раз чуть позже.", None
    return _apply_gif(answer)
