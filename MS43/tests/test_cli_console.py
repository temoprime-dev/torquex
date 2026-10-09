"""CLI MS43 и консоль Windows: cp1251 и перенаправленный вывод.

Ревью (`MS43/REVIEW_CLAUDE_RU.md`, задание 2) поймало падение
``python3 MS43/tools/policy.py --report`` на русской Windows: консоль там
по умолчанию cp1251, символа ``≤`` в ней нет, и ``print`` падал с
``UnicodeEncodeError``.

Здесь две проверки:

* **содержимое** — всё, что CLI печатают в консоль, обязано кодироваться
  в cp1251 (иначе на Windows символ превратится в ``?`` или упадёт).  Тест
  вызывает ``main()`` с обычным ``io.StringIO``, где страховка
  ``console.prepare_stdout()`` ничего не делает, поэтому любой не-cp1251
  символ виден сразу;
* **поведение** — каждый CLI обязан успешно отработать с
  ``PYTHONIOENCODING=cp1251``, то есть в условиях русской Windows (там
  страховка подменяет неподходящий символ, но инструмент не падает).

Markdown-наряды и JSON-манифесты пишутся файлами в UTF-8 осознанно: это
документы, а не консольный вывод.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from MS43.tools import calibration as cal
from MS43.tools import checksum as ck
from MS43.tools import patches as pt
from MS43.tools import policy as pol
from MS43.tools import stages
from MS43.tools import xdf

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "MS43" / "firmware"
REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"
REQUEST = ROOT / "MS43" / "examples" / "stage1_request.example.json"


def cp1251_offenders(text: str) -> list[str]:
    """Символы текста, которых нет в cp1251 (для понятного сообщения теста)."""
    offenders = []
    for char in sorted(set(text)):
        try:
            char.encode("cp1251")
        except UnicodeEncodeError:
            offenders.append(f"{char!r} (U+{ord(char):04X})")
    return offenders


def decode_cp1251(data: bytes) -> str:
    """Расшифровать вывод CLI как cp1251 (Windows-консоль) с внятной ошибкой."""
    try:
        return data.decode("cp1251")
    except UnicodeDecodeError as error:  # pragma: no cover - защита от регрессии
        raise AssertionError(f"вывод CLI не является текстом в cp1251: {error}") from error


class ConsoleContentTests(unittest.TestCase):
    """Тексты отчётов не должны содержать символов вне cp1251."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.image = REFERENCE.read_bytes()

    def capture(self, target, args: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = target(args) if callable(target) else target.main(args)
        return code, buffer.getvalue()

    def assert_cp1251_clean(self, text: str, where: str) -> None:
        offenders = cp1251_offenders(text)
        if offenders:
            self.fail(f"{where}: символы вне cp1251 — {', '.join(offenders)}")

    def test_policy_report_and_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "policy.json"
            code, text = self.capture(pol.main, ["--build", "--report", "--out", str(out)])
            self.assertEqual(0, code)
            self.assert_cp1251_clean(text, "policy.py --build --report")
            self.assertTrue(out.exists())

    def test_policy_report_alone(self) -> None:
        code, text = self.capture(pol.main, ["--report"])
        self.assertEqual(0, code)
        self.assert_cp1251_clean(text, "policy.py --report")

    def test_checksum_report(self) -> None:
        code, text = self.capture(ck.main, [str(REFERENCE)])
        self.assertEqual(0, code)
        self.assert_cp1251_clean(text, "checksum.py")

    def test_xdf_report(self) -> None:
        code, text = self.capture(xdf.main, [])
        self.assertEqual(0, code)
        self.assert_cp1251_clean(text, "xdf.py")

    def test_patches_catalog_and_check(self) -> None:
        code, text = self.capture(pt.main, ["--list"])
        self.assertEqual(0, code)
        self.assert_cp1251_clean(text, "patches.py --list")
        code, text = self.capture(pt.main, ["--check", str(REFERENCE)])
        self.assertEqual(0, code)
        self.assert_cp1251_clean(text, "patches.py --check")

    def test_calibration_plan(self) -> None:
        code, text = self.capture(
            cal.main,
            ["--image", str(REFERENCE), "--set", "c_vs_max_hys=6", "--dry-run"],
        )
        self.assertEqual(0, code)
        self.assert_cp1251_clean(text, "calibration.py --dry-run")

    def test_calibration_refusal_messages(self) -> None:
        # Версия без политики (430099): отказ по версии и текст в cp1251.
        with tempfile.TemporaryDirectory() as directory:
            broken = bytearray(REFERENCE.read_bytes())
            broken[0x70008:0x7000E] = b"430099"
            raw = Path(directory) / "raw.bin"
            raw.write_bytes(bytes(broken))
            foreign = Path(directory) / "foreign.bin"
            ck.repair_image(raw, foreign)  # CRC пересчитаны: отказ именно по версии
            # отказ по версии уходит в stdout плана и в stderr — проверяем оба текста
            buffer = io.StringIO()
            errors = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
                code = cal.main(["--image", str(foreign), "--set", "c_vs_max_hys=6", "--dry-run"])
            self.assertNotEqual(0, code)
            self.assertIn("CAL_VERSION_UNSUPPORTED", buffer.getvalue() + errors.getvalue())
            self.assert_cp1251_clean(buffer.getvalue(), "calibration.py (отказ)")

    def test_calibration_covered_version_writes_and_stays_cp1251(self) -> None:
        # 430055 покрыта своей политикой: ядро больше не отказывает по версии.
        foreign = ROOT / "MS43" / "firmware_archive" / "E46_330i__C155G50G_Ca430055_Stok.bin"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cal.main(["--image", str(foreign), "--set", "c_vs_max_hys=6", "--dry-run"])
        self.assertEqual(0, code)
        self.assertIn("430055", buffer.getvalue())
        self.assert_cp1251_clean(buffer.getvalue(), "calibration.py (430055)")

    def test_stages_manifest_and_note_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "manifest.json"
            note = Path(directory) / "note.md"
            code, text = self.capture(
                stages.main,
                ["--stage", "stage1", "--bin", str(REFERENCE), "--request", str(REQUEST),
                 "--report", str(report), "--note", str(note)],
            )
            self.assertEqual(0, code)
            self.assert_cp1251_clean(text, "stages.py")
            self.assertTrue(report.exists() and note.exists())


class ConsoleBehaviourTests(unittest.TestCase):
    """Те же CLI в cp1251-консоли: без падений и трейсбеков."""

    COMMANDS = [
        ["MS43/tools/policy.py", "--report"],
        ["MS43/tools/checksum.py", "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"],
        ["MS43/tools/xdf.py"],
        ["MS43/tools/patches.py", "--list"],
        ["MS43/tools/patches.py", "--check",
         "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"],
        ["MS43/tools/calibration.py", "--image",
         "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin",
         "--set", "c_vs_max_hys=6", "--dry-run"],
        ["MS43/tools/stages.py", "--stage", "stage1", "--bin",
         "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin",
         "--request", "MS43/examples/stage1_request.example.json"],
    ]

    def run_cli(self, arguments: list[str]) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "cp1251"
        environment["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
        )

    def test_every_cli_survives_a_cp1251_console(self) -> None:
        for arguments in self.COMMANDS:
            with self.subTest(command=" ".join(arguments)):
                result = self.run_cli(arguments)
                self.assertEqual(0, result.returncode, result.stderr.decode("cp1251", "replace"))
                decode_cp1251(result.stdout)  # вывод — текст в cp1251, без трейсбеков

    def test_help_pages_survive_a_cp1251_console(self) -> None:
        for tool in ("checksum", "xdf", "patches", "policy", "calibration", "stages"):
            with self.subTest(tool=tool):
                result = self.run_cli([f"MS43/tools/{tool}.py", "--help"])
                self.assertEqual(0, result.returncode)
                decode_cp1251(result.stdout)


if __name__ == "__main__":
    unittest.main()
