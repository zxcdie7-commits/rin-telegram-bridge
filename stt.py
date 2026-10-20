"""Локальная расшифровка голоса для Hermes: ffmpeg -> 16kHz wav -> faster-whisper.
Вызов: stt.py <input> <output_dir> <language> <model>"""
import os, shutil, subprocess, sys, wave
from pathlib import Path
import numpy as np
from faster_whisper import WhisperModel

inp, out_dir = sys.argv[1], Path(sys.argv[2])
lang = (sys.argv[3] if len(sys.argv) > 3 else "") or None
model_name = (sys.argv[4] if len(sys.argv) > 4 else "") or "small"
if lang in ("auto", "{language}"): lang = None
if model_name.startswith("{") or model_name.startswith("whisper"): model_name = "small"
out_dir.mkdir(parents=True, exist_ok=True)
ff = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
wav = out_dir / "audio16k.wav"
subprocess.run([ff, "-y", "-loglevel", "error", "-i", inp, "-ac", "1", "-ar", "16000", str(wav)], check=True)
with wave.open(str(wav), "rb") as w:
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
model = WhisperModel(model_name, device="cpu", compute_type="int8")
segs, _ = model.transcribe(audio, language=lang, beam_size=5, vad_filter=True)
(out_dir / "transcript.txt").write_text(" ".join(s.text.strip() for s in segs).strip())
