"""Команды владельца в userbot: /model, /new, /status, /баланс, /блок, /разблок, /лог."""
import asyncio
import shutil
import urllib.request

from .config import HOME, MODELS, PUBLIC_DAILY_TOTAL, PUBLIC_MODE
from .llm import openrouter_balance
from .public import today_stats
from .storage import blocked_count, chat_state, save, set_flag


def _up(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return "✅" if r.status == 200 else "❌"
    except Exception:  # noqa: BLE001
        return "❌"


def _status(chat_id):
    st = chat_state(chat_id)
    users, msgs = today_stats()
    free = shutil.disk_usage(str(HOME)).free / 1e9
    return ("Статус:\n"
            f"{_up('http://127.0.0.1:8642/health')} Hermes   {_up('http://127.0.0.1:11434/api/version')} Ollama   "
            f"{_up('http://127.0.0.1:9119/api/status')} Dashboard   {_up('http://127.0.0.1:3000/')} Workspace\n"
            f"Модель в этом чате: {st['model']} ({MODELS[st['model']]['model']})\n"
            f"Публичный режим: {'вкл' if PUBLIC_MODE else 'выкл'} · сегодня {users} чел., {msgs} сообщ. "
            f"(лимит {PUBLIC_DAILY_TOTAL}) · заблокировано {blocked_count()}\n"
            f"Диск свободно: {free:.0f} ГБ")


def _balance():
    try:
        left, total, used = openrouter_balance()
        return f"OpenRouter: осталось ${left:.2f} (пополнено ${total:.2f}, потрачено ${used:.2f})"
    except Exception as e:  # noqa: BLE001
        return f"Не смогла узнать баланс: {type(e).__name__}"


async def _resolve(client, who):
    who = who.strip()
    if who.lstrip("-").isdigit():
        return int(who)
    try:
        return await client.get_peer_id(who)
    except Exception:  # noqa: BLE001
        return None


async def _log(client, who):
    try:
        ent = await client.get_entity(int(who) if who.lstrip("-").isdigit() else who)
        msgs = await client.get_messages(ent, limit=20)
    except Exception as e:  # noqa: BLE001
        return f"Не нашла чат: {type(e).__name__}"
    name = getattr(ent, "username", None) or getattr(ent, "first_name", None) or who
    lines = [f"{m.date.strftime('%d.%m %H:%M')} {'Рин' if m.out else name}: {m.raw_text}" for m in reversed(msgs)]
    return f"📜 Переписка userbot с {name} (последние {len(lines)}):\n\n" + "\n".join(lines) if lines else "Переписки нет."


async def handle(client, chat_id, text):
    """Ответ на команду или None, если это обычное сообщение для Рин."""
    t = text.strip()
    cmd, _, arg = t.partition(" ")
    arg = arg.strip()
    if cmd == "/model":
        st = chat_state(chat_id)
        if arg.lower() in MODELS:
            st["model"] = arg.lower()
            save()
            return f"✅ Модель: {st['model']} ({MODELS[st['model']]['model']})"
        return f"Сейчас: {st['model']}. Доступно: " + ", ".join(f"/model {k}" for k in MODELS)
    if cmd == "/new":
        chat_state(chat_id)["gen"] += 1
        save()
        return "🆕 Новый разговор начат."
    if cmd == "/status":
        return await asyncio.to_thread(_status, chat_id)
    if cmd in ("/баланс", "/balance"):
        return await asyncio.to_thread(_balance)
    if cmd in ("/блок", "/разблок"):
        uid = await _resolve(client, arg) if arg else None
        if uid is None:
            return f"Не нашла. Пример: {cmd} @username или {cmd} 123456789"
        set_flag(uid, "blocked", cmd == "/блок")
        if cmd == "/блок":
            set_flag(uid, "stopped", 0)
            return f"🚫 Заблокирован: {uid}"
        return f"✅ Разблокирован: {uid}"
    if cmd in ("/лог", "/log"):
        return await _log(client, arg) if arg else "Пример: /лог @username или /лог 123456789"
    return None
