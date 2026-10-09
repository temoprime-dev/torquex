"""Доказательство ролей семейств MS45 по их стокам (шаг 3).

Метод (адреса между семействами не переносятся, каждый отпечаток проверяется
на **всех** стоках своей семьи и обязан сработать ровно один раз):

1. отпечатки **сначала воспроизводят известные адреса** семейств с
   первоисточниками — ``4560BO00S``/``4570LO00S`` (XDF MS4X) и ``4560BN00S``
   (проверенная XML-карта). Это защита от подгонки: отпечаток не подбирался под
   новую семью, он проверяется на уже доказанных;
2. затем те же отпечатки применяются к семействам без XDF (``4560BM00S``,
   ``4560BL00S``, ``4560BL30S``, ``4570LL00S``, ``4570LL30S``, ``4570LM00S``,
   ``4570LN00S``), и адрес обязан совпасть с записанным в политике.

Отпечатки:

* **лимитер скорости** (8 бит, км/ч): байты ``(-11, -9, -8, -7, -6, -5, -3, -2)
  = (40, 0, 0, 3, 4, 0, 3, 10)``, байт ``+7 = 100``, значение — км/ч в
  ``100..255``. Константы взяты из упаковки калибровки, а не из значений самого
  лимитера (те у семейств разные: 235/238/254/255).
* **порог «overrevving event»** (16 бит, rpm): ``[0] = 7200``, ``[+4] = 4000``,
  ``[+6] = 1200``, ``[+8] = 4701``; пара ``[-4], [-2]`` — из ``{0, 149, 480}``,
  содержит 480 и слова различны (у MS45.0 порядок ``480, 149``, у части MS45.1 —
  ``149, 480`` или ``0, 480``).
* **группа лимитеров оборотов**: шесть блоков по девять 16-бит слов со
  смещениями ``+0x00`` (amt), ``+0x12`` (at), ``+0x24`` (h_amt), ``+0x36``
  (h_at), ``+0x48`` (h_mt), ``+0x5A`` (mt) — порядок и смещения из XDF
  456BO02S/457LO02S; все слова в ``5000..7400`` rpm, «Increased» не ниже
  базового в каждом столбце, группа изолирована (слова до и после — не rpm).
* **группа задержек ПХХ**: см. ``test_pops_delay_group.py`` (второе слово
  ``msr = 1.00 s``, перед группой 100, после — 40 и 60).

Подробности и таблицы: ``MS45/docs/FAMILY_CALIBRATION_EVIDENCE_RU.md``.
"""

from __future__ import annotations

import unittest

from MS45.tests.test_pops_delay_group import find_pops_delay_group
from MS45.tools import checksums as ck
from MS45.tools import policy as pl
from MS45.tools import stages

#: Семейства с первоисточником адресов: отпечатки обязаны их воспроизвести.
KNOWN_SPEED_LIMITER = {"4560BO00S": 0x18DC, "4570LO00S": 0x1944, "4560BN00S": 0x18D4}
KNOWN_OVERREV = {"4560BO00S": 0x5AE2, "4570LO00S": 0x5B42, "4560BN00S": 0x5A7C}
KNOWN_REV_LIMIT = {"4560BO00S": 0x6048, "4570LO00S": 0x60D4, "4560BN00S": 0x5FDC}

#: Семейства, у которых адреса доказаны только стоками (шаг 3).
STOCK_DERIVED = (
    "4560BM00S", "4560BL00S", "4560BL30S", "4570LL00S", "4570LL30S",
    "4570LM00S", "4570LN00S",
)

#: Ожидаемое число стоков семейства (совпадает со stock_count в карте стоков).
STOCK_COUNT = {
    "4560BO00S": 81, "4570LO00S": 33, "4560BN00S": 39, "4560BM00S": 32,
    "4560BL00S": 7, "4560BL30S": 1, "4570LL00S": 1, "4570LL30S": 6,
    "4570LM00S": 7, "4570LN00S": 8,
}

REV_LIMIT_BLOCKS = {
    "id_n_max_amt": 0x00, "id_n_max_at": 0x12, "id_n_max_h_amt": 0x24,
    "id_n_max_h_at": 0x36, "id_n_max_h_mt": 0x48, "id_n_max_mt": 0x5A,
}
REV_LIMIT_LO, REV_LIMIT_HI = 5000, 7400


def u16(image: bytes, offset: int) -> int:
    return int.from_bytes(image[offset : offset + 2], "big")


def _unique(images: list[bytes], predicate, lo: int, hi: int, step: int = 1) -> list[int]:
    """Кандидаты по первому стоку, затем подтверждение по всем остальным.

    Полный перебор по всем стокам слишком дорог для теста, поэтому сначала
    дешёвый проход по одному стоку, а проверка остальных — только на
    кандидатах. Результат тот же: смещение обязано проходить по всем стокам.
    """
    first, rest = images[0], images[1:]
    found = [offset for offset in range(lo, hi, step) if predicate(first, offset)]
    return [offset for offset in found if all(predicate(image, offset) for image in rest)]


def speed_limiter_predicate(image: bytes, offset: int) -> bool:
    """Отпечаток ``c_vs_max``: упаковка вокруг байта лимитера скорости."""
    return (
        image[offset - 11] == 40
        and image[offset - 9] == 0
        and image[offset - 8] == 0
        and image[offset - 7] == 3
        and image[offset - 6] == 4
        and image[offset - 5] == 0
        and image[offset - 3] == 3
        and image[offset - 2] == 10
        and image[offset + 7] == 100
        and 100 <= image[offset] <= 255
    )


def find_speed_limiter(images: list[bytes]) -> list[int]:
    return _unique(images, speed_limiter_predicate, 0x1000, 0x3000)


def overrev_predicate(image: bytes, offset: int) -> bool:
    """Отпечаток ``c_n_lim_min``: 7200 rpm в окружении констант упаковки."""
    before = (u16(image, offset - 4), u16(image, offset - 2))
    return (
        u16(image, offset) == 7200
        and u16(image, offset + 4) == 4000
        and u16(image, offset + 6) == 1200
        and u16(image, offset + 8) == 4701
        and set(before) <= {0, 149, 480}
        and 480 in before
        and before[0] != before[1]
    )


def find_overrev_threshold(images: list[bytes]) -> list[int]:
    return _unique(images, overrev_predicate, 0x200, ck.CAL_SIZE - 12, 2)


def rev_limit_predicate(image: bytes, offset: int) -> bool:
    """Отпечаток группы лимитеров оборотов: шесть блоков по девять слов."""
    blocks = {
        name: [u16(image, offset + delta + 2 * index) for index in range(9)]
        for name, delta in REV_LIMIT_BLOCKS.items()
    }
    if not all(REV_LIMIT_LO <= value <= REV_LIMIT_HI for values in blocks.values() for value in values):
        return False
    for gear in range(9):
        if not (
            blocks["id_n_max_h_mt"][gear] >= blocks["id_n_max_mt"][gear]
            and blocks["id_n_max_h_at"][gear] >= blocks["id_n_max_at"][gear]
            and blocks["id_n_max_h_amt"][gear] >= blocks["id_n_max_amt"][gear]
        ):
            return False
    group_end = offset + REV_LIMIT_BLOCKS["id_n_max_mt"] + 9 * 2
    for neighbour in (u16(image, offset - 2), u16(image, group_end)):
        if REV_LIMIT_LO <= neighbour <= REV_LIMIT_HI:
            return False
    return True


def find_rev_limit_group(images: list[bytes]) -> list[int]:
    return _unique(images, rev_limit_predicate, 0x200, ck.CAL_SIZE - 0x80, 2)


class ProofBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = pl.load_policy()
        cls._images: dict[str, list[bytes]] = {}

    def images(self, family: str) -> list[bytes]:
        if family not in self._images:
            self._images[family] = [image for _, image in pl.stock_images(family)]
        return self._images[family]

    def assert_unique_hit(self, finder, family: str, expected: int) -> None:
        hits = finder(self.images(family))
        self.assertEqual(
            hits, [expected],
            f"{family}: отпечаток {finder.__name__} должен дать ровно один адрес",
        )


class TestStockCounts(ProofBase):
    def test_every_family_has_the_expected_number_of_stocks(self) -> None:
        for family, count in STOCK_COUNT.items():
            with self.subTest(family=family):
                self.assertEqual(len(self.images(family)), count)


class TestFingerprintsReproduceProvenFamilies(ProofBase):
    """Отпечатки обязаны воспроизводить адреса первоисточников (XDF/XML)."""

    def test_speed_limiter(self) -> None:
        for family, expected in KNOWN_SPEED_LIMITER.items():
            with self.subTest(family=family):
                self.assert_unique_hit(find_speed_limiter, family, expected)

    def test_overrev_threshold(self) -> None:
        for family, expected in KNOWN_OVERREV.items():
            with self.subTest(family=family):
                self.assert_unique_hit(find_overrev_threshold, family, expected)

    def test_rev_limit_group(self) -> None:
        for family, expected in KNOWN_REV_LIMIT.items():
            with self.subTest(family=family):
                self.assert_unique_hit(find_rev_limit_group, family, expected)

    def test_pops_delay_group(self) -> None:
        for family, expected in (("4560BO00S", 0x5CAA), ("4570LO00S", 0x5D36), ("4560BN00S", 0x5C42)):
            with self.subTest(family=family):
                self.assert_unique_hit(find_pops_delay_group, family, expected)

    def test_policy_addresses_match_definitions_for_proven_families(self) -> None:
        for family, expected in KNOWN_SPEED_LIMITER.items():
            with self.subTest(family=family):
                objects = self.policy.for_family(family).objects
                self.assertEqual(objects["c_vs_max"].address, expected)


class TestStockDerivedFamilies(ProofBase):
    """Адреса семи семейств без XDF обязаны совпасть с политикой и быть уникальны."""

    def test_speed_limiter(self) -> None:
        for family in STOCK_DERIVED:
            with self.subTest(family=family):
                objects = self.policy.for_family(family).objects
                self.assert_unique_hit(find_speed_limiter, family, objects["c_vs_max"].address)

    def test_overrev_threshold(self) -> None:
        for family in STOCK_DERIVED:
            with self.subTest(family=family):
                objects = self.policy.for_family(family).objects
                self.assert_unique_hit(find_overrev_threshold, family, objects["c_n_lim_min"].address)

    def test_rev_limit_group(self) -> None:
        expected_offsets = tuple(REV_LIMIT_BLOCKS.values())
        for family in STOCK_DERIVED:
            with self.subTest(family=family):
                objects = self.policy.for_family(family).objects
                base = objects["id_n_max_amt"].address
                for name, delta in REV_LIMIT_BLOCKS.items():
                    self.assertEqual(objects[name].address - base, delta, name)
                self.assert_unique_hit(find_rev_limit_group, family, base)
                self.assertIn(0x00, expected_offsets)

    def test_pops_delay_group(self) -> None:
        for family in STOCK_DERIVED:
            with self.subTest(family=family):
                objects = self.policy.for_family(family).objects
                self.assert_unique_hit(find_pops_delay_group, family, objects["c_t_min_pu"].address)

    def test_stock_values_are_plausible(self) -> None:
        for family in STOCK_DERIVED:
            objects = self.policy.for_family(family).objects
            with self.subTest(family=family):
                speed = objects["c_vs_max"].stock
                self.assertIsNotNone(speed.minimum)
                self.assertTrue(100 <= speed.minimum <= 255, f"{family}: {speed.minimum}")
                self.assertTrue(100 <= speed.maximum <= 255, f"{family}: {speed.maximum}")
                for name in REV_LIMIT_BLOCKS:
                    entry = objects[name]
                    self.assertEqual(entry.rows, 1)
                    self.assertEqual(entry.columns, 9)
                    self.assertGreaterEqual(entry.stock.minimum, REV_LIMIT_LO, name)
                    self.assertLessEqual(entry.stock.maximum, REV_LIMIT_HI, name)


class TestProtectionsStayReadOnly(ProofBase):
    def test_overrev_msr_and_checksums_are_read_only(self) -> None:
        for family in STOCK_DERIVED:
            fp = self.policy.for_family(family)
            for symbol in ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low"):
                with self.subTest(family=family, symbol=symbol):
                    self.assertTrue(fp.objects[symbol].read_only)
                    with self.assertRaises(pl.PolicyError) as ctx:
                        fp.entry(symbol)
                    self.assertEqual(ctx.exception.code, pl.CODE_READ_ONLY)

    def test_expected_read_only_set_is_declared(self) -> None:
        base = {"c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low"}
        ignition = {"ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"}
        for family in STOCK_DERIVED:
            with self.subTest(family=family):
                expected = base | (ignition if family in pl.IGNITION_STOCK_DERIVED_FAMILIES else set())
                self.assertEqual(set(pl.REQUIRED_READ_ONLY[family]), expected)
                self.assertIn(family, pl.STOCK_DERIVED_FAMILIES)


class TestStagesBuildOnEveryStock(ProofBase):
    """Сборка Stage 1 (лимиты + ПХХ) на всех стоках каждого нового семейства."""

    REQUEST = {
        "acknowledge_legality": True,
        "no_catalyst": True,
        "acknowledge_temperature": True,
        "intents": [
            {"kind": "vmax", "value": 250},
            {"kind": "rev_limit", "value": 6800, "transmission": "mt", "acknowledge_risk": True},
            {"kind": "pops", "delay_s": 0.5},
        ],
    }

    def test_build_is_byte_exact_outside_plan(self) -> None:
        for family in STOCK_DERIVED:
            for name, image in pl.stock_images(family):
                with self.subTest(family=family, stock=name):
                    result = stages.build("stage1", image, self.REQUEST, policy=self.policy, source_name=name)
                    self.assertEqual(result.status, "BUILT", [f["code"] for f in result.findings])
                    self.assertEqual([item.id for item in result.pending], [])
                    self.assertIsNotNone(result.plan)
                    self.assertIsNotNone(result.result)
                    assert result.plan is not None and result.result is not None
                    self.assertTrue(ck.verify_bytes(result.result.image).ok)
                    planned = {
                        offset
                        for cell in result.plan.planned_cells
                        for offset in range(cell.image_offset, cell.image_offset + cell.size_bytes)
                    }
                    allowed = planned | set(result.plan.checksum_cells)
                    changed = {
                        offset
                        for offset, (before, after) in enumerate(zip(image, result.result.image))
                        if before != after
                    }
                    self.assertTrue(changed)
                    self.assertLessEqual(changed, allowed)

    def test_values_land_in_the_planned_objects(self) -> None:
        family = "4570LN00S"
        name, image = pl.stock_images(family)[0]
        result = stages.build("stage1", image, self.REQUEST, policy=self.policy, source_name=name)
        assert result.result is not None
        objects = self.policy.for_family(family).objects
        self.assertEqual(result.result.image[objects["c_vs_max"].address], 250)
        self.assertEqual(u16(result.result.image, objects["id_n_max_mt"].address), 6800)
        self.assertEqual(u16(result.result.image, objects["id_n_max_h_mt"].address), 6800)
        for symbol in ("c_t_min_pu", "c_t_min_pu_rng_l", "c_t_min_pu_vs"):
            self.assertEqual(u16(result.result.image, objects[symbol].address), 50, symbol)
        # msr не пишется: остаётся стоковое 1.00 с
        self.assertEqual(u16(result.result.image, objects["c_t_min_pu_msr"].address), 100)


class TestUnprovenRolesWaitForData(ProofBase):
    """Карты без доказанных адресов остаются REQUIRES_DATA с конкретными нуждами."""

    def test_ignition_waits_for_data_on_stock_derived_families(self) -> None:
        # BL00S/BM00S/LL30S/LM00S/LN00S получили карту RON98 (доказательство —
        # test_family_ignition_proof.py); семейства с одним стоком ждут данных.
        for family in ("4560BL30S", "4570LL00S"):
            name, image = pl.stock_images(family)[0]
            with self.subTest(family=family):
                result = stages.build(
                    "stage1", image,
                    {"acknowledge_legality": True,
                     "intents": [{"kind": "ignition", "profile": "ron98", "source": "log"}]},
                    policy=self.policy, source_name=name,
                )
                self.assertEqual(result.status, "REQUIRES_DATA")
                self.assertEqual([item.id for item in result.pending], ["ignition"])
                self.assertGreaterEqual(len(result.pending[0].needs or []), 2)
                self.assertEqual(result.written_packages, ())

    def test_disa_off_waits_for_data_on_ms45(self) -> None:
        name, image = pl.stock_images("4560BM00S")[0]
        result = stages.build(
            "stage1", image,
            {"acknowledge_legality": True, "intents": [{"kind": "disa_off"}]},
            policy=self.policy, source_name=name,
        )
        self.assertEqual(result.status, "REQUIRES_DATA")
        self.assertEqual([item.id for item in result.pending], ["disa_off"])


if __name__ == "__main__":
    unittest.main()
