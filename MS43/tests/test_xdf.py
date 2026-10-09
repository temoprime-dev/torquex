"""Тесты чтения определений MS43 (`MS43/tools/xdf.py`)."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from MS43.tools import xdf as mx

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "MS43" / "firmware"
REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"

#: Двадцать констант с дефектом имени переменной (`X0`, `X00`, `X000`, `X0000`).
ALIASED_CONSTANTS = (
    "c_t_cp_1",
    "c_t_cp_2",
    "c_t_cp_3",
    "c_t_cp_3_cat_var",
    "c_t_cycnr_is",
    "c_t_diagcps_maf",
    "c_t_diagcps_n_2",
    "c_t_dly_diagcps",
    "c_t_dly_sa",
    "c_t_efp",
    "c_t_efp_prev",
    "c_t_puc_tco_ex_plaus_diag",
    "c_t_pvs_grd_min_aj",
    "c_t_sawup_on",
    "c_t_sawup_on_is",
    "c_t_ti_ad_1",
    "c_t_ti_ad_1_cat_var",
    "c_t_tia_tco_diag",
    "c_tia_tco_ex_diag",
    "c_tout_imob",
)


class DefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.full = mx.load_definition(mx.XDF_512K)
        cls.cal64 = mx.load_definition(mx.XDF_64K)

    def test_both_definitions_load_without_losses(self) -> None:
        for definition in (self.full, self.cal64):
            self.assertEqual([], definition.problems, definition.path.name)
            self.assertEqual(3731, definition.object_count, definition.path.name)
            self.assertEqual(3730, len(definition.objects), definition.path.name)

    def test_address_bases(self) -> None:
        self.assertEqual(0x70000, self.full.base_offset)
        self.assertEqual(0x80000, self.full.region_size)
        self.assertEqual(0x00000, self.cal64.base_offset)
        self.assertEqual(0x10000, self.cal64.region_size)
        # один и тот же символ: адрес в определении один, файловое смещение разное
        self.assertEqual(0x850, self.full.get("c_t_cp_1").address)
        self.assertEqual(0x70850, self.full.file_offset(self.full.get("c_t_cp_1")))
        self.assertEqual(0x850, self.cal64.file_offset(self.cal64.get("c_t_cp_1")))

    def test_the_twenty_variable_aliases_are_exactly_known(self) -> None:
        for definition in (self.full, self.cal64):
            aliased = definition.aliased()
            self.assertEqual(20, len(aliased), definition.path.name)
            self.assertEqual(
                sorted(ALIASED_CONSTANTS), sorted(obj.title for obj in aliased)
            )
            for obj in aliased:
                self.assertEqual(1, len(obj.aliased_variables), obj.title)
                self.assertIn(obj.aliased_variables[0], {"X0", "X00", "X000", "X0000"})
                self.assertTrue(obj.equation.startswith("0.1*"), obj.title)
                # подстановка сохранила масштаб: 0.1 * raw
                self.assertAlmostEqual(0.1, obj.scaler.a, places=12)
                self.assertAlmostEqual(0.0, obj.scaler.b, places=12)

    def test_the_scale_factor_survives_the_alias(self) -> None:
        # c_t_cp_1: 16 бит, «s», диапазон 0.1..6553.5 = 0.1 * u16
        obj = self.full.get("c_t_cp_1")
        self.assertEqual(16, obj.element_size_bits)
        self.assertEqual(320.0, obj.to_engineering(3200))
        # c_t_cycnr_is: 8 бит, диапазон 0.1..25.5 = 0.1 * u8
        small = self.full.get("c_t_cycnr_is")
        self.assertEqual(8, small.element_size_bits)
        self.assertEqual(1.8, small.to_engineering(18))

    def test_ignoring_the_alias_would_break_the_declared_range(self) -> None:
        """Сырое значение не влезает в объявленный диапазон — фактор обязателен."""
        image = REFERENCE.read_bytes()
        obj = self.full.get("c_t_cp_3_cat_var")
        raw = self.full.read_raw(image, obj)
        self.assertEqual(65534, raw)
        self.assertGreater(raw, obj.range_high or 0.0)          # 65534 > 6553.5
        self.assertLessEqual(obj.to_engineering(raw), obj.range_high or 0.0)  # 6553.4

    def test_aliased_values_are_physical_in_every_stock_image(self) -> None:
        checked = 0
        for path in sorted(FIRMWARE.glob("*.bin")):
            image = path.read_bytes()
            for title in ALIASED_CONSTANTS:
                obj = self.full.get(title)
                for value in self.full.read_engineering(image, obj):
                    self.assertGreaterEqual(value, obj.range_low or 0.0, f"{title} в {path.name}")
                    self.assertLessEqual(value, obj.range_high or 0.0, f"{title} в {path.name}")
                    checked += 1
        self.assertEqual(20 * 18, checked)

    def test_table_axes_are_classified(self) -> None:
        tables = [o for items in self.full.objects.values() for o in items if o.kind == "table"]
        self.assertEqual(1469, len(tables))
        static = [o for o in tables if "x" in o.labels]
        linked = [o for o in tables if o.linked_axes]
        self.assertEqual(1064, len(static))
        self.assertEqual(23, len(linked))
        # связанные оси — это ссылки в бинарь, а не подписи
        maf = self.full.get("id_maf_tab__v_maf")
        self.assertEqual(("y",), maf.linked_axes)
        self.assertEqual(256, maf.element_count)
        self.assertEqual("kg/h", maf.units)

    def test_values_of_a_table_read_as_engine_data(self) -> None:
        image = REFERENCE.read_bytes()
        maf = self.full.get("id_maf_tab__v_maf")
        values = self.full.read_engineering(image, maf)
        self.assertEqual(256, len(values))
        self.assertTrue(all(right >= left for left, right in zip(values, values[1:])), "ДМРВ не монотонна")
        self.assertGreater(max(values), 500)

    def test_rev_limit_axes_exist_under_ms43_names(self) -> None:
        """Имена MS43 (а не MS42) обязаны быть в определении."""
        gear = self.full.get("id_n_max_mt__gear")
        self.assertEqual(6, gear.element_count)
        self.assertEqual("rpm", gear.units)
        self.assertEqual(32.0, gear.scaler.a)
        self.assertIn("id_maf_tab__v_maf", self.full.objects)
        self.assertNotIn("id_n_max__n", self.full.objects)  # имя MS42


class ProblemReportingTests(unittest.TestCase):
    """Неизвестную переменную нельзя «молча исправить»."""

    TEMPLATE = """<?xml version="1.0"?>
<XDFFORMAT version="1.80">
  <XDFHEADER>
    <deftitle>test</deftitle>
    <BASEOFFSET offset="0x70000" subtract="0" />
    <REGION type="0xFFFFFFFF" startaddress="0x0" size="0x80000" />
  </XDFHEADER>
  <XDFCONSTANT uniqueid="0x1">
    <title>bad_object</title>
    <EMBEDDEDDATA mmedtypeflags="0x02" mmedaddress="0x100" mmedelementsizebits="16" />
    <MATH equation="0.1*Y"><VAR id="Y" /></MATH>
  </XDFCONSTANT>
</XDFFORMAT>
"""

    def test_unknown_variable_becomes_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.xdf"
            path.write_text(self.TEMPLATE, encoding="utf-8")
            definition = mx.load_definition(path)
        self.assertEqual(0, definition.object_count)
        self.assertEqual(1, len(definition.problems))
        self.assertIn("Y", definition.problems[0])

    def test_alias_of_a_known_variable_is_not_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ok.xdf"
            path.write_text(self.TEMPLATE.replace("Y", "X000"), encoding="utf-8")
            definition = mx.load_definition(path)
        self.assertEqual([], definition.problems)
        obj = definition.get("bad_object")
        self.assertEqual(("X000",), obj.aliased_variables)
        self.assertEqual(0.1, obj.scaler.a)

    def test_cli_reports_aliases_and_returns_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = mx.main([str(mx.XDF_512K)])
        self.assertEqual(0, code)
        self.assertIn("объектов с подстановкой переменной", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
