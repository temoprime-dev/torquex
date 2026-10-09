# -*- coding: utf-8 -*-
"""Анализатор «сток или тюн»: заводские файлы — stock, наш Stage 1 — tuned."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from MS43.tools import policy as ms43_policy  # noqa: E402
from MS43.tools import stages as ms43_stages  # noqa: E402
from tools import cal_state  # noqa: E402

STOCK = ROOT / "MS43/firmware/Siemens_MS43_MS430069_E46_M54B25_EU4_LHD.bin"


class CalStateTests(unittest.TestCase):
    def test_factory_files_are_stock(self) -> None:
        for path in sorted((ROOT / "MS43/firmware").glob("*.bin")):
            with self.subTest(file=path.name):
                result = cal_state.analyze("MS43", path.read_bytes())
                self.assertEqual("stock", result["state"], result)

    def test_our_stage1_is_tuned(self) -> None:
        request = {
            "stage": "stage1", "variant": "v1", "octane": "ron98",
            "_allow_linked_ignition": True,
            "intents": [{"id": "i", "kind": "ignition", "delta_deg": 3.0}],
        }
        policy = ms43_policy.load_policy_for_version("430069")
        with contextlib.redirect_stdout(io.StringIO()):
            result = ms43_stages.build("stage1", STOCK.read_bytes(), request, policy=policy)
        self.assertEqual("BUILT", result.status, result.findings)
        verdict = cal_state.analyze("MS43", result.result.image)
        self.assertEqual("tuned", verdict["state"], verdict)
        self.assertTrue(any(r.startswith("ignition_raised") for r in verdict["reasons"]))

    def test_factory_files_of_other_platforms_are_never_tuned(self) -> None:
        for platform, folder in (("MS42", "MS42/firmware"), ("MS41", "MS41/firmware"),
                                 ("MS45", "MS45/firmware")):
            for path in sorted((ROOT / folder).rglob("*.bin")):
                with self.subTest(platform=platform, file=path.name):
                    result = cal_state.analyze(platform, path.read_bytes())
                    self.assertNotEqual("tuned", result["state"], result)

    def test_ms42_factory_files_are_stock(self) -> None:
        for path in sorted((ROOT / "MS42/firmware").glob("*.bin")):
            with self.subTest(file=path.name):
                self.assertEqual("stock", cal_state.analyze("MS42", path.read_bytes())["state"])

    def test_ms42_ignition_stage1_is_tuned(self) -> None:
        from MS42.tools import stages as ms42_stages
        from MS42.tools.calibration import DEFAULT_POLICY, Policy
        import tempfile

        stock = ROOT / "MS42/firmware/Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin"
        request = {"stage": "stage1", "octane": "ron98", "_allow_linked_ignition": True,
                   "intents": [{"id": "i", "kind": "ignition_stage1", "delta_deg": 3.0}]}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.bin"
            with contextlib.redirect_stdout(io.StringIO()):
                ms42_stages.build("stage1", stock, request, Policy.load(DEFAULT_POLICY), out=out)
            verdict = cal_state.analyze("MS42", out.read_bytes())
        self.assertEqual("tuned", verdict["state"], verdict)

    def test_unknown_platform_is_unknown(self) -> None:
        verdict = cal_state.analyze("MS41", b"\0" * 10)
        self.assertEqual("unknown", verdict["state"])

    def test_fingerprint_logic_is_pure(self) -> None:
        db = {"maps": {"ron98": {"hashes": [cal_state.fingerprint([1.0, 2.0])], "variants": [[1.0, 2.0]]}}}
        self.assertEqual("stock", cal_state.evaluate({"ron98": [1.0, 2.0]}, db)["state"])
        self.assertEqual("tuned", cal_state.evaluate({"ron98": [5.0, 6.0]}, db)["state"])
        small = cal_state.evaluate({"ron98": [1.0, 2.2]}, db)
        self.assertEqual("unknown", small["state"])


if __name__ == "__main__":
    unittest.main()
