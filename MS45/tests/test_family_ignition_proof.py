"""Доказательство карты RON98 (зажигание Stage 1) у семейств MS45 без XDF.

Принцип тот же, что в ``test_family_stock_proof.py``: адреса между семействами
не переносятся, каждый отпечаток проверяется на **всех** стоках своей семьи,
обязан сработать ровно один раз во всей калибровке и **сначала воспроизводит
адреса первоисточников** (XDF ``4560BO00S``/``4570LO00S``, XML ``4560BN00S``).

Отпечатки (код ниже не использует ни ``map_match``, ни генератор):

* **кластер карт зажигания** — пять подряд лежащих карт 20x16 по 320 байт
  (``ip_iga_bas``, ``add_max_ivvt``, ``add_min_ivvt``, ``RON91``, ``RON98``, шаг
  ``0x140`` — порядок и шаг из XDF). Условия: байты карт ``bas``/``RON91``/
  ``RON98`` в ``0x10..0xF5``; ``RON98 >= RON91`` не менее чем в 95% ячеек, пик
  разницы > 10 кодов, худшая разница >= -15; в каждой строке каждой из трёх
  карт угол на минимальной нагрузке больше, чем на максимальной; последняя
  строка (высокие обороты) в среднем больше первой на >= 15 кодов (5.6°);
* **ось оборотов** — единственная во всей калибровке запись ``[20][20 x u16]``
  со строго растущими значениями от 250..1000 до 5500..7200 об/мин;
* **ось нагрузки** — запись ``[16][16 x u16]``, строго растущая, ``0.021195*X``
  от 20..100 до 400..1500 мг/такт, от которой цепочка записей пула осей
  ``[n][n x u16]`` без разрывов доходит до оси оборотов; из таких берётся
  ближайшая к оси оборотов (пул осей — одна длинная цепочка). Дополнительно
  последовательность длин записей между двумя осями сравнивается с эталоном
  поколения (MS45.0 — BO00S, MS45.1 — LO00S).

Для 4560BL00S есть второй, независимый источник: XML-определения сообщества
(ROM ``4560BL00``) дают те же адреса карты RON98 и обеих осей.

Подробности и таблицы: ``MS45/docs/FAMILY_CALIBRATION_EVIDENCE_RU.md`` §4.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from MS45.tools import checksums as ck
from MS45.tools import map_match
from MS45.tools import policy as pl
from MS45.tools import stages

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
CANDIDATES = ROOT / "definitions" / "map_match_candidates.json"
XML_DEFINITIONS = ROOT / "definitions" / "MS45 ECU Definitions.xml"
PACK_SPEC = REPO / "docs" / "pack_log_requirements.json"

ROWS, COLUMNS = 20, 16
MAP_BYTES = ROWS * COLUMNS
CLUSTER = 5 * MAP_BYTES
RON98_IN_CLUSTER = 4 * MAP_BYTES
RON91_IN_CLUSTER = 3 * MAP_BYTES
LOAD_SCALE = 0.021195
SIGNATURE = range(0x174, 0x1B4)
CRC_CELL = range(0x100, 0x104)

#: Первоисточники: (RON98, ось нагрузки, ось оборотов) — XDF MS4X и XML BN00.
KNOWN = {
    "4560BO00S": (0xAF0C, 0x853C, 0x86C0),
    "4570LO00S": (0xB24C, 0x8812, 0x8992),
    "4560BN00S": (0xAE64, 0x8494, 0x8618),
}
#: Эталон поколения для сравнения структуры пула осей.
GENERATION_REFERENCE = {"4560": "4560BO00S", "4570": "4570LO00S"}

#: Семейства, где зажигание включено (сток >= 2), и число их стоков.
IGNITION_FAMILIES = {
    "4560BL00S": 7, "4560BM00S": 32, "4570LL30S": 6, "4570LM00S": 7, "4570LN00S": 8,
}
SINGLE_STOCK = ("4560BL30S", "4570LL00S")

PHYS_MIN_DEG, PHYS_MAX_DEG = -20.0, 50.0


def u16(image: bytes, offset: int) -> int:
    return int.from_bytes(image[offset : offset + 2], "big")


def be32(image: bytes, offset: int) -> int:
    return int.from_bytes(image[offset : offset + 4], "big")


def to_deg(raw: int) -> float:
    return raw * 0.375 - 35.625


# ---------------------------------------------------------------------------
# Отпечатки
# ---------------------------------------------------------------------------


def cluster_predicate(image: bytes, start: int) -> bool:
    """Начало кластера пяти карт зажигания 20x16 по адресу ``start``."""
    maps = [image[start + k * MAP_BYTES : start + (k + 1) * MAP_BYTES] for k in range(5)]
    if any(len(item) != MAP_BYTES for item in maps):
        return False
    bas, _add_max, _add_min, r91, r98 = maps
    for item in (r98, r91, bas):
        if min(item) < 0x10 or max(item) > 0xF5:
            return False
    diffs = [a - b for a, b in zip(r98, r91)]
    if sum(1 for d in diffs if d >= 0) < 0.95 * MAP_BYTES or max(diffs) <= 10 or min(diffs) < -15:
        return False
    for item in (bas, r91, r98):
        for row in range(ROWS):
            if not item[row * COLUMNS] > item[row * COLUMNS + COLUMNS - 1]:
                return False
        if not sum(item[(ROWS - 1) * COLUMNS :]) > sum(item[:COLUMNS]) + 15 * COLUMNS:
            return False
    return True


def find_ron98(images: list[bytes]) -> list[int]:
    """Адреса RON98 (начало кластера + 0x500), общие для всех стоков."""
    first, rest = images[0], images[1:]
    hits = [s for s in range(0x200, ck.CAL_SIZE - CLUSTER) if cluster_predicate(first, s)]
    hits = [s for s in hits if all(cluster_predicate(image, s) for image in rest)]
    return [s + RON98_IN_CLUSTER for s in hits]


def _record(image: bytes, header: int, count: int) -> list[int] | None:
    if u16(image, header) != count:
        return None
    return [u16(image, header + 2 + 2 * i) for i in range(count)]


def rpm_axis_predicate(image: bytes, header: int) -> bool:
    values = _record(image, header, ROWS)
    return bool(
        values
        and all(a < b for a, b in zip(values, values[1:]))
        and 250 <= values[0] <= 1000
        and 5500 <= values[-1] <= 7200
    )


def load_axis_shape(image: bytes, header: int) -> bool:
    values = _record(image, header, COLUMNS)
    return bool(
        values
        and all(a < b for a, b in zip(values, values[1:]))
        and 20 <= values[0] * LOAD_SCALE <= 100
        and 400 <= values[-1] * LOAD_SCALE <= 1500
    )


def chain_counts(image: bytes, start_header: int, target_header: int, limit: int = 400) -> list[int] | None:
    """Длины записей ``[n][n x u16]`` от ``start_header`` до ``target_header``
    (не включая последнюю) или ``None``, если цепочка рвётся/перескакивает."""
    counts: list[int] = []
    header = start_header
    while len(counts) <= limit:
        if header == target_header:
            return counts
        if header > target_header:
            return None
        count = u16(image, header)
        if not 1 <= count <= 32:
            return None
        counts.append(count)
        header += 2 + 2 * count
    return None


def find_rpm_axis(images: list[bytes]) -> list[int]:
    first, rest = images[0], images[1:]
    hits = [h for h in range(0x200, ck.CAL_SIZE - 2 - 2 * ROWS, 2) if rpm_axis_predicate(first, h)]
    return [h + 2 for h in hits if all(rpm_axis_predicate(image, h) for image in rest)]


def nearest_load_axis(image: bytes, rpm_axis: int) -> int | None:
    """Ближайшая к оси оборотов ось нагрузки, связанная с ней цепочкой записей."""
    best: tuple[int, int] | None = None
    target = rpm_axis - 2
    for header in range(0x200, target, 2):
        if not load_axis_shape(image, header):
            continue
        counts = chain_counts(image, header, target)
        if counts is None:
            continue
        if best is None or len(counts) < best[0]:
            best = (len(counts), header + 2)
    return None if best is None else best[1]


def find_load_axis(images: list[bytes], rpm_axis: int) -> list[int]:
    found = {nearest_load_axis(image, rpm_axis) for image in images}
    return sorted(value for value in found if value is not None) if None not in found else []


def xml_ignition(rom_id: str) -> dict[str, tuple[int, int, int]]:
    """Адреса карт RON98/RON91 и их осей из XML-определений (116kb-ROM)."""
    text = XML_DEFINITIONS.read_text(encoding="utf-8", errors="replace")
    out: dict[str, tuple[int, int, int]] = {}
    for match in re.finditer(r"<rom\b.*?</rom>", text, re.S):
        rom = match.group(0)
        xmlid = re.search(r"<xmlid>(.*?)</xmlid>", rom)
        size = re.search(r"<filesize>(.*?)</filesize>", rom)
        if not xmlid or xmlid.group(1) != rom_id or not size or size.group(1).lower() != "116kb":
            continue
        for label, name in (("ron98", "Basic ignition angle for RON 98"),
                            ("ron91", "Basic ignition angle for RON 91")):
            found = re.search(
                r'<table name="' + name + r'" storageaddress="(.*?)">\s*'
                r'<table type="X Axis" storageaddress="(.*?)"/>\s*'
                r'<table type="Y Axis" storageaddress="(.*?)"/>', rom)
            if found:
                out[label] = tuple(int(value, 16) for value in found.groups())  # type: ignore[assignment]
    return out


# ---------------------------------------------------------------------------
# Независимые суммы (не функции checksums.py)
# ---------------------------------------------------------------------------


def _crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


_CRC_TABLE = []
for _index in range(256):
    _value = _index << 24
    for _ in range(8):
        _value = ((_value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _value & 0x80000000 else (_value << 1) & 0xFFFFFFFF
    _CRC_TABLE.append(_value)


def crc32_mpeg2(data: bytes) -> int:
    """CRC-32/MPEG-2 по таблице (таблица построена здесь же; сверено с побитовым)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


def mo3_pair(image: bytes) -> tuple[int, int]:
    total = 0x0123456789ABCDEF
    for offset in range(0x240, 0x7C8, 4):
        total += be32(image, offset)
    total &= 0xFFFFFFFFFFFFFFFF
    return total >> 32, total & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


class IgnitionProofBase(unittest.TestCase):
    _images: dict[str, list[tuple[str, bytes]]] = {}
    _ron98: dict[str, list[int]] = {}
    _rpm: dict[str, list[int]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = pl.load_policy()
        cls.candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))
        cls.spec = json.loads(PACK_SPEC.read_text(encoding="utf-8"))

    @classmethod
    def stocks(cls, family: str) -> list[tuple[str, bytes]]:
        if family not in cls._images:
            cls._images[family] = pl.stock_images(family)
        return cls._images[family]

    @classmethod
    def images(cls, family: str) -> list[bytes]:
        return [image for _, image in cls.stocks(family)]

    @classmethod
    def ron98_hits(cls, family: str) -> list[int]:
        if family not in cls._ron98:
            cls._ron98[family] = find_ron98(cls.images(family))
        return cls._ron98[family]

    @classmethod
    def rpm_hits(cls, family: str) -> list[int]:
        if family not in cls._rpm:
            cls._rpm[family] = find_rpm_axis(cls.images(family))
        return cls._rpm[family]


class TestMethodReproducesFirstSources(IgnitionProofBase):
    """Отпечатки обязаны воспроизвести XDF (BO00S/LO00S) и XML (BN00S)."""

    def test_cluster_gives_xdf_xml_ron98(self) -> None:
        for family, (ron98, _x, _y) in KNOWN.items():
            with self.subTest(family=family):
                self.assertEqual(self.ron98_hits(family), [ron98])

    def test_axes_give_xdf_xml_axes(self) -> None:
        for family, (_ron98, x_axis, y_axis) in KNOWN.items():
            with self.subTest(family=family):
                self.assertEqual(self.rpm_hits(family), [y_axis])
                self.assertEqual(find_load_axis(self.images(family), y_axis), [x_axis])

    def test_xdf_policy_agrees_for_bo_lo(self) -> None:
        # Адреса в политике BO00S/LO00S взяты из XDF, а не из отпечатков.
        for family in ("4560BO00S", "4570LO00S"):
            objects = self.policy.for_family(family).objects
            ron98, x_axis, y_axis = KNOWN[family]
            with self.subTest(family=family):
                self.assertEqual(objects["ip_iga_bas_ron_98__n__maf"].address, ron98)
                self.assertEqual(objects["ip_iga_bas_ron_91__n__maf"].address, ron98 - 0x140)
                self.assertEqual(objects["ip_iga_bas__n__maf"].address, ron98 - 0x500)
                self.assertEqual(objects["axis_ip_iga_bas_ron_98__n__maf_x"].address, x_axis)
                self.assertEqual(objects["axis_ip_iga_bas_ron_98__n__maf_y"].address, y_axis)

    def test_xml_agrees_for_bn(self) -> None:
        ron98, x_axis, y_axis = KNOWN["4560BN00S"]
        xml = xml_ignition("4560BN00")
        self.assertEqual(xml["ron98"], (ron98, x_axis, y_axis))
        self.assertEqual(xml["ron91"], (ron98 - 0x140, x_axis, y_axis))

    def test_map_match_baselines_pass(self) -> None:
        """Калибровка структурного метода PR #49 на эталонах: 30/30."""
        report = map_match.validate_baselines()
        self.assertEqual(report["overall_status"], "PASSED")
        self.assertEqual((report["passed_checks"], report["total_checks"]), (30, 30))


class TestIgnitionFamilies(IgnitionProofBase):
    """Пять семейств с картой RON98 в политике: доказательства (1)–(4)."""

    def test_stock_counts(self) -> None:
        self.assertEqual(tuple(sorted(IGNITION_FAMILIES)), tuple(sorted(pl.IGNITION_STOCK_DERIVED_FAMILIES)))
        for family, count in IGNITION_FAMILIES.items():
            with self.subTest(family=family):
                self.assertEqual(len(self.stocks(family)), count)
                self.assertGreaterEqual(count, 2)

    def test_ron98_address_unique_identical_and_in_policy(self) -> None:
        """(2) один адрес во всей калибровке и во всех стоках; совпадает с политикой."""
        for family in IGNITION_FAMILIES:
            objects = self.policy.for_family(family).objects
            with self.subTest(family=family):
                hits = self.ron98_hits(family)
                self.assertEqual(hits, [objects["ip_iga_bas_ron_98__n__maf"].address])
                self.assertEqual(objects["ip_iga_bas_ron_91__n__maf"].address, hits[0] - 0x140)
                self.assertEqual(objects["ip_iga_bas__n__maf"].address, hits[0] - 0x500)
                cand = self.candidates["families"][family]["maps"]
                self.assertEqual(int(cand["ip_iga_bas_ron_98__n__maf"]["address"], 16), hits[0])
                self.assertEqual(int(cand["ip_iga_bas_ron_91__n__maf"]["address"], 16), hits[0] - 0x140)

    def test_axes_unique_identical_and_in_policy(self) -> None:
        for family in IGNITION_FAMILIES:
            objects = self.policy.for_family(family).objects
            x_axis = objects["axis_ip_iga_bas_ron_98__n__maf_x"]
            y_axis = objects["axis_ip_iga_bas_ron_98__n__maf_y"]
            with self.subTest(family=family):
                self.assertEqual(self.rpm_hits(family), [y_axis.address])
                self.assertEqual(find_load_axis(self.images(family), y_axis.address), [x_axis.address])
                self.assertEqual((x_axis.element_size_bits, x_axis.rows, x_axis.columns), (16, 1, COLUMNS))
                self.assertEqual((y_axis.element_size_bits, y_axis.rows, y_axis.columns), (16, 1, ROWS))
                self.assertAlmostEqual(x_axis.scale_a, LOAD_SCALE)
                self.assertTrue(x_axis.read_only and y_axis.read_only)

    def test_axis_pool_structure_matches_generation_reference(self) -> None:
        """Длины записей пула осей между осью нагрузки и осью оборотов — как у эталона."""
        reference = {}
        for prefix, family in GENERATION_REFERENCE.items():
            _r, x_axis, y_axis = KNOWN[family]
            reference[prefix] = chain_counts(self.images(family)[0], x_axis - 2, y_axis - 2)
        for family in IGNITION_FAMILIES:
            objects = self.policy.for_family(family).objects
            x_axis = objects["axis_ip_iga_bas_ron_98__n__maf_x"].address
            y_axis = objects["axis_ip_iga_bas_ron_98__n__maf_y"].address
            expected = reference[family[:4]]
            for name, image in self.stocks(family):
                with self.subTest(family=family, stock=name):
                    counts = chain_counts(image, x_axis - 2, y_axis - 2)
                    self.assertIsNotNone(counts)
                    assert counts is not None and expected is not None
                    self.assertEqual(counts[0], COLUMNS)
                    if family.startswith("4570LL"):
                        # Поколение LL: на одну запись [8] меньше, чем у LO00S
                        # (нет записи из восьми нулей) — остальная цепочка та же.
                        self.assertEqual(len(counts), len(expected) - 1)
                        self.assertTrue(any(counts == expected[:i] + expected[i + 1:]
                                            for i in range(len(expected))), counts)
                    else:
                        self.assertEqual(counts, expected)

    def test_xml_independent_source_for_bl00(self) -> None:
        objects = self.policy.for_family("4560BL00S").objects
        xml = xml_ignition("4560BL00")
        self.assertEqual(xml["ron98"], (
            objects["ip_iga_bas_ron_98__n__maf"].address,
            objects["axis_ip_iga_bas_ron_98__n__maf_x"].address,
            objects["axis_ip_iga_bas_ron_98__n__maf_y"].address,
        ))
        self.assertEqual(xml["ron91"][0], objects["ip_iga_bas_ron_91__n__maf"].address)

    def test_map_and_axes_physical_on_every_stock(self) -> None:
        """(1) карта читается, 20x16, значения физичны, оси монотонны на каждом стоке."""
        for family in IGNITION_FAMILIES:
            objects = self.policy.for_family(family).objects
            entry = objects["ip_iga_bas_ron_98__n__maf"]
            x_axis = objects["axis_ip_iga_bas_ron_98__n__maf_x"]
            y_axis = objects["axis_ip_iga_bas_ron_98__n__maf_y"]
            self.assertEqual((entry.rows, entry.columns, entry.element_size_bits), (ROWS, COLUMNS, 8))
            self.assertEqual((entry.scale_a, entry.scale_b), (0.375, -35.625))
            for name, image in self.stocks(family):
                with self.subTest(family=family, stock=name):
                    cells = image[entry.address : entry.address + MAP_BYTES]
                    self.assertEqual(len(cells), MAP_BYTES)
                    degrees = [to_deg(raw) for raw in cells]
                    self.assertGreaterEqual(min(degrees), PHYS_MIN_DEG)
                    self.assertLessEqual(max(degrees), PHYS_MAX_DEG)
                    load = [u16(image, x_axis.address + 2 * i) * LOAD_SCALE for i in range(COLUMNS)]
                    rpm = [u16(image, y_axis.address + 2 * i) for i in range(ROWS)]
                    self.assertTrue(all(a < b for a, b in zip(load, load[1:])), load)
                    self.assertTrue(all(a < b for a, b in zip(rpm, rpm[1:])), rpm)
                    self.assertTrue(20 <= load[0] <= 100 and 400 <= load[-1] <= 1500, load)
                    self.assertTrue(250 <= rpm[0] <= 1000 and 5500 <= rpm[-1] <= 7200, rpm)
                    for row in range(ROWS):
                        self.assertGreater(cells[row * COLUMNS], cells[row * COLUMNS + COLUMNS - 1])

    def test_ron98_not_below_ron91(self) -> None:
        """(3) RON98 >= RON91 не менее чем в 95% ячеек на каждом стоке."""
        for family in IGNITION_FAMILIES:
            objects = self.policy.for_family(family).objects
            r98 = objects["ip_iga_bas_ron_98__n__maf"].address
            r91 = objects["ip_iga_bas_ron_91__n__maf"].address
            for name, image in self.stocks(family):
                with self.subTest(family=family, stock=name):
                    pairs = zip(image[r98 : r98 + MAP_BYTES], image[r91 : r91 + MAP_BYTES])
                    share = sum(1 for a, b in pairs if a >= b) / MAP_BYTES
                    self.assertGreaterEqual(share, 0.95)

    def test_no_overlap_with_other_objects(self) -> None:
        """(4) карта RON98 не пересекается ни с одним объектом политики и картами-кандидатами."""
        for family in IGNITION_FAMILIES:
            fp = self.policy.for_family(family)
            entry = fp.objects["ip_iga_bas_ron_98__n__maf"]
            mine = set(entry.byte_range)
            with self.subTest(family=family):
                self.assertTrue(entry.address >= 0x200 and entry.address + MAP_BYTES <= ck.CAL_SIZE)
                self.assertFalse(mine & set(SIGNATURE))
                self.assertFalse(mine & set(CRC_CELL))
                for symbol, other in fp.objects.items():
                    if symbol == entry.symbol:
                        continue
                    self.assertFalse(mine & set(other.byte_range), symbol)
                for symbol, cand in self.candidates["families"][family]["maps"].items():
                    if symbol == entry.symbol:
                        continue
                    start = int(cand["address"], 16)
                    self.assertFalse(mine & set(range(start, start + cand["total_bytes"])), symbol)

    def test_policy_roles_and_protection(self) -> None:
        for family in IGNITION_FAMILIES:
            fp = self.policy.for_family(family)
            with self.subTest(family=family):
                self.assertEqual(fp.roles["ignition"], ("ip_iga_bas_ron_98__n__maf",))
                self.assertEqual(fp.packs["ignition"], ("ip_iga_bas_ron_98__n__maf",))
                for role in ("fuel", "vanos", "dtc_driver", "cat_delete", "sap_delete"):
                    self.assertNotIn(role, fp.roles)
                for symbol in ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf",
                               "axis_ip_iga_bas_ron_98__n__maf_x", "axis_ip_iga_bas_ron_98__n__maf_y"):
                    self.assertTrue(fp.objects[symbol].read_only, symbol)
                    with self.assertRaises(pl.PolicyError) as ctx:
                        fp.entry(symbol)
                    self.assertEqual(ctx.exception.code, pl.CODE_READ_ONLY)
                writable = {s for s, e in fp.objects.items() if not e.read_only}
                for symbol in writable:
                    self.assertFalse(re.search(
                        r"ron_91|max_knk|ip_fac_|ip_tqi_|tq_req_driv_mon|id_pvs_|^ip_iga_bas__n", symbol), symbol)
                self.assertNotIn("ip_iga_bas_max_knk__n__maf", fp.objects)  # адрес не доказан


class TestSingleStockFamiliesWaitForData(IgnitionProofBase):
    def test_ignition_requires_second_image(self) -> None:
        for family in SINGLE_STOCK:
            fp = self.policy.for_family(family)
            self.assertNotIn("ignition", fp.roles)
            self.assertNotIn("ip_iga_bas_ron_98__n__maf", fp.objects)
            for name, image in self.stocks(family):
                with self.subTest(family=family, stock=name):
                    result = stages.build(
                        "stage1", image,
                        {"stage": "stage1", "variant": "v1", "customer": "тест",
                         "intents": [{"kind": "octane", "fuel": "ron98"},
                                     {"kind": "ignition", "delta_deg": 3.0}]},
                        policy=self.policy, source_name=name)
                    self.assertEqual(result.status, "REQUIRES_DATA")
                    self.assertEqual([item.id for item in result.pending], ["ignition"])
                    why = result.pending[0].detail
                    self.assertIn("один сток", why)
                    self.assertIn("нужен второй образ", why)
                    self.assertEqual(result.written_packages, ())
                    if result.result is not None:
                        self.assertEqual(result.result.image, image)


class TestStage1OutputIndependentCheck(IgnitionProofBase):
    """Сборка Stage 1 v1 RON98 +3.0° на каждом стоке и независимая проверка байтов."""

    REQUEST = {"stage": "stage1", "variant": "v1", "customer": "тест",
               "intents": [{"kind": "octane", "fuel": "ron98"},
                           {"kind": "ignition", "delta_deg": 3.0}]}

    def build(self, image: bytes, name: str, intents: list[dict]) -> stages.StageResult:
        request = dict(self.REQUEST, intents=intents)
        return stages.build("stage1", image, request, policy=self.policy, source_name=name)

    def check_output(self, family: str, stock: bytes, out: bytes, cap_deg: float) -> dict:
        objects = self.policy.for_family(family).objects
        entry = objects["ip_iga_bas_ron_98__n__maf"]
        high = objects["val_mo3_cal_cks_high"].address
        low = objects["val_mo3_cal_cks_low"].address
        self.assertEqual(low, high + 4)
        self.assertEqual(len(out), len(stock))
        allowed = set(entry.byte_range) | set(CRC_CELL) | set(range(high, high + 8))
        changed = {i for i, (a, b) in enumerate(zip(stock, out)) if a != b}
        self.assertTrue(changed)
        self.assertLessEqual(changed, allowed, sorted(hex(i) for i in changed - allowed))
        self.assertTrue(all(i < ck.CAL_SIZE for i in changed))
        self.assertEqual(out[0x174:0x1B4], stock[0x174:0x1B4])
        # суммы — своей реализацией
        self.assertEqual(be32(out, 0x100), crc32_mpeg2(out[0x200:ck.CAL_SIZE]))
        self.assertEqual((be32(out, high), be32(out, low)), mo3_pair(out))
        # содержимое карты
        before = stock[entry.address : entry.address + MAP_BYTES]
        after = out[entry.address : entry.address + MAP_BYTES]
        adds = [(a - b) * 0.375 for a, b in zip(after, before)]
        self.assertGreaterEqual(min(adds), 0.0)
        self.assertLessEqual(max(adds), cap_deg + 1e-9)
        x_axis = objects["axis_ip_iga_bas_ron_98__n__maf_x"].address
        load = [u16(stock, x_axis + 2 * i) * LOAD_SCALE for i in range(COLUMNS)]
        layout = self.spec["packages"]["ignition"]["layout"]
        step = float(layout["neighbour_max_step_deg"])
        low_mg = float(layout["shape_low_mg_stroke"])
        col_add = []
        for column in range(COLUMNS):
            column_adds = {adds[row * COLUMNS + column] for row in range(ROWS)}
            # форма по нагрузке одна на столбец: все строки столбца получают одно
            # и то же (ячейка не может уйти выше сетки — та же добавка в кодах)
            self.assertEqual(len(column_adds), 1, f"столбец {column}: {sorted(column_adds)}")
            col_add.append(column_adds.pop())
            if load[column] <= low_mg:
                self.assertEqual(col_add[-1], 0.0, f"столбец {column}, {load[column]:.1f} мг/такт")
        jumps = [abs(a - b) for a, b in zip(col_add, col_add[1:])]
        self.assertLessEqual(max(jumps), step + 1e-9)
        # RON91 и fallback-карта не тронуты (уже покрыто «вне плана», явная проверка)
        for symbol in ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"):
            obj = objects[symbol]
            self.assertEqual(out[obj.address : obj.address + MAP_BYTES],
                             stock[obj.address : obj.address + MAP_BYTES])
        return {"changed_cells": sum(1 for a in adds if a > 0), "max_add": max(adds),
                "max_jump": max(jumps)}

    def test_own_crc_matches_reference_vectors(self) -> None:
        # CRC-32/MPEG-2, check-значение каталога Williams/reveng для "123456789".
        self.assertEqual(crc32_mpeg2(b"123456789"), 0x0376E6E7)
        self.assertEqual(_crc32_mpeg2(b"123456789"), 0x0376E6E7)

    def test_ron98_plus_3_on_every_stock(self) -> None:
        for family in IGNITION_FAMILIES:
            for name, image in self.stocks(family):
                with self.subTest(family=family, stock=name):
                    self.assertEqual(be32(image, 0x100), crc32_mpeg2(image[0x200:ck.CAL_SIZE]))
                    result = self.build(image, name, self.REQUEST["intents"])
                    self.assertEqual(result.status, "BUILT", result.findings)
                    self.assertEqual({p.id for p in result.written_packages}, {"ignition"})
                    self.assertIn("MBT_LAYOUT_REQUIRES_DATA", {f["code"] for f in result.findings})
                    assert result.result is not None
                    stats = self.check_output(family, image, result.result.image, 3.0)
                    self.assertGreater(stats["changed_cells"], 0)

    def test_octane_caps_and_boundaries(self) -> None:
        for family in IGNITION_FAMILIES:
            name, image = self.stocks(family)[0]
            with self.subTest(family=family):
                ron95 = self.build(image, name, [{"kind": "octane", "fuel": "ron95"},
                                                 {"kind": "ignition", "delta_deg": 1.5}])
                self.assertEqual(ron95.status, "BUILT", ron95.findings)
                assert ron95.result is not None
                self.check_output(family, image, ron95.result.image, 1.5)
                over95 = self.build(image, name, [{"kind": "octane", "fuel": "ron95"},
                                                  {"kind": "ignition", "delta_deg": 1.51}])
                self.assertNotEqual(over95.status, "BUILT")
                over = self.build(image, name, [{"kind": "octane", "fuel": "ron98"},
                                                {"kind": "ignition", "delta_deg": 3.01}])
                self.assertEqual(over.status, "REJECTED")
                ron91 = self.build(image, name, [{"kind": "octane", "fuel": "ron91"},
                                                 {"kind": "ignition", "delta_deg": 0.75}])
                self.assertEqual(ron91.status, "REJECTED")
                self.assertIn("OCTANE_PROFILE_NO_ADDITION", {f["code"] for f in ron91.findings})

    def test_pedal_and_maps_without_proof_stay_closed(self) -> None:
        for family in IGNITION_FAMILIES:
            name, image = self.stocks(family)[0]
            with self.subTest(family=family):
                for intent in ({"kind": "fuel", "ti_fl_enrich_lambda": 0.02},
                               {"kind": "vanos", "shift_in_deg": 2.0}):
                    result = stages.build("stage1", image, {"intents": [intent]},
                                          policy=self.policy, source_name=name)
                    self.assertNotEqual(result.status, "BUILT", intent)
                    self.assertEqual(result.written_packages, ())

    def test_service_matches_stages(self) -> None:
        """server.core.TorquexService выдаёт те же байты, что stages.build."""
        from server import core

        service = core.TorquexService(limits=core.Limits(
            vin_per_day=10_000, account_per_day=10_000, account_per_hour=10_000))
        for family in IGNITION_FAMILIES:
            name, image = self.stocks(family)[0]
            with self.subTest(family=family):
                job = service.build(image, dict(self.REQUEST), account="proof", filename=name)
                self.assertEqual(job.status, "BUILT", job.log[-2000:])
                direct = self.build(image, name, self.REQUEST["intents"])
                assert direct.result is not None and job.image is not None
                self.assertEqual(job.image, direct.result.image)
                self.check_output(family, image, job.image, 3.0)


if __name__ == "__main__":
    unittest.main()
