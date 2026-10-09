"""Пути в артефактах MS45: прямые слэши, независимо от платформы.

Политика, манифест, наряд и отчёты — документы, которые собираются и на Windows,
и на Linux. Если путь попадёт в них в Windows-нотации, JSON перестанет совпадать
побайтово (на этом падал `test_build_is_deterministic_and_matches_file`), а диффы
будут «прыгать» при смене ОС.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from MS45.tools import checksums as ck
from MS45.tools import pathfmt
from MS45.tools import stages

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
BO00_CAL = MS45 / "firmware" / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"
REQUEST = MS45 / "examples" / "stage1_request.example.json"


class PathFmtTests(unittest.TestCase):
    """Сам нормализатор: принимает обе нотации, отдаёт POSIX."""

    def test_windows_notation(self) -> None:
        self.assertEqual(
            pathfmt.to_posix("definitions\\MS45 ECU Definitions.xml"),
            "definitions/MS45 ECU Definitions.xml",
        )
        self.assertEqual(pathfmt.to_posix("C:\\roms\\stock.bin"), "C:/roms/stock.bin")

    def test_posix_notation_unchanged(self) -> None:
        for value in ("definitions/x.xdf", "MS45/policies/stage_policy.json", "stock.bin"):
            self.assertEqual(pathfmt.to_posix(value), value)

    def test_path_object(self) -> None:
        self.assertEqual(pathfmt.to_posix(Path("MS45") / "tools"), "MS45/tools")

    def test_relative_posix(self) -> None:
        root = ROOT
        self.assertEqual(pathfmt.relative_posix(root / "MS45" / "x.xml", root), "MS45/x.xml")
        self.assertEqual(
            pathfmt.relative_posix("C:\\repo\\MS45\\x.xml", "C:\\repo"), "MS45/x.xml"
        )
        # вне корня — отдаём как есть, а не падаем
        self.assertEqual(pathfmt.relative_posix("/other/x.xml", root), "/other/x.xml")


class PathsInArtifactsTests(unittest.TestCase):
    """Пути, которые реально уходят в артефакты: отчёт, манифест, наряд."""

    def test_checksums_report_path_is_posix(self) -> None:
        report = ck.verify_image(BO00_CAL)
        self.assertNotIn("\\", report.path or "")
        self.assertNotIn("\\", json.dumps(report.as_dict(), ensure_ascii=False))
        self.assertTrue(report.ok)

    def test_checksums_report_accepts_windows_notation(self) -> None:
        report = ck.verify_bytes(BO00_CAL.read_bytes(), "roms\\stock.bin")
        self.assertEqual(report.path, "roms/stock.bin")
        self.assertIn("| stock.bin |", ck.report_as_markdown([report]))

    def test_manifest_and_note_source_name_are_posix(self) -> None:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = stages.build(
            "stage1", BO00_CAL.read_bytes(), request, source_name="roms\\stock.bin"
        )
        self.assertEqual(result.manifest["source_name"], "roms/stock.bin")
        note = stages.build_note(result, "roms\\stock.bin")
        self.assertIn("roms/stock.bin", note)
        self.assertNotIn("roms\\stock.bin", note)


class ArtifactLineEndingsTests(unittest.TestCase):
    """Артефакты пишутся с LF: файл, собранный на Windows, равен файлу из Linux."""

    def test_manifest_and_note_have_no_cr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "stage1.bin"
            manifest = Path(tmp) / "manifest.json"
            note = Path(tmp) / "naryad.md"
            code = stages.main([
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(REQUEST),
                "--out", str(out),
                "--manifest-out", str(manifest),
                "--note-out", str(note),
            ])
            self.assertEqual(code, 0)
            for artifact in (manifest, note):
                self.assertNotIn(b"\r", artifact.read_bytes(), artifact.name)

    def test_verify_report_has_no_cr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "stage1.bin"
            manifest = Path(tmp) / "manifest.json"
            report = Path(tmp) / "proverka.md"
            stages.main([
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(REQUEST),
                "--out", str(out),
                "--manifest-out", str(manifest),
            ])
            self.assertEqual(
                stages.main(["--verify-manifest", str(manifest), "--note-out", str(report)]), 0
            )
            self.assertNotIn(b"\r", report.read_bytes())

    def test_verify_report_has_no_backslash(self) -> None:
        """Отчёт проверки читают и на Windows: путей с обратным слэшем в нём нет."""
        verification = stages.ManifestVerification(
            "C:\\Users\\ivan\\manifest.json",
            "C:\\Users\\ivan\\stage1.bin",
            "VERIFIED",
            source_path="C:\\repo\\stock.bin",
        )
        verification.checks.append(stages.ManifestCheck("sha256", True, "сошёлся"))
        report = stages.verification_as_markdown(verification)
        self.assertNotIn("\\", report)
        for value in verification.as_dict().values():
            if isinstance(value, str):
                self.assertNotIn("\\", value)

    def test_checksums_json_has_no_cr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sums.json"
            self.assertEqual(ck.main([str(BO00_CAL), "--json-out", str(target)]), 0)
            self.assertNotIn(b"\r", target.read_bytes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
