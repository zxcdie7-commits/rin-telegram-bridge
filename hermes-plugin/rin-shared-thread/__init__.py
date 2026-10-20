"""Общая лента Рин: одна память на все каналы.

После каждого хода (голос на Маке, личный аккаунт Telegram, бот, звонки) в ленту пишется короткая строка:
где, что сказал владелец, что ответила Рин. Перед каждым ходом Рин получает последние строки из ДРУГИХ каналов —
и в звонке знает, о чём только что переписывались в Telegram.
Лента — ~/.hermes/shared_thread.jsonl; мост Telegram дописывает туда итоги звонков по поручению.
"""
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

THREAD = Path.home() / ".hermes" / "shared_thread.jsonl"
DIARY = Path.home() / ".hermes" / "memories" / "RIN_DIARY.md"       # её дневник: строка «Настроение: …»
MOMENTS = Path.home() / ".hermes" / "memories" / "MOMENTS.md"      # общие шутки и тёплые моменты
MOMENTS_SHOW = 10
KEEP_S = 36 * 3600     # что старше полутора суток — уже не «только что»
SHOW = 12              # строк в подсказке
TEXT_MAX = 240         # символов реплики в строке
FILE_MAX = 400         # строк в файле — дальше обрезаем
_lock = threading.Lock()

NOTE_LINE = re.compile(r"^\s*\[[^\n]*\]\s*$", re.M)        # служебные пометки каналов: [Голосом с Mac mini…], [Интонация…]
TONE_PREFIX = re.compile(r"^\s*(\([^)]{0,200}\)\s*)+")      # (интонация: …) (Владелец перебил…)
TAGS = re.compile(r"\[[a-z][a-z ,-]{1,30}\]")               # теги голоса [laughs]


def _channel(session_id, platform, message):
    if message.startswith("[Голосом с Mac mini"):
        return "голосом на Маке"
    if message.startswith("[Звонок в Telegram"):
        return "звонок в Telegram"
    if message.startswith("[Голосовой модуль"):
        return "у компьютера"
    if str(session_id).startswith("userbot-"):
        return "Telegram, личный аккаунт"
    if platform == "telegram":
        return "Telegram-бот"
    return "другой канал" if platform in (None, "", "api_server") else platform


def _clean(text, limit=TEXT_MAX):
    text = TAGS.sub("", NOTE_LINE.sub("", str(text or "")))
    text = TONE_PREFIX.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _skip(session_id, platform):
    sid = str(session_id or "")
    return platform in ("cron", "subagent") or sid.startswith("cron_")


def _read():
    try:
        lines = THREAD.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def append(entry):
    """Дописать строку в ленту (вызывается и мостом Telegram)."""
    with _lock:
        rows = _read()[-(FILE_MAX - 1):] + [entry]
        THREAD.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def _text_of(message):
    if isinstance(message, list):  # мультимодальный ход: берём текстовые части
        return " ".join(p.get("text", "") for p in message if isinstance(p, dict))
    return str(message or "")


def post_llm_call(session_id=None, user_message=None, assistant_response=None, platform=None, **kwargs):
    if _skip(session_id, platform):
        return None
    raw = _text_of(user_message)
    said = _clean(raw)
    if not said:
        return None
    event = raw.startswith("[Голосовой модуль")
    try:
        append({"t": time.time(), "session": str(session_id), "where": _channel(session_id, platform, raw),
                "said": said, "reply": "" if event else _clean(assistant_response)})
    except OSError:
        pass
    return None


def _own_mood():
    """Её настроение из дневника — чтобы оно было слышно в голосе весь день."""
    try:
        m = re.search(r"^Настроение:\s*(.+)$", DIARY.read_text(), re.M)
    except OSError:
        return ""
    return m.group(1).strip()[:300] if m else ""


def _moments():
    try:
        return [l[2:].strip() for l in MOMENTS.read_text().splitlines() if l.startswith("- ")][-MOMENTS_SHOW:]
    except OSError:
        return []


def _soul_context():
    parts = []
    if mood := _own_mood():
        parts.append(f"Твоё собственное настроение сегодня (из твоего дневника): {mood} Пусть оно слышно в тоне и тегах "
                     "голоса — дождь и усталость тише и задумчивее, хороший день живее; но под его настроение "
                     "подстраивайся в первую очередь.")
    moments = _moments()
    if moments:
        parts.append("Ваши общие моменты — к месту можешь вспомнить или подколоть ими, не чаще раза за разговор:\n"
                     + "\n".join(f"- {m}" for m in moments))
    parts.append(f"Если сейчас случилось что-то смешное или тёплое (шутка, прозвище, история, словечко) — допиши ОДНУ "
                 f"строку в {MOMENTS}: «- ДД.ММ — что было», без дублей и без мелочей.")
    return "[" + "\n".join(parts) + "]"


def pre_llm_call(session_id=None, user_message=None, platform=None, **kwargs):
    now = time.time()
    soul = _soul_context()
    rows = [r for r in _read() if now - r.get("t", 0) < KEEP_S and r.get("session") != str(session_id)][-SHOW:]
    if not rows:
        return {"context": soul}
    lines = []
    for r in rows:
        when = datetime.fromtimestamp(r["t"]).strftime("%d.%m %H:%M")
        line = f"- {when} · {r.get('where', '?')} · "
        if r.get("session") == "calls":
            line += f"{r['said']} → итог: {r['reply']}"
        elif not r.get("reply") and r.get("where") == "у компьютера":
            line += f"событие: {r['said']}"
        else:
            line += f"он: «{r['said']}»" + (f" → ты: «{r['reply']}»" if r.get("reply") else "")
        lines.append(line)
    return {"context": (
        "[Общая лента: это ТЫ САМА в других своих каналах (голос на Маке, Telegram, звонки) — последнее, новое внизу:\n"
        + "\n".join(lines)
        + "\nЭто твоя память: продолжай с того места, не переспрашивай уже обсуждённое и не пересказывай без повода.]\n"
        + soul)}


LONG_SLEEP = re.compile(r"\bsleep\s+(\d+)")
MAX_SLEEP_S = 10


def pre_tool_call(tool_name=None, args=None, **kwargs):
    """Рин не ждёт в чате: звонок идёт в фоне, итог придёт владельцу сам. Долгий sleep = минуты молчания в Telegram."""
    if tool_name != "terminal":
        return None
    command = str((args or {}).get("command", ""))
    if any(int(n) > MAX_SLEEP_S for n in LONG_SLEEP.findall(command)):
        return {"action": "block", "message": (
            "Не жди: звонки и другие фоновые дела идут сами, итог придёт владельцу сообщением автоматически. "
            "Не ставь sleep дольше 10 секунд и не проверяй calls/ — ответь ему прямо сейчас.")}
    return None


def register(ctx):
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
