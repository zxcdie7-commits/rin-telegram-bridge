"""Голос и файлы: синтез ElevenLabs, расшифровка, конвертация в голосовое, проверка файлов, медиа из ответов Hermes."""
import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import voice
from .config import FFMPEG, STT_CMD, TMP


def tmp_file(prefix, suffix):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(TMP))
    os.close(fd)
    return path


def cleanup_tmp(max_age_sec=3600):
    now = time.time()
    for p in TMP.iterdir():
        try:
            if now - p.stat().st_mtime > max_age_sec:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass


def to_voice_ogg(path):
    """Любое аудио -> ogg/opus (обычное голосовое Telegram)."""
    if Path(path).suffix.lower() in (".ogg", ".opus", ".oga"):
        return str(path)
    out = tmp_file("voice_", ".ogg")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(path), "-c:a", "libopus", "-b:a", "48k", out], check=True)
    return out


def tts_voice(text):
    """Текст -> голосовое голосом Рин (ElevenLabs v4: теги [laughs] [softly] звучат). Единственный источник её голоса."""
    mp3 = tmp_file("tts_", ".mp3")
    with voice.tts_open(text) as r:
        Path(mp3).write_bytes(r.read())
    try:
        return to_voice_ogg(mp3)
    finally:
        os.unlink(mp3)


def transcribe(path):
    """Голосовое -> текст: как в голосовом модуле Мака, запасной путь — локальный faster-whisper."""
    return hear(path)["text"]


def hear(path):
    """Голосовое -> {"text", "tone"}: whisper large-v3-turbo, эмоция и смех — через голосовой модуль."""
    heard = voice.hear_file(path)
    if heard is not None and heard.get("text"):
        return heard
    return {"text": _transcribe_whisper_small(path), "tone": ""}


def _transcribe_whisper_small(path):
    out = tempfile.mkdtemp(prefix="stt_", dir=str(TMP))
    env = dict(os.environ, PATH=str(Path(FFMPEG).parent) + ":" + os.environ.get("PATH", ""))
    try:
        subprocess.run(STT_CMD + [str(path), out, "ru", "small"], check=True, timeout=300, env=env)
        return (Path(out) / "transcript.txt").read_text().strip()
    finally:
        shutil.rmtree(out, ignore_errors=True)


def is_audio_or_video(path):
    """По содержимому файла, а не по расширению: так готовое «робо-аудио» не пролезет под другим именем."""
    try:
        mime = subprocess.run(["/usr/bin/file", "-b", "--mime-type", str(path)],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return True
    return mime.startswith(("audio/", "video/")) or mime in ("application/ogg", "application/octet-stream")


_MEDIA_RE = re.compile(r"(?:!?\[[^\]\n]*\]\()?data:([\w.+/-]+);base64,([A-Za-z0-9+/=\s]+)\)?")
_EXT = {"audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


_MEDIA_PATH_RE = re.compile(r"^[ \t]*MEDIA:[ \t]*(\S+)[ \t]*$", re.M)
_MIME_BY_EXT = {v: k for k, v in _EXT.items()}
_MIME_BY_EXT.update({".jpeg": "image/jpeg", ".opus": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4"})


# ---------- входящие медиа: гифки и стикеры от чужих, гифки/видео от владельца ----------

def media_kind(msg):
    """Тип входящего медиа: 'sticker' | 'gif' | 'photo' | 'video' | 'audio' | '' (нет медиа).
    Гифки Telegram — это video/mp4 с DocumentAttributeAnimated или image/gif."""
    try:
        if getattr(msg, "voice", None) is not None or getattr(msg, "audio", None) is not None:
            return "audio"
        if getattr(msg, "sticker", None) is not None:
            return "sticker"
        doc = getattr(msg, "document", None)
        if doc is None:
            return "photo" if getattr(msg, "photo", None) is not None else ""
        from telethon.tl.types import DocumentAttributeAnimated
        mime = (doc.mime_type or "").lower()
        if any(isinstance(a, DocumentAttributeAnimated) for a in doc.attributes) or mime == "image/gif":
            return "gif"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("audio/"):
            return "audio"
        return ""
    except Exception:  # noqa: BLE001
        return ""


def media_note(msg):
    """Пометка для LLM о входящем медиа: '[прислал гифку]' / '[стикер …]' / '' — чтобы
    упрощённая Рин видела гифку и не отвечала в пустоту."""
    kind = media_kind(msg)
    if kind == "sticker":
        alt = ""
        try:
            alt = (getattr(msg.sticker, "alt", "") or getattr(msg.sticker, "emoji", "") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return f" [прислал стикер{(' ' + alt) if alt else ''}]"
    if kind == "gif":
        return " [прислал гифку]"
    return ""


def save_frame(path):
    """Первый кадр гифки/видео -> jpg. -> путь или None (синхронно, ffmpeg один кадр)."""
    try:
        frame = tmp_file("frame_", ".jpg")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(path), "-frames:v", "1", frame],
                       check=True, timeout=30)
        return frame
    except Exception:  # noqa: BLE001
        return None


def extract_media(text):
    """Медиа из ответа Hermes: data:-ссылки (путь /v1/responses) и строки MEDIA:<путь> (путь /v1/runs).
    -> (чистый текст, [(путь, mime)])."""
    files = []

    def _save(m):
        mime, data = m.group(1), m.group(2)
        path = tmp_file("media_", _EXT.get(mime, ".bin"))
        Path(path).write_bytes(base64.b64decode(re.sub(r"\s", "", data)))
        files.append((path, mime))
        return ""

    def _local(m):
        p = Path(m.group(1).strip("`'\"")).expanduser()
        if not p.is_file():
            return m.group(0)
        files.append((str(p), _MIME_BY_EXT.get(p.suffix.lower(), "application/octet-stream")))
        return ""
    text = _MEDIA_RE.sub(_save, text)
    text = _MEDIA_PATH_RE.sub(_local, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), files
