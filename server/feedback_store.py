# -*- coding: utf-8 -*-
"""Обратная связь с сайта: короткие сообщения владельцу проекта.

Хранится одна строка JSON на сообщение (``feedback.jsonl`` рядом с журналом):
время, вид, текст, контакт (необязательный), контекст сборки (блок, версия,
первые 12 символов хеша результата) и хеш IP-адреса. Сам IP не пишется.

Защита от мусора: длина полей, один скрытый «медовый» ключ формы, лимит
сообщений с одного IP в сутки. Читает только администратор (``GET /feedback``).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

KINDS = ("result_ok", "result_problem", "bug", "idea")
MESSAGE_MIN = 5
MESSAGE_MAX = 2000
CONTACT_MAX = 120
PER_IP_PER_DAY = 5
CONTEXT_KEYS = ("platform", "version", "sha12")


class FeedbackError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _line(value: Any, limit: int) -> str:
    """Одна строка без управляющих символов."""
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", " ").strip()[:limit]


class FeedbackStore:
    def __init__(self, path: str | Path, per_ip_per_day: int = PER_IP_PER_DAY,
                 on_saved: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.on_saved = on_saved
        self.path = Path(path)
        self.per_ip_per_day = int(per_ip_per_day)
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], int] = {}

    def add(self, *, kind: str, message: str, contact: str = "",
            context: dict[str, Any] | None = None, ip_hash: str = "",
            lang: str = "ru", honeypot: str = "") -> dict[str, Any]:
        if honeypot:
            # бот: делаем вид, что приняли, ничего не пишем
            return {"status": "RECEIVED"}
        if kind not in KINDS:
            raise FeedbackError("FEEDBACK_INVALID", "неизвестный вид сообщения")
        text = _text(message, MESSAGE_MAX)
        if len(text) < MESSAGE_MIN:
            raise FeedbackError("FEEDBACK_INVALID", "сообщение слишком короткое")
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (ip_hash, day)
        with self._lock:
            if ip_hash and self._seen.get(key, 0) >= self.per_ip_per_day:
                raise FeedbackError("FEEDBACK_RATE_LIMIT", "слишком много сообщений за сутки", status=429)
            self._seen[key] = self._seen.get(key, 0) + 1
            if len(self._seen) > 5000:  # не растём бесконечно
                self._seen = {k: v for k, v in self._seen.items() if k[1] == day}
            source = context or {}
            record = {
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": kind,
                "message": text,
                "contact": _line(contact, CONTACT_MAX),
                "context": {name: _line(source.get(name), 60)
                            for name in CONTEXT_KEYS if source.get(name)},
                "ip_hash": ip_hash,
                "lang": "en" if lang == "en" else "ru",
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.on_saved is not None:
            try:
                self.on_saved(record)
            except Exception:  # noqa: BLE001 - сообщение уже сохранено
                pass
        return {"status": "RECEIVED"}

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-max(1, min(int(limit), 1000)):]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
