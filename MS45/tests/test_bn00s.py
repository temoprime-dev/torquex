"""Тесты семейства 4560BN00S (E60, SW 456) — третьего поддержанного.

Здесь проверяется не код, а **основание** его появления:

1. первоисточник адресов (XML-определения сообщества) верен: у семейства
   4560BO00 те же роли дают ровно те адреса, которые уже доказаны XDF;
2. адреса ролей 4560BN00S упакованы без дыр и пересечений, а значения по ним
   осмысленны и устойчивы во всех 39 файлах семейства (38 калибровок стока +
   область калибровки реального чтения ЭБУ);
3. ядро на этом семействе пишет ровно то, что планировало, и не выходит за
   область калибровки (в том числе в полном образе 1 МиБ).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from MS45.tools import calibration as cal
from MS45.tools import checksums as ck
from MS45.tools import policy as pl
from MS45.tools import stages
from MS45.tests.support import unknown_family_image  # noqa: E402
from MS45.tools import xml_defs as xm

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
SPDATEN = MS45 / "firmware_spdaten" / "cal"
DUMPS = MS45 / "firmware_dumps"

FAMILY = "4560BN00S"
BN00_CAL = SPDATEN / "MS45.0_0044560BN00S07F0G_P7550088_cal_116K.bin"
BN00_DUMP_CAL = SPDATEN / "MS45.0_0044560BN00S0FS0N_P7574503_cal_116K.bin"
REAL_DUMP = DUMPS / "MS45.0_0044560BN_fullread_ORI.bin"
MPC_DUMP = DUMPS / "MS45.0_0044560BN_MPC_ORI.bin"
POLICY = pl.load_policy()

#: Роли, которые источник должен подтверждать у BO00: имя в XML -> символ политики.
XML_TO_SYMBOL = {
    "Vehicle speed limiter": "c_vs_max",
    "Engine speed limiter (MT)": "id_n_max_mt",
    "Increased engine speed limiter (MT)": "id_n_max_h_mt",
    "Engine speed limiter (AT)": "id_n_max_at",
    "Increased engine speed limiter (AT)": "id_n_max_h_at",
    "Engine speed limiter (AMT)": "id_n_max_amt",
    "Increased engine speed limiter (AMT)": "id_n_max_h_amt",
    "Engine speed threshold for overrevving event": "c_n_lim_min",
    "Reset learned variants": "lc_ad_clr_var",
}


def _stock_files() -> list[tuple[str, bytes]]:
    return [(name, image) for name, image in pl.stock_images(FAMILY)]


class TestSourceProvenance(unittest.TestCase):
    """Первоисточник адресов BN00S проверен по уже доказанному семейству BO00."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.xml = MS45 / "definitions" / "MS45 ECU Definitions.xml"

    def test_source_file_is_tracked(self) -> None:
        self.assertTrue(self.xml.exists(), "определения сообщества должны лежать в репозитории")

    def test_source_matches_verified_bo_addresses(self) -> None:
        """Все роли BO00 в XML совпадают с адресами, доказанными XDF-семейством."""
        rom = xm.load_rom(self.xml, "4560BO00", filesize="116kb")
        addresses = {table.name: table.address for table in rom.tables}
        bo = POLICY.for_family("4560BO00S")
        for title, symbol in XML_TO_SYMBOL.items():
            self.assertIn(title, addresses, title)
            expected = bo.objects[symbol].address
            self.assertEqual(addresses[title], expected, f"{title} -> {symbol}")

    def test_source_delays_match_verified_bo_roles(self) -> None:
        """Задержки ПХХ: имена в XML однозначно соответствуют ролям политики BO00."""
        rom = xm.load_rom(self.xml, "4560BO00", filesize="116kb")
        addresses = {table.name: table.address for table in rom.tables}
        bo = POLICY.for_family("4560BO00S")
        pairs = {
            "c_t_min_pu": (
                "Delay before activating trailing throttle fuel cut when clutch is "
                "pressed or cruise active"
            ),
            "c_t_min_pu_rng_l": (
                "Delay before activating trailing throttle fuel cut when clutch is "
                "pressed or cruise active (low range)"
            ),
            "c_t_min_pu_vs": (
                "Delay before activating trailing throttle fuel cut when car is stationary"
            ),
        }
        for symbol, title in pairs.items():
            self.assertIn(title, addresses, symbol)
            self.assertEqual(addresses[title], bo.objects[symbol].address, symbol)

    def test_bn_addresses_come_from_source(self) -> None:
        """Каждый адрес с пометкой xml совпадает с таблицей ROM 4560BN00."""
        rom = xm.load_rom(self.xml, pl.XML_FAMILIES[FAMILY], filesize="116kb")
        addresses = {table.name: table.address for table in rom.tables}
        bn = POLICY.for_family(FAMILY)
        checked = 0
        for spec in pl.BN00S_SYMBOLS:
            if spec.source != "xml":
                continue
            self.assertEqual(addresses[spec.title], bn.objects[spec.symbol].address, spec.symbol)
            checked += 1
        self.assertGreaterEqual(checked, 10)

    def test_bn_delay_titles_match_roles(self) -> None:
        """Роли задержек ПХХ у BN заданы теми же именами, что у проверенного BO00."""
        titles = {spec.symbol: spec.title for spec in pl.BN00S_SYMBOLS if spec.title}
        self.assertIn("clutch is pressed or cruise active", titles["c_t_min_pu"])
        self.assertNotIn("low range", titles["c_t_min_pu"])
        self.assertIn("(low range)", titles["c_t_min_pu_rng_l"])
        self.assertIn("when car is stationary", titles["c_t_min_pu_vs"])

    def test_mo3_pair_addresses_proven_by_checksums(self) -> None:
        bn = POLICY.for_family(FAMILY)
        base = ck.MO3_STORE_OFFSET[FAMILY]
        self.assertEqual(bn.objects["val_mo3_cal_cks_high"].address, base)
        self.assertEqual(bn.objects["val_mo3_cal_cks_low"].address, base + 4)

    def test_switch_address_not_claimed(self) -> None:
        """Переключатель MO3 у BN не доказан — в политике его быть не должно."""
        self.assertNotIn("val_mo3_swi_cal_cks", POLICY.for_family(FAMILY).objects)
        self.assertNotIn(FAMILY, ck.MO3_SWITCH_VERIFIED)

    def test_switch_candidates_are_zero_in_stock(self) -> None:
        """Оба кандидата на роль переключателя равны нулю во всех файлах.

        Значение переключателя влияет только на справку отчёта; проверка суммы
        выполняется пересчётом. Тест фиксирует, что в стоке сумма нигде не
        отключена, какая бы из двух позиций ни была настоящим переключателем.
        """
        candidates = (ck.MO3_SWITCH_OFFSET[FAMILY], 0x5788)
        for name, image in _stock_files():
            for offset in candidates:
                self.assertEqual(image[offset], 0x00, f"{name}: 0x{offset:05X}")


class TestStockData(unittest.TestCase):
    """Значения по адресам ролей во всех стоковых файлах семейства."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = _stock_files()
        cls.bn = POLICY.for_family(FAMILY)

    def test_all_stock_files_present(self) -> None:
        self.assertEqual(len(self.files), 39, "38 калибровок SP-Daten + реальный дамп")

    def test_sums_of_every_file(self) -> None:
        for name, image in self.files:
            report = ck.verify_bytes(image)
            self.assertTrue(report.crc32_ok, name)
            self.assertTrue(report.mo3_ok, name)

    def test_vmax_values(self) -> None:
        entry = self.bn.objects["c_vs_max"]
        for name, image in self.files:
            value = entry.to_engineering(image[entry.address])
            self.assertIn(value, (235.0, 254.0), f"{name}: {value}")

    def test_rev_limiters_are_plausible_and_consistent(self) -> None:
        low, high = 6000, 7000
        for pair in ("rev_limit_mt", "rev_limit_at", "rev_limit_amt"):
            normal, increased = self.bn.roles[pair]
            for name, image in self.files:
                for symbol in (normal, increased):
                    entry = self.bn.objects[symbol]
                    values = [
                        self._read(image, entry, index)
                        for index in range(entry.rows * entry.columns)
                    ]
                    self.assertEqual(len(values), 9, symbol)
                    for value in values:
                        self.assertTrue(low <= value <= high, f"{name}:{symbol}={value}")
                for index in range(9):
                    self.assertGreaterEqual(
                        self._read(image, self.bn.objects[increased], index),
                        self._read(image, self.bn.objects[normal], index),
                        f"{name}: {increased} < {normal} в передаче {index + 1}",
                    )

    def test_overrev_threshold_is_stock_constant(self) -> None:
        entry = self.bn.objects["c_n_lim_min"]
        for name, image in self.files:
            self.assertEqual(self._read(image, entry, 0), 7200, name)
        self.assertEqual(entry.stock.variants, 1)

    def test_msr_delay_is_constant_one_second(self) -> None:
        entry = self.bn.objects["c_t_min_pu_msr"]
        for name, image in self.files:
            self.assertEqual(self._read(image, entry, 0), 100, f"{name}: ожидалось 1.00 c")

    def test_reset_variants_is_zero(self) -> None:
        entry = self.bn.objects["lc_ad_clr_var"]
        for name, image in self.files:
            self.assertEqual(image[entry.address], 0, name)

    def test_limiter_tables_are_packed_without_gaps(self) -> None:
        """Шесть таблиц отсечек идут вплотную: 9 значений по 2 байта каждая."""
        ranges = []
        for role in ("rev_limit_amt", "rev_limit_at", "rev_limit_mt"):
            normal, increased = [self.bn.objects[s] for s in self.bn.roles[role]]
            for entry in (normal, increased):
                ranges.append((entry.address, entry.address + entry.rows * entry.columns * 2))
        ranges.sort()
        for (_, end), (start, _) in zip(ranges, ranges[1:]):
            self.assertEqual(end, start, "таблицы отсечек обязаны идти без дыр")

    def test_real_dump_per_gear_values(self) -> None:
        """Реальное чтение E60: отсечка автомата различается по передачам."""
        assert REAL_DUMP.exists()
        image = REAL_DUMP.read_bytes()[ck.EXT_CAL_OFFSET : ck.EXT_CAL_OFFSET + ck.CAL_SIZE]
        self.assertEqual(ck.read_family(image), FAMILY)
        at = self.bn.objects["id_n_max_at"]
        self.assertEqual(self._read(image, at, 0), 6520)
        self.assertEqual([self._read(image, at, i) for i in range(1, 9)], [6620] * 8)
        self.assertEqual(self._read(image, self.bn.objects["c_vs_max"], 0), 254)

    @staticmethod
    def _read(image: bytes, entry: pl.PolicyEntryData, index: int) -> int:
        start = entry.address + index * entry.element_size_bytes
        return int.from_bytes(image[start : start + entry.element_size_bytes], "big")


class TestCoreOnBn(unittest.TestCase):
    """Ядро на BN: планирование, запись, суммы и границы области калибровки."""

    def setUp(self) -> None:
        self.image = BN00_CAL.read_bytes()
        self.bn = POLICY.for_family(FAMILY)

    def _plan(self, requests: list[dict], acknowledgements: set[str] | None = None) -> cal.PatchPlan:
        return cal.plan_changes(self.image, requests, policy=POLICY, acknowledgements=acknowledgements)

    def test_stage1_planned_and_applied(self) -> None:
        plan = self._plan(
            [
                {"symbol": "c_vs_max", "values": [255.0]},
                {"symbol": "id_n_max_mt", "values": [6700.0]},
                {"symbol": "id_n_max_h_mt", "values": [6700.0]},
            ],
            {"no-catalyst"},
        )
        self.assertEqual(plan.status, "PLANNED", [f.code for f in plan.findings])
        result = cal.apply_plan(self.image, plan)
        report = ck.verify_bytes(result.image)
        self.assertTrue(report.crc32_ok and report.mo3_ok)
        changed = {i for i in range(len(self.image)) if self.image[i] != result.image[i]}
        allowed = set()
        for cell in plan.planned_cells:
            allowed.update(range(cell.image_offset, cell.image_offset + cell.size_bytes))
        allowed.update(range(0x100, 0x104))
        allowed.update(range(ck.MO3_STORE_OFFSET[FAMILY], ck.MO3_STORE_OFFSET[FAMILY] + 8))
        self.assertTrue(changed <= allowed, sorted(changed - allowed)[:8])

    def test_read_only_and_unknown_symbols(self) -> None:
        for symbol in ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "lc_ad_clr_var"):
            plan = self._plan([{"symbol": symbol, "values": [1.0]}])
            self.assertIn(pl.CODE_READ_ONLY, [f.code for f in plan.findings], symbol)
        plan = self._plan([{"symbol": "c_conf_cat", "values": [1.0]}])
        self.assertIn(pl.CODE_SYMBOL_NOT_ALLOWED, [f.code for f in plan.findings])

    def test_pops_needs_acknowledgement(self) -> None:
        request = [{"symbol": "c_t_min_pu", "values": [0.5]}]
        plan = self._plan(request)
        self.assertEqual([f.code for f in plan.findings], [cal.CODE_ACKNOWLEDGEMENT_REQUIRED])
        plan = self._plan(request, {"no-catalyst"})
        self.assertEqual(plan.status, "PLANNED", [f.code for f in plan.findings])

    def test_full_image_untouched_outside_calibration(self) -> None:
        """Полный образ 1 МиБ (реальное чтение): вне калибровки — байт в байт."""
        source = REAL_DUMP.read_bytes()
        self.assertEqual(len(source), ck.EXT_FLASH_SIZE)
        plan = cal.plan_changes(
            source, [{"symbol": "c_vs_max", "values": [250.0]}], policy=POLICY
        )
        self.assertEqual(plan.status, "PLANNED", [f.code for f in plan.findings])
        self.assertEqual(plan.kind, "full")
        result = cal.apply_plan(source, plan)
        self.assertEqual(result.image[: ck.EXT_CAL_OFFSET], source[: ck.EXT_CAL_OFFSET])
        tail = ck.EXT_CAL_OFFSET + ck.CAL_SIZE
        self.assertEqual(result.image[tail:], source[tail:])
        report = ck.verify_bytes(result.image)
        self.assertTrue(report.crc32_ok and report.mo3_ok)
        patched_cal = result.image[ck.EXT_CAL_OFFSET : ck.EXT_CAL_OFFSET + ck.CAL_SIZE]
        self.assertEqual(patched_cal[0x18D4], 250)

    def test_mpc_image_refused(self) -> None:
        plan = self._plan_for(MPC_DUMP.read_bytes())
        self.assertEqual([f.code for f in plan.findings], [ck.MpcImageError.code])

    def test_other_families_still_refused(self) -> None:
        """Чужой (не из десяти целевых) идентификатор семейства закрыт.

        С шага 3 BL00S/LL00S/LM00S/LN00S поддержаны, поэтому «чужим» служит
        синтезированный идентификатор (MS45/tests/support.py).
        """
        other = SPDATEN / "MS45.0_0044560BL00S0IA3N_P7537853_cal_116K.bin"
        plan = cal.plan_changes(
            unknown_family_image(other.read_bytes()),
            [{"symbol": "c_vs_max", "values": [255.0]}],
            policy=POLICY,
        )
        self.assertEqual([f.code for f in plan.findings], [pl.CODE_CAL_VERSION_UNSUPPORTED])

    def _plan_for(self, image: bytes) -> cal.PatchPlan:
        return cal.plan_changes(image, [{"symbol": "c_vs_max", "values": [255.0]}], policy=POLICY)


class TestStagesOnBn(unittest.TestCase):
    """Генератор Stage 1 на BN: сборка, наряд и отказы."""

    def setUp(self) -> None:
        self.image = BN00_CAL.read_bytes()
        self.request = {
            "no_catalyst": True,
            "acknowledge_temperature": True,
            "intents": [
                {"kind": "vmax", "value": 255},
                {"kind": "rev_limit", "value": 6700, "transmission": "mt", "acknowledge_risk": True},
            ],
        }

    def test_stage1_built(self) -> None:
        result = stages.build("stage1", self.image, self.request)
        self.assertEqual(result.status, "BUILT", list(result.findings))
        assert result.result is not None
        report = ck.verify_bytes(result.result.image)
        self.assertTrue(report.crc32_ok and report.mo3_ok)
        self.assertEqual(report.family, FAMILY)
        note = stages.build_note(result, BN00_CAL.name)
        self.assertIn("4560BN00S", note)
        self.assertIn("# Наряд MS45", note)

    def test_stage1_above_stock_needs_risk_acknowledgement(self) -> None:
        request = {
            "no_catalyst": True,
            "intents": [{"kind": "rev_limit", "value": 6800, "transmission": "mt"}],
        }
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_REQUIRED", [f["code"] for f in result.findings])


if __name__ == "__main__":
    unittest.main()
