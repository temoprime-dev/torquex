# -*- coding: utf-8 -*-
"""Регрессия плагинного диспетчера на реальных стоках.

The old explicit platform switch and the new registry must produce identical
output bytes.  This is intentionally an image-level assertion, not only a
comparison of status/manifest metadata.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import torquex  # noqa: E402


MS43_STOCKS = (
    ROOT / "MS43/firmware_archive/E39_530__b137j54g_Ca430037_Stok.bin",
    ROOT / "MS43/firmware_archive/E46_330i__C155G50G_Ca430055_Stok.bin",
    ROOT / "MS43/firmware_archive/E46_330i__c256954g_Ca430056_Stok.bin",
    ROOT / "MS43/firmware_archive/E39_530__c6668528_Ca430066_Stok.bin",
    ROOT / "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin",
)
MS42_STOCKS = tuple(sorted((ROOT / "MS42/firmware").glob("*.bin")))


class PluginByteRegressionTests(unittest.TestCase):
    def _args(self, output: Path) -> argparse.Namespace:
        return argparse.Namespace(
            out=str(output), note=None, manifest=None, dry_run=False,
            lang="en", customer="plugin-byte-regression",
        )

    def _run(self, platform: str, stock: Path, request: dict, builder) -> bytes:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.bin"
            args = self._args(output)
            detection = torquex.detect(stock)
            with contextlib.redirect_stdout(io.StringIO()):
                code = builder(args, request, stock)
            self.assertEqual(0, code, (platform, stock.name))
            self.assertTrue(output.exists(), (platform, stock.name))
            return output.read_bytes()

    def test_registry_matches_the_pre_refactor_switch_on_all_target_stocks(self) -> None:
        requests = {
            "MS43": {
                "variant": "v1",
                "octane": "ron98",
                "intents": [{"kind": "ignition", "delta_deg": 1.0}],
            },
            "MS42": {
                "octane": "ron98",
                "intents": [{"kind": "vmax", "value_kmh": 240,
                             "hard_add_kmh": 5}],
            },
        }
        legacy = {
            "MS42": torquex._build_ms42,
            "MS43": torquex._build_ms43,
        }
        cases = [("MS43", path) for path in MS43_STOCKS]
        cases += [("MS42", path) for path in MS42_STOCKS]
        self.assertEqual(5 + len(MS42_STOCKS), len(cases))
        for platform, stock in cases:
            with self.subTest(platform=platform, stock=stock.name):
                self.assertTrue(stock.exists(), stock)
                plugin_bytes = self._run(
                    platform, stock, requests[platform],
                    lambda args, req, path: torquex._build_platform(
                        args, torquex.detect(path), req, path),
                )
                legacy_bytes = self._run(
                    platform, stock, requests[platform], legacy[platform]
                )
                self.assertEqual(legacy_bytes, plugin_bytes,
                                 f"plugin changed bytes for {platform}/{stock.name}")


if __name__ == "__main__":
    unittest.main()
