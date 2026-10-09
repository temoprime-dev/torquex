"""Тесты ядра патчинга MS43 (`MS43/tools/calibration.py`)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from MS43.tools import calibration as cal
from MS43.tools import checksum as ck
from MS43.tools import patches as pt
from MS43.tools.policy import load_policy

FIRMWARE = Path(__file__).resolve().parents[2] / "MS43" / "firmware"
REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"
LC_TITLE = "[PATCH] Launch Control & Rolling Anti Lag"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.image = REFERENCE.read_bytes()
        cls.policy = load_policy()
        cls.catalog = pt.load_catalog()

    def plan(self, requests, **kwargs):
        return cal.plan_changes(
            self.image, requests, policy=self.policy, patch_catalog=self.catalog, **kwargs
        )

    def codes(self, plan) -> list[str]:
        return [finding.code for finding in plan.findings]

    def verify(self, image: bytes, name: str = "x.bin") -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_bytes(image)
            self.assertTrue(ck.verify_image(path).ok)


class PlanTests(Base):
    def test_simple_scalar_plan(self) -> None:
        plan = self.plan([{"symbol": "c_vs_max_hys", "values": [6.0]}])
        self.assertEqual("PLANNED", plan.status)
        self.assertEqual(1, plan.changed_cells)
        symbol = plan.symbols[0]
        self.assertEqual("vmax", symbol.group)
        self.assertEqual(3, symbol.cells[0].before)
        self.assertEqual(6, symbol.cells[0].after)

    def test_unknown_symbol_is_rejected(self) -> None:
        plan = self.plan([{"symbol": "c_tq_unicorn", "values": [1]}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("SYMBOL_NOT_ALLOWED", self.codes(plan))

    def test_read_only_symbol_is_rejected(self) -> None:
        plan = self.plan([{"symbol": "ip_iga_tqr__n__maf", "values": [0.0] * 72}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("READ_ONLY", self.codes(plan))

    def test_symbol_needs_its_patch(self) -> None:
        plan = self.plan([{"symbol": "c_n_max_lc", "values": [4000]}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("PATCH_REQUIRED", self.codes(plan))
        with_patch = self.plan(
            [{"symbol": "c_n_max_lc", "values": [4000]}], patches=(LC_TITLE,)
        )
        self.assertEqual("PLANNED", with_patch.status)

    def test_bounds_are_enforced(self) -> None:
        plan = self.plan([{"symbol": "id_n_max_mt__gear", "values": [9000] * 6}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("VALUE_NOT_REPRESENTABLE", self.codes(plan))

    def test_quantization_error_is_enforced(self) -> None:
        # c_n_max_lc: шаг 32 об/мин; 4000 ложится точно, 33 — с ошибкой 3 %
        exact = self.plan([{"symbol": "c_n_max_lc", "values": [4000]}], patches=(LC_TITLE,))
        self.assertEqual("PLANNED", exact.status)
        coarse = self.plan([{"symbol": "c_n_max_lc", "values": [33]}], patches=(LC_TITLE,))
        self.assertEqual("REJECTED", coarse.status)
        self.assertIn("QUANTIZATION_TOO_COARSE", self.codes(coarse))

    def test_delta_limit_is_enforced(self) -> None:
        # ip_tq_max__n__pvs_cor_rpl: max_delta_percent = 20
        plan = self.plan([{"symbol": "ip_tq_max__n__pvs_cor_rpl", "values": [400.0] * 64}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("DELTA_TOO_LARGE", self.codes(plan))

    def test_cell_limit_is_enforced(self) -> None:
        """Лимит ячеек на пакет: проверяем на политике с искусственно низким лимитом."""
        import dataclasses

        entry = self.policy.objects["id_n_max_mt__gear"]
        tight = dataclasses.replace(entry, max_cells_per_patch=2)
        policy = dataclasses.replace(
            self.policy, objects={**self.policy.objects, "id_n_max_mt__gear": tight}
        )
        plan = cal.plan_changes(
            self.image,
            [{"symbol": "id_n_max_mt__gear", "values": [7000] * 6}],
            policy=policy,
            patch_catalog=self.catalog,
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("TOO_MANY_CELLS", self.codes(plan))

    def test_table_requires_all_cells(self) -> None:
        plan = self.plan([{"symbol": "id_n_max_mt__gear", "values": [7000]}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("CELL_COUNT", self.codes(plan))

    def test_none_keeps_the_current_cell(self) -> None:
        plan = self.plan(
            [{"symbol": "id_n_max_mt__gear", "values": [7000, None, None, None, None, 7000]}]
        )
        self.assertEqual("PLANNED", plan.status)
        self.assertEqual(2, plan.changed_cells)

    def test_monotonic_table_is_enforced(self) -> None:
        entry = self.policy.objects["id_maf_tab__v_maf"]
        size = entry.element_size_bits // 8
        values = []
        for index in range(entry.rows * entry.columns):
            raw = int.from_bytes(
                self.image[entry.address + index * size : entry.address + (index + 1) * size],
                "little",
            )
            values.append(entry.scale_a * raw + entry.scale_b)
        self.assertGreater(values[-1], values[0], "стоковая кривая ДМРВ должна расти")
        broken = list(values)
        broken[128] = values[127] - 20.0  # ломаем монотонность, но в пределах Δ30 %
        plan = self.plan([{"symbol": "id_maf_tab__v_maf", "values": broken}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("NOT_MONOTONIC", self.codes(plan))
        good = list(values)
        good[-1] = good[-2] + 10.0
        plan = self.plan([{"symbol": "id_maf_tab__v_maf", "values": good}])
        self.assertEqual("PLANNED", plan.status, self.codes(plan))

    def test_duplicate_symbol_is_rejected(self) -> None:
        plan = self.plan(
            [
                {"symbol": "c_vs_max_hys", "values": [6.0]},
                {"symbol": "c_vs_max_hys", "values": [7.0]},
            ]
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("DUPLICATE_SYMBOL", self.codes(plan))

    def test_broken_source_crc_is_rejected(self) -> None:
        broken = bytearray(self.image)
        broken[0x70100] ^= 0x55
        plan = cal.plan_changes(
            bytes(broken),
            [{"symbol": "c_vs_max_hys", "values": [6.0]}],
            policy=self.policy,
            patch_catalog=self.catalog,
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("SOURCE_CRC_INVALID", self.codes(plan))

    def test_empty_plan_is_rejected(self) -> None:
        plan = self.plan([])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("NOTHING_TO_DO", self.codes(plan))

    def test_plan_carries_provenance(self) -> None:
        plan = self.plan([{"symbol": "c_vs_max_hys", "values": [6.0]}])
        symbol = plan.symbols[0]
        self.assertTrue(symbol.source)
        self.assertTrue(symbol.note)
        self.assertEqual("vmax", symbol.group)


class ApplyTests(Base):
    def test_apply_changes_only_cells_and_crc(self) -> None:
        plan = self.plan(
            [
                {"symbol": "id_n_max_mt__gear", "values": [7000] * 6},
                {"symbol": "c_vs_max_hys", "values": [6.0]},
            ]
        )
        result = cal.apply_plan(self.image, plan, patch_catalog=self.catalog)
        self.verify(result.image)
        allowed = set(cal.crc_storage_offsets(len(self.image)))
        for symbol in plan.symbols:
            for cell in symbol.cells:
                size = max(1, symbol.entry.element_size_bits // 8)
                allowed.update(range(cell.address, cell.address + size))
        changed = {i for i, (a, b) in enumerate(zip(self.image, result.image)) if a != b}
        self.assertTrue(changed)
        self.assertTrue(changed <= allowed, sorted(changed - allowed)[:5])
        self.assertEqual(7, result.changed_cells)
        self.assertEqual(1, len(result.crc_words))
        self.assertEqual("cal", result.crc_words[0][0])

    def test_byte_level_result(self) -> None:
        plan = self.plan([{"symbol": "c_vs_max_hys", "values": [6.0]}])
        result = cal.apply_plan(self.image, plan, patch_catalog=self.catalog)
        self.assertEqual(6, result.image[0x70DD9])
        self.assertEqual(3, self.image[0x70DD9])

    def test_patches_are_applied_before_calibration(self) -> None:
        """Дефолты [DATA]-патча не должны затирать запрошенное значение."""
        plan = self.plan(
            [{"symbol": "c_n_max_lc", "values": [4000]}], patches=(LC_TITLE,)
        )
        result = cal.apply_plan(self.image, plan, patch_catalog=self.catalog)
        self.verify(result.image)
        self.assertEqual(125, result.image[0x7131B])  # сырое: 125 * 32 = 4000 об/мин
        self.assertEqual(LC_TITLE, result.patches[0])
        boot_before = self.image[0x3C24:0x3C26]
        self.assertEqual(boot_before, result.image[0x3C24:0x3C26], "boot CRC не должен меняться")

    def test_apply_refuses_a_plan_for_another_image(self) -> None:
        plan = self.plan([{"symbol": "c_vs_max_hys", "values": [6.0]}])
        other = FIRMWARE / "Siemens_MS43_MS430069_E39_M54B30_EU4_LHD.bin"
        with self.assertRaises(cal.PatchError):
            cal.apply_plan(other.read_bytes(), plan, patch_catalog=self.catalog)

    def test_apply_refuses_rejected_plan(self) -> None:
        plan = self.plan([{"symbol": "c_tq_unicorn", "values": [1]}])
        with self.assertRaises(cal.PatchError):
            cal.apply_plan(self.image, plan, patch_catalog=self.catalog)

    def test_patch_only_plan_matches_the_patch_engine(self) -> None:
        plan = self.plan([], patches=("[PATCH] Ignition Cut Limiter",))
        result = cal.apply_plan(self.image, plan, patch_catalog=self.catalog)
        direct = pt.apply_patches(self.image, self.catalog, ["[PATCH] Ignition Cut Limiter"])
        self.assertEqual(direct.image, result.image)
        self.assertEqual(0, result.changed_cells)

    def test_conflicting_patch_pair_is_rejected(self) -> None:
        plan = self.plan(
            [],
            patches=(
                "[PATCH] MAF Sensor Hack 2048kg/h",
                "[PATCH] MAF Sensor Hack 4096kg/h",
            ),
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("PATCH_CONFLICT", self.codes(plan))

    def test_every_stock_image_accepts_a_stage_like_plan(self) -> None:
        requests = [
            {"symbol": "c_vs_max_hys", "values": [6.0]},
            {"symbol": "c_n_max_hys", "values": [32]},
            {"symbol": "id_n_max_mt__gear", "values": [7000] * 6},
            {"symbol": "id_n_max_max_mt__gear", "values": [7100] * 6},
        ]
        for path in sorted(FIRMWARE.glob("*.bin")):
            image = path.read_bytes()
            plan = cal.plan_changes(
                image, requests, policy=self.policy, patch_catalog=self.catalog
            )
            if path.name == "Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin":
                # известная аномалия: cal-сумма не сходится (CHECKSUM_RU.md §5),
                # ядро обязано отказаться, а не «чинить» её молча
                self.assertEqual("REJECTED", plan.status, path.name)
                self.assertIn("SOURCE_CRC_INVALID", self.codes(plan))
                continue
            self.assertEqual("PLANNED", plan.status, f"{path.name}: {self.codes(plan)}")
            result = cal.apply_plan(image, plan, patch_catalog=self.catalog)
            self.verify(result.image, path.name)


class CliTests(Base):
    def test_dry_run_prints_the_plan(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = cal.main(
                ["--image", str(REFERENCE), "--set", "c_vs_max_hys=6", "--dry-run"]
            )
        self.assertEqual(0, code)
        self.assertIn("PLANNED", captured.getvalue())

    def test_rejected_plan_returns_two(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = cal.main(
                ["--image", str(REFERENCE), "--set", "c_tq_unicorn=1", "--dry-run"]
            )
        self.assertEqual(2, code)

    def test_apply_writes_a_verified_file_and_a_json_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.bin"
            plan = Path(directory) / "plan.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = cal.main(
                    [
                        "--image", str(REFERENCE),
                        "--set", "c_vs_max_hys=6",
                        "--out", str(out),
                        "--json-out", str(plan),
                    ]
                )
            self.assertEqual(0, code)
            self.assertTrue(ck.verify_image(out).ok)
            payload = json.loads(plan.read_text(encoding="utf-8"))
            self.assertEqual("PLANNED", payload["status"])
            self.assertEqual("0x70DD9", payload["symbols"][0]["cells"][0]["address"])

    def test_set_parsing_with_none(self) -> None:
        values = cal._parse_set("id_n_max_mt__gear=7000,None,None,None,None,7000")
        self.assertEqual([7000.0, None, None, None, None, 7000.0], values["values"])


ARCHIVE = Path(__file__).resolve().parents[2] / "MS43" / "firmware_archive"

#: CRC-валидные стоки других версий (проверено `verify_image_bytes`) и 64 КиБ чтения.
FOREIGN_STOCKS = {
    "430037": ARCHIVE / "E46_330i__b137954g_Ca430037_Stok.bin",
    "430055": ARCHIVE / "E46_330i__C155G50G_Ca430055_Stok.bin",
    "430056": ARCHIVE / "E46_330i__c256954g_Ca430056_Stok.bin",
    "430066": ARCHIVE / "E39_530__c6668528_Ca430066_Stok.bin",
}
FOREIGN_CAL64 = {
    "430037": ARCHIVE / "E39_525__b137c56g_430037_Stok.Bin",
    "430056": ARCHIVE / "E39_525__c256b52d_430056_Stok.bin",
    "430064": ARCHIVE / "E46_330i__c464650r_Ca430064_Stok_Cal.bin",
    "430069": ARCHIVE / "E53__C769X54J_Ca430069_Stok_Cal.bin",
}


class CalVersionTests(Base):
    """Защита по версии калибровки (REVIEW_CLAUDE_RU.md, задание 1)."""

    def test_reference_is_430069_and_supported(self) -> None:
        self.assertEqual("430069", cal.read_cal_version(self.image))
        self.assertIsNone(cal.check_cal_version(self.image, self.policy))
        plan = self.plan([{"symbol": "c_vs_max_hys", "values": [6.0]}])
        self.assertEqual("430069", plan.cal_version)
        self.assertEqual("430069", plan.as_dict()["cal_version"])

    def test_64k_reads_are_read_at_their_own_offset(self) -> None:
        for version, path in FOREIGN_CAL64.items():
            image = path.read_bytes()
            self.assertEqual(0x10000, len(image), path.name)
            self.assertEqual(version, cal.read_cal_version(image), path.name)
        # 64 КиБ чтение нельзя записывать: ядро обязано отказывать с версией в тексте
        image = FOREIGN_CAL64["430056"].read_bytes()
        finding = cal.check_cal_version(image, self.policy)
        self.assertIsNotNone(finding)
        self.assertEqual("IMAGE_SIZE", finding.code)
        self.assertIn("430056", finding.message)
        plan = cal.plan_changes(image, [], policy=self.policy, patch_catalog=self.catalog)
        self.assertEqual("REJECTED", plan.status)
        self.assertEqual(["IMAGE_SIZE"], self.codes(plan))

    def test_other_versions_are_rejected_before_any_write(self) -> None:
        for version, path in FOREIGN_STOCKS.items():
            image = path.read_bytes()
            ok, problems = cal.verify_image_bytes(image)
            self.assertTrue(ok, f"{path.name}: {problems}")  # сток сам по себе целый
            plan = cal.plan_changes(
                image,
                [{"symbol": "c_vs_max_hys", "values": [6.0]}],
                policy=self.policy,
                patch_catalog=self.catalog,
            )
            self.assertEqual("REJECTED", plan.status, path.name)
            self.assertIn("CAL_VERSION_UNSUPPORTED", self.codes(plan), path.name)
            # отказ по версии стоит до CRC и до планирования: символов нет
            self.assertNotIn("SOURCE_CRC_INVALID", self.codes(plan), path.name)
            self.assertEqual((), plan.symbols, path.name)
            self.assertEqual(version, plan.cal_version, path.name)
            with self.assertRaises(cal.PatchError):
                cal.apply_plan(image, plan, patch_catalog=self.catalog)

    def test_unknown_version_string_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broken = bytearray(self.image)
            broken[0x70008:0x7000E] = b"430099"
            source = Path(directory) / "v.bin"
            fixed = Path(directory) / "v_fixed.bin"
            source.write_bytes(bytes(broken))
            ck.repair_image(source, fixed)  # суммы пересчитаны: отказ будет именно по версии
            image = fixed.read_bytes()
            self.assertEqual("430099", cal.read_cal_version(image))
            plan = cal.plan_changes(image, [], policy=self.policy, patch_catalog=self.catalog)
            self.assertEqual("REJECTED", plan.status)
            self.assertIn("CAL_VERSION_UNSUPPORTED", self.codes(plan))

    def test_unreadable_version_is_rejected(self) -> None:
        broken = bytearray(self.image)
        broken[0x70008:0x7000E] = b"\xff\xfe\xfd\xfc\xfb\xfa"
        self.assertIsNone(cal.read_cal_version(bytes(broken)))
        image = bytes(broken)
        plan = cal.plan_changes(image, [], policy=self.policy, patch_catalog=self.catalog)
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("CAL_VERSION_UNSUPPORTED", self.codes(plan))

    def test_supported_version_still_builds_on_every_stock(self) -> None:
        for path in sorted(FIRMWARE.glob("*.bin")):
            image = path.read_bytes()
            self.assertEqual("430069", cal.read_cal_version(image), path.name)


if __name__ == "__main__":
    unittest.main()
