# -*- coding: utf-8 -*-
"""Тесты ядра серверной обёртки (`server/core.py`, задание §5).

Проверяем то, что обещано в задании, без FastAPI и без сети:

* загрузка ≤ 1 МиБ; мусор, огромный и битый входы отклоняются кодом;
* учёт по VIN и учётной записи: лимиты считаются за период и дают понятный отказ;
* журнал: время, хеш учётной записи, хеш VIN + последние 7 символов, хеш IP,
  sha256 стока и результата, версия генератора — и **никакого VIN в открытом
  виде** (ни в памяти, ни в файле журнала);
* результаты живут коротко: после TTL `JOB_NOT_FOUND`;
* поддерживаемые версии отдаются списком (MS43 — версии, MS45 — семейства).
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import core  # noqa: E402

MS45_STOCK = sorted((ROOT / "MS45" / "firmware" / "MS45.0_456BO00S_E83").glob("*.bin"))[0]

VIN = "WBAEV11000KL12345"
REQUEST = {
    "variant": "v1",
    "customer": "тест",
    "intents": [{"id": "ign1", "kind": "ignition", "delta_deg": 1.0}],
}


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = core.TorquexService(donate_url="https://example.invalid/donate")

    def test_stock_is_detected(self) -> None:
        result = self.service.detect(MS45_STOCK.read_bytes(), filename="stock.bin")
        self.assertEqual(result["platform"], "MS45")
        self.assertEqual(result["version"], "4560BO00S")
        self.assertTrue(result["supported"])
        self.assertEqual(result["donate_url"], "https://example.invalid/donate")

    def test_junk_and_empty_upload_are_refused_with_codes(self) -> None:
        with self.assertRaises(core.ServiceError) as empty:
            self.service.detect(b"")
        self.assertEqual(empty.exception.code, "REQUEST_INVALID")

        with self.assertRaises(core.ServiceError) as junk:
            self.service.detect(b"\x00" * 4096)
        self.assertEqual(junk.exception.code, "UNSUPPORTED_IMAGE")

    def test_oversized_upload_is_refused(self) -> None:
        with self.assertRaises(core.ServiceError) as ctx:
            self.service.detect(b"\x00" * (core.MAX_UPLOAD_BYTES + 1))
        self.assertEqual(ctx.exception.code, "UPLOAD_TOO_LARGE")
        self.assertEqual(ctx.exception.status, 413)

    def test_versions_are_listed(self) -> None:
        versions = self.service.versions()
        self.assertIn("MS41", versions["ecus"])
        self.assertIn("430069", versions["ecus"]["MS43"]["versions"])
        self.assertIn("4560BO00S", versions["ecus"]["MS45"]["families"])
        self.assertEqual(versions["max_upload_bytes"], core.MAX_UPLOAD_BYTES)


class LogUploadTests(unittest.TestCase):
    """Server-side Stage 2 log gates: bytes in, opaque evidence out."""

    def setUp(self) -> None:
        self.service = core.TorquexService()
        self.csv = ROOT / "MS43" / "examples" / "logs" / "wot_wideband.csv"

    def test_valid_csv_is_analyzed_and_gets_opaque_evidence_id(self) -> None:
        result = self.service.analyze_log(
            self.csv.read_bytes(), filename=self.csv.name, package="fuel", consent=True,
        )
        self.assertEqual(result["status"], "ANALYZED")
        self.assertTrue(result["evidence_id"])
        self.assertTrue(result["analysis"]["valid"])
        self.assertEqual(result["analysis"]["source"], self.csv.name)
        self.assertNotIn("/", result["analysis"]["source"])

    def test_consent_path_junk_and_fake_db_are_rejected(self) -> None:
        cases = (
            (b"time,rpm\\n0,1000\\n", "log.csv", "fuel", False, "LOG_CONSENT_REQUIRED"),
            (b"not a logger", "log.csv", "fuel", True, "LOG_INVALID"),
            (b"not sqlite", "log.db", "fuel", True, "LOG_INVALID"),
            (b"not a logger", "../log.csv", "fuel", True, "LOG_PATH_FORBIDDEN"),
        )
        for data, filename, package, consent, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(core.ServiceError) as caught:
                    self.service.analyze_log(
                        data, filename=filename, package=package, consent=consent,
                    )
                self.assertEqual(caught.exception.code, code)

    def test_huge_log_is_rejected_before_analysis(self) -> None:
        with self.assertRaises(core.ServiceError) as caught:
            self.service.analyze_log(
                b"x" * (core.MAX_LOG_BYTES + 1), filename="log.csv",
                package="fuel", consent=True,
            )
        self.assertEqual(caught.exception.code, "UPLOAD_TOO_LARGE")
        self.assertEqual(caught.exception.status, 413)

    def test_server_evidence_can_be_consumed_by_a_real_ms43_build(self) -> None:
        evidence = self.service.analyze_log(
            self.csv.read_bytes(), filename=self.csv.name, package="fuel", consent=True,
        )
        stock = ROOT / "MS43" / "firmware" / (
            "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin")
        request = {
            "variant": "v1",
            "log_evidence_id": evidence["evidence_id"],
            "intents": [{"kind": "fuel", "tib_add_ms": 0.1, "tib_add_percent": 4.0}],
        }
        job = self.service.build(stock.read_bytes(), request, account="log-test", vin=VIN)
        self.assertEqual(job.status, "BUILT", job.log)
        self.assertNotIn(str(stock), json.dumps(job.manifest, ensure_ascii=False))


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = core.TorquexService(donate_url="https://example.invalid/donate")

    def test_build_returns_file_note_and_manifest(self) -> None:
        job = self.service.build(
            MS45_STOCK.read_bytes(), REQUEST, account="user-1", vin=VIN, ip="203.0.113.7",
        )
        self.assertEqual(job.status, "BUILT", job.log)
        self.assertIsNotNone(job.image)
        self.assertEqual(job.manifest.get("stage"), "stage1")
        self.assertEqual(job.manifest.get("source_sha256"),
                         hashlib.sha256(MS45_STOCK.read_bytes()).hexdigest())
        payload = job.as_dict(with_image=True)
        self.assertTrue(payload["image_base64"])
        self.assertEqual(payload["image_size"], len(job.image or b""))
        self.assertIn("manifest_version", payload["manifest"])
        self.assertIn("Откат", job.note)

        stored = self.service.job(job.job_id)
        self.assertEqual(stored.status, "BUILT")

    def test_tuned_file_is_refused_and_detect_reports_calibration(self) -> None:
        """Файл с уже поднятым зажиганием (наш же Stage 1) не принимается за сток."""
        stock = ROOT / "MS43/firmware/Siemens_MS43_MS430069_E46_M54B25_EU4_LHD.bin"
        request = {"stage": "stage1", "variant": "v1", "octane": "ron98",
                   "intents": [{"id": "i", "kind": "ignition", "delta_deg": 3.0}]}
        first = self.service.build(stock.read_bytes(), request, account="u-cal", vin=VIN)
        self.assertEqual(first.status, "BUILT", first.log)
        detected = self.service.detect(stock.read_bytes(), "stock.bin")
        self.assertEqual("stock", detected["calibration"]["state"])
        tuned_detect = self.service.detect(first.image, "tuned.bin")
        self.assertEqual("tuned", tuned_detect["calibration"]["state"])
        with self.assertRaises(core.ServiceError) as caught:
            self.service.build(first.image, request, account="u-cal2", vin=VIN)
        self.assertEqual("CAL_NOT_STOCK", caught.exception.code)

    def test_file_paths_in_web_request_are_refused(self) -> None:
        """Путь к файлу в заявке выбирает клиент: сервер не должен его открывать."""
        request = dict(REQUEST)
        request["intents"] = [
            {"id": "ign1", "kind": "ignition", "delta_deg": 1.0,
             "evidence": "/etc/passwd"},
        ]
        with self.assertRaises(core.ServiceError) as caught:
            self.service.build(MS45_STOCK.read_bytes(), request, account="user-2", vin=VIN)
        self.assertEqual(caught.exception.code, "REQUEST_INVALID")

    def test_rejected_request_is_journalled_with_code(self) -> None:
        request = {"intents": [{"kind": "pops", "delay_s": 0.5}]}  # без no_catalyst
        job = self.service.build(
            MS45_STOCK.read_bytes(), request, account="user-2", vin=VIN, ip="203.0.113.8",
        )
        self.assertEqual(job.status, "REJECTED")
        self.assertIsNone(job.image)
        entry = self.service.journal()[-1]
        self.assertIn(entry["code"], ("NO_CATALYST_REQUIRED", "ACKNOWLEDGE_REQUIRED"))
        self.assertEqual(entry["result_sha256"], "")

    def test_limits_are_counted_per_vin_and_account(self) -> None:
        service = core.TorquexService(limits=core.Limits(vin_per_day=1, account_per_day=10,
                                                         account_per_hour=10))
        service.build(MS45_STOCK.read_bytes(), REQUEST, account="u", vin=VIN, ip="1.1.1.1")
        with self.assertRaises(core.ServiceError) as ctx:
            service.build(MS45_STOCK.read_bytes(), REQUEST, account="u", vin=VIN, ip="1.1.1.1")
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")
        self.assertEqual(ctx.exception.status, 429)

        hourly = core.TorquexService(limits=core.Limits(vin_per_day=10, account_per_day=10,
                                                        account_per_hour=1))
        hourly.build(MS45_STOCK.read_bytes(), REQUEST, account="u2", vin="WBAEV11000KL54321", ip="1.1.1.2")
        with self.assertRaises(core.ServiceError) as ctx:
            hourly.build(MS45_STOCK.read_bytes(), REQUEST, account="u2", vin="WBAEV11000KL54321", ip="1.1.1.2")
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")

    def test_jobs_expire(self) -> None:
        service = core.TorquexService(ttl_seconds=0)
        job = service.build(MS45_STOCK.read_bytes(), REQUEST, account="u", vin=VIN, ip="")
        with self.assertRaises(core.ServiceError) as ctx:
            service.job(job.job_id)
        self.assertEqual(ctx.exception.code, "JOB_NOT_FOUND")
        self.assertEqual(ctx.exception.status, 404)


class JournalTests(unittest.TestCase):
    def test_journal_keeps_hashes_and_tail_but_not_the_vin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            service = core.TorquexService(journal_path=path)
            service.build(
                MS45_STOCK.read_bytes(), REQUEST, account="User-1", vin=VIN, ip="203.0.113.7",
            )
            entry = service.journal()[-1]
            self.assertEqual(entry["vin_tail"], VIN[-7:])
            self.assertNotIn(VIN, json.dumps(entry, ensure_ascii=False))
            self.assertTrue(entry["vin_hash"])
            self.assertTrue(entry["ip_hash"])
            self.assertNotEqual(entry["ip_hash"], "203.0.113.7")
            self.assertEqual(
                entry["stock_sha256"], hashlib.sha256(MS45_STOCK.read_bytes()).hexdigest()
            )
            self.assertEqual(entry["generator_version"], core.GENERATOR_VERSION)

            text = path.read_text(encoding="utf-8")
            self.assertNotIn(VIN, text)
            self.assertIn(entry["vin_hash"], text)

    def _with_vin_required(self, value: str | None):
        import os
        import contextlib

        @contextlib.contextmanager
        def ctx():
            old = os.environ.get("TORQUEX_VIN_REQUIRED")
            if value is None:
                os.environ.pop("TORQUEX_VIN_REQUIRED", None)
            else:
                os.environ["TORQUEX_VIN_REQUIRED"] = value
            try:
                yield
            finally:
                if old is None:
                    os.environ.pop("TORQUEX_VIN_REQUIRED", None)
                else:
                    os.environ["TORQUEX_VIN_REQUIRED"] = old
        return ctx()

    def test_vin_is_optional_by_default(self) -> None:
        # заглушка: привязка по VIN отложена, по умолчанию VIN не нужен
        with self._with_vin_required(None):
            self.assertFalse(core.vin_required())
            self.assertEqual(core.normalise_vin(""), ("", ""))
            service = core.TorquexService()
            job = service.build(MS45_STOCK.read_bytes(), REQUEST, account="opt", vin="")
            self.assertEqual(job.status, "BUILT", job.log)
            self.assertFalse(service.versions()["vin_required"])

    def test_vin_validation_when_required(self) -> None:
        with self._with_vin_required("on"):
            self.assertTrue(core.vin_required())
            for bad in ("X" * 40, "WBA EV11000KL1234", "WBAEV11000KL1234", "WBAEV11000KL123456",
                        "WBAEV11000KL1234I", "WBAEV11000KL1234O", "WBAEV11000KL1234Q",
                        "WBAEV11000KL-2345"):
                with self.assertRaises(core.ServiceError, msg=bad) as ctx:
                    core.normalise_vin(bad)
                self.assertEqual(ctx.exception.code, "VIN_INVALID", bad)
            with self.assertRaises(core.ServiceError) as ctx:
                core.normalise_vin("")
            self.assertEqual(ctx.exception.code, "VIN_REQUIRED")
            self.assertEqual(ctx.exception.status, 422)
            digest, tail = core.normalise_vin(" wbaev11000kl12345 ")
            self.assertEqual(tail, "KL12345")
            self.assertEqual(digest, core.normalise_vin("WBAEV11000KL12345")[0])
            service = core.TorquexService()
            with self.assertRaises(core.ServiceError) as ctx:
                service.build(MS45_STOCK.read_bytes(), REQUEST, account="nv", vin="", ip="")
            self.assertEqual(ctx.exception.code, "VIN_REQUIRED")
            self.assertEqual(service.journal(), [])
            self.assertTrue(service.versions()["vin_required"])

    def test_soft_mode_still_validates_nonempty_vin(self) -> None:
        with self._with_vin_required("off"):
            with self.assertRaises(core.ServiceError) as ctx:
                core.normalise_vin("X" * 40)
            self.assertEqual(ctx.exception.code, "VIN_INVALID")


if __name__ == "__main__":
    unittest.main()
