"""Локальный сокет для send.py: Рин пишет первой с userbot ТОЛЬКО по приказу владельца (текст, голос, файл, чтение ответов)."""
import asyncio
import datetime
import json
import os
import time
from pathlib import Path

from .config import (OWNER_ID, READ_MAX, SEND_NEW_PER_DAY, SEND_PER_DAY, SEND_PER_HOUR, SEND_SOCK, log)
from .media import is_audio_or_video, to_voice_ogg, tts_voice
from .storage import TASK_CHATS, add_task_chat

LAST_VOICE_TO = {}     # user_id -> время последнего голосового по send.py (чтобы мост не слал второе)
_sent = []             # [(время, user_id)] за сутки — для лимитов, без текстов
_new_today = {}        # день -> {user_id} новых получателей


def _check_limits(now):
    _sent[:] = [x for x in _sent if now - x[0] < 86400]
    if sum(1 for t, _ in _sent if now - t < 3600) >= SEND_PER_HOUR or len(_sent) >= SEND_PER_DAY:
        raise ValueError(f"лимит отправок ({SEND_PER_HOUR}/час, {SEND_PER_DAY}/день)")


async def _entity(client, who):
    who = str(who).strip()
    return await client.get_entity(int(who) if who.lstrip("-").isdigit() and not who.startswith("+") else who)


async def _read(client, req):
    ent = await _entity(client, req["to"])
    if getattr(ent, "id", None) not in TASK_CHATS:
        raise ValueError("читать можно только чаты, куда ты писала по просьбе владельца")
    msgs = await client.get_messages(ent, limit=min(int(req.get("limit", 10)), READ_MAX))
    return {"ok": True, "chat": getattr(ent, "username", None) or getattr(ent, "first_name", None),
            "messages": [{"from": "Рин" if m.out else "собеседник", "date": m.date.isoformat()[:16],
                          "text": m.raw_text} for m in reversed(msgs)]}


async def _send(client, req):
    from telethon.tl.types import User
    text = str(req.get("text", "")).strip()
    file_path = str(req.get("file") or "").strip()
    voice = bool(req.get("voice"))
    if file_path and not Path(file_path).exists():
        raise ValueError(f"файл не найден: {file_path}")
    if file_path and is_audio_or_video(file_path):
        raise ValueError('готовые аудиофайлы не принимаю — голосовое только через send.py --voice <кому> "<текст>" (голос Рин)')
    if voice and not text:
        raise ValueError("пустой текст для голосового")
    if not text and not file_path:
        raise ValueError("пустой текст или файл")

    now = time.time()
    _check_limits(now)
    entity = await _entity(client, req["to"])
    if not isinstance(entity, User) or entity.bot:
        raise ValueError("писать можно только людям (не в группы, каналы и ботам)")
    known = bool(await client.get_messages(entity, limit=1))
    today = datetime.date.today().isoformat()
    if not known and len(_new_today.get(today, set()) - {entity.id}) >= SEND_NEW_PER_DAY:
        raise ValueError(f"лимит новых получателей ({SEND_NEW_PER_DAY}/день)")

    if voice:
        ogg = await asyncio.to_thread(tts_voice, text)
        msg = await client.send_file(entity, ogg, voice_note=True)
        LAST_VOICE_TO[entity.id] = time.time()
        log.info("voice synthesized via ElevenLabs for send")
    elif file_path:
        msg = await client.send_file(entity, file_path, caption=text[:1024] or None)
    else:
        msg = await client.send_message(entity, text[:4000])

    _sent.append((now, entity.id))
    if not known:
        _new_today.setdefault(today, set()).add(entity.id)
    add_task_chat(entity.id)
    log.info("sent by owner request: to=%s new=%s kind=%s len=%d", entity.id, not known,
             "voice" if voice else "file" if file_path else "text", len(text))
    return {"ok": True, "to": entity.username or entity.first_name, "id": entity.id,
            "new_contact": not known, "message_id": msg.id}


async def _call(client, req):
    """Звонок голосом Рин — только по приказу владельца; с людьми, которым она ещё не писала, — в лимите новых."""
    from telethon.tl.types import User
    from . import calls
    entity = await _entity(client, req["to"])
    if not isinstance(entity, User) or entity.bot:
        raise ValueError("звонить можно только людям")
    task = str(req.get("text", "")).strip()
    if not task and entity.id != OWNER_ID:
        raise ValueError("скажи, зачем звонить: send.py --call <кому> \"<поручение>\"")
    return await calls.start_call(entity, task)


async def start(client):
    async def handle(reader, writer):
        try:
            req = json.loads((await reader.readline()).decode())
            op = req.get("op")
            res = await (_read(client, req) if op == "read" else _call(client, req) if op == "call" else _send(client, req))
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write((json.dumps(res, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()

    if SEND_SOCK.exists():
        SEND_SOCK.unlink()
    server = await asyncio.start_unix_server(handle, path=str(SEND_SOCK))
    os.chmod(SEND_SOCK, 0o600)
    return server
