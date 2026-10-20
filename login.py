"""Одноразовый вход в личный аккаунт Telegram для моста Hermes."""
import getpass
import os
from pathlib import Path

from telethon.sync import TelegramClient  # noqa

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"

print("Вход в аккаунт Telegram для Hermes.")
print("api_id и api_hash возьми на https://my.telegram.org/apps (войди с ЭТИМ номером).\n")
import re


def ask(prompt, check, err, hidden=False):
    while True:
        v = (getpass.getpass(prompt) if hidden else input(prompt)).strip()
        if check(v):
            return v
        print("  ❌ " + err + " Попробуй ещё раз.")


_saved = dict(l.split("=", 1) for l in (ENV.read_text().splitlines() if ENV.exists() else []) if "=" in l)
if _saved.get("TG_API_ID") and _saved.get("TG_API_HASH"):
    print("api_id и api_hash уже сохранены — пропускаю.")
    api_id, api_hash = _saved["TG_API_ID"], _saved["TG_API_HASH"]
else:
    api_id = ask("api_id (только цифры): ", lambda v: v.isdigit(), "api_id — это только цифры.")
    api_hash = ask("api_hash (32 символа, при вставке не видны): ",
                   lambda v: re.fullmatch(r"[0-9a-fA-F]{32}", v), "api_hash — ровно 32 символа (0-9, a-f).", hidden=True)
phone = ask("Номер телефона аккаунта Рин (+код страны): ",
            lambda v: re.fullmatch(r"\+?\d{10,15}", v), "Номер в формате +XXXXXXXXXXX.")

lines = [l for l in (ENV.read_text().splitlines() if ENV.exists() else [])
         if not l.startswith(("TG_API_ID=", "TG_API_HASH="))]
lines += [f"TG_API_ID={api_id}", f"TG_API_HASH={api_hash}"]
ENV.write_text("\n".join(lines) + "\n")
os.chmod(ENV, 0o600)

from telethon import errors, functions

WHERE = {"SentCodeTypeApp": "в приложение Telegram (чат «Telegram» на этом аккаунте)",
         "SentCodeTypeSms": "по SMS", "SentCodeTypeCall": "звонком (код продиктуют)",
         "SentCodeTypeFlashCall": "сбросом звонка (код — последние цифры номера)",
         "SentCodeTypeMissedCall": "пропущенным звонком (код — последние цифры номера)",
         "SentCodeTypeFragmentSms": "через Fragment"}


def show(sent):
    t = type(sent.type).__name__
    print(f"\n📨 Код отправлен: {WHERE.get(t, t)}")
    nt = getattr(sent, "next_type", None)
    if nt:
        print(f"   Если не придёт — введи r, и пришлют {WHERE.get(type(nt).__name__, type(nt).__name__)}")


client = TelegramClient(str(BASE / "userbot"), int(api_id), api_hash)
client.connect()
try:
    if not client.is_user_authorized():
        sent = client.send_code_request(phone)
        show(sent)
        while True:
            code = input("Код (или r — отправить заново): ").strip()
            if code.lower() == "r":
                sent = client(functions.auth.ResendCodeRequest(phone, sent.phone_code_hash))
                show(sent)
                continue
            if not code.isdigit():
                print("  ❌ Код — это цифры.")
                continue
            try:
                client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
                break
            except errors.SessionPasswordNeededError:
                client.sign_in(password=getpass.getpass("Пароль 2FA (символы не видны): "))
                break
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
                print(f"  ❌ {type(e).__name__}: неверный или просроченный код. Введи ещё раз или r.")
    me = client.get_me()
    print(f"\n✅ Вошёл как {me.first_name} (@{me.username or '—'}), id {me.id}")
except errors.FloodWaitError as e:
    raise SystemExit(f"❌ Telegram просит подождать {e.seconds} с (~{e.seconds // 60} мин). Потом запусти вход снова.")
except (errors.PhoneNumberBannedError, errors.PhoneNumberInvalidError, errors.ApiIdInvalidError) as e:
    raise SystemExit(f"❌ {type(e).__name__}: {e}")
finally:
    client.disconnect()
for f in BASE.glob("userbot.session*"):
    os.chmod(f, 0o600)
