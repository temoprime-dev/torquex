# -*- coding: utf-8 -*-
"""Уведомление в Telegram: текст без IP, выключено без токена, ошибки не ломают приём."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import notify  # noqa: E402
from server.feedback_store import FeedbackStore  # noqa: E402

RECORD = {
    "kind": "result_problem", "message": "После прошивки рывки на холодную",
    "contact": "@driver", "context": {"platform": "MS43", "version": "430069", "sha12": "abc"},
    "ip_hash": "SECRETHASH", "lang": "ru",
}


class NotifyTests(unittest.TestCase):
    def test_message_has_context_contact_and_no_ip(self) -> None:
        text = notify.format_message(RECORD)
        self.assertIn("Отчёт: есть проблема", text)
        self.assertIn("MS43 430069 abc", text)
        self.assertIn("https://t.me/driver", text)
        self.assertNotIn("SECRETHASH", text)

    def test_disabled_without_token(self) -> None:
        sent = []
        notifier = notify.TelegramNotifier(token="", chat_id="1", sender=lambda u, p: sent.append(p),
                                           background=False)
        self.assertFalse(notifier.notify(RECORD))
        self.assertEqual([], sent)

    def test_enabled_sends_once_and_hides_token_in_payload(self) -> None:
        sent = []
        notifier = notify.TelegramNotifier(token="123:TOKEN", chat_id="42",
                                           sender=lambda u, p: sent.append((u, p)), background=False)
        self.assertTrue(notifier.notify(RECORD))
        url, payload = sent[0]
        self.assertIn("123:TOKEN", url)
        self.assertNotIn("TOKEN", payload["text"])
        self.assertEqual("42", payload["chat_id"])

    def test_sender_failure_does_not_break_feedback(self) -> None:
        def boom(url, payload):
            raise OSError("нет сети")

        notifier = notify.TelegramNotifier(token="t", chat_id="1", sender=boom, background=False)
        with tempfile.TemporaryDirectory() as directory:
            store = FeedbackStore(Path(directory) / "f.jsonl", on_saved=notifier.notify)
            answer = store.add(kind="idea", message="проверка уведомления", ip_hash="h")
            self.assertEqual("RECEIVED", answer["status"])
            self.assertEqual(1, len(store.recent()))

    def test_honeypot_does_not_notify(self) -> None:
        sent = []
        notifier = notify.TelegramNotifier(token="t", chat_id="1", sender=lambda u, p: sent.append(p),
                                           background=False)
        with tempfile.TemporaryDirectory() as directory:
            store = FeedbackStore(Path(directory) / "f.jsonl", on_saved=notifier.notify)
            store.add(kind="bug", message="спам спам", honeypot="x")
        self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
