"""CLI MS45 и консоль Windows: cp1251 и перенаправленный вывод.

Две проверки (как в MS43):

* **содержимое** — всё, что CLI печатают в консоль, обязано кодироваться в
  cp1251 (иначе на русской Windows символ превратится в ``?`` или упадёт);
* **поведение** — CLI обязаны отработать с ``PYTHONIOENCODING=cp1251``, то есть
  в условиях русской Windows.

Наряды (Markdown) и манифесты (JSON) пишутся файлами в UTF-8 осознанно: это
документы, а не консольный вывод.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from MS45.tools import calibration as cal
from MS45.tools import checksums as ck
from MS45.tools import map_match as mm
from MS45.tools import policy as pol
from MS45.tools import stages
from MS45.tools import xdf

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
BO00_CAL = MS45 / "firmware" / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"
REQUEST = MS45 / "examples" / "stage1_request.example.json"


def cp1251_offenders(text: str) -> list[str]:
    """Символы текста, которых нет в cp1251 (для понятного сообщения теста)."""
    offenders = []
    for char in sorted(set(text)):
        try:
            char.encode("cp1251")
        except UnicodeEncodeError:
            offenders.append(f"{char!r} (U+{ord(char):04X})")
    return offenders


class ConsoleContentTests(unittest.TestCase):
    """Тексты отчётов не должны содержать символов вне cp1251."""

    def capture(self, target, args: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = target(args) if callable(target) else target.main(args)
        return code, buffer.getvalue()

    def assert_cp1251_clean(self, text: str, where: str) -> None:
        offenders = cp1251_offenders(text)
        if offenders:
            self.fail(f"{where}: символы вне cp1251 — {', '.join(offenders)}")

    def test_checksums_report(self) -> None:
        code, text = self.capture(ck.main, [str(BO00_CAL)])
        self.assertEqual(code, 0)
        self.assert_cp1251_clean(text, "checksums.py")

    def test_xdf_report(self) -> None:
        code, text = self.capture(xdf.main, ["--family", "4560BO00S"])
        self.assertEqual(code, 0)
        self.assert_cp1251_clean(text, "xdf.py")

    def test_policy_show_and_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "policy.json"
            code, text = self.capture(pol.main, ["--build", "--show", "--policy", str(out)])
            self.assertEqual(code, 0)
            self.assert_cp1251_clean(text, "policy.py --build --show")
            code, text = self.capture(pol.main, ["--validate", "--policy", str(out)])
            self.assertEqual(code, 0)
            self.assert_cp1251_clean(text, "policy.py --validate")

    def test_calibration_plan_and_refusals(self) -> None:
        code, text = self.capture(
            cal.main, ["--image", str(BO00_CAL), "--set", "c_vs_max=255", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assert_cp1251_clean(text, "calibration.py (план)")

        buffer, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = cal.main(["--image", str(BO00_CAL), "--set", "c_n_lim_min=7500", "--dry-run"])
        self.assertEqual(code, 2)
        combined = buffer.getvalue() + errors.getvalue()
        self.assertIn("READ_ONLY", combined)
        self.assert_cp1251_clean(combined, "calibration.py (отказ)")

    def test_stages_report(self) -> None:
        code, text = self.capture(
            stages.main,
            ["--image", str(BO00_CAL), "--stage", "stage1", "--request", str(REQUEST), "--dry-run"],
        )
        self.assertEqual(code, 0)
        self.assert_cp1251_clean(text, "stages.py")

    def test_map_match_report(self) -> None:
        code, text = self.capture(mm.main, ["--validate-baselines"])
        self.assertEqual(code, 0)
        self.assert_cp1251_clean(text, "map_match.py")


class ConsoleBehaviourTests(unittest.TestCase):
    """CLI работают с PYTHONIOENCODING=cp1251 (русская Windows)."""

    def run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
        return subprocess.run(
            [sys.executable, *args],
            capture_output=True,
            env=env,
            cwd=str(ROOT),
        )

    def test_checksums_cli_under_cp1251(self) -> None:
        result = self.run_cli(["-m", "MS45.tools.checksums", str(BO00_CAL)])
        self.assertEqual(result.returncode, 0, result.stderr.decode("cp1251", "replace"))
        self.assertIn("всего", result.stdout.decode("cp1251"))

    def test_policy_cli_under_cp1251(self) -> None:
        result = self.run_cli(["-m", "MS45.tools.policy", "--show"])
        self.assertEqual(result.returncode, 0, result.stderr.decode("cp1251", "replace"))
        self.assertIn("Политика MS45", result.stdout.decode("cp1251"))

    def test_stages_cli_under_cp1251(self) -> None:
        result = self.run_cli(
            [
                "-m",
                "MS45.tools.stages",
                "--image",
                str(BO00_CAL),
                "--stage",
                "stage1",
                "--request",
                str(REQUEST),
                "--dry-run",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("cp1251", "replace"))
        text = result.stdout.decode("cp1251")
        self.assertIn("стадия stage1", text)
        self.assertIn("BUILT", text)

    def test_map_match_cli_under_cp1251(self) -> None:
        result = self.run_cli(["-m", "MS45.tools.map_match", "--validate-baselines"])
        self.assertEqual(result.returncode, 0, result.stderr.decode("cp1251", "replace"))
        self.assertIn("Калибровка эталонов", result.stdout.decode("cp1251"))


class ScriptRunTests(unittest.TestCase):
    """CLI работают и как скрипты: ``python MS45/tools/<инструмент>.py``.

    Так же ведут себя MS42/MS43: инструмент можно запустить и как модуль
    (``python -m MS45.tools.stages``), и просто файлом. Проверяется в условиях
    русской Windows (``PYTHONIOENCODING=cp1251``).
    """

    TOOLS = ("checksums", "xdf", "xml_defs", "a2l_probe", "policy", "calibration", "stages", "map_match")

    def run_script(self, tool: str, args: list[str] | None = None):
        env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
        return subprocess.run(
            [sys.executable, str(MS45 / "tools" / f"{tool}.py"), *(args or [])],
            capture_output=True,
            env=env,
            cwd=str(ROOT),
            timeout=300,
        )

    def decoded(self, result: subprocess.CompletedProcess) -> tuple[str, str]:
        return (
            result.stdout.decode("cp1251", "replace"),
            result.stderr.decode("cp1251", "replace"),
        )

    def test_every_cli_prints_help_as_script(self) -> None:
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                result = self.run_script(tool, ["--help"])
                out, err = self.decoded(result)
                self.assertEqual(result.returncode, 0, err)
                self.assertNotIn("ModuleNotFoundError", err)
                self.assertNotIn("Traceback", err)
                self.assertIn("usage", out.lower())

    def test_spdaten_extract_explains_usage(self) -> None:
        result = self.run_script("spdaten_extract")
        out, err = self.decoded(result)
        self.assertEqual(result.returncode, 2, out + err)
        self.assertIn("использование", err)
        self.assertNotIn("Traceback", err)

    def test_stages_script_end_to_end(self) -> None:
        """Скриптом: выпуск файла, наряд, манифест и проверка по манифесту."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "stage1.bin"
            note = Path(tmp) / "naryad.md"
            manifest = Path(tmp) / "manifest.json"
            result = self.run_script("stages", [
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(REQUEST),
                "--out", str(out),
                "--note-out", str(note),
                "--manifest-out", str(manifest),
            ])
            stdout, stderr_text = self.decoded(result)
            self.assertEqual(result.returncode, 0, stderr_text)
            self.assertIn("записано", stdout)
            self.assertTrue(ck.verify_image(out).ok)
            self.assertIn("Наряд MS45", note.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["status"], "BUILT"
            )
            check = self.run_script("stages", ["--verify-manifest", str(manifest)])
            check_out, check_err = self.decoded(check)
            self.assertEqual(check.returncode, 0, check_err)
            self.assertIn("VERIFIED", check_out)

    def test_policy_and_calibration_as_scripts(self) -> None:
        result = self.run_script("policy", ["--show"])
        out, err = self.decoded(result)
        self.assertEqual(result.returncode, 0, err)
        self.assertIn("Политика MS45", out)

        result = self.run_script("calibration", [
            "--image", str(BO00_CAL), "--set", "c_vs_max=255", "--dry-run",
        ])
        out, err = self.decoded(result)
        self.assertEqual(result.returncode, 0, err)
        self.assertIn("c_vs_max", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
