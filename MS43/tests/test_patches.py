"""Тесты патчей MS43 (`MS43/tools/patches.py`)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from MS43.tools import checksum as ck
from MS43.tools import patches as pt

FIRMWARE = Path(__file__).resolve().parents[2] / "MS43" / "firmware"
REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"

#: Исходный патчлист: есть только в окружении разработчика (в репозиторий не
#: кладём — это файл сообщества; каталог из него уже извлечён и лежит рядом).
SOURCE_XDF = Path(os.environ.get("MS43_PATCHLIST_XDF", "/tmp/ecudefs/BMW/MS43_M54/"
                                 "Siemens_MS43_MS430069_Community_Patchlist_v2.9.2.xdf"))

#: Патч, который в патчлисте не имеет basedata (очистка области ISN).
NO_BASE_DATA_PATCHES = {
    "[PATCH] Clear Immobilizer Data",
    "[DATA] Remove Deprecated Launch Control",
    "[PATCH] Map Reduction",
    "[DATA] LC & RAL Default Values",
    "[DATA] MAF Based FL Default Values - M54B30",
}


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = pt.load_catalog()

    def test_catalog_shape(self) -> None:
        self.assertEqual(pt.EXPECTED_PATCHES, len(self.catalog.patches))
        self.assertEqual(
            pt.EXPECTED_ENTRIES,
            sum(len(patch.entries) for patch in self.catalog.patches),
        )
        self.assertEqual(46, len(self.catalog.symbols))
        self.assertEqual("MS430069 (полный образ 512 КиБ)", self.catalog.applies_to)

    def test_source_provenance_is_recorded(self) -> None:
        self.assertEqual(
            "ac7349125756548708c03327dc11ce920b93905a252374f25b78ebd054e6eadd",
            self.catalog.source_sha256,
        )
        self.assertIn("ms4x.net", self.catalog.source_url)
        self.assertTrue(self.catalog.source_file.endswith(".xdf"))

    def test_stock_images_verify_the_patchlist(self) -> None:
        """Все патчи, у которых есть basedata, обязаны совпасть на всех 18 стоках.

        Исключение — снятие старого LC: его basedata описывает состояние после
        патча v2.5, которого на чистом стоке нет (patchdata, наоборот, совпадает
        со стоком, поэтому патч на стоке — no-op).
        """
        for patch in self.catalog.patches:
            if patch.title == "[PATCH] Remove Deprecated Launch Control":
                self.assertEqual(0, patch.verified_images, patch.title)
                self.assertEqual(18, patch.checked_images, patch.title)
                continue
            if all(entry.base_data is None for entry in patch.entries):
                continue  # сверять нечего: в патчлисте нет basedata
            self.assertEqual(18, patch.verified_images, patch.title)
            self.assertEqual(18, patch.checked_images, patch.title)

    def test_groups_are_curated(self) -> None:
        """Категории патчлиста позиционные и перемешаны — группы задаём мы сами."""
        groups = {patch.group for patch in self.catalog.patches}
        self.assertEqual(17, len(groups))
        self.assertTrue(all(group.startswith("patch_") for group in groups))
        policy = json.loads(
            (FIRMWARE.parent / "policies" / "stage_policy.json").read_text(encoding="utf-8")
        )
        policy_groups = {entry["group"] for entry in policy["objects"].values()}
        self.assertEqual(
            {
                "patch_lc",
                "patch_mil_knk",
                "patch_mcluster",
                "patch_ds2",
                "patch_maf_fl",
                "patch_maf_hack",
                "patch_map_reduction",
                "patch_can",
            },
            groups & policy_groups,
            "группы каталога и политики разошлись",
        )

    def test_gates(self) -> None:
        by_title = {patch.title: patch for patch in self.catalog.patches}
        self.assertFalse(pt.gate(by_title["[PATCH] Checksum Bypass"])["offered"])
        self.assertFalse(
            pt.gate(by_title["[PATCH] Remove Deprecated Launch Control"])["offered"]
        )
        self.assertEqual(
            "immo_change", pt.gate(by_title["[PATCH] Immobilizer Bypass"])["declaration"]
        )

    def test_extract_is_reproducible_when_the_source_is_present(self) -> None:
        if not SOURCE_XDF.exists():  # pragma: no cover
            self.skipTest(f"нет исходного патчлиста {SOURCE_XDF}")
        again = pt.extract_catalog(SOURCE_XDF, extracted=self.catalog.extracted)
        self.assertEqual(self.catalog.as_dict(), again.as_dict())

    def test_report_lists_every_patch(self) -> None:
        text = pt.report_catalog(self.catalog)
        for patch in self.catalog.patches:
            self.assertIn(patch.title[:40], text)


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = pt.load_catalog()
        cls.image = REFERENCE.read_bytes()

    def test_stock_image_reports_applicable_and_one_inapplicable(self) -> None:
        states = {status.patch.title: status.state for status in pt.check_image(self.image, self.catalog)}
        self.assertEqual(27, len(states))
        for title, state in states.items():
            if title in (
                "[PATCH] Clear Immobilizer Data",  # область ISN в стоках уже пуста (0xFF)
                "[PATCH] Remove Deprecated Launch Control",  # patchdata = сток
                "[DATA] Remove Deprecated Launch Control",
            ):
                self.assertEqual("applied", state, title)
            else:
                self.assertEqual("applicable", state, title)

    def test_patches_dont_overlap_except_the_maf_hack_alternatives(self) -> None:
        offered = [p for p in self.catalog.patches if pt.gate(p).get("offered", True)]
        problems = pt.conflicts(offered)
        self.assertEqual(2, len(problems), problems)  # два байта: 0x3B4C3 и 0x3B4E7
        for problem in problems:
            self.assertIn("MAF Sensor Hack 2048kg/h", problem)
            self.assertIn("MAF Sensor Hack 4096kg/h", problem)


class ApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = pt.load_catalog()
        cls.image = REFERENCE.read_bytes()

    def _verify(self, image: bytes) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.bin"
            path.write_bytes(image)
            report = ck.verify_image(path)
        self.assertTrue(report.ok, [f"{r.name}: {r.stored:04X}!={r.computed:04X}" for r in report.failures])

    def test_single_patch_applies_and_verifies(self) -> None:
        result = pt.apply_patches(self.image, self.catalog, ["[PATCH] Launch Control & Rolling Anti Lag"])
        patch = self.catalog.patch("[PATCH] Launch Control & Rolling Anti Lag")
        for entry in patch.entries:
            self.assertEqual(entry.patch_data, result.image[entry.address : entry.end], entry.name)
        self._verify(result.image)
        # вне записей патча и позиций CRC ничего не поменялось
        allowed = {0x3C24, 0x3C25, 0x6FDE0, 0x6FDE1, 0x73FE0, 0x73FE1}
        for entry in patch.entries:
            allowed.update(range(entry.address, entry.end))
        changed = {i for i, (a, b) in enumerate(zip(self.image, result.image)) if a != b}
        self.assertTrue(changed)
        self.assertTrue(changed <= allowed, sorted(changed - allowed)[:5])

    def test_revert_restores_the_stock_byte_for_byte(self) -> None:
        titles = [
            "[PATCH] Launch Control & Rolling Anti Lag",
            "[PATCH] Ignition Cut Limiter",
            "[PATCH] M Cluster LED Support",
            "[DATA] M3 Cluster Default Values",
        ]
        applied = pt.apply_patches(self.image, self.catalog, titles)
        self._verify(applied.image)
        back = pt.revert_patches(applied.image, self.catalog, titles)
        self.assertEqual(self.image, back.image)

    def test_revert_is_refused_when_the_patchlist_has_no_basedata(self) -> None:
        declarations = {"immo_change", "immo_virginize"}
        for title in NO_BASE_DATA_PATCHES:
            applied = pt.apply_patches(
                self.image,
                self.catalog,
                [title],
                declarations=declarations,
                allow_gated=True,
            )
            with self.assertRaises(pt.PatchError) as caught:
                pt.revert_patches(applied.image, self.catalog, [title])
            self.assertIn("basedata", str(caught.exception))

    def test_immobilizer_patch_needs_a_declaration(self) -> None:
        with self.assertRaises(pt.PatchError) as caught:
            pt.apply_patches(self.image, self.catalog, ["[PATCH] Immobilizer Bypass"])
        self.assertIn("immo_change", str(caught.exception))
        result = pt.apply_patches(
            self.image,
            self.catalog,
            ["[PATCH] Immobilizer Bypass", "[PATCH] Immobilizer Bypass DTC Fix"],
            declarations={"immo_change"},
        )
        self._verify(result.image)

    def test_not_offered_patches_are_refused_unless_forced(self) -> None:
        with self.assertRaises(pt.PatchError) as caught:
            pt.apply_patches(self.image, self.catalog, ["[PATCH] Checksum Bypass"])
        self.assertIn("не предлагается", str(caught.exception))
        result = pt.apply_patches(
            self.image, self.catalog, ["[PATCH] Checksum Bypass"], allow_gated=True
        )
        self._verify(result.image)

    def test_conflicting_alternatives_are_refused(self) -> None:
        with self.assertRaises(pt.PatchError) as caught:
            pt.apply_patches(
                self.image,
                self.catalog,
                ["[PATCH] MAF Sensor Hack 2048kg/h", "[PATCH] MAF Sensor Hack 4096kg/h"],
            )
        self.assertIn("пересекаются", str(caught.exception))

    def test_a_modified_image_is_refused(self) -> None:
        """Если в образе уже правленые байты, патч не применяется."""
        broken = bytearray(self.image)
        patch = self.catalog.patch("[PATCH] Ignition Cut Limiter")
        entry = patch.entries[0]
        broken[entry.address] ^= 0xFF
        with self.assertRaises(pt.PatchError):
            pt.apply_patches(bytes(broken), self.catalog, [patch.title])

    def test_already_applied_patch_is_idempotent(self) -> None:
        first = pt.apply_patches(self.image, self.catalog, ["[PATCH] Ignition Cut Limiter"])
        again = pt.apply_patches(first.image, self.catalog, ["[PATCH] Ignition Cut Limiter"])
        self.assertEqual(first.image, again.image)
        self.assertEqual(0, again.changed_bytes)

    def test_all_offered_patches_apply_together(self) -> None:
        titles = [
            patch.title
            for patch in self.catalog.patches
            if pt.gate(patch).get("offered", True)
            and patch.title != "[PATCH] MAF Sensor Hack 4096kg/h"
        ]
        result = pt.apply_patches(
            self.image, self.catalog, titles, declarations={"immo_change", "immo_virginize"}
        )
        self._verify(result.image)
        self.assertEqual(("boot", "cal", "program"), result.regions)
        self.assertGreater(result.changed_bytes, 1000)

    def test_every_stock_image_accepts_the_offered_patches(self) -> None:
        """Все 18 стоков 430069 обязаны принимать предлагаемые патчи."""
        titles = [
            patch.title
            for patch in self.catalog.patches
            if pt.gate(patch).get("offered", True)
            and patch.title != "[PATCH] MAF Sensor Hack 4096kg/h"
        ]
        for path in sorted(FIRMWARE.glob("*.bin")):
            image = path.read_bytes()
            result = pt.apply_patches(
                image, self.catalog, titles, declarations={"immo_change", "immo_virginize"}
            )
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / path.name
                out.write_bytes(result.image)
                self.assertTrue(ck.verify_image(out).ok, path.name)

    def test_wrong_image_size_is_refused(self) -> None:
        with self.assertRaises(pt.PatchError):
            pt.apply_patches(b"\x00" * 0x10000, self.catalog, ["[PATCH] Alpha/N"])


class CliTests(unittest.TestCase):
    def test_list_and_check(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(0, pt.main(["--list"]))
        self.assertIn("Launch Control", captured.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(0, pt.main(["--check", str(REFERENCE)]))
        self.assertIn("applicable", captured.getvalue())

    def test_apply_cli_writes_a_verified_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "patched.bin"
            with contextlib.redirect_stdout(io.StringIO()):
                code = pt.main(
                    [
                        "--apply", str(REFERENCE),
                        "--patch", "[PATCH] Ignition Cut Limiter",
                        "--out", str(out),
                    ]
                )
            self.assertEqual(0, code)
            self.assertTrue(out.exists())
            self.assertTrue(ck.verify_image(out).ok)

    def test_gated_cli_refusal_is_an_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stderr(io.StringIO()):
                code = pt.main(
                    [
                        "--apply", str(REFERENCE),
                        "--patch", "[PATCH] Checksum Bypass",
                        "--out", str(Path(directory) / "x.bin"),
                    ]
                )
            self.assertEqual(2, code)


ARCHIVE = Path(__file__).resolve().parents[2] / "MS43" / "firmware_archive"
LC_TITLE = "[PATCH] Launch Control & Rolling Anti Lag"


class VersionGuardTests(unittest.TestCase):
    """Патчи каталога не применяются к другим версиям (REVIEW_CLAUDE_RU.md, задание 1)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = pt.load_catalog()
        cls.foreign = (ARCHIVE / "E46_330i__C155G50G_Ca430055_Stok.bin").read_bytes()

    def test_supported_software_comes_from_the_policy(self) -> None:
        self.assertEqual(("430069",), pt.supported_software())

    def test_apply_refuses_a_foreign_version(self) -> None:
        with self.assertRaises(pt.PatchError) as caught:
            pt.apply_patches(self.foreign, self.catalog, [LC_TITLE])
        self.assertIn("CAL_VERSION_UNSUPPORTED", str(caught.exception))

    def test_revert_refuses_a_foreign_version(self) -> None:
        with self.assertRaises(pt.PatchError) as caught:
            pt.revert_patches(self.foreign, self.catalog, [LC_TITLE])
        self.assertIn("CAL_VERSION_UNSUPPORTED", str(caught.exception))

    def test_supported_stock_still_applies(self) -> None:
        result = pt.apply_patches(REFERENCE.read_bytes(), self.catalog, [LC_TITLE])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patched.bin"
            path.write_bytes(result.image)
            self.assertTrue(ck.verify_image(path).ok)

    def test_report_image_prints_the_version_verdict(self) -> None:
        foreign = pt.report_image(self.foreign, self.catalog)
        self.assertIn("430055", foreign)
        self.assertIn("НЕ поддерживается", foreign)
        # и не разбирает basedata: «конфликты» на чужой версии — мусор
        self.assertIn("патчи не проверяются", foreign)
        self.assertNotIn("conflict", foreign)
        supported = pt.report_image(REFERENCE.read_bytes(), self.catalog)
        self.assertIn("430069", supported)
        self.assertIn("поддерживается", supported)
        self.assertNotIn("патчи не проверяются", supported)

    def test_check_cli_refuses_a_foreign_version_with_code_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "foreign.bin"
            path.write_bytes(self.foreign)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = pt.main(["--check", str(path)])
            self.assertEqual(2, code)
            self.assertIn("НЕ поддерживается", out.getvalue())
        code = pt.main(["--check", str(REFERENCE)])
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
