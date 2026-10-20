"""Путь владельца через Hermes /v1/runs: ход полной Рин + подтверждение рискованных шагов прямо в userbot.

Hermes сам пропускает безопасные команды («умные подтверждения»); человеку уходит только то, что судья-модель
запретила или в чём сомневается. Такой вопрос мост пересылает владельцу, ждёт «да/нет» (тайм-аут — отказ).
"""
import asyncio
import re
import time

from .config import APPROVAL_TIMEOUT_SEC, RUN_MAX_SEC, RUN_POLL_SEC, log
from .llm import run_approve, run_create, run_status
from .storage import add_pending_approval, clear_pending_approval, owner_session_id, pending_approvals

YES = re.compile(r"^\s*(да|ага|угу|разреш\w*|давай|можно|ок|окей|ok|yes|y|\+)\s*[.!)]*\s*$", re.I)
NO = re.compile(r"^\s*(нет|не|запре[щт]\w*|стоп|отмена|не\s*надо|no|n|-)\s*[.!)]*\s*$", re.I)

_waiting = {}   # chat_id -> {"future": Future, "reminded": bool}


def try_answer(chat_id, text):
    """Если в этом чате ждём «да/нет» — принять ответ. -> None (не ждём) | "yes" | "no" | "other"."""
    w = _waiting.get(chat_id)
    if not w or w["future"].done():
        return None
    if YES.match(text):
        w["future"].set_result("once")
        return "yes"
    if NO.match(text):
        w["future"].set_result("deny")
        return "no"
    if w["reminded"]:
        return "other_again"
    w["reminded"] = True
    return "other"


def _describe(approval):
    desc = (approval.get("description") or "рискованное действие").strip()
    cmd = (approval.get("command") or "").strip()
    if cmd:
        cmd = cmd if len(cmd) <= 160 else cmd[:157] + "…"
        return f"{desc} — `{cmd}`"
    return desc


async def _ask_owner(event, chat_id, run_id, approval):
    request_id = approval.get("request_id")
    loop = asyncio.get_running_loop()
    _waiting[chat_id] = {"future": loop.create_future(), "reminded": False}
    add_pending_approval(run_id, request_id)
    await event.reply(f"...хочу сделать: {_describe(approval)}.\nРазрешить? да / нет")
    log.info("approval asked run=%s", run_id)
    try:
        choice = await asyncio.wait_for(_waiting[chat_id]["future"], timeout=APPROVAL_TIMEOUT_SEC)
        reason = "owner"
    except asyncio.TimeoutError:
        choice, reason = "deny", "timeout"
    finally:
        _waiting.pop(chat_id, None)
    try:
        await asyncio.to_thread(run_approve, run_id, request_id, choice)
    except Exception as e:  # noqa: BLE001
        log.warning("approval post failed run=%s: %s", run_id, type(e).__name__)
    clear_pending_approval(run_id)
    log.info("approval decision run=%s choice=%s by=%s", run_id, choice, reason)
    if reason == "timeout":
        wait = f"{APPROVAL_TIMEOUT_SEC // 60} мин" if APPROVAL_TIMEOUT_SEC >= 60 else f"{APPROVAL_TIMEOUT_SEC} с"
        await event.reply(f"...не дождалась ответа за {wait} — не стала делать.")


async def ask(client, event, chat_id, text):
    """Полная Рин через runs. Возвращает текст ответа (как старый ask_hermes)."""
    try:
        run_id = await asyncio.to_thread(run_create, owner_session_id(chat_id), chat_id, text)
    except Exception as e:  # noqa: BLE001
        log.warning("run create failed: %s", type(e).__name__)
        return "⚠️ Hermes недоступен. Проверь, что он запущен на Маке."
    handled = set()
    started = time.time()
    while time.time() - started < RUN_MAX_SEC:
        try:
            st = await asyncio.to_thread(run_status, run_id)
        except Exception as e:  # noqa: BLE001
            log.warning("run status failed run=%s: %s", run_id, type(e).__name__)
            await asyncio.sleep(RUN_POLL_SEC * 2)
            continue
        status = st.get("status")
        approval = st.get("approval") or {}
        if status == "waiting_for_approval" and approval.get("request_id") not in handled:
            handled.add(approval.get("request_id"))
            await _ask_owner(event, chat_id, run_id, approval)
            continue
        if status not in ("running", "waiting_for_approval", "stopping", "started", "queued"):
            out = st.get("output")
            if status == "completed" and out:
                return str(out).strip()
            log.warning("run ended run=%s status=%s", run_id, status)
            err = st.get("error") or status
            return f"⚠️ Не получилось: {err}" if not out else str(out).strip()
        if status != "waiting_for_approval":
            try:
                async with client.action(chat_id, "typing"):
                    await asyncio.sleep(RUN_POLL_SEC)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(RUN_POLL_SEC)
        else:
            await asyncio.sleep(RUN_POLL_SEC)
    log.warning("run too long run=%s", run_id)
    return "...это заняло слишком долго. Я ещё могу доделывать в фоне — спроси меня позже."


def deny_leftovers():
    """При старте моста: всё, что ждало подтверждения до перезапуска, — отказ."""
    for p in pending_approvals():
        try:
            run_approve(p["run_id"], p.get("request_id"), "deny")
            log.info("approval decision run=%s choice=deny by=bridge-restart", p["run_id"])
        except Exception as e:  # noqa: BLE001
            log.info("leftover approval not active run=%s: %s", p["run_id"], type(e).__name__)
        clear_pending_approval(p["run_id"])
