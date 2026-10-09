# -*- coding: utf-8 -*-
"""Добровольная передача лога проекту (`server/log_store.py`, `server/core.py`).

Что обязано выполняться (задание «передать лог проекту»):

* без ``share_consent=true`` на диск не пишется ничего — даже при включённой
  передаче на сервере;
* очистка по allowlist: в сохранённой копии только ось времени и числовые
  каналы из ``docs/test0_channels.json``/анализатора; VIN, FGNR, ISN, e-mail,
  IP, пути, имена файлов, строки результатов job-ов — отброшены;
* после очистки — самопроверка; если она не прошла, сохранение отменяется
  кодом ``LOG_SCRUB_FAILED``, а анализ остаётся у пользователя;
* метаданные без IP, VIN, имени файла и без самого токена удаления;
* удаление по токену: неверный токен и «нет такого» дают одинаковый 404;
* лимиты (размер, частота на хеш IP, потолок хранилища), автоудаление по сроку.

Все данные синтетические; VIN выдуман (WBAXX11000KL12345).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import core  # noqa: E402
from server import log_store  # noqa: E402
from tools import pack_log_analyzer as analyzer  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402
from tools import refusals  # noqa: E402

#: Выдуманный VIN (формат BMW, буквы без I/O/Q) и его 7-символьный хвост.
FAKE_VIN = "WBAXX11000KL12345"
FAKE_TAIL = FAKE_VIN[-7:]
FAKE_EMAIL = "driver@example.invalid"
FAKE_IP = "203.0.113.9"
VIN_RUN = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.I)

CLEAN_CSV = ROOT / "MS43" / "examples" / "logs" / "wot_wideband.csv"


def dirty_csv_bytes() -> bytes:
    """Настоящий (синтетический) лог топлива + «грязные» колонки идентификации.

    Анализатор такие колонки игнорирует, поэтому лог проходит ворота; задача
    очистки — не пропустить их в сохраняемую копию.
    """
    lines = CLEAN_CSV.read_text(encoding="utf-8").splitlines()
    extra_header = ["VIN", "FGNR", "Fahrgestellnummer", "ISN", "Comment", "IP",
                    "Source file", FAKE_VIN]
    extra_cells = [FAKE_VIN, FAKE_VIN, FAKE_TAIL, FAKE_TAIL, FAKE_EMAIL, FAKE_IP,
                   "C:/Users/driver/Documents/" + FAKE_VIN + "_log.csv", "1"]
    out = [lines[0] + "," + ",".join(extra_header)]
    for line in lines[1:]:
        out.append(line + "," + ",".join(extra_cells))
    return ("\n".join(out) + "\n").encode("utf-8")


def dirty_test0_db(path: Path) -> Path:
    """Синтетическая база Test0 + job идентификации с VIN/FGNR/ISN в значениях."""
    fx.write_test0_db(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
            (99, "MS430DS0", "IDENT", "IDENT " + FAKE_VIN, "", "", 0, FAKE_EMAIL))
        for idx, name in enumerate(["_TEL_AUFTRAG", "_TEL_ANTWORT", "FGNR",
                                    "STAT_FAHRGESTELL_NR", "ISN_WERT", "STAT_MOTORDREHZAHL_WERT"]):
            connection.execute(
                "INSERT INTO result_definitions(jobId, idx, name, unit) VALUES (?,?,?,?)",
                (99, idx, name, ""))
        connection.execute(
            "INSERT INTO job_executions(jobId, jobNum, ts, vals) VALUES (?,?,?,?)",
            (99, 9999, 150, json.dumps(
                ["83" + FAKE_VIN, FAKE_IP, FAKE_VIN, FAKE_VIN, FAKE_TAIL, FAKE_VIN])))
        # Посторонняя таблица с персональными данными: её читать нельзя вовсе.
        connection.execute("CREATE TABLE owner (vin TEXT, email TEXT)")
        connection.execute("INSERT INTO owner VALUES (?,?)", (FAKE_VIN, FAKE_EMAIL))
        connection.commit()
    return path


def assert_clean(test: unittest.TestCase, data: bytes) -> None:
    text = data.decode("utf-8")
    test.assertNotIn(FAKE_VIN, text)
    test.assertNotIn(FAKE_TAIL, text)
    test.assertNotIn("@", text)
    test.assertNotIn(FAKE_IP, text)
    test.assertNotIn("Users", text)
    test.assertNotIn("\r", text)
    test.assertIsNone(VIN_RUN.search(text), "подстрока, похожая на VIN")
    test.assertEqual([], log_store.verify_scrubbed(data))
    allowed = log_store.allowed_channels()
    header = text.split("\n", 1)[0].split(",")
    test.assertEqual("time", header[0])
    for name in header:
        test.assertIn(name, allowed)


class ScrubTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_csv_identity_columns_are_dropped(self) -> None:
        path = self.dir / "dirty.csv"
        path.write_bytes(dirty_csv_bytes())
        result = log_store.scrub_log_file(path)
        assert_clean(self, result.data)
        header = result.data.decode("utf-8").split("\n", 1)[0].split(",")
        for channel in ("rpm", "load", "lambda_wideband", "coolant_temp"):
            self.assertIn(channel, header)
        self.assertGreater(result.rows, 100)

    def test_time_axis_is_relative(self) -> None:
        """Абсолютное время записи — тоже след человека: храним от нуля."""
        path = dirty_test0_db(self.dir / "2026_10_07.db")
        result = log_store.scrub_log_file(path)
        first_row = result.data.decode("utf-8").split("\n")[1]
        self.assertEqual("0", first_row.split(",")[0])

    def test_db_is_rebuilt_as_clean_csv(self) -> None:
        path = dirty_test0_db(self.dir / "2026_10_07.db")
        result = log_store.scrub_log_file(path)
        self.assertFalse(result.data.startswith(b"SQLite"))
        assert_clean(self, result.data)
        self.assertEqual("MS43", result.platform)
        text = result.data.decode("utf-8")
        self.assertNotIn("IDENT", text)
        self.assertNotIn("MS430DS0", text)

    def test_vin_in_a_known_channel_becomes_empty(self) -> None:
        table = {
            "channels": {
                "time": [0.0, 0.1, 0.2],
                "rpm": [900.0, None, 12345678901234567.0],
                "coolant_temp": [float("nan"), 90.0, float("inf")],
                "unknown_channel": [1.0, 2.0, 3.0],
            },
        }
        data, rows, channels = log_store.scrub_table(table)
        assert_clean(self, data)
        self.assertNotIn("unknown_channel", channels)
        self.assertEqual(3, rows)

    def test_verify_rejects_identifiers_text_and_unknown_headers(self) -> None:
        bad = [
            b"time,rpm\n0,900\n0.1," + FAKE_VIN.encode() + b"\n",
            b"time,rpm\n0,900\n0.1,abc\n",
            b"time,VIN\n0,1\n0.1,2\n",
            b"time,rpm\r\n0,900\r\n0.1,901\r\n",
            b"time,rpm\n0,900\n0.1,1e5\n",
            b"time,rpm\n0,900\n0.1,901,5\n",
            b"time,rpm\n0," + b"1" * 17 + b"\n0.1,901\n",
            b"time,rpm\n0,900\n0.1,901",          # нет LF в конце
            b"rpm,time\n0,900\n0.1,901\n",        # первая колонка не время
            "time,rpm\n0,900\n0.1,90\u0661\n".encode("utf-8"),  # не-ASCII цифра
        ]
        for data in bad:
            with self.subTest(data=data[:40]):
                self.assertTrue(log_store.verify_scrubbed(data), data)
        self.assertEqual([], log_store.verify_scrubbed(b"time,rpm\n0,900\n0.1,901.5\n"))

    def test_oversized_scrub_result_is_refused(self) -> None:
        path = self.dir / "dirty.csv"
        path.write_bytes(dirty_csv_bytes())
        with self.assertRaises(log_store.ScrubError) as caught:
            log_store.scrub_log_file(path, max_bytes=512)
        self.assertEqual("LOG_SHARE_TOO_LARGE", caught.exception.code)

    def test_stored_numbers_match_what_the_analyzer_read(self) -> None:
        """Очистка не искажает данные: числа те же, что видел анализатор (до 4 знаков)."""
        path = self.dir / "dirty.csv"
        path.write_bytes(dirty_csv_bytes())
        result = log_store.scrub_log_file(path)
        table = analyzer.read_log_table(path)
        lines = result.data.decode("ascii").splitlines()
        header = lines[0].split(",")
        for channel in ("rpm", "lambda_wideband", "coolant_temp"):
            column = header.index(channel)
            stored = [float(line.split(",")[column]) if line.split(",")[column] else None
                      for line in lines[1:]]
            expected = [None if value is None else round(value, 4)
                        for value in table["channels"][channel]]
            self.assertEqual(expected, stored, channel)


class ServiceShareTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store_dir = Path(directory.name) / "store"
        self.store = log_store.LogStore(self.store_dir)
        self.service = core.TorquexService(log_store=self.store, log_donation=True)

    def files(self) -> list[Path]:
        if not self.store_dir.exists():
            return []
        return sorted(p for p in self.store_dir.rglob("*") if p.is_file())

    def analyze(self, service=None, share=True, ip=FAKE_IP, filename=FAKE_VIN + ".csv"):
        return (service or self.service).analyze_log(
            dirty_csv_bytes(), filename=filename, package="fuel", consent=True,
            share_consent=share, ip=ip)

    def test_without_share_consent_nothing_is_written(self) -> None:
        result = self.analyze(share=False)
        self.assertEqual("ANALYZED", result["status"])
        self.assertNotIn("share", result)
        self.assertEqual([], self.files())

    def test_share_consent_defaults_to_false(self) -> None:
        result = self.service.analyze_log(
            dirty_csv_bytes(), filename="log.csv", package="fuel", consent=True)
        self.assertNotIn("share", result)
        self.assertEqual([], self.files())

    def test_share_stores_only_scrubbed_csv_and_clean_meta(self) -> None:
        result = self.analyze()
        share = result["share"]
        self.assertEqual("STORED", share["status"], share)
        self.assertTrue(share["delete_token"])
        self.assertRegex(share["log_id"], r"^[A-Za-z0-9_-]{16,64}$")
        files = self.files()
        self.assertEqual(2, len(files), files)
        csv_file = next(p for p in files if p.suffix == ".csv")
        meta_file = next(p for p in files if p.name.endswith(".meta.json"))
        self.assertEqual(share["log_id"] + ".csv", csv_file.name)
        assert_clean(self, csv_file.read_bytes())

        meta_text = meta_file.read_text(encoding="utf-8")
        meta = json.loads(meta_text)
        for needle in (FAKE_VIN, FAKE_TAIL, FAKE_IP, FAKE_EMAIL, share["delete_token"],
                       ".csv\"", "driver"):
            self.assertNotIn(needle, meta_text)
        for key in ("id", "created", "expires", "platform", "version", "package",
                    "size_bytes", "rows", "sha256", "consent_version",
                    "delete_token_sha256"):
            self.assertIn(key, meta)
        self.assertEqual(log_store.CONSENT_VERSION, meta["consent_version"])
        self.assertEqual("fuel", meta["package"])
        self.assertIsNone(meta["version"])
        self.assertEqual(csv_file.stat().st_size, meta["size_bytes"])
        self.assertNotIn("ip", {k.lower() for k in meta})
        self.assertNotIn("ip_hash", meta)
        expires = date.fromisoformat(meta["expires"])
        self.assertEqual(expires - date.fromisoformat(meta["created"]), timedelta(days=365))
        # Ответ с анализом не меняется: evidence_id, как раньше.
        self.assertTrue(result["evidence_id"])

    def test_delete_requires_the_right_token_and_404_is_uniform(self) -> None:
        share = self.analyze()["share"]
        with self.assertRaises(core.ServiceError) as wrong:
            self.service.delete_shared_log(share["log_id"], "not-the-token")
        with self.assertRaises(core.ServiceError) as missing:
            self.service.delete_shared_log("A" * 24, share["delete_token"])
        for caught in (wrong, missing):
            self.assertEqual("LOG_DELETE_NOT_FOUND", caught.exception.code)
            self.assertEqual(404, caught.exception.status)
        self.assertEqual(str(wrong.exception), str(missing.exception))
        self.assertEqual(2, len(self.files()))

        self.service.delete_shared_log(share["log_id"], share["delete_token"])
        self.assertEqual([], self.files())
        with self.assertRaises(core.ServiceError) as again:
            self.service.delete_shared_log(share["log_id"], share["delete_token"])
        self.assertEqual("LOG_DELETE_NOT_FOUND", again.exception.code)

    def test_path_traversal_ids_never_touch_the_disk(self) -> None:
        outside = self.store_dir.parent / "victim.csv"
        outside.write_text("keep", encoding="utf-8")
        (self.store_dir.parent / "victim.meta.json").write_text("{}", encoding="utf-8")
        for log_id in ("../victim", "..\\victim", "/etc/passwd", "victim/../../x",
                       "", ".", "..", "a" * 200, "C:\\victim", "%2e%2e%2fvictim"):
            with self.subTest(log_id=log_id):
                with self.assertRaises(core.ServiceError) as caught:
                    self.service.delete_shared_log(log_id, "token")
                self.assertEqual("LOG_DELETE_NOT_FOUND", caught.exception.code)
        self.assertTrue(outside.exists())

    def test_donation_disabled_keeps_analysis_and_stores_nothing(self) -> None:
        service = core.TorquexService(log_store=self.store, log_donation=False)
        result = self.analyze(service=service)
        self.assertEqual("ANALYZED", result["status"])
        self.assertEqual("NOT_STORED", result["share"]["status"])
        self.assertEqual("LOG_SHARE_DISABLED", result["share"]["code"])
        self.assertNotIn("delete_token", result["share"])
        self.assertEqual([], self.files())

    def test_scrub_failure_cancels_saving_but_not_analysis(self) -> None:
        def leaky(path, max_bytes=log_store.MAX_STORED_LOG_BYTES):
            raise log_store.ScrubError("LOG_SCRUB_FAILED", "найдена подстрока, похожая на VIN")

        with mock.patch.object(log_store, "scrub_log_file", side_effect=leaky):
            result = self.analyze()
        self.assertEqual("ANALYZED", result["status"])
        self.assertTrue(result["evidence_id"])
        self.assertEqual("LOG_SCRUB_FAILED", result["share"]["code"])
        self.assertIn("title_ru", result["share"])
        self.assertEqual([], self.files())

    def test_self_check_catches_a_broken_scrubber(self) -> None:
        """Если очистка «пропустила» VIN, самопроверка обязана отменить запись."""
        def broken(table, max_bytes=log_store.MAX_STORED_LOG_BYTES):
            return (b"time,rpm\n0,900\n0.1," + FAKE_VIN.encode() + b"\n", 2, ["time", "rpm"])

        with mock.patch.object(log_store, "scrub_table", side_effect=broken):
            result = self.analyze()
        self.assertEqual("LOG_SCRUB_FAILED", result["share"]["code"])
        self.assertEqual([], self.files())

    def test_rejected_analysis_stores_nothing(self) -> None:
        with self.assertRaises(core.ServiceError):
            self.service.analyze_log(
                b"time,rpm\n0,900\n0.1,901\n", filename="log.csv", package="fuel",
                consent=True, share_consent=True, ip=FAKE_IP)
        self.assertEqual([], self.files())

    def test_rate_limit_per_ip_hash(self) -> None:
        service = core.TorquexService(log_store=self.store, log_donation=True,
                                      log_shares_per_ip_per_day=1)
        self.assertEqual("STORED", self.analyze(service=service)["share"]["status"])
        second = self.analyze(service=service)
        self.assertEqual("LOG_RATE_LIMITED", second["share"]["code"])
        other_ip = self.analyze(service=service, ip="198.51.100.4")
        self.assertEqual("STORED", other_ip["share"]["status"])
        self.assertEqual(4, len(self.files()))

    def test_store_ceiling(self) -> None:
        tiny = log_store.LogStore(self.store_dir, max_total_bytes=1024)
        service = core.TorquexService(log_store=tiny, log_donation=True)
        result = self.analyze(service=service)
        self.assertEqual("LOG_STORE_FULL", result["share"]["code"])
        self.assertEqual([], self.files())

    def test_stored_file_size_limit(self) -> None:
        small = log_store.LogStore(self.store_dir, max_file_bytes=1024)
        service = core.TorquexService(log_store=small, log_donation=True)
        result = self.analyze(service=service)
        self.assertEqual("LOG_SHARE_TOO_LARGE", result["share"]["code"])
        self.assertEqual([], self.files())

    def test_journal_has_no_plain_vin_or_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            service = core.TorquexService(log_store=self.store, log_donation=True,
                                          journal_path=journal)
            share = self.analyze(service=service)["share"]
            service.delete_shared_log(share["log_id"], share["delete_token"])
            text = journal.read_text(encoding="utf-8")
            for needle in (FAKE_VIN, FAKE_TAIL, FAKE_IP, share["delete_token"], share["log_id"]):
                self.assertNotIn(needle, text)
            statuses = [json.loads(line)["status"] for line in text.splitlines()]
            self.assertEqual(["LOG_SHARED", "LOG_DELETED"], statuses)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_store_inside_web_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            log_store.LogStore(ROOT / "web" / "logs")

    def test_default_store_is_outside_web_and_ignored_by_git(self) -> None:
        default = log_store.default_store_path()
        self.assertNotIn("web", default.relative_to(ROOT).parts)
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/", gitignore.splitlines())

    def test_from_env(self) -> None:
        env = {"TORQUEX_LOG_STORE": str(self.dir / "s"), "TORQUEX_LOG_RETENTION_DAYS": "30",
               "TORQUEX_LOG_STORE_MAX_BYTES": "4096"}
        with mock.patch.dict(os.environ, env):
            store = log_store.LogStore.from_env()
            self.assertFalse(core.TorquexService().log_donation)
        self.assertEqual(self.dir / "s", store.root)
        self.assertEqual(30, store.retention_days)
        self.assertEqual(4096, store.max_total_bytes)
        with mock.patch.dict(os.environ, {"TORQUEX_LOG_DONATION": "on",
                                          "TORQUEX_LOG_STORE": str(self.dir / "s")}):
            self.assertTrue(core.TorquexService().log_donation)

    def _store_one(self, store: log_store.LogStore) -> str:
        log_id, _token, _meta = store.save(
            b"time,rpm\n0,900\n0.1,901\n", package="fuel", platform=None, rows=2,
            channels=["time", "rpm"])
        return log_id

    def _age(self, store: log_store.LogStore, log_id: str, days: int) -> None:
        meta_path = store.root / f"{log_id}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        old = date.today() - timedelta(days=days)
        meta["created"] = old.isoformat()
        meta["expires"] = (old + timedelta(days=store.retention_days)).isoformat()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def test_purge_removes_only_expired(self) -> None:
        store = log_store.LogStore(self.dir / "s", retention_days=10)
        old_id = self._store_one(store)
        fresh_id = self._store_one(store)
        self._age(store, old_id, 11)
        foreign = store.root / "README.txt"
        foreign.write_text("not ours", encoding="utf-8")
        self.assertEqual(1, store.purge_expired())
        self.assertFalse((store.root / f"{old_id}.csv").exists())
        self.assertFalse((store.root / f"{old_id}.meta.json").exists())
        self.assertTrue((store.root / f"{fresh_id}.csv").exists())
        self.assertTrue(foreign.exists(), "чужие файлы purge не трогает")

    def test_lowered_retention_applies_to_old_entries(self) -> None:
        store = log_store.LogStore(self.dir / "s", retention_days=365)
        log_id = self._store_one(store)
        self._age(store, log_id, 40)
        self.assertEqual(0, store.purge_expired())
        self.assertEqual(1, log_store.LogStore(self.dir / "s", retention_days=30).purge_expired())

    def test_orphan_csv_without_meta_is_purged(self) -> None:
        store = log_store.LogStore(self.dir / "s")
        log_id = self._store_one(store)
        (store.root / f"{log_id}.meta.json").unlink()
        self.assertEqual(1, store.purge_expired())
        self.assertEqual([], list(store.root.iterdir()))

    def test_service_purges_on_start(self) -> None:
        store = log_store.LogStore(self.dir / "s", retention_days=10)
        log_id = self._store_one(store)
        self._age(store, log_id, 11)
        core.TorquexService(log_store=store, log_donation=False)
        self.assertFalse((store.root / f"{log_id}.csv").exists())

    def test_purge_cli(self) -> None:
        store = log_store.LogStore(self.dir / "s", retention_days=10)
        old_id = self._store_one(store)
        fresh_id = self._store_one(store)
        self._age(store, old_id, 11)
        env = dict(os.environ, TORQUEX_LOG_STORE=str(store.root),
                   TORQUEX_LOG_RETENTION_DAYS="10", PYTHONIOENCODING="utf-8")
        dry = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "purge_logs.py"), "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT))
        self.assertEqual(0, dry.returncode, dry.stderr)
        self.assertTrue((store.root / f"{old_id}.csv").exists())
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "purge_logs.py")],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("1", done.stdout)
        self.assertFalse((store.root / f"{old_id}.csv").exists())
        self.assertTrue((store.root / f"{fresh_id}.csv").exists())


class RefusalTextTests(unittest.TestCase):
    def test_new_codes_have_ru_and_en(self) -> None:
        for code in ("LOG_SHARE_DISABLED", "LOG_SCRUB_FAILED", "LOG_STORE_FULL",
                     "LOG_RATE_LIMITED", "LOG_DELETE_NOT_FOUND", "LOG_SHARE_TOO_LARGE"):
            for lang in ("ru", "en"):
                with self.subTest(code=code, lang=lang):
                    self.assertIsNotNone(refusals.explain(code, lang))


try:  # HTTP-слой: только если установлены fastapi и httpx
    from fastapi.testclient import TestClient
    from server.app import create_app
except ImportError:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx не установлены")
class HttpShareTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store_dir = Path(directory.name) / "store"
        self.service = core.TorquexService(
            log_store=log_store.LogStore(self.store_dir), log_donation=True)
        self.client = TestClient(create_app(self.service))

    def post(self, share: str | None):
        data = {"package": "fuel", "consent": "true"}
        if share is not None:
            data["share_consent"] = share
        return self.client.post(
            "/logs", files={"file": ("log.csv", dirty_csv_bytes())}, data=data,
            headers={"X-Forwarded-For": FAKE_IP})

    def test_health_reports_donation_flag(self) -> None:
        body = self.client.get("/health").json()
        self.assertIs(True, body["log_donation"])
        self.assertEqual(365, body["log_retention_days"])
        off = TestClient(create_app(core.TorquexService(
            log_store=log_store.LogStore(self.store_dir), log_donation=False)))
        self.assertIs(False, off.get("/health").json()["log_donation"])

    def test_upload_share_and_delete_over_http(self) -> None:
        plain = self.post(None)
        self.assertEqual(200, plain.status_code, plain.text)
        self.assertNotIn("share", plain.json())
        self.assertFalse(self.store_dir.exists() and any(self.store_dir.iterdir()))

        shared = self.post("true")
        self.assertEqual(200, shared.status_code, shared.text)
        share = shared.json()["share"]
        self.assertEqual("STORED", share["status"])

        wrong = self.client.delete(f"/logs/{share['log_id']}",
                                   headers={"X-Delete-Token": "nope"})
        absent = self.client.delete("/logs/" + "B" * 24,
                                    headers={"X-Delete-Token": share["delete_token"]})
        no_header = self.client.delete(f"/logs/{share['log_id']}")
        for response in (wrong, absent, no_header):
            self.assertEqual(404, response.status_code)
            self.assertEqual("LOG_DELETE_NOT_FOUND", response.json()["code"])
        self.assertEqual(wrong.json(), absent.json())

        ok = self.client.delete(f"/logs/{share['log_id']}",
                                headers={"X-Delete-Token": share["delete_token"]})
        self.assertEqual(200, ok.status_code, ok.text)
        self.assertEqual("DELETED", ok.json()["status"])
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_traversal_in_url_is_404(self) -> None:
        for path in ("/logs/..%2F..%2Fetc", "/logs/%2e%2e", "/logs/a%5Cb"):
            response = self.client.delete(path, headers={"X-Delete-Token": "x"})
            self.assertIn(response.status_code, (404, 405), path)


if __name__ == "__main__":
    unittest.main()
