# -*- coding: utf-8 -*-
"""Обратная связь: приём коротких сообщений, лимит, приманка для ботов, чтение админом."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import core  # noqa: E402
from server.feedback_store import FeedbackStore  # noqa: E402


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "feedback.jsonl"
        self.store = FeedbackStore(self.path, per_ip_per_day=3)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_message_is_stored_without_ip(self) -> None:
        self.store.add(kind="result_ok", message="Всё отлично, 330i на 98-м", contact="@user",
                       context={"platform": "MS43", "version": "430069", "sha12": "abc123", "x": "y"},
                       ip_hash="hash1")
        record = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("result_ok", record["kind"])
        self.assertEqual({"platform": "MS43", "version": "430069", "sha12": "abc123"}, record["context"])
        self.assertNotIn("1.2.3.4", self.path.read_text(encoding="utf-8"))

    def test_validation_and_limit(self) -> None:
        with self.assertRaises(Exception):
            self.store.add(kind="spam", message="достаточно длинное сообщение")
        with self.assertRaises(Exception):
            self.store.add(kind="bug", message="ок")
        for _ in range(3):
            self.store.add(kind="bug", message="ошибка на странице", ip_hash="same")
        with self.assertRaises(Exception) as caught:
            self.store.add(kind="bug", message="ошибка на странице", ip_hash="same")
        self.assertEqual("FEEDBACK_RATE_LIMIT", caught.exception.code)
        self.store.add(kind="bug", message="другой пользователь", ip_hash="other")

    def test_honeypot_writes_nothing(self) -> None:
        answer = self.store.add(kind="bug", message="купите дёшево", honeypot="http://x")
        self.assertEqual("RECEIVED", answer["status"])
        self.assertFalse(self.path.exists())

    def test_long_message_is_cut(self) -> None:
        self.store.add(kind="idea", message="я" * 5000, contact="к" * 500)
        record = self.store.recent()[0]
        self.assertEqual(2000, len(record["message"]))
        self.assertEqual(120, len(record["contact"]))


class FeedbackServiceTests(unittest.TestCase):
    def test_unconfigured_service_refuses(self) -> None:
        old = os.environ.pop("TORQUEX_FEEDBACK_PATH", None)
        try:
            service = core.TorquexService(donate_url="x", journal_path=None)
            with self.assertRaises(core.ServiceError) as caught:
                service.submit_feedback(kind="bug", message="проверка работы приёма")
            self.assertEqual("FEEDBACK_UNAVAILABLE", caught.exception.code)
        finally:
            if old is not None:
                os.environ["TORQUEX_FEEDBACK_PATH"] = old

    def test_service_stores_next_to_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = core.TorquexService(donate_url="x", journal_path=Path(directory) / "journal.jsonl")
            service.submit_feedback(kind="idea", message="добавьте больше блоков", ip="203.0.113.5")
            self.assertTrue((Path(directory) / "feedback.jsonl").exists())
            self.assertEqual(1, len(service.recent_feedback()))


if __name__ == "__main__":
    unittest.main()
