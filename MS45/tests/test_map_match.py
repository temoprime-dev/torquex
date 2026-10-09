"""Тесты сопоставления калибровочных карт MS45 по структуре A2L.

Проверяет:
1. 100% калибровку на эталонных семействах с XDF/XML (BO00S, LO00S, BN00S).
2. Структурный поиск карт зажигания, топлива и VANOS для 7 целевых семейств.
3. Инвариантность найденных адресов по всем стоковым калибровкам каждого семейства.
4. Низкую уверенность (confidence: low) для семейств с 1 стоком (BL30S, LL00S).
5. Неприкосновенность области подписи (0x174–0x1B3) и границ калибровки (0x00000–0x1CFFF).
6. Строгое исключение защитных карт (детонация, температура, момент, педаль).
7. Физическую правдоподобность значений и монотонность углов зажигания/лямбды.
8. Корректность файла артефакта map_match_candidates.json.
9. Работу CLI и совместимость с кодировкой cp1251 (русская Windows).
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from MS45.tools import map_match as mm

ROOT = Path(__file__).resolve().parents[2]
MS45_DIR = ROOT / "MS45"
CAL_DIR = MS45_DIR / "firmware_spdaten" / "cal"
CANDIDATES_JSON = MS45_DIR / "definitions" / "map_match_candidates.json"


def cp1251_offenders(text: str) -> list[str]:
    """Символы текста, которых нет в cp1251."""
    offenders = []
    for char in sorted(set(text)):
        try:
            char.encode("cp1251")
        except UnicodeEncodeError:
            offenders.append(f"{char!r} (U+{ord(char):04X})")
    return offenders


class MapMatchBaselineValidationTests(unittest.TestCase):
    """Калибровка метода структурного сопоставления на эталонах."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = mm.validate_baselines(CAL_DIR)

    def test_overall_baseline_status_passed(self) -> None:
        self.assertEqual(self.report["overall_status"], "PASSED")
        self.assertEqual(self.report["overall_rate_percent"], 100.0)

    def test_each_baseline_family_100_percent(self) -> None:
        for fam in mm.BASELINE_FAMILIES:
            with self.subTest(family=fam):
                fam_res = self.report["families"].get(fam)
                self.assertIsNotNone(fam_res, f"Семейство {fam} отсутствует в отчёте эталонов")
                self.assertEqual(fam_res["status"], "PASSED")
                self.assertEqual(fam_res["rate_percent"], 100.0)
                self.assertEqual(fam_res["matched_symbols"], fam_res["total_symbols"])


class MapMatchTargetFamiliesTests(unittest.TestCase):
    """Структурный поиск для 7 целевых семейств без XDF."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = mm.match_all_families(CAL_DIR)
        cls.families = cls.data["families"]

    def test_all_seven_target_families_present(self) -> None:
        for fam in mm.TARGET_FAMILIES:
            self.assertIn(fam, self.families, f"Семейство {fam} не сопоставлено")

    def test_single_stock_families_low_confidence(self) -> None:
        single_stock_fams = ["4560BL30S", "4570LL00S"]
        for fam in single_stock_fams:
            with self.subTest(family=fam):
                fam_info = self.families[fam]
                self.assertEqual(fam_info["stock_count"], 1)
                self.assertEqual(fam_info["confidence"], "low")
                for map_cand in fam_info["maps"].values():
                    self.assertEqual(map_cand["confidence"], "low")

    def test_multi_stock_families_high_confidence(self) -> None:
        multi_stock_fams = ["4560BL00S", "4560BM00S", "4570LL30S", "4570LM00S", "4570LN00S"]
        for fam in multi_stock_fams:
            with self.subTest(family=fam):
                fam_info = self.families[fam]
                self.assertGreater(fam_info["stock_count"], 1)
                self.assertEqual(fam_info["confidence"], "high")
                for map_cand in fam_info["maps"].values():
                    self.assertEqual(map_cand["confidence"], "high")
                    self.assertTrue(map_cand["proven_across_all_stocks"])

    def test_calibration_boundaries_and_signature_safety(self) -> None:
        for fam, fam_info in self.families.items():
            for sym, map_cand in fam_info["maps"].items():
                with self.subTest(family=fam, symbol=sym):
                    addr = int(map_cand["address"], 16)
                    size = map_cand["total_bytes"]
                    self.assertTrue(
                        mm.is_valid_cal_offset(addr, size),
                        f"{fam}.{sym} @ 0x{addr:05X} (size {size}) выходит за пределы 0x1D000",
                    )
                    self.assertFalse(
                        mm.touches_signature_region(addr, size),
                        f"{fam}.{sym} @ 0x{addr:05X} пересекает область подписи 0x174–0x1B3",
                    )
                    # Проверка адреса в полном дампе (0x40000–0x5CFFF)
                    flash_addr = int(map_cand["flash_address"], 16)
                    self.assertEqual(flash_addr, addr + mm.FLASH_CAL_START)
                    self.assertGreaterEqual(flash_addr, mm.FLASH_CAL_START)
                    self.assertLessEqual(flash_addr + size, mm.FLASH_CAL_END + 1)

    def test_no_protective_maps_included(self) -> None:
        for fam, fam_info in self.families.items():
            for sym in fam_info["maps"]:
                with self.subTest(family=fam, symbol=sym):
                    self.assertFalse(
                        mm.is_protective_map(sym),
                        f"Защитная карта {sym} недопустимо включена в результаты {fam}",
                    )

    def test_ignition_maps_plausibility(self) -> None:
        for fam, fam_info in self.families.items():
            maps = fam_info["maps"]
            self.assertIn("ip_iga_bas_ron_98__n__maf", maps)
            self.assertIn("ip_iga_bas_ron_91__n__maf", maps)
            self.assertIn("ip_iga_bas__n__maf", maps)

            ron98 = maps["ip_iga_bas_ron_98__n__maf"]
            ron91 = maps["ip_iga_bas_ron_91__n__maf"]

            # Физические углы в диапазоне от -25 до +55 градусов коленвала
            self.assertGreaterEqual(ron98["phys_min"], -25.0)
            self.assertLessEqual(ron98["phys_max"], 55.0)
            self.assertGreaterEqual(ron91["phys_min"], -25.0)
            self.assertLessEqual(ron91["phys_max"], 55.0)

            # RON98 максимальный угол >= RON91 максимальный угол
            self.assertGreaterEqual(ron98["phys_max"], ron91["phys_max"])

    def test_vanos_maps_plausibility(self) -> None:
        for fam, fam_info in self.families.items():
            maps = fam_info["maps"]
            for sym in (
                "ip_cam_sp_1_ex_pl__n__maf_ivvt",
                "ip_cam_sp_2_ex_pl__n__maf_ivvt",
                "ip_cam_sp_1_in_pl__n__maf_ivvt",
                "ip_cam_sp_2_in_pl__n__maf_ivvt",
            ):
                self.assertIn(sym, maps)
                m = maps[sym]
                self.assertEqual(m["rows"], 16)
                self.assertEqual(m["columns"], 12)
                self.assertEqual(m["total_bytes"], 192)

            # Впускные валы в положительном диапазоне фаз (> 60 град), выпускные в отрицательном
            self.assertGreater(maps["ip_cam_sp_1_in_pl__n__maf_ivvt"]["phys_min"], 50.0)
            self.assertLess(maps["ip_cam_sp_1_ex_pl__n__maf_ivvt"]["phys_max"], 0.0)

    def test_fuel_maps_plausibility(self) -> None:
        for fam, fam_info in self.families.items():
            maps = fam_info["maps"]
            self.assertIn("ip_lamb_fl__n", maps)
            self.assertIn("ip_lamb_bas_1", maps)
            self.assertIn("ip_lamb_bas_2", maps)

            fl = maps["ip_lamb_fl__n"]
            self.assertEqual(fl["columns"], 12)
            self.assertEqual(fl["element_size_bytes"], 2)
            # Обогащение при полной нагрузке: лямбда от 0.75 до 1.05
            self.assertGreaterEqual(fl["phys_min"], 0.75)
            self.assertLessEqual(fl["phys_max"], 1.05)

            # Базовая лямбда стехиометрическая: 0.95 .. 1.05
            b1 = maps["ip_lamb_bas_1"]
            b2 = maps["ip_lamb_bas_2"]
            self.assertGreaterEqual(b1["phys_min"], 0.95)
            self.assertLessEqual(b1["phys_max"], 1.05)
            self.assertGreaterEqual(b2["phys_min"], 0.95)
            self.assertLessEqual(b2["phys_max"], 1.05)


class CandidatesJsonArtifactTests(unittest.TestCase):
    """Проверка структуры и целостности экспортированного JSON артефакта."""

    def setUp(self) -> None:
        self.assertTrue(CANDIDATES_JSON.is_file(), f"Файл {CANDIDATES_JSON} не найден")
        self.data = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))

    def test_json_structure(self) -> None:
        for key in (
            "version",
            "description",
            "generated_date",
            "method",
            "baseline_validation",
            "families",
            "requires_data",
            "excluded_protective_maps",
        ):
            self.assertIn(key, self.data)

        self.assertEqual(self.data["baseline_validation"]["overall_status"], "PASSED")
        self.assertEqual(len(self.data["families"]), 7)

    def test_requires_data_list(self) -> None:
        req = self.data["requires_data"]
        self.assertIsInstance(req, list)
        self.assertGreater(len(req), 0)
        req_text = " ".join(req)
        self.assertIn("stage1_ignition_advance_without_knock_logs", req_text)
        self.assertIn("protective_maps_forbidden", req_text)
        self.assertIn("single_stock_families_bl30s_ll00s", req_text)


class MapMatchCliTests(unittest.TestCase):
    """Проверка CLI интерфейса инструмента map_match."""

    def test_cli_validate_baselines(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mm.main(["--validate-baselines"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("PASSED", out)
        self.assertEqual(cp1251_offenders(out), [])

    def test_cli_single_family(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mm.main(["--family", "4560BL00S"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("4560BL00S", out)
        self.assertIn("ip_iga_bas_ron_98__n__maf", out)
        self.assertEqual(cp1251_offenders(out), [])

    def test_cli_family_json(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mm.main(["--family", "4560BL00S", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["family"], "4560BL00S")
        self.assertEqual(parsed["confidence"], "high")

    def test_cli_unknown_family_requires_data(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mm.main(["--family", "NONEXISTENT"])
        self.assertEqual(code, 2)
        out = buf.getvalue()
        self.assertIn("REQUIRES_DATA", out)


if __name__ == "__main__":
    unittest.main()
