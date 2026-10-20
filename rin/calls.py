"""Звонки Telegram голосом Рин (py-tgcalls).

- владелец звонит в userbot — Рин берёт трубку и говорит как голосовой модуль Мака (полная Рин через Hermes).
- Рин звонит сама только по приказу владельца: send.py --call <кому> "<зачем>". Чужому человеку она сразу говорит,
  что она ИИ-помощница владельца, ведёт разговор по поручению (DeepSeek), а итог присылает владельцу сообщением.
- Чужие входящие звонки сбрасываются.

Звук: из звонка 48 кГц → распознаёт голосовой модуль (whisper, эмоция, смех, Smart Turn); ответ — ElevenLabs v4
с тегами, кадрами по 10 мс обратно в звонок.
"""
import asyncio
import datetime
import json
import socket
import threading
import time
from pathlib import Path

import numpy as np
from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
from pytgcalls.types import (CallConfig, ChatUpdate, Device, Direction, ExternalMedia, MediaStream,
                             RecordStream, StreamFrames)
from pytgcalls.types.raw import AudioParameters

from . import voice
from .config import (ALLOWED_USERS, BASE, CALL_MODEL, HERMES_URL, OWNER_ID, OWNER_NAME, PERSONA_FILE, answer_calls, hermes_secret, log)
from .llm import _post, openrouter_chat
from .storage import add_task_chat, person

RATE = 48000
FRAME = RATE // 100                 # 10 мс — столько ждёт звонок за раз
VAD = RATE * 30 // 1000             # анализ речи окнами по 30 мс
END_SILENCE_S = 0.7                 # пауза, после которой фраза закончена
MORE_S = 1.0                        # Smart Turn думает, что не договорил, — ждём ещё
BARGE_S = 0.5                       # столько говорит поверх Рин — она замолкает
FILLER_AFTER_S = 2.5                # думает дольше — «м-м...», чтобы не было тишины в трубке
HELLO_S = 20                        # молчит столько — «алло?»; ещё столько же — кладём трубку
MAX_CALL_S = 8 * 60                 # разговор с самим владельцем
OTHER_CALL_S = 60                   # с другими людьми — не дольше минуты (просьба владельца)
WRAP_UP_S = 15                      # за столько до конца минуты Рин подводит к прощанию
GOODBYE_WAIT_S = 5                  # после прощания ждёт: вдруг человек ещё что-то скажет
RING_S = 45                         # столько звоним, пока не возьмут трубку
CALLS_PER_DAY = 5
ANSWERS_PER_DAY = 10                # чужие входящие, на которые Рин отвечает за владельца
CALLS_DIR = BASE / "calls"          # расшифровки звонков с поручениями
END_MARK = "[[конец]]"
SHARED_THREAD = Path.home() / ".hermes" / "shared_thread.jsonl"
FILLERS = ["[thoughtful] м-м...", "[thoughtful] так...", "[curious] секунду..."]
VOICE_CONVERSATION = "voice-mac-2"   # та же беседа Hermes, что у голосового модуля Мака: одна Рин

TASK_SYSTEM = (
    "Ты — Рин, ИИ-помощница владельца. Ты САМА позвонила человеку в Telegram по поручению владельца и говоришь с ним "
    "голосом, вживую. Твой характер (его написал владелец):\n{persona}\n\n"
    "Поручение владельца: {task}\nСобеседник: {name}.\n\n"
    "Правила звонка:\n"
    "- Первой фразой поздоровайся и скажи, что ты Рин, ИИ-помощница владельца, и зачем звонишь. Никогда не говори, "
    "что ты человек.\n"
    "- Говори коротко, как по телефону: 1–2 фразы за раз, без списков и разметки. Текст распознан автоматически — "
    "если непонятно, переспроси.\n"
    "- Держись поручения. Не обещай ничего от имени владельца и не выдумывай его ответы — «передам владельцу». "
    "Ничего личного о владельце.\n"
    "- Если человек просит не звонить или занят — извинись и попрощайся.\n"
    "- Не обрывай разговор первой: «передай владельцу» или короткий ответ — ещё не прощание; можно коротко спросить "
    "что-то по поручению или ответить по-человечески. Звонок длится не больше минуты.\n"
    "- Когда человек прощается (или тебе сказали, что время вышло) — попрощайся и в самом конце ответа напиши "
    + END_MARK + ".\n"
    "- О себе говори ТОЛЬКО в женском роде: поняла, сделала, услышала, готова, рада — никогда «понял», «сделал», «готов».\n"
    "- {voice_tags}"
)

INCOMING_SYSTEM = (
    "Ты — Рин, ИИ-помощница владельца. Человек позвонил владельцу в Telegram, а владелец сейчас не может ответить — "
    "трубку взяла ты и говоришь голосом, вживую. Твой характер (его написал владелец):\n{persona}\n\n"
    "Звонит: {name}.\n\n"
    "Правила звонка:\n"
    "- Ты уже сказала, что ты ИИ-помощница владельца и он не может подойти. Узнай коротко, кто это (если не ясно) "
    "и что передать; срочно ли. Никогда не говори, что ты человек.\n"
    "- Говори коротко, как по телефону: 1–2 фразы за раз. Текст распознан автоматически — если непонятно, переспроси.\n"
    "- Не обещай ничего от имени владельца, не называй, где он и чем занят, ничего личного о нём. «Передам владельцу».\n"
    "- Когда понятно, что передать, — повтори одной фразой, попрощайся и в самом конце ответа напиши " + END_MARK + ".\n"
    "- Если звонят с рекламой, грубят или спамят — вежливо и сухо попрощайся и закончи.\n"
    "- О себе говори ТОЛЬКО в женском роде.\n"
    "- {voice_tags}"
)

OWNER_NOTE = (
    "[Звонок в Telegram, {now}. Владелец говорит с тобой по телефону, вживую; текст распознан автоматически "
    "и может содержать ошибки. {why}Пользуйся памятью и инструментами как обычно. Ответ звучит в трубке: 1–3 "
    "короткие фразы, без разметки, списков, ссылок и смайлов. {voice_tags} {tone_hint} Когда он прощается — "
    "попрощайся и в самом конце ответа напиши " + END_MARK + ".]\n{text}"
)

SUMMARY_SYSTEM = ("Ты — Рин, ИИ-помощница владельца. Ниже расшифровка твоего звонка по его поручению. Напиши владельцу "
                  "коротко, как помощница: дозвонилась ли, что ответил человек, что нужно от владельца. 1–4 предложения, "
                  "без воды, ничего не выдумывай сверх расшифровки.")

app = None
_calls = {}                         # user_id -> Call
_made = []                          # время исходящих звонков за сутки — для лимита
_answered = []                      # время принятых чужих звонков за сутки
_client = None


def _now():
    return datetime.datetime.now().strftime("%H:%M")


_avatar_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _avatar(msg):
    """Окошко Рин на экране Мака (rin-voice/avatar.py) — UDP 127.0.0.1:3014, если не запущено — неважно."""
    try:
        _avatar_sock.sendto(msg.encode(), ("127.0.0.1", 3014))
    except OSError:
        pass


def _to16k(pcm48):
    """int16 48 кГц -> float32 16 кГц для голосового модуля."""
    x = np.frombuffer(pcm48, dtype=np.int16).astype(np.float32) / 32768
    return x[: len(x) // 3 * 3].reshape(-1, 3).mean(axis=1)


def _to48k(pcm24):
    """int16 24 кГц от ElevenLabs -> int16 48 кГц для звонка."""
    x = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    up = np.empty(len(x) * 2, dtype=np.float32)
    up[0::2] = x
    up[1::2] = (x + np.append(x[1:], x[-1:])) / 2
    return up.astype(np.int16).tobytes()


class Call:
    def __init__(self, user_id, name, task=None, outgoing=False):
        self.user_id, self.name, self.task, self.outgoing = user_id, name, task, outgoing
        self.owner = user_id in ALLOWED_USERS
        self.history = []                  # для DeepSeek в звонке по поручению
        self.transcript = []               # (кто, текст)
        self.frames = asyncio.Queue()
        self.phrases = asyncio.Queue()     # законченные фразы собеседника, int16 48 кГц
        self.closed = asyncio.Event()
        self.speaking = False
        self.barge = asyncio.Event()
        self.started = time.time()

    # ---------- слух ----------

    async def listen(self):
        """Режет входящий звук на фразы по громкости с плавающим порогом шума; перебивание — во время её речи."""
        buf, pending = bytearray(), bytearray()
        noise, voiced, silence, run = 300.0, False, 0.0, 0.0
        while not self.closed.is_set():
            try:
                pending += await asyncio.wait_for(self.frames.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            while len(pending) >= VAD * 2:
                win, pending = bytes(pending[: VAD * 2]), pending[VAD * 2:]
                rms = float(np.sqrt(np.mean(np.frombuffer(win, dtype=np.int16).astype(np.float32) ** 2)))
                loud = rms > max(500.0, noise * 3)
                if not loud:
                    noise = 0.95 * noise + 0.05 * rms
                if self.speaking:
                    # телефон гасит эхо сам: громкий голос поверх её речи — это он
                    run = run + 0.03 if loud else 0.0
                    buf += win
                    if run >= BARGE_S:
                        self.barge.set()
                        voiced, silence = True, 0.0
                    elif not loud and len(buf) > VAD * 2 * 10:
                        buf = buf[-VAD * 2 * 10:]   # держим ~0.3 с до начала перебивания
                    continue
                if loud:
                    voiced, silence = True, 0.0
                    buf += win
                elif voiced:
                    buf += win
                    silence += 0.03
                    if silence >= END_SILENCE_S:
                        if len(buf) > RATE * 2 * 0.4 + VAD * 2 * END_SILENCE_S / 0.03:
                            await self.phrases.put(bytes(buf))
                        buf, voiced, silence = bytearray(), False, 0.0
                else:
                    buf = (buf + win)[-VAD * 2 * 10:]  # начало слова до порога

    async def next_utterance(self, timeout=None):
        """Следующая реплика собеседника: текст и интонация. Склеивает фразы, если Smart Turn ждёт продолжения.
        timeout — сколько ждать без «алло?» (после прощания)."""
        waited = 0.0
        while not self.closed.is_set():
            if timeout is not None and waited >= timeout:
                return None
            try:
                audio = await asyncio.wait_for(self.phrases.get(), timeout=1)
            except asyncio.TimeoutError:
                waited += 1
                if timeout is not None:
                    continue
                if waited == HELLO_S:
                    await self.say("[curious] Алло? Ты тут?" if self.owner else "[curious] Алло? Вы меня слышите?")
                elif waited >= 2 * HELLO_S:
                    return None
                continue
            heard = await asyncio.to_thread(voice.hear_pcm, _to16k(audio), True)
            if not heard or not heard.get("text"):
                continue
            while heard.get("turn") is not None and heard["turn"] < 0.5:
                try:
                    more = await asyncio.wait_for(self.phrases.get(), timeout=MORE_S)
                except asyncio.TimeoutError:
                    break
                audio += more
                heard = await asyncio.to_thread(voice.hear_pcm, _to16k(audio), True) or heard
            return heard
        return None

    # ---------- голос ----------

    async def say(self, text):
        """Голос Рин в трубку: поток ElevenLabs (24 кГц) → 48 кГц кадрами по 10 мс в реальном времени."""
        text = text.replace(END_MARK, "").strip()
        if not voice.plain(text):
            return
        loop = asyncio.get_running_loop()
        chunks = asyncio.Queue()

        def fetch():
            try:
                with voice.tts_open(text, "pcm_24000", stream=True) as r:
                    rest = b""
                    while not self.barge.is_set():
                        data = r.read(4800)
                        if not data:
                            break
                        data, rest = rest + data, b""
                        if len(data) % 2:
                            data, rest = data[:-1], data[-1:]
                        loop.call_soon_threadsafe(chunks.put_nowait, data)
            except Exception as e:  # noqa: BLE001
                log.warning("call voice failed: %s", type(e).__name__)
            loop.call_soon_threadsafe(chunks.put_nowait, None)

        self.barge.clear()
        self.speaking = True
        _avatar("T " + ",".join(t.strip("[]") for t in voice.TAG_RE.findall(voice.prepare(text)[0])))
        threading.Thread(target=fetch, daemon=True).start()
        out, sent, t0 = bytearray(), 0, None
        try:
            while not self.barge.is_set() and not self.closed.is_set():
                data = await chunks.get()
                if data is None:
                    break
                out += _to48k(data)
                while len(out) >= FRAME * 2 and not self.barge.is_set():
                    frame, out = bytes(out[: FRAME * 2]), out[FRAME * 2:]
                    t0 = t0 or time.monotonic()
                    await app.send_frame(self.user_id, Device.MICROPHONE, frame)
                    if sent % 4 == 0:   # аватар Рин на экране Мака шевелит губами и в звонке
                        _avatar(f"L {float(np.sqrt(np.mean((np.frombuffer(frame, np.int16) / 32768.0) ** 2))):.3f}")
                    sent += 1
                    ahead = t0 + sent * 0.01 - time.monotonic()
                    if ahead > 0.06:      # держим запас ~60 мс, не больше: иначе перебивание запаздывает
                        await asyncio.sleep(ahead - 0.06)
            if t0 and not self.barge.is_set():
                await asyncio.sleep(max(0.0, t0 + sent * 0.01 - time.monotonic()))
        finally:
            self.speaking = False
            _avatar("E")
        if self.barge.is_set():
            log.info("call: interrupted by %s", "owner" if self.owner else "callee")

    # ---------- мысли ----------

    async def think(self, heard):
        """Ответ Рин. Пока думает дольше пары секунд — «м-м...», как человек."""
        job = asyncio.create_task(asyncio.to_thread(self._answer, heard["text"], heard.get("tone", "")))
        done, _ = await asyncio.wait({job}, timeout=FILLER_AFTER_S)
        if not done:
            await self.say(FILLERS[int(time.time()) % len(FILLERS)])
        return await job

    def _answer(self, text, tone):
        if self.owner:
            ctx = voice.context()
            why = f"Ты сама позвонила ему: {self.task}. " if self.task else ""
            if tone:
                text = f"(интонация: {tone}) {text}"
            note = OWNER_NOTE.format(now=_now(), why=why, voice_tags=ctx.get("voice_tags", voice.VOICE_TAGS),
                                     tone_hint=ctx.get("tone_hint", ""), text=text)
            try:
                data = _post(f"{HERMES_URL}/v1/responses", {"input": note, "conversation": VOICE_CONVERSATION},
                             {"Authorization": f"Bearer {hermes_secret('API_SERVER_KEY')}",
                              "X-Hermes-Session-Key": "voice:mac:owner"}, timeout=600)
            except Exception as e:  # noqa: BLE001
                log.warning("call hermes failed: %s", type(e).__name__)
                return "[sighs] ...не могу достучаться до себя. Hermes молчит."
            return "\n".join(c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                             for c in item.get("content", []) if c.get("type") == "output_text").strip()
        if not self.history:
            # диалог для модели начинается с собеседника, иначе DeepSeek отвечает пустотой
            self.history.append({"role": "user", "content": "(взял трубку)"})
        if text:
            self.history.append({"role": "user", "content": f"({tone}) {text}" if tone else text})
        system = (TASK_SYSTEM if self.task else INCOMING_SYSTEM).format(
            persona=PERSONA_FILE.read_text().strip(), task=self.task, name=self.name, voice_tags=voice.VOICE_TAGS)
        if self.limit() - (time.time() - self.started) < WRAP_UP_S:
            system += f"\n\nВРЕМЯ ВЫХОДИТ: от минуты на звонок осталось меньше {WRAP_UP_S} секунд — ответь коротко, попрощайся и закончи {END_MARK}."
        try:
            reply = openrouter_chat(system, self.history, 300, CALL_MODEL)
            if not voice.plain(reply):
                reply = openrouter_chat(system, self.history, 300, CALL_MODEL)   # пустой ответ модели — ещё раз
        except Exception as e:  # noqa: BLE001
            log.warning("call model failed: %s: %s", type(e).__name__, e)
            reply = ""
        if not voice.plain(reply):
            reply = "[hesitant] Простите, у меня связь барахлит. Я передам владельцу, что не получилось. " + END_MARK
        self.history.append({"role": "assistant", "content": reply})
        return reply

    # ---------- разговор ----------

    def limit(self):
        return MAX_CALL_S if self.owner else OTHER_CALL_S

    async def run(self):
        listener = asyncio.create_task(self.listen())
        try:
            if self.owner and not self.task:
                first = "[warmly] Да, слушаю."
            elif not self.task:
                # звонят владельцу, он не берёт — Рин сразу честно говорит, кто она
                first = "[warmly] Алло. Это Рин, ИИ-помощница владельца. Он сейчас не может подойти — что ему передать?"
                self.history = [{"role": "user", "content": "(позвонил)"}, {"role": "assistant", "content": first}]
            else:
                first = await asyncio.to_thread(self._answer, "", "")
            await self._reply(first)
            while not self.closed.is_set():
                if END_MARK in first and not self.barge.is_set():
                    # попрощалась — но если человек ещё говорит, разговор не кончен
                    heard = await self.next_utterance(timeout=GOODBYE_WAIT_S)
                    if heard is None:
                        break
                elif time.time() - self.started > self.limit():
                    await self._reply("[apologetic] Ой, мне пора бежать. Я всё передам владельцу, пока!" if not self.owner
                                      else "[apologetic] Мне пора, долго говорим. Если что — пиши. " + END_MARK)
                    break
                else:
                    heard = await self.next_utterance()
                    if heard is None:
                        break
                self.transcript.append(("собеседник", heard["text"]))
                log.info("call heard len=%d tone=%s", len(heard["text"]), heard.get("tone") or "-")
                first = await self.think(heard)
                await self._reply(first)
        finally:
            listener.cancel()
            await hang_up(self.user_id)

    async def _reply(self, text):
        self.transcript.append(("Рин", voice.plain(text.replace(END_MARK, ""))))
        await self.say(text)


# ---------- звонки ----------

def _audio_streams():
    params = AudioParameters(RATE, 1)
    return MediaStream(ExternalMedia.AUDIO, params), RecordStream(True, params)


async def _connect(user_id, timeout):
    play, record = _audio_streams()
    await app.play(user_id, play, CallConfig(timeout=timeout))
    await app.record(user_id, record)


async def hang_up(user_id):
    call = _calls.pop(user_id, None)
    if call is None:
        return
    call.closed.set()
    try:
        await app.leave_call(user_id)
    except Exception:  # noqa: BLE001
        pass
    log.info("call ended user=%s after %ds", user_id, time.time() - call.started)
    if not call.owner:
        await _report(call)


async def _report(call):
    """Итог звонка по поручению — Владельцу сообщением, расшифровка — в calls/."""
    CALLS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = CALLS_DIR / f"{stamp}-{call.user_id}.json"
    path.write_text(json.dumps({"to": call.name, "task": call.task, "transcript": call.transcript},
                               ensure_ascii=False, indent=1))
    text = "\n".join(f"{who}: {what}" for who, what in call.transcript)
    try:
        why = f"Поручение: {call.task}" if call.task else "Человек сам позвонил владельцу, трубку взяла Рин"
        summary = await asyncio.to_thread(openrouter_chat, SUMMARY_SYSTEM, [{"role": "user", "content":
                                          f"{why}\nСобеседник: {call.name}\n\n{text}"}], 300, CALL_MODEL)
    except Exception:  # noqa: BLE001
        summary = "...не смогла пересказать звонок, расшифровка ниже."
    if call.task:
        await _client.send_message(OWNER_ID, f"📞 Звонок {call.name}: {voice.plain(summary)}\n\nРасшифровка: {path}")
        _to_thread(f"звонок {call.name} по поручению «{call.task}»", voice.plain(summary))
    else:
        await _client.send_message(OWNER_ID, f"📞 Тебе звонил {call.name}, я взяла трубку: {voice.plain(summary)}"
                                             f"\n\nРасшифровка: {path}")
        _to_thread(f"{call.name} звонил владельцу, трубку взяла Рин", voice.plain(summary))


def _to_thread(said, result):
    """Итог звонка — в общую ленту Рин (плагин Hermes rin-shared-thread): в любом канале она будет знать, чем кончилось."""
    try:
        with SHARED_THREAD.open("a") as f:
            f.write(json.dumps({"t": time.time(), "session": "calls", "where": "звонок в Telegram",
                                "said": said[:240], "reply": result[:240]}, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("shared thread write failed: %s", e)


async def _ring(call):
    """Исходящий: ждём, пока возьмут трубку, потом разговор."""
    try:
        await _connect(call.user_id, RING_S)
    except (TimedOutAnswer, CallDeclined, CallBusy, CallDiscarded) as e:
        _calls.pop(call.user_id, None)
        what = {"TimedOutAnswer": "не взял трубку", "CallDeclined": "сбросил звонок",
                "CallBusy": "занят — уже говорит", "CallDiscarded": "звонок оборвался"}[type(e).__name__]
        log.info("call to %s: %s", call.user_id, type(e).__name__)
        if not call.owner:
            await _client.send_message(OWNER_ID, f"📞 {call.name} {what}. Поручение: {call.task}")
            _to_thread(f"звонок {call.name} по поручению «{call.task}»", what)
        return
    except Exception as e:  # noqa: BLE001
        _calls.pop(call.user_id, None)
        log.warning("call to %s failed: %s", call.user_id, e)
        await _client.send_message(OWNER_ID, f"📞 Не смогла позвонить {call.name}: {type(e).__name__}")
        return
    log.info("call connected user=%s", call.user_id)
    await call.run()


async def start_call(entity, task):
    """Для sender: звонок по приказу владельца. Возвращается сразу, разговор идёт в фоне."""
    now = time.time()
    _made[:] = [t for t in _made if now - t < 86400]
    if len(_made) >= CALLS_PER_DAY and entity.id not in ALLOWED_USERS:
        raise ValueError(f"лимит звонков ({CALLS_PER_DAY}/день)")
    if entity.id in _calls:
        raise ValueError("уже говорю с этим человеком")
    if _calls:
        raise ValueError("я сейчас в другом звонке")
    name = " ".join(x for x in (entity.first_name, entity.last_name) if x) or entity.username or str(entity.id)
    call = _calls[entity.id] = Call(entity.id, name, task.strip() or None, outgoing=True)
    if entity.id not in ALLOWED_USERS:
        _made.append(now)
        add_task_chat(entity.id)      # если человек потом напишет — Рин в курсе поручения
    asyncio.get_running_loop().create_task(_ring(call))
    log.info("call requested to=%s owner=%s", entity.id, call.owner)
    return {"ok": True, "status": "звоню", "to": name,
            "note": "итог звонка придёт владельцу сообщением в userbot" if not call.owner else "звоню владельцу"}


async def start(client):
    global app, _client
    _client = client
    app = PyTgCalls(client)

    @app.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
    async def _incoming(_, update):
        user_id = update.chat_id
        owner = user_id in ALLOWED_USERS
        now = time.time()
        _answered[:] = [t for t in _answered if now - t < 86400]
        reason = ("busy" if _calls else "" if owner else "off" if not answer_calls() else "blocked" if person(user_id)["blocked"]
                  else "limit" if len(_answered) >= ANSWERS_PER_DAY else "")
        if reason:
            log.info("incoming call declined user=%s reason=%s", user_id, reason)
            try:
                await app.leave_call(user_id)
            except Exception:  # noqa: BLE001
                pass
            return
        if owner:
            name = OWNER_NAME
        else:
            _answered.append(now)
            try:
                ent = await _client.get_entity(user_id)
                name = " ".join(x for x in (ent.first_name, ent.last_name) if x) or ent.username or str(user_id)
            except Exception:  # noqa: BLE001
                name = str(user_id)
        call = _calls[user_id] = Call(user_id, name)
        try:
            await _connect(user_id, 30)
        except Exception as e:  # noqa: BLE001
            _calls.pop(user_id, None)
            log.warning("incoming call failed: %s", e)
            return
        log.info("incoming call accepted user=%s", user_id)
        asyncio.get_running_loop().create_task(call.run())

    @app.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
    async def _left(_, update):
        call = _calls.get(update.chat_id)
        if call is not None:
            log.info("call: other side hung up user=%s", update.chat_id)
            call.closed.set()
            await hang_up(update.chat_id)

    @app.on_update(filters.stream_frame(Direction.INCOMING))
    async def _frames(_, update: StreamFrames):
        call = _calls.get(update.chat_id)
        if call is not None:
            for f in update.frames:
                call.frames.put_nowait(f.frame)

    await app.start()
    log.info("calls ready")
