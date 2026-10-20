"""Вызовы моделей: полная Рин через Hermes и упрощённая через OpenRouter. Все функции блокирующие (в потоке)."""
import json
import urllib.error
import urllib.request

from .config import (HERMES_CHANNEL_NOTE, HERMES_URL, MODELS, PUBLIC_MODEL, hermes_secret, log)
from .storage import chat_state


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ask_hermes(chat_id, text):
    """Полная Рин (память, инструменты). Один чат Telegram = один разговор Hermes."""
    st = chat_state(chat_id)
    m = MODELS[st["model"]]
    body = {"model": m["model"], "provider": m["provider"], "input": text,
            "conversation": f"tg-{chat_id}-{st['gen']}", "instructions": HERMES_CHANNEL_NOTE, "store": True}
    try:
        data = _post(f"{HERMES_URL}/v1/responses", body,
                     {"Authorization": f"Bearer {hermes_secret('API_SERVER_KEY')}"}, timeout=900)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log.warning("hermes http %s: %s", e.code, detail)
        return f"⚠️ Hermes вернул ошибку {e.code}: {detail}"
    except Exception as e:  # noqa: BLE001
        log.warning("hermes unreachable: %s", e)
        return "⚠️ Hermes недоступен. Проверь, что он запущен на Маке."
    parts = [c.get("text", "") for item in data.get("output", [])
             if item.get("type") == "message" and item.get("phase") != "commentary"
             for c in item.get("content", []) if c.get("type") == "output_text"]
    return "\n".join(p for p in parts if p).strip() or "(пустой ответ)"


def _hermes_auth():
    return {"Authorization": f"Bearer {hermes_secret('API_SERVER_KEY')}"}


def run_create(session_id, chat_id, text):
    """Новый ход полной Рин через /v1/runs. -> run_id"""
    m = MODELS[chat_state(chat_id)["model"]]
    body = {"input": text, "session_id": session_id, "instructions": HERMES_CHANNEL_NOTE,
            "model": m["model"], "provider": m["provider"]}
    return _post(f"{HERMES_URL}/v1/runs", body, _hermes_auth(), timeout=60)["run_id"]


def run_status(run_id):
    req = urllib.request.Request(f"{HERMES_URL}/v1/runs/{run_id}", headers=_hermes_auth())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def run_approve(run_id, request_id, choice):
    """choice: "once" (разрешить один раз) или "deny"."""
    body = {"choice": choice}
    if request_id:
        body["request_id"] = request_id
    return _post(f"{HERMES_URL}/v1/runs/{run_id}/approval", body, _hermes_auth(), timeout=30)


def openrouter_chat(system, messages, max_tokens, model=None):
    """Упрощённая Рин: без инструментов и памяти. Бросает исключение при ошибке."""
    body = {"model": model or PUBLIC_MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + messages}
    data = _post("https://openrouter.ai/api/v1/chat/completions", body,
                 {"Authorization": f"Bearer {hermes_secret('OPENROUTER_API_KEY')}"}, timeout=120)
    return (data["choices"][0]["message"].get("content") or "").strip()


def openrouter_balance():
    req = urllib.request.Request("https://openrouter.ai/api/v1/credits",
                                 headers={"Authorization": f"Bearer {hermes_secret('OPENROUTER_API_KEY')}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())["data"]
    total, used = float(d["total_credits"]), float(d["total_usage"])
    return total - used, total, used
