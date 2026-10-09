# -*- coding: utf-8 -*-
"""Блоки GS8.60.x: определение по хешам, контрольные суммы, откат на фабричную калибровку."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import gs8_state  # noqa: E402

ALPINA = Path(os.environ.get("TORQUEX_ALPINA_DIR", Path(os.environ.get("TEMP", "")) / "alpina_src"))
HAVE_ALPINA = ALPINA.exists() and any(ALPINA.rglob("Bosch_GS8.60.*_Full.bin"))


def alpina(pattern: str) -> bytes:
    return next(ALPINA.rglob(pattern)).read_bytes()


class DatabaseTests(unittest.TestCase):
    def test_databases_exist_and_hold_expected_shapes(self) -> None:
        db = gs8_state.load_db()
        cal = gs8_state._load_cal()
        self.assertTrue(db and cal, "config/gs8_factory*.json не найдены")
        for system in gs8_state.LAYOUTS:
            self.assertTrue(db[system]["programs"])
            self.assertTrue(db[system]["calibrations"])
            for program in db[system]["programs"]:
                for start, digest in program["blocks"][:3]:
                    self.assertEqual(12, len(digest))
            for entry in db[system]["calibrations"]:
                self.assertEqual(20, len(entry["hash"]))
                self.assertIn(entry["hash"], cal[system])

    def test_random_file_is_unknown(self) -> None:
        self.assertEqual("unknown", gs8_state.identify(os.urandom(262144))["state"])
        self.assertEqual("unknown", gs8_state.identify(b"\xff" * 1000)["state"])


@unittest.skipUnless(HAVE_ALPINA, "нет клона ms4x-net/alpina")
class AlpinaFilesTests(unittest.TestCase):
    def test_alpina_files_are_recognised_and_checksums_ok(self) -> None:
        for path in sorted(ALPINA.rglob("Bosch_GS8.60.*_Full.bin")):
            with self.subTest(file=path.name):
                data = path.read_bytes()
                result = gs8_state.identify(data)
                self.assertEqual("alpina", result["state"], result)
                self.assertIn(result["system"], path.name)
                self.assertTrue(all(row["ok"] for row in gs8_state.checksums(data, result["system"])))

    def test_changed_calibration_is_modified(self) -> None:
        data = bytearray(alpina("Bosch_GS8.60.0_*_Full.bin"))
        data[0x9000] ^= 0x01
        self.assertEqual("modified", gs8_state.identify(bytes(data))["state"])

    def test_restore_gives_factory_stock_with_known_checksum(self) -> None:
        data = alpina("Bosch_GS8.60.0_*_Full.bin")
        options = gs8_state.factory_options("GS8.60.0")
        ka20 = next(o for o in options if "E39 M52B25 523i ab" in o["vehicle"])
        out, report = gs8_state.restore(data, ka20["hash"])
        # публично документировано: калибровка KA20 в стоке имеет сумму 0x47DB;
        # от Alpina она отличается ровно 2547 байтами
        self.assertEqual("0x47DB", report["checksums"][-1]["value"])
        self.assertEqual(2547, report["changed_bytes"])
        self.assertEqual("stock", gs8_state.identify(out)["state"])
        self.assertTrue(all(row["ok"] for row in gs8_state.checksums(out, "GS8.60.0")))
        self.assertEqual(data[0x10000:0x3F77C], out[0x10000:0x3F77C])  # программа не тронута
        self.assertEqual(data[0:0x4300], out[0:0x4300])  # загрузчик не тронут

    def test_restore_works_on_every_system(self) -> None:
        for pattern, system in (("Bosch_GS8.60.2_17C0_*_Full.bin", "GS8.60.2"),
                                ("Bosch_GS8.60.4_*_Full.bin", "GS8.60.4")):
            with self.subTest(system=system):
                data = alpina(pattern)
                option = gs8_state.factory_options(system)[0]
                out, _report = gs8_state.restore(data, option["hash"])
                self.assertEqual("stock", gs8_state.identify(out)["state"])
                self.assertTrue(all(row["ok"] for row in gs8_state.checksums(out, system)))

    def test_alpina_issue_round_trip_is_byte_exact(self) -> None:
        """Сток + калибровка Alpina даёт файл Alpina побайтно (сумма 0x1851 из документов)."""
        original = alpina("Bosch_GS8.60.0_*_Full.bin")
        ka20 = next(o for o in gs8_state.factory_options("GS8.60.0") if "E39 M52B25 523i ab" in o["vehicle"])
        stock, _ = gs8_state.restore(original, ka20["hash"])
        info = gs8_state.identify(stock)
        options = gs8_state.factory_options("GS8.60.0", source="alpina", program=info["program"])
        self.assertEqual(1, len(options))
        out, report = gs8_state.restore(stock, options[0]["hash"], source="alpina")
        self.assertEqual(original, out)
        self.assertEqual("0x1851", report["checksums"][-1]["value"])

    def test_alpina_needs_matching_program_and_kind(self) -> None:
        file_62 = alpina("Bosch_GS8.60.2_17C0_*_Full.bin")
        wrong = gs8_state.factory_options("GS8.60.2", source="alpina")
        info = gs8_state.identify(file_62)
        compatible = gs8_state.factory_options("GS8.60.2", source="alpina", program=info["program"])
        self.assertEqual(1, len(compatible))
        other = next(o for o in wrong if o["hash"] != compatible[0]["hash"])
        with self.assertRaises(gs8_state.Gs8Error) as caught:
            gs8_state.restore(file_62, other["hash"], source="alpina")
        self.assertEqual("GS8_ALPINA_INCOMPATIBLE", caught.exception.code)
        bmw = gs8_state.factory_options("GS8.60.2")[0]
        with self.assertRaises(gs8_state.Gs8Error) as caught:
            gs8_state.restore(file_62, bmw["hash"], source="alpina")
        self.assertEqual("GS8_DATASET_KIND", caught.exception.code)

    def test_catalog_summary_lists_alpina(self) -> None:
        summary = gs8_state.catalog_summary()
        self.assertEqual(["Alpina B3 (24 10 620)"], summary["GS8.60.0"]["alpina"])
        self.assertEqual(2, len(summary["GS8.60.2"]["alpina"]))

    def test_restore_refuses_damaged_or_foreign_files(self) -> None:
        data = alpina("Bosch_GS8.60.0_*_Full.bin")
        option = gs8_state.factory_options("GS8.60.0")[0]
        broken = bytearray(data)
        broken[0x20000] ^= 0x01
        with self.assertRaises(gs8_state.Gs8Error) as caught:
            gs8_state.restore(bytes(broken), option["hash"])
        self.assertEqual("GS8_SOURCE_CRC_INVALID", caught.exception.code)
        with self.assertRaises(gs8_state.Gs8Error) as caught:
            gs8_state.restore(os.urandom(262144), option["hash"])
        self.assertEqual("GS8_NOT_RECOGNISED", caught.exception.code)
        with self.assertRaises(gs8_state.Gs8Error) as caught:
            gs8_state.restore(data, "0" * 20)
        self.assertEqual("GS8_DATASET_UNKNOWN", caught.exception.code)


@unittest.skipUnless(HAVE_ALPINA, "нет клона ms4x-net/alpina")
class ServerTests(unittest.TestCase):
    def test_detect_options_and_restore_job(self) -> None:
        import tempfile

        from server import core

        data = alpina("Bosch_GS8.60.0_*_Full.bin")
        with tempfile.TemporaryDirectory() as directory:
            service = core.TorquexService(donate_url="x", journal_path=Path(directory) / "j.jsonl")
            detected = service.detect(data, "x.bin")
            self.assertEqual("GS8", detected["platform"])
            self.assertEqual("alpina", detected["calibration"]["state"])
            self.assertTrue(detected["options"])
            job = service.build(
                data,
                {"stage": "gs8", "intents": [{"kind": "gs8_restore",
                                              "calibration": detected["options"][0]["hash"]}]},
                account="t", vin="", ip="203.0.113.1")
            self.assertEqual("BUILT", job.status)
            self.assertEqual("stock", service.detect(job.image, "r.bin")["calibration"]["state"])
            with self.assertRaises(core.ServiceError) as caught:
                service.build(data, {"intents": [{"kind": "ignition", "delta_deg": 3}]},
                              account="t", vin="", ip="203.0.113.1")
            self.assertEqual("TCU_READ_ONLY", caught.exception.code)

    def test_server_issues_alpina_and_lists_options(self) -> None:
        import tempfile

        from server import core

        original = alpina("Bosch_GS8.60.0_*_Full.bin")
        ka20 = next(o for o in gs8_state.factory_options("GS8.60.0") if "E39 M52B25 523i ab" in o["vehicle"])
        stock, _ = gs8_state.restore(original, ka20["hash"])
        with tempfile.TemporaryDirectory() as directory:
            service = core.TorquexService(donate_url="x", journal_path=Path(directory) / "j.jsonl")
            detected = service.detect(stock, "stock.bin")
            self.assertEqual("stock", detected["calibration"]["state"])
            self.assertEqual(1, len(detected["alpina_options"]))
            job = service.build(
                stock, {"intents": [{"kind": "gs8_alpina", "calibration": detected["alpina_options"][0]["hash"]}]},
                account="t", vin="", ip="203.0.113.2")
            self.assertEqual("BUILT", job.status)
            self.assertEqual(original, job.image)
            self.assertEqual("gs8_alpina", job.manifest["stage"])
            versions = service.versions()
            self.assertIn("GS8", versions["ecus"])


if __name__ == "__main__":
    unittest.main()
