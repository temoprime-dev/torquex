"""Доказательство группы задержек ПХХ ``c_t_min_pu*`` по стокам каждого семейства.

Появилось по ревью ``MS45/REVIEW_BM00S_CLAUDE_RU.md``: первая версия BM-карты
привязала группу к словам ``0x5C06..0x5C0C``, приняв хвостовые константы
``40, 60, 0`` за слова группы. Здесь — воспроизводимый метод, который:

1. на уже доказанных семействах (``4560BO00S`` — XDF, ``4570LO00S`` — XDF,
   ``4560BN00S`` — проверенная XML-карта) возвращает **известные** адреса
   ``0x5CAA`` / ``0x5D36`` / ``0x5C42`` — то есть метод проверяется, а не
   подгоняется;
2. на ``4560BM00S`` единственный раз находит группу в ``0x5C00..0x5C06``;
3. отвергает старую привязку ``0x5C06..0x5C0C``;
4. с шага 3 то же самое делает для семейств, добавленных по отпечаткам
   (``4560BL00S``, ``4560BL30S``, ``4570LL00S``, ``4570LL30S``, ``4570LM00S``,
   ``4570LN00S``): адрес из политики обязан совпасть с единственным кандидатом
   отпечатка — см. ``tests/test_family_stock_proof.py`` и
   ``docs/FAMILY_CALIBRATION_EVIDENCE_RU.md`` §3.

Метод (все ворота — по данным и по доказанной семантике, адреса между
семействами не переносятся):

* группа — четыре подряд идущих BE-u16 слова ``(pu, msr, rng_l, vs)``;
* **инвариант ``msr = 1.00 s``**: второе слово равно 100 во *всех* стоках
  семьи. В трёх доказанных семействах это слово — ``c_t_min_pu_msr``
  («Minimum delay time of PUC activation after MSR», XDF BO/LO; 1.00 s во всех
  114 файлах), поэтому 100 — семантический маркер второго слова, а не
  подогнанное число;
* три остальных слова во всех стоках лежат в продуктовой задержке
  ``0..1.5 s`` (raw ``0..150``, квант ``0.01``);
* упаковка: непосредственно перед группой во всех стоках стоит слово 100,
  сразу после группы — слова 40 и 60 (0.40/0.60 с). Роли этих соседних слов
  **не доказаны** и не пишутся; они используются только как маркер той же
  кластерной упаковки, что во всех четырёх семействах. Старая привязка
  как раз и была сдвигом на этот хвост.

Подробности и источники: ``MS45/docs/FAMILY_CALIBRATION_EVIDENCE_RU.md`` §1.3
и ``MS45/docs/ANALYSIS_WEB_RU.md`` §12.
"""

from __future__ import annotations

import struct
import unittest

from MS45.tools import checksums as ck
from MS45.tools import policy as pl

#: Известные адреса ``c_t_min_pu`` доказанных семейств (первоисточники:
#: XDF 456BO02S / 457LO02S и проверенная XML-карта 4560BN00).
KNOWN_GROUP_START = {
    "4560BO00S": 0x5CAA,
    "4570LO00S": 0x5D36,
    "4560BN00S": 0x5C42,
}

#: Доказанный адрес группы у частично поддержанного семейства.
BM00S_GROUP_START = 0x5C00

#: Продуктовая граница задержек: 0..1.5 с (сырые 0..150 при кванте 0.01).
MAX_DELAY_RAW = 150


def u16(image: bytes, offset: int) -> int:
    return struct.unpack_from(">H", image, offset)[0]


def find_pops_delay_group(images: list[bytes], lo: int = 0x200, hi: int = ck.CAL_SIZE - 12) -> list[int]:
    """Найти старт четырёхсловной группы задержек ПХХ по стокам семьи.

    Возвращает все смещения, удовлетворяющие воротам (см. модуль);
    доказательство — ровно один кандидат.
    """
    found: list[int] = []
    for offset in range(lo, hi, 2):
        ok = True
        for image in images:
            before = u16(image, offset - 2)
            pu, msr, rng_l, vs = (u16(image, offset + k) for k in (0, 2, 4, 6))
            trail_a, trail_b = u16(image, offset + 8), u16(image, offset + 10)
            if msr != 100 or before != 100 or trail_a != 40 or trail_b != 60:
                ok = False
                break
            if not all(0 <= word <= MAX_DELAY_RAW for word in (pu, rng_l, vs)):
                ok = False
                break
        if ok:
            found.append(offset)
    return found


def group_tuples(images: list[bytes], start: int) -> dict[tuple[int, int, int, int], int]:
    """Четырёхсловные кортежи группы по всем стокам и их частоты."""
    counter: dict[tuple[int, int, int, int], int] = {}
    for image in images:
        key = tuple(u16(image, start + 2 * i) for i in range(4))
        counter[key] = counter.get(key, 0) + 1
    return counter


class GroupMethodBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = pl.load_policy()

    def images(self, family: str) -> list[bytes]:
        return [image for _, image in pl.stock_images(family)]


class TestMethodReproducesProvenFamilies(GroupMethodBase):
    """Метод обязан воспроизводить известные адреса трёх доказанных семейств."""

    def test_known_group_start_matches_policy_and_xdf(self) -> None:
        for family, expected in KNOWN_GROUP_START.items():
            with self.subTest(family=family):
                entry = self.policy.for_family(family).objects["c_t_min_pu"]
                self.assertEqual(entry.address, expected)

    def test_finder_returns_exactly_the_known_group(self) -> None:
        for family, expected in KNOWN_GROUP_START.items():
            with self.subTest(family=family):
                images = self.images(family)
                self.assertEqual(find_pops_delay_group(images), [expected])

    def test_msr_word_is_the_constant_100_in_every_stock(self) -> None:
        for family in KNOWN_GROUP_START:
            with self.subTest(family=family):
                images = self.images(family)
                start = KNOWN_GROUP_START[family]
                for image in images:
                    self.assertEqual(u16(image, start + 2), 100)

    def test_trailing_and_leading_packaging_holds_in_proven_families(self) -> None:
        for family in KNOWN_GROUP_START:
            with self.subTest(family=family):
                images = self.images(family)
                start = KNOWN_GROUP_START[family]
                for image in images:
                    self.assertEqual(u16(image, start - 2), 100)
                    self.assertEqual(u16(image, start + 8), 40)
                    self.assertEqual(u16(image, start + 10), 60)


class TestBm00sGroupProof(GroupMethodBase):
    FAMILY = "4560BM00S"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.images_list = [image for _, image in pl.stock_images(cls.FAMILY)]

    def test_finder_is_unique_and_lands_at_0x5c00(self) -> None:
        self.assertEqual(len(self.images_list), 32)
        self.assertEqual(find_pops_delay_group(self.images_list), [BM00S_GROUP_START])

    def test_msr_word_is_constant_100_in_all_32_stocks(self) -> None:
        for image in self.images_list:
            self.assertEqual(u16(image, BM00S_GROUP_START + 2), 100)

    def test_group_values_and_distribution(self) -> None:
        tuples = group_tuples(self.images_list, BM00S_GROUP_START)
        self.assertEqual(
            tuples,
            {
                (0, 100, 0, 46): 15,
                (10, 100, 120, 18): 6,
                (24, 100, 32, 18): 6,
                (24, 100, 36, 32): 5,
            },
        )
        # pu: 0.00/0.10/0.24 s; rng_l: 0.00/1.20/0.32/0.36 s; vs: 0.18/0.32/0.46 s.
        pus = {key[0] for key in tuples}
        rngs = {key[2] for key in tuples}
        vss = {key[3] for key in tuples}
        self.assertEqual(pus, {0, 10, 24})
        self.assertEqual(rngs, {0, 32, 36, 120})
        self.assertEqual(vss, {18, 32, 46})

    def test_every_bm_tuple_is_a_known_variant_of_bo_or_bn(self) -> None:
        """Семантическая сверка: все BM-кортежи байт-в-байт совпадают с
        вариантами, уже наблюдаемыми в доказанных семействах (у каждого свой
        адрес — совпадают именно наборы значений, а не привязки)."""
        bm = set(group_tuples(self.images_list, BM00S_GROUP_START))
        bo = set(group_tuples(self.images("4560BO00S"), KNOWN_GROUP_START["4560BO00S"]))
        bn = set(group_tuples(self.images("4560BN00S"), KNOWN_GROUP_START["4560BN00S"]))
        self.assertTrue(bm <= bo | bn, sorted(bm - (bo | bn)))

    def test_old_alignment_is_rejected_by_the_msr_gate(self) -> None:
        """Старая привязка (группа от 0x5C06) не проходит: её «второе слово»
        0x5C08 = 40 во всех 32 стоках, а не инвариантные 100."""
        old_start = 0x5C06
        for image in self.images_list:
            self.assertNotEqual(u16(image, old_start + 2), 100)
            self.assertEqual(u16(image, old_start + 2), 40)
        self.assertNotIn(old_start, find_pops_delay_group(self.images_list))

    def test_trailing_words_are_constant_and_never_written_symbols(self) -> None:
        """Хвостовые слова 40/60/0 постоянны во всех 32 стоках; ролей в
        политике у них нет и быть не может (их нет в карте семейства)."""
        family_policy = self.policy.for_family(self.FAMILY)
        for image in self.images_list:
            self.assertEqual(u16(image, 0x5C08), 40)
            self.assertEqual(u16(image, 0x5C0A), 60)
            self.assertEqual(u16(image, 0x5C0C), 0)
        for address in (0x5C08, 0x5C0A, 0x5C0C):
            self.assertNotIn(
                address,
                {entry.address for entry in family_policy.objects.values()},
            )


class TestStep3FamiliesGroupProof(GroupMethodBase):
    """Семьи, добавленные шагом 3: отпечаток обязан дать ровно адрес политики."""

    STEP3_FAMILIES = (
        "4560BL00S", "4560BL30S", "4570LL00S", "4570LL30S", "4570LM00S", "4570LN00S",
    )

    def test_finder_is_unique_and_matches_policy(self) -> None:
        for family in self.STEP3_FAMILIES:
            with self.subTest(family=family):
                images = self.images(family)
                entry = self.policy.for_family(family).objects["c_t_min_pu"]
                self.assertEqual(find_pops_delay_group(images), [entry.address])

    def test_msr_word_is_constant_100_and_neighbours_hold(self) -> None:
        for family in self.STEP3_FAMILIES:
            images = self.images(family)
            start = self.policy.for_family(family).objects["c_t_min_pu"].address
            with self.subTest(family=family):
                for image in images:
                    self.assertEqual(u16(image, start + 2), 100)
                    self.assertEqual(u16(image, start - 2), 100)
                    self.assertEqual(u16(image, start + 8), 40)
                    self.assertEqual(u16(image, start + 10), 60)


if __name__ == "__main__":
    unittest.main()
