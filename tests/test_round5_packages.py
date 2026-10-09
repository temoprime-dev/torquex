# -*- coding: utf-8 -*-
"""Раунд 5: общие модели и новые opt-in writers на реальных стоках."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from MS4x.tools import stages as common_stages  # noqa: E402
from MS42.tools import stages as ms42_stages  # noqa: E402
from MS42.tools.calibration import DEFAULT_POLICY, Policy  # noqa: E402
from MS43.tools import stages as ms43_stages  # noqa: E402
from MS43.tools.policy import load_policy as load_ms43_policy  # noqa: E402


MS42_STOCK = ROOT / "MS42/firmware/Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"
MS43_STOCK = ROOT / "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"


class CommonModelTests(unittest.TestCase):
    def test_pedal_torque_model_is_monotone_and_at_is_soft(self) -> None:
        stock = [float(index) for index in range(12 * 8)]
        mt = common_stages.monotone_torque_model(stock, 12, 8, 2, "mt")
        at = common_stages.monotone_torque_model(stock, 12, 8, 2, "at")
        for values in (mt, at):
            for row in range(12):
                cells = values[row * 8:(row + 1) * 8]
                self.assertTrue(common_stages.monotonic_increasing(cells))
        # Round 5b keeps the last pedal column exactly stock; compare the
        # interior response where AT is deliberately softer than MT.
        self.assertLess(at[4], mt[4])
        self.assertGreaterEqual(min(at), min(stock))
        self.assertTrue(any(value > before for value, before in zip(at, stock)))

    def test_default_bench_gate_is_stable(self) -> None:
        finding = common_stages.bench_gate_finding(
            "MS43", [{"kind": "pedal_response"}],
            config_path=ROOT / "config" / "torquex.json",
        )
        self.assertIsNotNone(finding)
        self.assertEqual("AWAITING_BENCH_VERIFICATION", finding["code"])
        self.assertIsNone(common_stages.bench_gate_finding(
            "MS45", [{"kind": "pedal_response"}],
            config_path=ROOT / "config" / "torquex.json",
        ))


class MS43Round5PackageTests(unittest.TestCase):
    def test_pedal_and_vanos_are_opt_in_and_risk_gated(self) -> None:
        policy = load_ms43_policy()
        no_ack = {"stage": "stage1", "intents": [{"kind": "pedal_response", "level": 1}]}
        with contextlib.redirect_stdout(io.StringIO()):
            rejected = ms43_stages.build("stage1", MS43_STOCK.read_bytes(), no_ack, policy=policy)
        self.assertEqual("REJECTED", rejected.status)
        self.assertTrue(any(item["code"] == "ACKNOWLEDGE_RISK_REQUIRED"
                            for item in rejected.findings))

        request = {
            "stage": "stage1",
            "variant": "v1",
            "intents": [
                {"kind": "pedal_response", "level": 1, "transmission": "at",
                 "acknowledge_risk": True},
                {"kind": "vanos_stage1", "acknowledge_risk": True},
            ],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            built = ms43_stages.build("stage1", MS43_STOCK.read_bytes(), request, policy=policy)
        self.assertEqual("BUILT", built.status, built.findings)
        package_by_id = {item.id: item for item in built.packages}
        self.assertEqual("written", package_by_id["pedal_response"].status)
        self.assertEqual("written", package_by_id["vanos_stage1"].status)
        self.assertTrue(built.plan and built.plan.changed_cells)
        self.assertFalse(any(item["severity"] == "reject" for item in built.findings))


class MS42Round5PackageTests(unittest.TestCase):
    def test_linked_ignition_pedal_and_vanos_write_real_stock(self) -> None:
        policy = Policy.load(DEFAULT_POLICY)
        request = {
            "stage": "stage1",
            "octane": "ron98",
            "intents": [
                {"kind": "ignition_stage1", "delta_deg": 1.0},
                {"kind": "pedal_response", "level": 1, "transmission": "at",
                 "acknowledge_risk": True},
                {"kind": "vanos_stage1", "acknowledge_risk": True},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "round5.bin"
            with contextlib.redirect_stdout(io.StringIO()):
                result = ms42_stages.build(
                    "stage1", MS42_STOCK, request, policy, out=output,
                )
            output_bytes = output.read_bytes()
        self.assertEqual("READY", result.status, result.findings)
        self.assertTrue(output_bytes)
        self.assertGreater(len(result.patches), 512)
        self.assertIn("ip_iga_optm_tco_2__n__maf", {item["symbol"] for item in result.patches})
        self.assertTrue(any(item.id == "pedal_response" and item.status == "READY"
                            for item in result.packages))
        self.assertTrue(any(item.id == "vanos_stage1" and item.status == "READY"
                            for item in result.packages))
        self.assertNotIn("READ_ONLY", {item["code"] for item in result.findings})


if __name__ == "__main__":
    unittest.main()
