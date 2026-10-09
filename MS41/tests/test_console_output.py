"""Console output must survive a Windows code page.

Windows consoles and pipes encode in the local ANSI code page (cp1251 on a
Russian system, cp866 in the OEM console), and neither has arrows, Greek
letters or German umlauts. A crash there is a UnicodeEncodeError from ``print``,
which is exactly what used to happen with the ``->``/``sigma`` spellings these
tests pin down.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
EXAMPLES = ROOT / "MS41" / "examples"
TOOLS = ROOT / "MS41" / "tools"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"

#: Redirected output on a Russian Windows uses the ANSI code page (cp1251) for
#: pipes and files. Console writes themselves go through the Unicode API, so
#: cp1251 is the encoding a crash can actually come from.
CONSOLE_ENCODING = "cp1251"


class ConsoleOutputTests(unittest.TestCase):
    def run_tool(self, *args: Path) -> str:
        env = dict(os.environ, PYTHONIOENCODING=CONSOLE_ENCODING)
        completed = subprocess.run([sys.executable, *map(str, args)],
                                   capture_output=True, env=env)
        self.assertEqual(completed.returncode, 0,
                         f"{args[0].name} failed on a {CONSOLE_ENCODING} pipe: "
                         f"{completed.stderr.decode(CONSOLE_ENCODING, 'replace')}")
        return completed.stdout.decode(CONSOLE_ENCODING)

    def test_the_vanos_stock_dump_prints_on_a_windows_console(self):
        with tempfile.TemporaryDirectory() as folder:
            out = self.run_tool(TOOLS / "vanos_switch.py", "--bin", PARTIAL_B28,
                                "--dump-stock", Path(folder) / "stock.json")
        self.assertIn("stock VANOS table written", out)

    def test_the_maf_limit_report_prints_on_a_windows_console(self):
        out = self.run_tool(TOOLS / "maf_limits.py", "--bin", PARTIAL_B28,
                            "--curve", EXAMPLES / "maf_curve_example.json")
        self.assertIn("sigma=", out)
        self.assertIn("c_maf_max", out)

    def test_the_launch_limiter_report_prints_on_a_windows_console(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / "request.json"
            request.write_text(json.dumps({"version": 1, "intents": [
                {"id": "l", "kind": "launch_limiter", "value_kmh": 180,
                 "acknowledge_risk": True}]}), encoding="utf-8")
            out = self.run_tool(TOOLS / "generate_patches.py", "--bin", PARTIAL_B28,
                                "--request", request,
                                "--profile", EXAMPLES / "stage2_profile.example.json")
        self.assertIn("ESCALATION_KEPT", out)
        self.assertIn("км/ч->", out)


if __name__ == "__main__":
    unittest.main()
