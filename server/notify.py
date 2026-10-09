# -*- coding: utf-8 -*-
"""Уведомление владельцу о новом сообщении обратной связи (Telegram-бот).

Включается переменными окружения ``TORQUEX_TG_TOKEN`` и ``TORQUEX_TG_CHAT_ID``;
без них модуль ничего не делает. Отправка идёт в фоновом потоке с коротким
таймаутом и никогда не бросает исключение наружу: сообщение уже сохранено в
``feedback.jsonl``, уведомление — только удобство. Токен не попадает ни в
журнал, ни в тексты ошибок. В уведомление не входят IP-адрес и его хеш.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from typing import Any, Callable

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 8
LIMIT = 3500

KIND_LABEL = {
    "result_ok": "Отчёт: всё хорошо",
    "result_problem": "Отчёт: есть проблема",
    "bug": "Ошибка на сайте",
    "idea": "Предложение",
}


def format_message(record: dict[str, Any]) -> str:
    """Текст уведомления: вид, файл, контакт, сообщение (без IP)."""
    context = record.get("context") or {}
    file_line = " ".join(str(context.get(k)) for k in ("platform", "version", "sha12") if context.get(k))
    contact = str(record.get("contact") or "").strip()
    parts = [f"TorqueX · {KIND_LABEL.get(str(record.get('kind')), record.get('kind'))}"]
    if file_line:
        parts.append(f"Файл: {file_line}")
    parts.append(f"Контакт: {contact or 'не указан'}")
    if contact.startswith("@") and len(contact) > 1:
        parts.append(f"Ответить: https://t.me/{contact[1:]}")
    parts.append("")
    parts.append(str(record.get("message") or ""))
    return "\n".join(parts)[:LIMIT]


def _post(url: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        response.read(1024)


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 sender: Callable[[str, dict[str, Any]], None] | None = None,
                 background: bool = True) -> None:
        self.token = token if token is not None else os.environ.get("TORQUEX_TG_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TORQUEX_TG_CHAT_ID", "")
        self._sender = sender or _post
        self._background = background

    @property
    def enabled(self) -> bool:
        return bool(self.token.strip() and str(self.chat_id).strip())

    def notify(self, record: dict[str, Any]) -> bool:
        """Поставить отправку в очередь; ``False``, если уведомления выключены."""
        if not self.enabled:
            return False
        payload = {
            "chat_id": str(self.chat_id).strip(),
            "text": format_message(record),
            "disable_web_page_preview": True,
        }
        url = API.format(token=self.token.strip())

        def run() -> None:
            try:
                self._sender(url, payload)
            except Exception:  # noqa: BLE001 - уведомление не должно ломать приём
                pass

        if self._background:
            threading.Thread(target=run, name="tg-notify", daemon=True).start()
        else:
            run()
        return True
