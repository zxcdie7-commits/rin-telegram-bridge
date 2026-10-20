#!/bin/bash
# Установка: окружение, вход в аккаунт Telegram, автозапуск (macOS, launchd).
set -e
D="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3.14}"
command -v "$PY" >/dev/null || { echo "Нужен Python 3.12+ (py-tgcalls). Укажи: PYTHON=/путь/к/python3 ./setup.sh"; exit 1; }
[ -d "$D/venv" ] || "$PY" -m venv "$D/venv"
"$D/venv/bin/pip" install -q -r "$D/requirements.txt"
[ -f "$D/.env" ] || { cp "$D/.env.example" "$D/.env"; chmod 600 "$D/.env"; echo "Заполни $D/.env и запусти setup.sh ещё раз."; exit 0; }
[ -f "$D/userbot.session" ] || "$D/venv/bin/python" "$D/login_qr.py"
sed "s#@DIR@#$D#g" "$D/com.hermes.userbot.plist.example" > ~/Library/LaunchAgents/com.hermes.userbot.plist
launchctl bootout gui/$(id -u)/com.hermes.userbot 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hermes.userbot.plist
sleep 5
tail -2 "$D/bridge.log" 2>/dev/null || true
echo; echo "✅ Мост запущен и будет стартовать сам. Напиши аккаунту Рин со своего основного Telegram."
