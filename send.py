#!/usr/bin/env python3
"""Отправить сообщение с личного аккаунта Рин «userbot» — ТОЛЬКО по прямой просьбе владельца.
Использование: python3 send.py <@username | +телефон | id> "<текст>"
          python3 send.py --voice <@username | id> "<текст>"      — голосовое сообщение голосом Рин
          python3 send.py --file <@username | id> <путь> [подпись] — файл/картинка
          python3 send.py --gif <@username | id> <путь к .gif> [подпись] — гифка
          python3 send.py --read <@username | id> [сколько=10]   — прочитать переписку (ответы)
          python3 send.py --call <@username | id> "<зачем звонишь>" — позвонить голосом Рин (итог придёт владельцу)
Лимиты: 10/час, 30/день, не больше 3 новых людей в день; только люди (не группы/каналы/боты).
Отправляется ровно то, что попросил владелец, без представлений."""
import json, socket, sys
from pathlib import Path

if len(sys.argv) < 3:
    sys.exit(__doc__)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(60)
try:
    s.connect(str(Path(__file__).resolve().parent / "send.sock"))
except OSError:
    sys.exit(json.dumps({"ok": False, "error": "мост не запущен"}, ensure_ascii=False))
if sys.argv[1] == "--read":
    req = {"op": "read", "to": sys.argv[2], "limit": int(sys.argv[3]) if len(sys.argv) > 3 else 10}
elif sys.argv[1] == "--call":
    # python3 send.py --call <@user> "<поручение>"  — звонок в Telegram голосом Рин, разговор идёт в фоне
    req = {"op": "call", "to": sys.argv[2], "text": " ".join(sys.argv[3:])}
elif sys.argv[1] == "--voice":
    # python3 send.py --voice <@user> "<текст>"  — голосовое сообщение голосом Рин
    req = {"to": sys.argv[2], "text": " ".join(sys.argv[3:]), "voice": True}
elif sys.argv[1] == "--gif":
    # python3 send.py --gif <@user> <путь к .gif> [подпись]  — гифка (анимация)
    req = {"to": sys.argv[2], "file": sys.argv[3], "text": " ".join(sys.argv[4:])}
elif sys.argv[1] == "--file":
    # python3 send.py --file <@user> <file_path> [<text>]
    target = sys.argv[2]
    fpath = sys.argv[3]
    text = " ".join(sys.argv[4:])
    req = {"to": target, "file": fpath, "text": text}
else:
    req = {"to": sys.argv[1], "text": " ".join(sys.argv[2:])}
s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
print(s.makefile().readline().strip())
