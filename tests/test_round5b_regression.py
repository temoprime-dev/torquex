# -*- coding: utf-8 -*-
"""Раунд 5b: регрессии PR45, которые должны быть красными на старом PR45."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from MS4x.tools import stages as common_stages  # noqa: E402
from MS42.tools import stages as ms42_stages  # noqa: E402
from MS42.tools.calibration import DEFAULT_POLICY, Policy  # noqa: E402
from MS43.tools import stages as ms43_stages  # noqa: E402
from MS43.tools.policy import load_policy as load_ms43_policy  # noqa: E402
from MS43.tools import policy as ms43_policy  # noqa: E402
from server import core  # noqa: E402
from tools import pedal_report  # noqa: E402

MS42_STOCK = ROOT / "MS42/firmware/Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"
MS43_STOCK = ROOT / "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"


class PedalDeltaRegressionTests(unittest.TestCase):
    def test_surface_is_stock_plus_nonnegative_delta(self) -> None:
        stock = [float(index) for index in range(3 * 5)]
        result = common_stages.monotone_torque_model(stock, 3, 5, 3, "mt")
        self.assertEqual(len(stock), len(result))
        for before, after in zip(stock, result):
            self.assertGreaterEqual(after + 1e-9, before)
            self.assertLessEqual(after, before * 1.24 + 1e-9)
        # Zero-pedal and zero-stock cells are deliberately untouched.  The
        # edge columns are not allowed to receive a response delta either.
        for row in range(3):
            self.assertEqual(stock[row * 5], result[row * 5])
            self.assertEqual(stock[row * 5 + 4], result[row * 5 + 4])
        self.assertEqual(0.0, result[0])

    def test_threshold_levels_are_exact_ten_twenty_thirty_percent(self) -> None:
        stock = [10.0, 20.0, 30.0, 40.0]
        for level, fraction in ((1, 0.10), (2, 0.20), (3, 0.30)):
            result = common_stages.pedal_threshold_model(stock, level, "at")
            for before, after in zip(stock, result):
                self.assertAlmostEqual(before * (1.0 - fraction), after, places=8)
        self.assertTrue(common_stages.monotonic_increasing(
            common_stages.pedal_threshold_model(stock, 3, "mt")))

    def test_levels_two_and_three_need_separate_risk_acknowledgement(self) -> None:
        policy = load_ms43_policy()
        request = {
            "stage": "stage1", "variant": "v1",
            "intents": [{"kind": "pedal_response", "level": 2,
                         "transmission": "at", "acknowledge_risk": True}],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = ms43_stages.build("stage1", MS43_STOCK.read_bytes(), request, policy=policy)
        self.assertEqual("REJECTED", result.status)
        self.assertTrue(any(item["code"] == "ACKNOWLEDGE_LEVEL_RISK_REQUIRED"
                            for item in result.findings))

    def test_ms42_levels_two_and_three_need_separate_risk_acknowledgement(self) -> None:
        request = {
            "stage": "stage1", "octane": "ron98",
            "intents": [{"kind": "pedal_response", "level": 2,
                         "transmission": "at", "acknowledge_risk": True}],
        }
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()):
                result = ms42_stages.build(
                    "stage1", MS42_STOCK, request, Policy.load(DEFAULT_POLICY),
                    out=Path(directory) / "out.bin",
                )
        self.assertEqual("BLOCKED", result.status)
        self.assertTrue(any(item["code"] == "ACKNOWLEDGE_LEVEL_RISK_REQUIRED"
                            for item in result.findings))

    def test_real_ms43_pedal_writer_never_lowers_stock(self) -> None:
        policy = load_ms43_policy()
        request = {
            "stage": "stage1",
            "variant": "v1",
            "intents": [{"kind": "pedal_response", "level": 3,
                         "transmission": "mt", "acknowledge_risk": True,
                         "acknowledge_level_risk": True}],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = ms43_stages.build("stage1", MS43_STOCK.read_bytes(), request, policy=policy)
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertTrue(result.plan)
        names = {symbol.symbol for symbol in result.plan.symbols}
        self.assertFalse([name for name in names if name.startswith("ip_tqi")],
                         "ранний режим полной нагрузки не трогает модель момента")
        self.assertIn("id_pvs_fl__n", names)
        for symbol in result.plan.symbols:
            for cell in symbol.cells:
                self.assertLessEqual(float(cell.after), float(cell.before) + 1e-9)


class RealImageRegressionTests(unittest.TestCase):
    MS43_STOCKS = {
        "430037": ROOT / "MS43/firmware_archive/E39_530__b137j54g_Ca430037_Stok.bin",
        "430055": ROOT / "MS43/firmware_archive/E46_330i__C155G50G_Ca430055_Stok.bin",
        "430056": ROOT / "MS43/firmware_archive/E46_330i__c256954g_Ca430056_Stok.bin",
        "430066": ROOT / "MS43/firmware_archive/E39_530__c6668528_Ca430066_Stok.bin",
        "430069": MS43_STOCK,
    }

    def test_pedal_writer_runs_on_every_required_ms43_version(self) -> None:
        for version, path in self.MS43_STOCKS.items():
            with self.subTest(version=version):
                request = {
                    "stage": "stage1", "variant": "v1",
                    "intents": [{"kind": "pedal_response", "level": 1,
                                 "transmission": "at", "acknowledge_risk": True}],
                }
                with contextlib.redirect_stdout(io.StringIO()):
                    result = ms43_stages.build(
                        "stage1", path.read_bytes(), request,
                        policy=ms43_policy.load_policy_for_version(version),
                    )
                self.assertEqual("BUILT", result.status, result.findings)
                self.assertTrue(result.result and result.result.image)

    def test_report_contains_stock_result_for_required_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.bin"
            request = {
                "stage": "stage1", "variant": "v1",
                "intents": [{"kind": "pedal_response", "level": 1,
                             "transmission": "at", "acknowledge_risk": True}],
            }
            with contextlib.redirect_stdout(io.StringIO()):
                result = ms43_stages.build(
                    "stage1", MS43_STOCK.read_bytes(), request,
                    policy=load_ms43_policy(),
                )
            result_path.write_bytes(result.result.image)
            report = pedal_report.build_report(MS43_STOCK, result_path)
        self.assertIn("ip_tqi_pvs__n__pvs", report["symbols"])
        self.assertIn("ip_tqi_maf__n__maf", report["symbols"])
        self.assertIn("id_pvs_fl__n", report["symbols"])
        self.assertTrue(any(row["changed"] for row in report["rows"]))


class BenchRegistryRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = {
            key: os.environ.get(key)
            for key in ("TORQUEX_BENCH_REQUIRED", "TORQUEX_BENCH_VERIFIED_PATH")
        }

    def tearDown(self) -> None:
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_empty_registry_never_means_bench_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bench_verified.json"
            path.write_text(json.dumps({"verified": []}), encoding="utf-8")
            self.assertFalse(common_stages.bench_verified(
                "MS43", "430069", registry_path=path))
            os.environ["TORQUEX_BENCH_REQUIRED"] = "false"
            os.environ["TORQUEX_BENCH_VERIFIED_PATH"] = str(path)
            request = {
                "variant": "v1",
                "octane": "ron98",
                "intents": [{"kind": "pedal_response", "level": 1,
                             "transmission": "at", "acknowledge_risk": True}],
            }
            job = core.TorquexService().build(
                MS43_STOCK.read_bytes(), request, account="round5b-empty-registry", vin="WBAEV11000KL12345"
            )
        self.assertEqual("BUILT", job.status, job.log)
        self.assertEqual("bytes+crc verified", job.manifest.get("trust_level"))
        self.assertTrue(job.manifest.get("released_without_bench"))
        self.assertNotEqual("bench verified", job.manifest.get("trust_level"))

    def test_only_explicit_version_registry_entry_is_bench_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bench_verified.json"
            path.write_text(json.dumps({"verified": [{
                "platform": "MS43", "version": "430069",
                "verified_at": "2026-10-08", "field_report": "reports/ms43-430069.md",
            }]}), encoding="utf-8")
            os.environ["TORQUEX_BENCH_REQUIRED"] = "true"
            os.environ["TORQUEX_BENCH_VERIFIED_PATH"] = str(path)
            request = {
                "variant": "v1",
                "octane": "ron98",
                "intents": [{"kind": "pedal_response", "level": 1,
                             "transmission": "at", "acknowledge_risk": True}],
            }
            job = core.TorquexService().build(
                MS43_STOCK.read_bytes(), request, account="round5b-registry", vin="WBAEV11000KL12345"
            )
        self.assertEqual("BUILT", job.status, job.log)
        self.assertEqual("bench verified", job.manifest.get("trust_level"))
        self.assertFalse(job.manifest.get("released_without_bench", False))


class PublicIgnitionRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = os.environ.pop("TORQUEX_BENCH_REQUIRED", None)

    def tearDown(self) -> None:
        if self._old is not None:
            os.environ["TORQUEX_BENCH_REQUIRED"] = self._old

    def test_public_ms43_stage1_is_conservative_without_linked_mbt(self) -> None:
        request = {
            "variant": "v1", "octane": "ron98",
            "intents": [{"kind": "ignition", "profile": "stage1", "delta_deg": 1.0}],
        }
        job = core.TorquexService().build(
            MS43_STOCK.read_bytes(), request, account="round5b-public-ms43", vin="WBAEV11000KL12345"
        )
        self.assertEqual("BUILT", job.status, job.log)
        symbols = {item.get("symbol") for item in job.manifest.get("requests", [])}
        self.assertNotIn("ip_iga_optm_tco_2__n__maf", symbols)
        self.assertIn("мягкий", job.note.lower() + json.dumps(job.manifest, ensure_ascii=False).lower())

    def test_public_ms42_stage1_is_conservative_without_linked_mbt(self) -> None:
        request = {
            "variant": "v1", "octane": "ron98",
            "intents": [{"kind": "ignition_stage1", "delta_deg": 1.0}],
        }
        job = core.TorquexService().build(
            MS42_STOCK.read_bytes(), request, account="round5b-public-ms42", vin="WBAEV11000KL12345"
        )
        self.assertEqual("BUILT", job.status, job.log)
        symbols = {item.get("symbol") for item in job.manifest.get("requests", [])}
        self.assertNotIn("ip_iga_optm_tco_2__n__maf", symbols)


if __name__ == "__main__":
    unittest.main()


class PopcornAcOnlyTests(unittest.TestCase):
    """Хлопки «только с кондиционером»: карты A/C off остаются заводскими."""

    def _build(self, ac_only: bool):
        request = {
            "stage": "stage1", "variant": "v1",
            "no_catalyst": True, "acknowledge_legality": True,
            "intents": [{
                "kind": "popcorn", "band": "light",
                "no_catalyst": "катализатор физически снят, вне дорог общего пользования",
                "thermal_watch": True,
                "road_validation_plan": "первые заезды на закрытой площадке, температуру смотрит владелец",
                "ac_only": ac_only,
            }],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            return ms43_stages.build("stage1", MS43_STOCK.read_bytes(), request,
                                     policy=load_ms43_policy())

    def test_ac_only_leaves_ac_off_maps_untouched(self) -> None:
        result = self._build(True)
        self.assertEqual("BUILT", result.status, result.findings)
        names = {symbol.symbol for symbol in result.plan.symbols}
        self.assertIn("ip_iga_accin_puc__n__tco", names)
        self.assertIn("ip_n_min_accin_puc__tco", names)
        for off in ("ip_iga_puc__n__tco", "ip_n_min_puc__tco", "c_n_hys_max_puc"):
            self.assertNotIn(off, names)

    def test_default_keeps_both_maps(self) -> None:
        result = self._build(False)
        names = {symbol.symbol for symbol in result.plan.symbols}
        self.assertIn("ip_iga_puc__n__tco", names)
        self.assertIn("ip_iga_accin_puc__n__tco", names)
