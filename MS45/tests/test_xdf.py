"""Тесты разбора определений MS45 (``MS45/tools/xdf.py``).

Проверяется не только разбор XML, но и **соответствие определений реальным
стоковым образам**: адреса, размеры элементов, порядок байт и формулы.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from MS45.tools import xdf as xd

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
FIRMWARE = MS45 / "firmware"
SPDATEN = MS45 / "firmware_spdaten" / "cal"
FULL_DUMP = MS45 / "firmware_dumps" / "MS45.0_0044560BN_fullread_ORI.bin"

BO00_FILES = sorted((FIRMWARE / "MS45.0_456BO00S_E83").glob("*.bin")) + sorted(
    SPDATEN.glob("MS45.0_0044560BO00S*.bin")
)
LO00_FILES = sorted(SPDATEN.glob("MS45.1_0044570LO00S*.bin"))

#: Контракт «документ ↔ определение ↔ данные»: адреса из CALIBRATION_MAP_RU.md.
EXPECTED_ADDRESSES = {
    "4560BO00S": {
        "c_vs_max": (0x18DC, 8),
        "c_n_lim_min": (0x5AE2, 16),
        "c_t_min_pu": (0x5CAA, 16),
        "c_t_min_pu_vs": (0x5CB0, 16),
        "c_t_min_pu_rng_l": (0x5CAE, 16),
        "c_t_min_pu_msr": (0x5CAC, 16),
        "id_n_max_mt": (0x60A2, 16),
        "id_n_max_h_mt": (0x6090, 16),
        "id_n_max_at": (0x605A, 16),
        "id_n_max_h_at": (0x607E, 16),
        "id_n_max_amt": (0x6048, 16),
        "id_n_max_h_amt": (0x606C, 16),
        "ip_n_min_puc__tco__gr_mt": (0xB3A6, 8),
        "ip_t_min_pu__n__tco": (0x10AF8, 16),
        "val_mo3_swi_cal_cks": (0x5803, 8),
        "val_mo3_cal_cks_high": (0x5820, 32),
        "val_mo3_cal_cks_low": (0x5824, 32),
    },
    "4570LO00S": {
        "c_vs_max": (0x1944, 8),
        "c_n_lim_min": (0x5B42, 16),
        "c_t_min_pu": (0x5D36, 16),
        "c_t_min_pu_vs": (0x5D3C, 16),
        "c_t_min_pu_rng_l": (0x5D3A, 16),
        "id_n_max_mt": (0x612E, 16),
        "id_n_max_h_mt": (0x611C, 16),
        "ip_n_min_puc__tco__gr_mt": (0xB6E6, 8),
        "val_mo3_swi_cal_cks": (0x57BF, 8),
        "val_mo3_cal_cks_high": (0x57DC, 32),
        "val_mo3_cal_cks_low": (0x57E0, 32),
    },
}


class TestDefinitionLoading(unittest.TestCase):
    """Загрузка определений и их метаданные."""

    def test_partial_and_full_variants(self) -> None:
        partial = xd.load_definition(family="4560BO00S", variant="116K")
        full = xd.load_definition(family="4560BO00S", variant="1024K")
        self.assertEqual(partial.base_offset, 0)
        self.assertEqual(full.base_offset, 0x40000)
        self.assertEqual(partial.cal_region, (0, 0x1D000))
        self.assertEqual(full.cal_region, (0x40000, 0x1D000))
        self.assertEqual(set(partial.objects), set(full.objects))

    def test_object_counts(self) -> None:
        bo = xd.load_definition(family="4560BO00S", variant="116K")
        lo = xd.load_definition(family="4570LO00S", variant="116K")
        self.assertEqual(len(bo.objects), 5456)
        self.assertEqual(len(lo.objects), 5705)
        self.assertEqual(sum(1 for o in bo.objects.values() if o.kind == "table"), 1795)
        self.assertEqual(sum(1 for o in lo.objects.values() if o.kind == "table"), 1820)

    def test_every_object_inside_calibration(self) -> None:
        for family in ("4560BO00S", "4570LO00S"):
            for variant in ("116K", "1024K"):
                definition = xd.load_definition(family=family, variant=variant)
                self.assertEqual(definition.objects_outside_cal(), [], f"{family} {variant}")

    def test_no_duplicate_names(self) -> None:
        for family in ("4560BO00S", "4570LO00S"):
            definition = xd.load_definition(family=family)
            self.assertEqual(definition.duplicates, (), family)

    def test_degenerate_equations_are_marked(self) -> None:
        definition = xd.load_definition(family="4560BO00S")
        self.assertEqual(definition.degenerate_count, 9)
        for obj in definition.objects.values():
            if not obj.scaling_usable:
                self.assertIn("0.000000", obj.equation)


class TestAddressContract(unittest.TestCase):
    """Адреса и разрядность из карты калибровки совпадают с определением."""

    def test_expected_addresses(self) -> None:
        for family, expected in EXPECTED_ADDRESSES.items():
            definition = xd.load_definition(family=family)
            for title, (address, bits) in expected.items():
                obj = definition.find(title)
                self.assertEqual(obj.address, address, f"{family}:{title} адрес")
                self.assertEqual(obj.element_size_bits, bits, f"{family}:{title} разрядность")

    def test_table_shapes(self) -> None:
        bo = xd.load_definition(family="4560BO00S")
        self.assertEqual((bo.find("id_n_max_mt").rows, bo.find("id_n_max_mt").columns), (1, 9))
        self.assertEqual(
            (bo.find("ip_n_min_puc__tco__gr_mt").rows, bo.find("ip_n_min_puc__tco__gr_mt").columns),
            (6, 13),
        )
        self.assertEqual(
            (bo.find("ip_t_min_pu__n__tco").rows, bo.find("ip_t_min_pu__n__tco").columns),
            (8, 6),
        )

    def test_mo3_and_crc_symbols_cover_our_locations(self) -> None:
        """Символы сумм в определении совпадают с найденными по данным местами."""
        bo = xd.load_definition(family="4560BO00S")
        self.assertEqual(bo.find("val_mo3_cal_cks_high").address, 0x5820)
        self.assertEqual(bo.find("val_mo3_cal_cks_low").address, 0x5824)
        self.assertEqual(bo.find("val_mo3_swi_cal_cks").address, 0x5803)
        lo = xd.load_definition(family="4570LO00S")
        self.assertEqual(lo.find("val_mo3_cal_cks_high").address, 0x57DC)
        self.assertEqual(lo.find("val_mo3_swi_cal_cks").address, 0x57BF)


class TestScalers(unittest.TestCase):
    """Формулы ``MATH`` для символов, которые пишет генератор."""

    def test_scaler_values(self) -> None:
        bo = xd.load_definition(family="4560BO00S")
        cases = {
            "c_vs_max": (1.0, 0.0),
            "id_n_max_mt": (1.0, 0.0),
            "c_n_lim_min": (1.0, 0.0),
            "c_t_min_pu": (0.01, 0.0),
            "c_t_min_pu_vs": (0.01, 0.0),
            "c_t_min_pu_rng_l": (0.01, 0.0),
        }
        for title, (a, b) in cases.items():
            scaler = bo.find(title).scaler
            assert scaler is not None, title
            self.assertAlmostEqual(scaler.a, a, places=9, msg=title)
            self.assertAlmostEqual(scaler.b, b, places=9, msg=title)

    def test_round_trip(self) -> None:
        obj = xd.load_definition(family="4560BO00S").find("c_t_min_pu")
        self.assertAlmostEqual(obj.to_engineering(obj.to_raw(0.5)), 0.5, places=9)
        self.assertAlmostEqual(obj.to_raw(0.5), 50.0, places=9)

    def test_degenerate_object_raises(self) -> None:
        definition = xd.load_definition(family="4560BO00S")
        broken = next(o for o in definition.objects.values() if not o.scaling_usable)
        with self.assertRaises(xd.XdfError):
            broken.to_engineering(0)


class TestStockValidation(unittest.TestCase):
    """Определение против реальных стоковых образов (порядок байт и диапазоны)."""

    def test_c_n_lim_min_is_7200_in_all_supported_files(self) -> None:
        """Big-endian подтверждается значением, совпадающим с описанием ms4x."""
        for family, files in (("4560BO00S", BO00_FILES), ("4570LO00S", LO00_FILES)):
            definition = xd.load_definition(family=family)
            obj = definition.find("c_n_lim_min")
            for path in files:
                values = xd.read_raw(path.read_bytes(), obj)
                self.assertEqual(values, [7200], f"{family}:{path.name}")

    def test_c_vs_max_stock_values(self) -> None:
        definition = xd.load_definition(family="4560BO00S")
        obj = definition.find("c_vs_max")
        values = {xd.read_raw(path.read_bytes(), obj)[0] for path in BO00_FILES}
        self.assertEqual(values, {206, 238, 254, 255})

    def test_rev_limiter_range(self) -> None:
        definition = xd.load_definition(family="4560BO00S")
        for title in ("id_n_max_mt", "id_n_max_h_mt", "id_n_max_amt", "id_n_max_h_amt"):
            obj = definition.find(title)
            for path in BO00_FILES:
                values = xd.read_raw(path.read_bytes(), obj)
                self.assertEqual(len(values), 9)
                self.assertGreaterEqual(min(values), 6000, f"{title}:{path.name}")
                self.assertLessEqual(max(values), 7000, f"{title}:{path.name}")

    def test_pops_delay_range(self) -> None:
        definition = xd.load_definition(family="4560BO00S")
        for title in ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l"):
            obj = definition.find(title)
            for path in BO00_FILES:
                raw = xd.read_raw(path.read_bytes(), obj)[0]
                self.assertLessEqual(obj.to_engineering(raw), 1.5, f"{title}:{path.name}")

    def test_engineering_scaling_plausible(self) -> None:
        """Инженерные значения стока: 0.24…0.36 s и 7200 rpm — а не «сырое как есть»."""
        definition = xd.load_definition(family="4560BO00S")
        obj = definition.find("c_t_min_pu_msr")
        values = {xd.read_engineering(p.read_bytes(), obj)[0] for p in BO00_FILES}
        self.assertEqual(values, {1.0})


class TestFullImageAddressing(unittest.TestCase):
    """Определение 1024K читает те же значения через BASEOFFSET 0x40000."""

    def test_base_offset_on_synthetic_image(self) -> None:
        cal = BO00_FILES[0].read_bytes()
        image = bytes(0x40000) + cal + bytes(xd.FULL_SIZE - 0x40000 - len(cal))
        partial = xd.load_definition(family="4560BO00S", variant="116K")
        full = xd.load_definition(family="4560BO00S", variant="1024K")
        for title in ("c_vs_max", "c_n_lim_min", "id_n_max_mt"):
            # Адресация «файл калибровки» (BASEOFFSET 0) и «полный образ»
            # (BASEOFFSET 0x40000) обязана давать одно и то же значение.
            self.assertEqual(
                xd.read_raw(cal, partial.find(title), base_offset=0),
                xd.read_raw(image, full.find(title), base_offset=full.base_offset),
                title,
            )
        self.assertIn(
            xd.read_raw(image, full.find("c_vs_max"), base_offset=full.base_offset)[0],
            {206, 238, 254, 255},
        )

    def test_real_dump_readable(self) -> None:
        """Реальное чтение 1 МиБ: адреса определения попадают в его область."""
        image = FULL_DUMP.read_bytes()
        definition = xd.load_definition(family="4560BO00S", variant="1024K")
        obj = definition.find("val_mo3_cal_cks_high")
        start = definition.base_offset + obj.address
        self.assertEqual(image[start : start + 4], image[0x45820:0x45824])

    def test_writable_objects_helper(self) -> None:
        definition = xd.load_definition(family="4570LO00S")
        writable = xd.writable_objects(definition)
        self.assertEqual(len(writable), len(definition.objects) - 9)


class TestCli(unittest.TestCase):
    """CLI разбора определения."""

    def test_report_runs(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "MS45.tools.xdf", "--family", "4560BO00S", "--check"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("456BO02S Partial", result.stdout)
        self.assertIn("5456", result.stdout)

    def test_symbol_output(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "MS45.tools.xdf", "--family", "4570LO00S", "--symbol", "c_vs_max"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0x1944", result.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
