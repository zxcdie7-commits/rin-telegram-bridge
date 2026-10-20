"""Telegram-клиент userbot и маршрутизация входящих: Владелец → полная Рин, поручения и чужие → упрощённая."""
import asyncio
import os
import time

from telethon import TelegramClient, events, functions

from . import calls, commands, owner_runs, public, sender, voice
from .config import (ALLOWED_GROUPS, ALLOWED_USERS, BASE, BIO, LAST_IMAGE, MAX_TG, OWNER_PATH, PUBLIC_MODE,
                     SESSION, TG_API_HASH, TG_API_ID, TMP, VOICE_ASK_RE, VOICE_DEDUP_SEC, VOICE_REPLY_MAX, log)
from .llm import ask_hermes
from .media import cleanup_tmp, extract_media, media_kind, media_note, save_frame, to_voice_ogg, hear, tts_voice
from .storage import TASK_CHATS, drop_task_chat, person, set_flag

_locks = {}   # chat_id -> asyncio.Lock: один ответ за раз в чате


def split(text):
    while text:
        if len(text) <= MAX_TG:
            yield text
            return
        cut = text.rfind("\n", 0, MAX_TG)
        if cut < MAX_TG // 2:
            cut = MAX_TG
        yield text[:cut]
        text = text[cut:].lstrip("\n")


async def _reply_text(event, text):
    for chunk in split(voice.plain(text)):  # теги интонаций [laughs] — только для голоса
        await event.reply(chunk)


def _lock(chat_id):
    return _locks.setdefault(chat_id, asyncio.Lock())


# ---------- владелец: полная Рин через Hermes

async def on_owner(client, event, text, want_voice, tone=""):
    chat_id = event.chat_id
    if OWNER_PATH == "runs":
        answer = owner_runs.try_answer(chat_id, text)
        if answer is not None:
            log.info("approval reply chat=%s kind=%s", chat_id, answer)
            if answer == "yes":
                await event.reply("...ок, делаю.")
            elif answer == "no":
                await event.reply("...ладно, не буду.")
            elif answer == "other":
                await event.reply("...я жду ответ на вопрос выше: да или нет?")
            return
    log.info("msg chat=%s len=%d", chat_id, len(text))
    reply = await commands.handle(client, chat_id, text)
    is_command = reply is not None
    wants_voice = want_voice or bool(VOICE_ASK_RE.search(text))
    if reply is None:
        # как голосовой модуль Мака: интонация, настроение, теги для голоса, матч Dota
        notes = await asyncio.to_thread(lambda: [voice.voice_note(tone) if wants_voice else "", voice.dota_note()])
        text = "\n".join([text] + [n for n in notes if n])
        if OWNER_PATH == "runs":
            reply = await owner_runs.ask(client, event, chat_id, text)
        else:
            async with client.action(chat_id, "typing"):
                reply = await asyncio.to_thread(ask_hermes, chat_id, text)
    reply, files = extract_media(reply)

    already_voiced = time.time() - sender.LAST_VOICE_TO.get(event.sender_id, 0) < VOICE_DEDUP_SEC \
        and sender.LAST_VOICE_TO.get(event.sender_id, 0) >= event.date.timestamp()
    if wants_voice and not is_command and not already_voiced and reply and not files and len(reply) <= VOICE_REPLY_MAX:
        try:
            ogg = await asyncio.to_thread(tts_voice, reply)
            await client.send_file(chat_id, ogg, voice_note=True, reply_to=event.id)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("voice reply failed: %s", type(e).__name__)

    for path, mime in files:
        try:
            if mime.startswith("audio/"):
                await client.send_file(chat_id, await asyncio.to_thread(to_voice_ogg, path), voice_note=True, reply_to=event.id)
            else:
                await client.send_file(chat_id, path, reply_to=event.id)
        except Exception as e:  # noqa: BLE001
            log.warning("media send failed: %s", type(e).__name__)
    if reply:
        await _reply_text(event, reply)


# ---------- человек из поручения владельца: Рин продолжает разговор в рамках задачи

async def on_task_chat(client, event, text):
    chat_id = event.chat_id
    if text.strip().lower() in ("/стоп", "/stop", "стоп"):
        drop_task_chat(event.sender_id)
        await event.reply("...поняла. Больше не пишу.")
        return
    if _lock(chat_id).locked():
        return
    n = public.take_task_slot(event.sender_id)
    if n is None:
        return
    async with _lock(chat_id):
        msgs = await client.get_messages(chat_id, limit=16)
        thread = [{"role": "assistant" if m.out else "user", "content": m.raw_text}
                  for m in reversed(msgs) if m.raw_text]
        async with client.action(chat_id, "typing"):
            reply, gif_path = await asyncio.to_thread(public.ask_task, thread)
        log.info("task reply chat=%s n=%d", chat_id, n)
        if gif_path:
            try:
                await client.send_file(chat_id, gif_path, caption=(reply[:1024] or None), reply_to=event.id)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("task gif send failed: %s", type(e).__name__)
        await _reply_text(event, reply)


# ---------- чужие люди: упрощённая Рин

async def on_stranger(client, event, text):
    chat_id = event.chat_id
    sender_ent = await event.get_sender()
    if getattr(sender_ent, "bot", False):
        return
    p = person(event.sender_id)
    if p["blocked"]:
        return
    cmd = text.strip().lower()
    if cmd in ("/стоп", "/stop"):
        set_flag(event.sender_id, "stopped", 1)
        await event.reply("...ладно. Больше не отвечаю. Напиши /старт, если передумаешь.")
        return
    if cmd in ("/старт", "/start"):
        set_flag(event.sender_id, "stopped", 0)
        p["stopped"] = False
        if cmd == "/старт":
            return
        text = "Привет"
    if p["stopped"]:
        return
    if cmd == "/new":
        public.HISTORY.pop(chat_id, None)
        await event.reply("🆕 Начнём заново.")
        return
    if text.startswith("/"):
        return  # остальные команды — только для владельца
    if not public.take_public_slot(event.sender_id):
        log.info("public limit chat=%s", chat_id)
        if public.first_limit_notice(chat_id):
            await event.reply("Лимит сообщений на сегодня исчерпан, напиши завтра 🙂")
        return
    if _lock(chat_id).locked():
        return
    async with _lock(chat_id):
        log.info("public msg chat=%s len=%d", chat_id, len(text))
        async with client.action(chat_id, "typing"):
            reply, gif_path = await asyncio.to_thread(public.ask_public, chat_id, text, p["gifs"])
        if gif_path:
            try:
                await client.send_file(chat_id, gif_path, caption=(reply[:1024] or None), reply_to=event.id)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("public gif send failed: %s", type(e).__name__)
        await _reply_text(event, reply)


# ---------- маршрутизация

async def on_message(client, event):
    owner = event.sender_id in ALLOWED_USERS
    text = event.raw_text or ""
    want_voice = False
    tone = ""

    if owner and event.is_private:
        kind = media_kind(event.message)
        if kind == "photo":
            try:
                await event.download_media(str(LAST_IMAGE))
                os.chmod(LAST_IMAGE, 0o600)
                log.info("saved owner image to %s", LAST_IMAGE)
                if not text:
                    text = f"[Владелец прислал фото, оно сохранено в {LAST_IMAGE} — посмотри его]"
            except Exception as e:  # noqa: BLE001
                log.warning("failed to save image: %s", e)
        elif kind in ("gif", "video"):
            try:
                path = str(await event.download_media(str(TMP / f"in_media_{event.id}")))
                os.chmod(path, 0o600)
                frame_path = await asyncio.to_thread(save_frame, path)
                extra = f", кадр: {frame_path}" if frame_path else ""
                label = "гифку" if kind == "gif" else "видео"
                log.info("saved owner %s -> %s", kind, path)
                note = f"[Владелец прислал {label}, сохранила: {path}{extra} — посмотри его]"
                text = (text + " " + note).strip() if text else note
            except Exception as e:  # noqa: BLE001
                log.warning("failed to save gif/video: %s", e)
        elif kind == "sticker":
            note = media_note(event.message)
            text = (text + " " + note).strip() if text else note.strip()
        if event.voice or event.audio:
            try:
                path = await event.download_media(str(TMP / f"in_voice_{event.id}.ogg"))
                heard = await asyncio.to_thread(hear, path)
                text, tone = heard["text"], heard.get("tone", "")
                want_voice = True
                log.info("owner voice transcribed len=%d tone=%s", len(text), tone or "-")
            except Exception as e:  # noqa: BLE001
                log.warning("voice transcribe failed: %s", type(e).__name__)
                await event.reply("...не расслышала. Повтори текстом?")
                return
    elif not owner:
        # чужие: гифки и стикеры не должны теряться — помечаем, чтобы упрощённая Рин отреагировала
        med_note = media_note(event.message)
        if med_note:
            text = (text + " " + med_note).strip() if text else med_note.strip()

    if not text:
        return
    if event.is_group:
        if owner and event.chat_id in ALLOWED_GROUPS and event.mentioned:
            await on_owner(client, event, text, want_voice, tone)
        return
    if not event.is_private:
        return  # каналы игнорируем
    if owner:
        await on_owner(client, event, text, want_voice, tone)
    elif event.sender_id in TASK_CHATS:
        await on_task_chat(client, event, text)
    elif PUBLIC_MODE:
        await on_stranger(client, event, text)


async def _sync_bio(client):
    full = await client(functions.users.GetFullUserRequest("me"))
    if (full.full_user.about or "").strip() != BIO:
        await client(functions.account.UpdateProfileRequest(about=BIO))
        log.info("bio set")


async def _housekeeping():
    while True:
        cleanup_tmp()
        await asyncio.sleep(1800)


def main():
    client = TelegramClient(str(SESSION), int(TG_API_ID), TG_API_HASH)

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event):
        try:
            await on_message(client, event)
        except Exception as e:  # noqa: BLE001
            log.exception("handler failed: %s", type(e).__name__)

    loop = client.loop
    loop.run_until_complete(client.connect())
    if not loop.run_until_complete(client.is_user_authorized()):
        raise SystemExit("Нет входа в аккаунт: сначала запусти login_qr.py")
    loop.run_until_complete(sender.start(client))
    try:
        loop.run_until_complete(calls.start(client))
    except Exception as e:  # noqa: BLE001
        log.warning("calls unavailable: %s", e)
    for f in BASE.glob("userbot.session*"):
        os.chmod(f, 0o600)
    if PUBLIC_MODE:
        try:
            loop.run_until_complete(_sync_bio(client))
        except Exception as e:  # noqa: BLE001
            log.warning("bio not set: %s", type(e).__name__)
    if OWNER_PATH == "runs":
        owner_runs.deny_leftovers()   # всё, что ждало «да/нет» до перезапуска, — отказ
    loop.create_task(_housekeeping())
    log.info("bridge started; owner path=%s; allowed users=%d groups=%d",
             OWNER_PATH, len(ALLOWED_USERS), len(ALLOWED_GROUPS))
    client.run_until_disconnected()
