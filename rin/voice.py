"""Голос Рин как в голосовом модуле Мака (~/.local/rin-voice): теги интонаций ElevenLabs v4 и «слух» —
распознавание, эмоция и смех по голосу. Модели держит голосовой модуль, мост обращается к нему по 127.0.0.1:3012."""
import json
import re
import subprocess
import urllib.request

import numpy as np

from .config import FFMPEG, VOICE_ID, hermes_secret, log

HEAR_URL = "http://127.0.0.1:3012"
TAG_MODEL = "eleven_v4_turbo"           # играет теги [laughs] [softly], а не читает их
PLAIN_MODEL = "eleven_multilingual_v2"   # запасной: без тегов
TAG_RE = re.compile(r"\[[a-z][a-z -]{1,30}\]")

# на случай, если голосовой модуль выключен — те же правила, что у него
VOICE_TAGS = (
    "Твой голос играет интонации по тегам на английском в квадратных скобках — они прозвучат, а не прочитаются. "
    "Ставь их в КАЖДЫЙ ответ, 1–3 штуки, перед фразой, к которой относятся: [softly], [whispers], [chuckles], "
    "[laughs], [sighs], [smirks], [sarcastic], [teasing], [curious], [thoughtful], [excited], [warmly], [tired], "
    "[pause]. Один тег — одно-два английских слова без запятых. Русских ремарок в скобках не пиши. "
    "О себе говори ТОЛЬКО в женском роде: поняла, сделала, услышала, готова, рада — никогда «понял», «сделал», «готов»."
)


def _fix_brackets(m):
    """[tired, chuckling] → [tired] [chuckling]: с запятой голос читает тег вслух. [смеётся] — ремарка, убираем."""
    inner = m.group(1).strip()
    if re.fullmatch(r"[A-Za-z ,;/-]+", inner):
        tags = [t.strip().lower() for t in re.split(r"[,;/]|\band\b|\bthen\b", inner) if t.strip()]
        return " ".join(f"[{t}]" for t in tags if re.fullmatch(r"[a-z][a-z -]{1,30}", t))
    if re.fullmatch(r"[А-Яа-яЁё ,.-]+", inner) and len(inner.split()) <= 4:
        return ""
    return inner


def prepare(text):
    """Текст ответа -> (для голоса с тегами, чистый текст без тегов)."""
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"\[([^\]]*)\]", _fix_brackets, text).strip()
    return text, plain(text)


def plain(text):
    """Убрать теги интонаций — для текстовых сообщений."""
    lines = (re.sub(r"[ \t]{2,}", " ", line).strip() for line in TAG_RE.sub("", text).split("\n"))
    return "\n".join(lines).strip()


# ---------- синтез ----------

def tts_request(text, model, output_format="mp3_44100_128", stream=False):
    path = f"/v1/text-to-speech/{VOICE_ID}" + ("/stream" if stream else "")
    req = urllib.request.Request(f"https://api.elevenlabs.io{path}?output_format={output_format}",
                                 data=json.dumps({"text": text[:2500], "model_id": model}).encode(),
                                 headers={"xi-api-key": hermes_secret("ELEVENLABS_API_KEY"),
                                          "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=90)


def tts_open(text, output_format="mp3_44100_128", stream=False):
    """Ответ ElevenLabs: v4 с тегами, при ошибке (одни теги без слов дают 400) — без тегов."""
    tagged, clean = prepare(text)
    try:
        return tts_request(tagged, TAG_MODEL, output_format, stream)
    except Exception as e:  # noqa: BLE001
        log.warning("v4 voice failed (%s), plain fallback", type(e).__name__)
        return tts_request(clean, PLAIN_MODEL, output_format, stream)


# ---------- слух ----------

def hear_pcm(audio, turn=False):
    """float32 16 кГц -> {"text", "tone", "turn"} или None, если голосовой модуль не запущен."""
    req = urllib.request.Request(f"{HEAR_URL}/hear" + ("?turn=1" if turn else ""),
                                 data=np.asarray(audio, dtype=np.float32).tobytes())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        log.warning("hear api unavailable: %s", type(e).__name__)
        return None


def decode16k(path):
    out = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ar", "16000", "-ac", "1", "-"],
                         capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(out, dtype=np.float32)


def hear_file(path):
    """Голосовое -> {"text", "tone"} или None."""
    try:
        return hear_pcm(decode16k(path))
    except subprocess.CalledProcessError:
        return None


def context():
    """Настроение за день, матч Dota и правила голоса — из голосового модуля, если он запущен."""
    try:
        with urllib.request.urlopen(f"{HEAR_URL}/context", timeout=5) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return {"mood": "", "dota": "", "voice_tags": VOICE_TAGS, "tone_hint": ""}


def voice_note(tone="", spoken_reply=True):
    """Пометка для Hermes к голосовому владельца: как он сказал, настроение, игра, правила тегов."""
    ctx = context()
    parts = []
    if tone:
        parts.append(f"Интонация этого голосового: {tone}.")
    if ctx.get("tone_hint"):
        parts.append(ctx["tone_hint"])
    if ctx.get("mood"):
        parts.append(f"Его настроение по голосу сегодня: {ctx['mood']} — замечай перемены, но не пересказывай без повода.")
    if spoken_reply:
        parts.append("Ответ озвучится голосовым: 1–3 короткие живые фразы без разметки и списков. "
                     + ctx.get("voice_tags", VOICE_TAGS))
    return "[" + " ".join(parts) + "]"


def dota_note():
    """Матч Dota, если владелец сейчас играет: совет по сборке из Telegram тоже должен знать игру."""
    game = context().get("dota")
    if not game:
        return ""
    return (f"[Владелец сейчас играет в Dota 2 (данные из игры): {game}. Героев и предметы врагов ты не видишь — "
            "для совета по сборке спроси, против кого он.]")
