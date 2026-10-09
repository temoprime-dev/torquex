# -*- coding: utf-8 -*-
"""Тесты единой точки входа `tools/torquex.py` (задание `docs/TASK_FINISH_RU.md` §4).

Проверяем то, что обещано клиенту:

* определение ЭБУ и версии по самому файлу — на всех четырёх платформах;
* сборка варианта 1 из готового состава: MS45/MS43 дают файл + наряд + манифест,
  MS42 пишет файл сам, MS41 выпускает файл через своё ядро (патчи → запись);
* жёсткие значения по умолчанию: хлопки без `--no-catalyst`/`--legality` не
  выпускаются вовсе (отказ до сборки);
* откат на сток: сверка `source_sha256`, проверка выпущенного файла, суммы
  восстановленного образа и отказ, если сток не тот;
* человекочитаемые отказы: известный код объясняется, неизвестный не выдумывается.

Тесты не пишут в репозиторий: все выходные файлы идут во временный каталог.
Стоки и примеры берутся из репозитория; VIN, логи и файлы клиентов не
используются.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from MS45.tests.support import unknown_family_image  # noqa: E402
from tools import refusals  # noqa: E402
from tools import torquex  # noqa: E402

MS45_STOCK = ROOT / "MS45" / "firmware" / "MS45.0_456BO00S_E83"
MS43_STOCK = ROOT / "MS43" / "firmware" / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"
MS42_STOCK = ROOT / "MS42" / "firmware" / "Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin"
MS41_STOCK = (
    ROOT / "MS41" / "firmware" / "MS41_1429861_E36_320i_M52B20_0060041010000_full_A.bin"
)


def run_cli(argv: list[str]) -> tuple[int, str]:
    """Запустить CLI и вернуть (код возврата, весь вывод)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = torquex.main(argv)
    return code, buffer.getvalue()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DetectionTests(unittest.TestCase):
    def test_ms45_stock_is_recognised_by_family(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        code, output = run_cli(["detect", str(stock)])
        self.assertEqual(code, 0, output)
        self.assertIn("ЭБУ: MS45", output)
        self.assertIn("4560BO00S", output)
        self.assertIn("поддержано: да", output)

    def test_ms43_stock_is_recognised_by_cal_version(self) -> None:
        code, output = run_cli(["detect", str(MS43_STOCK)])
        self.assertEqual(code, 0, output)
        self.assertIn("ЭБУ: MS43", output)
        self.assertIn("430069", output)

    def test_ms42_and_ms41_stocks_are_told_apart(self) -> None:
        code, output = run_cli(["detect", str(MS42_STOCK)])
        self.assertEqual(code, 0, output)
        self.assertIn("ЭБУ: MS42", output)
        self.assertIn("0110C6", output)

        code, output = run_cli(["detect", str(MS41_STOCK)])
        self.assertEqual(code, 0, output)
        self.assertIn("ЭБУ: MS41", output)

    def test_unknown_family_is_refused_with_code(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "unknown.bin"
            image.write_bytes(unknown_family_image(stock.read_bytes()))
            code, output = run_cli(["detect", str(image)])
            self.assertEqual(code, 2)
            self.assertIn("CAL_VERSION_UNSUPPORTED", output)
            self.assertIn("This ECU version is not supported", output)

    def test_foreign_size_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.bin"
            junk.write_bytes(b"\x00" * 1234)
            code, output = run_cli(["detect", str(junk)])
            self.assertEqual(code, 2)
            self.assertIn("UNSUPPORTED_IMAGE", output)


class BuildTests(unittest.TestCase):
    def test_ms45_variant1_builds_file_and_note(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            note = Path(tmp) / "note.md"
            manifest = Path(tmp) / "manifest.json"
            code, output = run_cli([
                "build", "--file", str(stock), "--variant", "v1",
                "--out", str(out), "--note", str(note), "--manifest", str(manifest),
            ])
            self.assertEqual(code, 0, output)
            self.assertIn("BUILT", output)
            self.assertTrue(out.exists())
            self.assertTrue(note.exists() and manifest.exists())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_sha256"], sha256(stock))
            # Сток не тронут, выпущен файл с другой суммой.
            self.assertEqual(sha256(stock), payload["source_sha256"])
            self.assertNotEqual(sha256(out), payload["source_sha256"])

    def test_ms43_variant1_builds_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ms43.bin"
            code, output = run_cli([
                "build", "--file", str(MS43_STOCK), "--variant", "v1", "--out", str(out),
            ])
            self.assertEqual(code, 0, output)
            self.assertIn("готовый файл", output)
            self.assertTrue(out.exists())

    def test_ms42_and_ms41_variant1_produce_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ms42_out = Path(tmp) / "ms42.bin"
            code, output = run_cli([
                "build", "--file", str(MS42_STOCK), "--variant", "v1", "--out", str(ms42_out),
            ])
            self.assertEqual(code, 0, output)
            self.assertTrue(ms42_out.exists(), output)

            ms41_out = Path(tmp) / "ms41.bin"
            code, output = run_cli([
                "build", "--file", str(MS41_STOCK), "--variant", "v1", "--out", str(ms41_out),
            ])
            self.assertEqual(code, 0, output)
            self.assertTrue(ms41_out.exists(), output)
            self.assertTrue(ms41_out.with_name(ms41_out.name + ".patches.json").exists())

    def test_variant_requires_logs_is_explained_not_guessed(self) -> None:
        code, output = run_cli([
            "build", "--file", str(MS42_STOCK), "--variant", "v2",
            "--out", str(Path(tempfile.gettempdir()) / "never.bin"),
        ])
        self.assertEqual(code, 2)
        self.assertIn("VARIANT_REQUIRES_LOGS", output)
        self.assertIn("Вариант 2/3", output)
        self.assertIn("Variants 2/3", output)

    def test_pops_need_no_catalyst_and_legality(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            request.write_text(
                json.dumps({
                    "intents": [{"kind": "pops", "delay_s": 0.5}],
                    # Температурный риск подтверждает сам клиент: генератор
                    # никогда не подставляет подтверждения за него.
                    "acknowledge_temperature": True,
                }),
                encoding="utf-8",
            )
            out = Path(tmp) / "out.bin"
            base = ["build", "--file", str(stock), "--request", str(request), "--out", str(out)]

            code, output = run_cli(base)
            self.assertEqual(code, 2)
            self.assertIn("NO_CATALYST_REQUIRED", output)
            self.assertFalse(out.exists())

            code, output = run_cli(base + ["--no-catalyst"])
            self.assertEqual(code, 2)
            self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", output)
            self.assertFalse(out.exists())

            code, output = run_cli(base + ["--no-catalyst", "--legality"])
            self.assertEqual(code, 0, output)
            self.assertTrue(out.exists())

    def test_dry_run_writes_nothing(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            code, output = run_cli([
                "build", "--file", str(stock), "--variant", "v1",
                "--out", str(out), "--dry-run",
            ])
            self.assertEqual(code, 0, output)
            self.assertIn("dry-run", output)
            self.assertFalse(out.exists())


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_the_stock_and_checks_crc(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "built.bin"
            manifest = Path(tmp) / "manifest.json"
            code, output = run_cli([
                "build", "--file", str(stock), "--variant", "v1",
                "--out", str(out), "--manifest", str(manifest),
            ])
            self.assertEqual(code, 0, output)

            restored = Path(tmp) / "restored.bin"
            code, output = run_cli([
                "rollback", "--manifest", str(manifest), "--stock", str(stock),
                "--written", str(out), "--out", str(restored),
            ])
            self.assertEqual(code, 0, output)
            self.assertEqual(restored.read_bytes(), stock.read_bytes())
            self.assertIn("контрольные суммы MS45: ok", output)

    def test_rollback_refuses_a_foreign_stock(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        foreign = sorted(MS45_STOCK.glob("*.bin"))[1]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            out = Path(tmp) / "built.bin"
            code, _ = run_cli([
                "build", "--file", str(stock), "--variant", "v1",
                "--out", str(out), "--manifest", str(manifest),
            ])
            self.assertEqual(code, 0)
            restored = Path(tmp) / "restored.bin"
            code, output = run_cli([
                "rollback", "--manifest", str(manifest), "--stock", str(foreign),
                "--out", str(restored),
            ])
            self.assertEqual(code, 2)
            self.assertIn("STOCK_MISMATCH", output)
            self.assertFalse(restored.exists())

    def test_rollback_refuses_a_broken_result(self) -> None:
        stock = sorted(MS45_STOCK.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            out = Path(tmp) / "built.bin"
            code, _ = run_cli([
                "build", "--file", str(stock), "--variant", "v1",
                "--out", str(out), "--manifest", str(manifest),
            ])
            self.assertEqual(code, 0)
            damaged = Path(tmp) / "damaged.bin"
            data = bytearray(out.read_bytes())
            data[-1] ^= 0xFF
            damaged.write_bytes(bytes(data))
            restored = Path(tmp) / "restored.bin"
            code, output = run_cli([
                "rollback", "--manifest", str(manifest), "--stock", str(stock),
                "--written", str(damaged), "--out", str(restored),
            ])
            self.assertEqual(code, 2)
            self.assertIn("MANIFEST_MISMATCH", output)


class RefusalTextTests(unittest.TestCase):
    def test_every_message_has_both_languages(self) -> None:
        for code, entry in refusals.MESSAGES.items():
            with self.subTest(code=code):
                for lang in ("ru", "en"):
                    self.assertIn(lang, entry)
                    title, action = entry[lang]
                    self.assertTrue(title.strip(), code)
                    self.assertTrue(action.strip(), code)

    def test_known_code_is_explained(self) -> None:
        code, output = run_cli(["explain", "CAL_VERSION_UNSUPPORTED"])
        self.assertEqual(code, 0)
        self.assertIn("Версия ЭБУ не поддерживается", output)

    def test_english_explanation(self) -> None:
        code, output = run_cli(["--lang", "en", "explain", "CAL_VERSION_UNSUPPORTED"])
        self.assertEqual(code, 0)
        self.assertIn("This ECU version is not supported", output)

    def test_unknown_code_is_not_invented(self) -> None:
        code, output = run_cli(["explain", "TOTALLY_UNKNOWN"])
        self.assertEqual(code, 1)
        self.assertEqual(output.strip(), "TOTALLY_UNKNOWN")

    def test_variants_are_described(self) -> None:
        code, output = run_cli(["variants"])
        self.assertEqual(code, 0)
        for key in ("v1", "v2", "v3"):
            self.assertIn(key, output)
        self.assertIn("не получишь", output)


if __name__ == "__main__":
    unittest.main()
