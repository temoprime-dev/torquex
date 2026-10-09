"""Тесты политики записи MS43 (`MS43/tools/policy.py`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from MS43.tools import policy as pol
from MS43.tools.xdf import load_definition, XDF_512K

#: Имена, которые существуют в MS42 и которых не должно быть в политике MS43.
MS42_ONLY_NAMES = (
    "id_n_max_mt__nvs",
    "id_n_max_max_mt__nvs",
    "id_n_max_at__nvs",
    "c_vs_max_1",
    "ip_n_min_puc__tco_ac",
)


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.definition = load_definition(XDF_512K)
        cls.policy = pol.build_policy(cls.definition)

    def test_symbol_count_and_groups(self) -> None:
        calib = [e for e in self.policy.objects.values() if e.origin == "xdf"]
        patch = [e for e in self.policy.objects.values() if e.origin == "patchlist"]
        # 63 калибровки задания 1-2 + пакеты зажигание/топливо/ВАНос
        # (ip_tib, ip_cam_sp_tco_2_in/ex_pl__n__maf_iv) + 4 оси карт зажигания
        # (только чтение, нужны для потолка ip_iga_optm_tco_2) + 4 оси карт
        # топлива и фаз (только чтение, раскладка по зонам лога) + 7 счётчиков
        # DTC слоя «без катализатора» (группа dtc) + 3 счётчика дребезга
        # привода DISA (VIM) и клапана ХХ (ISA) слоя диагностики (шаг 2)
        self.assertEqual(84, len(calib))
        self.assertEqual(34, len(patch))
        groups = {entry.group for entry in self.policy.objects.values()}
        self.assertEqual(
            {"limits", "vmax", "puc", "full_load", "disa", "vanos", "fuel",
             "ignition", "maf", "torque", "config", "axes", "dtc",
             "patch_lc", "patch_lc", "patch_mcluster", "patch_ds2",
             "patch_mil_knk", "patch_maf_fl", "patch_maf_hack",
             "patch_map_reduction", "patch_can"},
            groups,
        )

    def test_every_symbol_exists_in_the_definition(self) -> None:
        for symbol, entry in self.policy.objects.items():
            if entry.origin == "xdf":
                self.assertIn(symbol, self.definition.objects, symbol)

    def test_patch_symbols_reference_real_patches(self) -> None:
        catalog = pol.load_patch_catalog()
        titles = set(catalog.titles())
        for symbol, entry in self.policy.objects.items():
            if entry.origin != "patchlist":
                continue
            self.assertIsNotNone(entry.requires_patch, symbol)
            self.assertIn(entry.requires_patch, titles, symbol)

    def test_patch_symbols_match_the_patchlist(self) -> None:
        catalog = pol.load_patch_catalog()
        by_title = {s.title: s for s in catalog.symbols}
        for symbol, entry in self.policy.objects.items():
            if entry.origin != "patchlist":
                continue
            source = by_title[symbol]
            self.assertEqual(source.address, entry.address, symbol)
            self.assertEqual(source.size_bits, entry.element_size_bits, symbol)
            self.assertEqual((source.rows, source.columns), (entry.rows, entry.columns), symbol)

    def test_loader_matches_the_builder(self) -> None:
        """Файл политики читается ядром так же, как собирается из каталога."""
        built = pol.build_policy()
        loaded = pol.load_policy()
        self.assertEqual(set(built.objects), set(loaded.objects))
        for symbol, entry in built.objects.items():
            other = loaded.objects[symbol]
            self.assertEqual(entry.address, other.address, symbol)
            self.assertEqual(entry.group, other.group, symbol)
            self.assertEqual(entry.element_size_bits, other.element_size_bits, symbol)
            self.assertEqual((entry.rows, entry.columns), (other.rows, other.columns), symbol)
            self.assertEqual((entry.minimum, entry.maximum), (other.minimum, other.maximum), symbol)
            self.assertEqual((entry.scale_a, entry.scale_b), (other.scale_a, other.scale_b), symbol)
            self.assertEqual(entry.max_delta_percent, other.max_delta_percent, symbol)
            self.assertEqual(entry.max_cells_per_patch, other.max_cells_per_patch, symbol)
            self.assertEqual(entry.require_monotonic, other.require_monotonic, symbol)
            self.assertEqual((entry.read_only, entry.requires_patch, entry.origin),
                             (other.read_only, other.requires_patch, other.origin), symbol)
            self.assertEqual(entry.equation, other.equation, symbol)
            self.assertEqual((entry.stock.samples, entry.stock.variants),
                             (other.stock.samples, other.stock.variants), symbol)

    def test_read_only_set_is_explicit(self) -> None:
        read_only = {s for s, e in self.policy.objects.items() if e.read_only}
        self.assertEqual(
            {
                "c_conf_cat",  # признак катализатора для ворот хлопков
                # оси карт зажигания: только чтение (интерполяция потолка
                # ip_iga_optm_tco_2 на сетку RON98, docs/PACKS_WEB_ANALYSIS_RU.md §1.2)
                "ldpm_n_14",
                "ldpm_maf_iga_2",
                "ldpm_n_4",
                "ldpm_n_5",
                "ldpm_maf_ivvt_2",
                "ldpm_n_11",
                "ldpm_maf_8",
                "ldpm_maf_iga_1",
                "id_maf_tab__v_maf (2048kg/h)",
                "id_maf_tab__v_maf (4096kg/h)",
                "id_maf_tab__v_maf_1__v_maf_2 (2048kg/h)",
                "id_maf_tab__v_maf_1__v_maf_2 (4096kg/h)",
                "ip_ti_fl__n__maf___afr",
                "ip_ti_fl__n__maf___lambda",
                "ldp_toil_led",
                "ldp_n_led",
                "CAN Bus Initialization Table",
                "CAN Bus ARBID Table",
                # модель момента: справочные, из TORQUE_RU.md §2
                "lv_mil_knk_lv_1",
                "lv_mil_knk_lv_2",
                "ip_tqi_pvs__n__pvs",
                "ip_pvs_tqi__n__tqi",
                "ip_tqi_maf__n__maf",
                "ip_tqfr__n__maf",
                "ip_iga_tqr__n__maf",
                "ip_iga_tqr_amt__n__maf",
                "ip_iga_optm_tco_1__n__maf",
                "ip_iga_optm_tco_2__n__maf",
                "ip_fac_tqd_cor__tqi_asr_can",
                "c_tq_max_msr",
            },
            read_only,
        )

    def test_every_entry_carries_a_source_and_a_note(self) -> None:
        for symbol, entry in self.policy.objects.items():
            self.assertTrue(entry.source.strip(), symbol)
            self.assertTrue(entry.note.strip(), symbol)
            self.assertTrue(entry.group, symbol)

    def test_stock_values_are_inside_the_bounds(self) -> None:
        for symbol, entry in self.policy.objects.items():
            self.assertLess(entry.minimum, entry.maximum, symbol)
            self.assertGreaterEqual(entry.stock.minimum + 1e-6, entry.minimum, symbol)
            self.assertLessEqual(entry.stock.maximum - 1e-6, entry.maximum, symbol)

    def test_addresses_are_absolute_and_in_range(self) -> None:
        """Адрес в политике — абсолютное смещение в образе 512 КиБ."""
        for symbol, entry in self.policy.objects.items():
            last = entry.address + entry.rows * entry.columns * max(1, entry.element_size_bits // 8)
            self.assertLessEqual(last, 0x80000, symbol)
            self.assertGreaterEqual(entry.address, 0, symbol)
            if entry.origin == "xdf":
                # символы определения указывают в калибровочную область 0x70000..
                self.assertGreaterEqual(entry.address, 0x70000, symbol)
                self.assertLess(entry.address, 0x80000, symbol)

    def test_ms43_names_and_not_ms42_names(self) -> None:
        for name in ("id_n_max_mt__gear", "id_maf_tab__v_maf", "ip_cam_sp_tco_2_in_fl__n"):
            self.assertIn(name, self.policy.objects, name)
        for name in MS42_ONLY_NAMES:
            self.assertNotIn(name, self.policy.objects, name)

    def test_stock_values_match_a_direct_measurement(self) -> None:
        """Политика обязана описывать те же стоки, что и прямое чтение образов."""
        images = [path.read_bytes() for path in sorted(pol.FIRMWARE_DIR.glob("*.bin"))]
        self.assertEqual(18, len(images))
        checks = {
            "c_n_min_vs_max": 800.0,
            "c_vs_max_add": 5.0,
            "c_vs_max_hys": 3.0,
            "c_tq_stnd": 400.0,
            "c_maf_min_diag": 4.0,
            "c_conf_tq_lim_gear": 0.0,
        }
        for symbol, expected in checks.items():
            entry = self.policy.objects[symbol]
            self.assertEqual(expected, entry.stock.minimum, symbol)
            self.assertEqual(expected, entry.stock.maximum, symbol)
            self.assertTrue(entry.stock.uniform, symbol)
            obj = self.definition.get(symbol)
            values = {round(v, 6) for image in images for v in self.definition.read_engineering(image, obj)}
            self.assertEqual({expected}, values, symbol)

    def test_rev_limiter_stock_range_is_what_the_images_contain(self) -> None:
        entry = self.policy.objects["id_n_max_mt__gear"]
        self.assertEqual(6464.0, entry.stock.minimum)
        self.assertEqual(6656.0, entry.stock.maximum)
        self.assertEqual(4, entry.stock.variants)
        self.assertEqual(6, entry.max_cells_per_patch)

    def test_maf_table_requires_monotonicity_and_picks_a_definition(self) -> None:
        entry = self.policy.objects["id_maf_tab__v_maf"]
        self.assertTrue(entry.require_monotonic)
        self.assertEqual(0, entry.xdf_index)
        self.assertEqual(256, entry.rows * entry.columns)
        self.assertEqual(30.0, entry.max_delta_percent)

    def test_catalyst_config_is_read_only(self) -> None:
        entry = self.policy.objects["c_conf_cat"]
        self.assertTrue(entry.read_only)
        self.assertIsNone(entry.max_cells_per_patch)
        # сток: 3 или 4 — оба варианта должны быть внутри границ
        self.assertEqual(3.0, entry.stock.minimum)
        self.assertEqual(4.0, entry.stock.maximum)

    def test_oil_pump_threshold_is_documented_in_the_file(self) -> None:
        self.assertEqual(6750, pol.OIL_PUMP_ACK_RPM)
        self.assertEqual(6750, self.policy.as_dict()["oil_pump_ack_rpm"])

    def test_build_is_deterministic(self) -> None:
        again = pol.build_policy(self.definition)
        self.assertEqual(
            json.dumps(self.policy.as_dict(), sort_keys=True),
            json.dumps(again.as_dict(), sort_keys=True),
        )

    def test_report_lists_every_symbol(self) -> None:
        text = pol.report(self.policy)
        for symbol in self.policy.objects:
            self.assertIn(symbol, text, symbol)
        self.assertIn("только чтение", text)


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.definition = load_definition(XDF_512K)
        cls.policy = pol.build_policy(cls.definition)

    def test_clean_policy_has_no_warnings(self) -> None:
        self.assertEqual([], pol.validate_policy(self.policy, self.definition))

    def test_a_symbol_missing_from_the_definition_is_refused(self) -> None:
        entry = self.policy.objects["c_vs_max_hys"]
        broken = pol.Policy(
            version=1, family="MS43", software=(), generated_note="",
            max_quantization_error_percent=1.0,
            objects={"no_such_symbol": replace(entry, symbol="no_such_symbol")},
        )
        with self.assertRaises(pol.PolicyError) as caught:
            pol.validate_policy(broken, self.definition)
        self.assertIn("нет в определении", str(caught.exception))

    def test_bounds_that_cut_the_stock_are_refused(self) -> None:
        entry = self.policy.objects["c_vs_max_mt_1"]
        narrow = replace(entry, maximum=200.0)  # сток доходит до 255
        broken = pol.Policy(
            version=1, family="MS43", software=(), generated_note="",
            max_quantization_error_percent=1.0, objects={entry.symbol: narrow},
        )
        with self.assertRaises(pol.PolicyError) as caught:
            pol.validate_policy(broken, self.definition)
        self.assertIn("выходит за границы", str(caught.exception))

    def test_impossible_bounds_for_the_raw_size_are_refused(self) -> None:
        entry = self.policy.objects["c_n_max_hys_max"]  # 8 бит по 32 об/мин
        impossible = replace(entry, maximum=9000.0)
        broken = pol.Policy(
            version=1, family="MS43", software=(), generated_note="",
            max_quantization_error_percent=1.0, objects={entry.symbol: impossible},
        )
        with self.assertRaises(pol.PolicyError) as caught:
            pol.validate_policy(broken, self.definition)
        self.assertIn("не представим", str(caught.exception))

    def test_duplicate_title_requires_an_explicit_xdf_index(self) -> None:
        duplicates = [
            title for title, items in self.definition.objects.items() if len(items) > 1
        ]
        self.assertTrue(duplicates, "в определении MS43 должно быть дублирующееся имя")
        template = pol.CATALOG[0]
        for title in duplicates:
            catalog = (replace(template, symbol=title, minimum=0, maximum=1, xdf_index=None),)
            with self.assertRaises(pol.PolicyError) as caught:
                pol.build_policy(self.definition, catalog=catalog)
            self.assertIn("xdf_index", str(caught.exception))
            catalog = (replace(template, symbol=title, minimum=0, maximum=1, xdf_index=0),)
            built = pol.build_policy(self.definition, catalog=catalog)
            self.assertIn(title, built.objects)

    def test_a_catalog_symbol_absent_from_the_definition_is_refused(self) -> None:
        catalog = (replace(pol.CATALOG[0], symbol="id_n_max__n"),)  # имя MS42
        with self.assertRaises(pol.PolicyError) as caught:
            pol.build_policy(self.definition, catalog=catalog)
        self.assertIn("нет в определении", str(caught.exception))


class FileTests(unittest.TestCase):
    def test_json_round_trip(self) -> None:
        policy = pol.build_policy(load_definition(XDF_512K))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "policy.json"
            pol.write_policy(policy, target)
            payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(118, len(payload["objects"]))
        self.assertEqual(1, payload["version"])
        self.assertEqual("MS43", payload["family"])
        # software — это версии, на которых политика проверена целиком (не «известные»)
        self.assertEqual(["430069"], payload["software"])
        entry = payload["objects"]["id_maf_tab__v_maf"]
        self.assertEqual("0x70EC6", entry["address"])
        self.assertEqual("256x1", entry["shape"])
        self.assertTrue(entry["require_monotonic"])

    def test_committed_policy_matches_the_catalog(self) -> None:
        """Файл в репозитории обязан совпадать с тем, что собирает каталог."""
        if not pol.DEFAULT_POLICY.exists():  # pragma: no cover
            self.skipTest("политика ещё не собрана")
        committed = json.loads(pol.DEFAULT_POLICY.read_text(encoding="utf-8"))
        built = pol.build_policy(load_definition(XDF_512K)).as_dict()
        self.assertEqual(built, committed)

    def test_cli_build_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "p.json"
            self.assertEqual(0, pol.main(["--build", "--report", "--out", str(out)]))
            self.assertTrue(out.exists())
        self.assertEqual(0, pol.main(["--report"]))


if __name__ == "__main__":
    unittest.main()
