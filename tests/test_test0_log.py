# -*- coding: utf-8 -*-
"""Тесты формата Test0/TestO: каталог каналов, пресеты, чтение CSV и ``.db``.

Задание `docs/TASK_LOGS_RU.md`: реальных логов MS4x у владельца нет, поэтому
проверки идут на синтетических фикстурах (`tools/pack_log_fixtures.py`), а
имена job-ов и результатов сверяются с выгрузками PRG (`docs/sgbd/*_jobs.md`).
Ни одно имя в каталоге не должно быть выдумано; записи из обрезанных строк
выгрузки обязаны быть помечены ``truncated_export`` и перечислены здесь.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import pack_log_analyzer as analyzer  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402
from tools import test0_log  # noqa: E402

CATALOG_PATH = REPO_ROOT / "docs" / "test0_channels.json"
PRESETS_DIR = REPO_ROOT / "docs" / "test0_presets"
SGBD_DIR = REPO_ROOT / "docs" / "sgbd"

_JOB_ROW = re.compile(r"^\*\s+`([^`]+)`\s+—(.*)$")


def parse_jobs_markdown(path: Path) -> dict[str, dict]:
    """Разбирает ``docs/sgbd/<ЭБУ>_jobs.md`` в {job: результаты}."""
    jobs: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _JOB_ROW.match(line.strip())
        if not match:
            continue
        job, tail = match.group(1), match.group(2)
        if "results:" not in tail:
            continue
        tail = tail.split("results:", 1)[1]
        truncated = "…" in tail
        visible = [name.strip().strip("`")
                   for name in tail.split("…")[0].split(",") if name.strip()]
        jobs[job] = {"results": set(visible), "truncated": truncated}
    return jobs


class CatalogTests(unittest.TestCase):
    """Каталог каналов: имена — только из выгрузок PRG, спорное помечено."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.sgbd: dict[str, dict[str, dict]] = {}
        for ecu, info in cls.catalog["ecus"].items():
            parsed: dict[str, dict] = {}
            for name in info["jobs_files"]:
                parsed.update(parse_jobs_markdown(SGBD_DIR / name))
            cls.sgbd[ecu] = parsed

    def test_every_job_exists_in_prg_exports(self):
        for channel, spec in self.catalog["channels"].items():
            for ecu, sources in spec.get("ecus", {}).items():
                for source in sources:
                    self.assertIn(source["job"], self.sgbd[ecu],
                                  f"{channel}/{ecu}: job {source['job']}")

    def test_every_result_is_confirmed_by_the_export(self):
        unverified = []
        for channel, spec in self.catalog["channels"].items():
            for ecu, sources in spec.get("ecus", {}).items():
                for source in sources:
                    row = self.sgbd[ecu][source["job"]]
                    for result in source["results"]:
                        if result in row["results"]:
                            continue
                        # Строка выгрузки обрезана (…+N) и имени не видно:
                        # запись допускается только с честной пометкой.
                        self.assertTrue(
                            row["truncated"] and source.get("truncated_export"),
                            f"{channel}/{ecu}: {source['job']} → {result} нет "
                            "в выгрузке PRG и это не помечено truncated_export")
                        unverified.append(f"{channel}/{ecu}/{result}")
        self.assertLessEqual(
            len(unverified), 1,
            "непроверенных имён стало больше — каждое требует пометки "
            f"truncated_export: {unverified}")

    def test_ambiguous_names_are_resolved_only_by_job(self):
        # STAT_AUSGANG есть у E_LUEFTER, LL_STELLER и VIMDISA: по одному имени
        # канал определить нельзя, поэтому без job-а он не отдаётся.
        self.assertIsNone(test0_log.channel_for_result("STAT_AUSGANG"))
        self.assertEqual("fan_duty", test0_log.channel_for_result(
            "STAT_AUSGANG", "STATUS_E_LUEFTER"))
        self.assertEqual("idle_actuator", test0_log.channel_for_result(
            "STAT_AUSGANG", "STATUS_LL_STELLER"))
        self.assertEqual("disa_status", test0_log.channel_for_result(
            "STAT_AUSGANG", "STATUS_VIMDISA"))

    def test_ms45_status_messwerte_candidates_are_visible(self):
        # Кандидаты, которых требовала проверка: у MS45 блока результатов
        # STATUS_MESSWERTE строка выгрузки обрезана, но эти имена видны.
        row = self.sgbd["MS45"]["STATUS_MESSWERTE"]
        for result in ("STAT_NMOT_WERT", "STAT_MSHFM_WERT", "STAT_MIIST_WERT"):
            self.assertIn(result, row["results"])


class PresetTests(unittest.TestCase):
    """Пресеты Test0: свежесть, имена файлов, ссылки на job-ы из каталога."""

    def test_presets_are_fresh(self):
        result = subprocess.run(
            [sys.executable, "tools/test0_presets.py", "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("расхождений: 0", result.stdout)

    def test_every_preset_references_catalog_entries(self):
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        files = sorted(PRESETS_DIR.glob("*.json"))
        # 11 функций × 4 ЭБУ: базовые (stage0/ignition/fuel/vanos/limits/service)
        # и функции варианта 1 (octane/disa_off/isa_off/idle/fan_thermostat).
        self.assertEqual(44, len(files), [p.name for p in files])
        for path in files:
            preset = json.loads(path.read_text(encoding="utf-8"))
            ecu = preset["ecu"]
            for entry in preset["jobs"]:
                channel = entry["channel"]
                spec = catalog["channels"].get(channel)
                self.assertIsNotNone(spec, f"{path.name}: канал {channel}")
                if entry.get("source") == "external_ru":
                    self.assertTrue(spec.get("external"),
                                    f"{path.name}: {channel} не внешний")
                    continue
                sources = spec.get("ecus", {}).get(ecu, [])
                match = [source for source in sources
                         if source["job"] == entry["job"]
                         and set(entry["results"]) <= set(source["results"])]
                self.assertTrue(match, f"{path.name}: {entry}")

    def test_preset_files_are_lf_and_utf8(self):
        for path in PRESETS_DIR.glob("*.json"):
            data = path.read_bytes()
            self.assertNotIn(b"\r\n", data, path.name)
            data.decode("utf-8")  # не должно падать


class Test0DbTests(unittest.TestCase):
    """Родная база Test0 (``.db``): схема, склейка job-ов, отказы."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_native_schema_is_read_and_channels_are_merged(self):
        db = fx.write_test0_db(self.dir / "2026_10_07.db")
        table = test0_log.read_test0_db(db)
        self.assertEqual("test0_db", table["format"])
        self.assertEqual(
            {"time", "rpm", "load", "ignition", "maf_airflow", "coolant_temp",
             "intake_temp"},
            set(table["channels"]))
        self.assertEqual(table["rows"], len(table["channels"]["rpm"]))
        # Каждый job опрашивается реже, чем идёт общая сетка: значение канала
        # действительно до следующего опроса, поэтому пропусков почти нет.
        filled = sum(1 for value in table["channels"]["rpm"] if value is not None)
        self.assertGreater(filled, table["rows"] * 0.9)
        # Единицы взяты из парных результатов *_EINH.
        self.assertEqual("1/min", table["test0_db"]["units"]["rpm"])
        self.assertEqual("deg C", table["test0_db"]["units"]["coolant_temp"])
        self.assertEqual("MS430DS0", table["test0_db"]["jobs"][0]["prg"])
        self.assertEqual("MS43", analyzer.detect_ecu(table))

    def test_analyzer_reads_db_and_speaks_in_test0_terms(self):
        db = fx.write_test0_db(self.dir / "2026_10_07.db")
        outcome = analyzer.analyze_pack_log(db, "ignition", 98)
        self.assertEqual("test0_db", outcome["source_format"])
        self.assertEqual(3, outcome["results"]["wot_runs"])
        # В PRG MS41–MS45 вердикта детонации нет: анализатор обязан сказать,
        # чего не хватает и где взять набор job-ов, а не подменить канал.
        self.assertFalse(outcome["valid"])
        report = next(issue for issue in outcome["issues"]
                      if issue.startswith("нет обязательных каналов"))
        self.assertIn("knock_retard", report)
        self.assertIn("docs/test0_presets/", report)

    def test_foreign_unit_is_skipped_with_message(self):
        db = fx.write_test0_db(self.dir / "foreign.db",
                               foreign_unit="coolant_temp")
        table = analyzer.read_log_table(db)
        self.assertTrue(all(value is None
                            for value in table["channels"]["coolant_temp"]))
        self.assertTrue(any("не совпадает" in issue for issue in table["issues"]),
                        table["issues"])

    def test_corrupt_frame_is_reported_not_invented(self):
        db = fx.write_test0_db(self.dir / "broken.db", corrupt_json=True)
        table = test0_log.read_test0_db(db)
        self.assertTrue(any("JSON" in issue for issue in table["issues"]))
        self.assertGreater(table["rows"], 0)

    def test_files_without_the_native_schema_are_rejected(self):
        other = self.dir / "other.db"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE t(x)")
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            test0_log.read_test0_db(other)

    def test_csv_is_not_mistaken_for_a_native_db(self):
        csv_path = fx.write_test0_csv(self.dir / "testo.csv")
        self.assertFalse(test0_log.is_test0_db(csv_path))


class Test0CsvTests(unittest.TestCase):
    """CSV-экспорт Test0/TestO: поля PRG, префикс SET_n., единицы, AFR."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_prg_names_with_logger_prefix_are_mapped(self):
        csv_path = fx.write_test0_csv(self.dir / "testo.csv")
        table = analyzer.read_pack_csv(csv_path)
        self.assertEqual("test0_csv", table["format"])
        for channel in ("time", "rpm", "load", "ignition", "maf_airflow",
                        "coolant_temp", "intake_temp"):
            filled = [v for v in table["channels"][channel] if v is not None]
            self.assertGreater(len(filled), 100, channel)

    def test_ambiguous_ms45_name_is_not_guessed_in_csv(self):
        # В CSV нет имени job-а: STAT_AUSGANG от трёх разных job-ов отличить
        # нельзя, поэтому канал не заполняется (лучше пусто, чем неверно).
        path = self.dir / "ms45.csv"
        path.write_text("TIME [s];STAT_AUSGANG_WERT [%]\n0,0;10\n0,1;11\n",
                        encoding="utf-8", newline="\n")
        table = analyzer.read_pack_csv(path)
        for channel in ("fan_duty", "idle_actuator", "disa_status"):
            self.assertTrue(all(v is None for v in table["channels"][channel]),
                            channel)

    def test_afr_is_converted_to_lambda_and_recorded(self):
        path = self.dir / "afr.csv"
        path.write_text("time;wideband [AFR]\n0.0;12.6\n0.1;14.7\n",
                        encoding="utf-8", newline="\n")
        table = analyzer.read_pack_csv(path)
        values = table["channels"]["lambda_wideband"]
        self.assertAlmostEqual(12.6 / test0_log.AFR_STOICH, values[0], places=4)
        self.assertAlmostEqual(1.0, values[1], places=4)
        self.assertTrue(table["conversions"])

    def test_lambda_unit_is_left_untouched(self):
        path = self.dir / "lambda.csv"
        path.write_text("time;wideband [lambda]\n0.0;0.86\n",
                        encoding="utf-8", newline="\n")
        table = analyzer.read_pack_csv(path)
        self.assertAlmostEqual(0.86, table["channels"]["lambda_wideband"][0],
                               places=4)
        self.assertEqual([], table["conversions"])

    def test_test0_csv_with_knock_retard_passes_the_gate(self):
        csv_path = fx.write_test0_csv(self.dir / "full.csv", full=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertTrue(outcome["valid"], outcome["issues"])
        self.assertEqual(3, outcome["results"]["wot_runs"])
        self.assertEqual("load", outcome["results"]["wot_detection"])


class GuideTests(unittest.TestCase):
    """Инструкция клиенту (RU/EN) и веб-анализ формата лежат в docs/."""

    def test_client_guides_exist_and_cover_all_ecus(self):
        for name in ("docs/TEST0_CLIENT_GUIDE_RU.md",
                     "docs/TEST0_CLIENT_GUIDE_EN.md"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            for ecu in ("MS41", "MS42", "MS43", "MS45"):
                self.assertIn(ecu, text, name)
            self.assertIn("pack_log_analyzer", text, name)
            self.assertIn("test0_presets", text, name)

    def test_web_analysis_document_exists(self):
        text = (REPO_ROOT / "docs" / "TEST0_WEB_ANALYSIS_RU.md").read_text(
            encoding="utf-8")
        for anchor in ("phenoboy.kapsi.fi", "2026", "testo.ini", ".db"):
            self.assertIn(anchor, text)
        for section in ("§2", "§6"):
            self.assertIn(section, text)


if __name__ == "__main__":
    unittest.main()
