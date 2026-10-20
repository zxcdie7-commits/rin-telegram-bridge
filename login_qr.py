"""Вход в аккаунт Telegram для моста Hermes по QR-коду (без SMS)."""
import asyncio
import getpass
import os
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
cfg = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l)


def show_qr(url):
    os.system("clear")
    print("Вход в аккаунт Telegram для Hermes по QR-коду\n")
    print("На телефоне, где открыт НОВЫЙ аккаунт:")
    print("  Telegram → Настройки → Устройства → Подключить устройство → наведи камеру на код\n")
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print("\nКод обновляется каждые ~30 секунд. Если не сканируется — увеличь окно (Cmd + минус уменьшает шрифт).")


async def main():
    client = TelegramClient(str(BASE / "userbot"), int(cfg["TG_API_ID"]), cfg["TG_API_HASH"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            while True:
                qr = await client.qr_login()
                show_qr(qr.url)
                try:
                    await qr.wait(timeout=30)
                    break
                except asyncio.TimeoutError:
                    continue
                except errors.SessionPasswordNeededError:
                    await client.sign_in(password=getpass.getpass("\nПароль 2FA (символы не видны): "))
                    break
        me = await client.get_me()
        print(f"\n✅ Вошёл как {me.first_name} (@{me.username or '—'}), id {me.id}")
    except errors.FloodWaitError as e:
        raise SystemExit(f"❌ Telegram просит подождать {e.seconds} с (~{e.seconds // 60} мин).")
    finally:
        await client.disconnect()
    for f in BASE.glob("userbot.session*"):
        os.chmod(f, 0o600)


asyncio.run(main())
