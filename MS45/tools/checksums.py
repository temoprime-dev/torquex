#!/usr/bin/env python3
"""Контрольные суммы MS45 (Siemens MS45.0 / MS45.1, SW 456 / 457).

Схема доказана на **239 файлах** из ``MS45/firmware``, ``MS45/firmware_spdaten/cal``
и на реальном полном чтении 1 МиБ ``MS45/firmware_dumps`` (все семейства
4560BL00S / 4560BL30S / 4560BM00S / 4560BN00S / 4560BO00S / 4570LL00S /
4570LL30S / 4570LM00S / 4570LN00S / 4570LO00S + RR-части).  Отчёт о проверке --
``MS45/docs/CHECKSUMS_MS45_RU.md``.

Две суммы и обе внутри области калибровки
-----------------------------------------

**1. Основная (``crc32``), смещение калибровки ``0x100``, big-endian u32.**
CRC-32/MPEG-2 по области ``0x200..0x1D000`` (в полном образе --
``0x40200..0x5D000``): полином ``0x04C11DB7``, init ``0xFFFFFFFF``, без
отражения (refin = refout = False), xor-out ``0x00000000``.  Совпадает с
описанием ms4x.net («Siemens MS45»): «The primary checksum ... is found at
0x40100 ... polynomial 04C11DB7, default initial value 0xFFFFFFFF, no
reflection, XOR out 00000000», проверено побайтово на всех 239 файлах.

**2. Дополнительная (``mo3``), смещение зависит от семейства.**
Аддитивная сумма 32-битных big-endian слов по области ``0x240..0x7C8``
(354 слова) с начальным значением ``0x0123456789ABCDEF``; результат --
одно 64-битное значение, которое хранится двумя big-endian u32:
сначала старшая половина, затем младшая.  Диапазон задан парой указателей
в программной области (полный образ ``0x60620`` / ``0x60624`` =
``0xFFE40240`` / ``0xFFE407C8``, база ``0xFFE00000``); во всех 10
программных образах SP-Daten эти указатели одинаковы, поэтому диапазон
инвариантен.  Сама пара лежит в середине калибровки, и её смещение
**своё для каждого семейства прошивки** (см. :data:`MO3_STORE_OFFSET`) --
адреса между версиями не переносятся.

Что важно для записи
--------------------

* Область ``mo3`` (``0x240..0x7C8``) не пересекается с местом хранения пары,
  поэтому сумма ``mo3`` от собственной записи не зависит.
* Область ``crc32`` (``0x200..0x1D000``) **включает** место хранения пары
  ``mo3``, поэтому порядок пересчёта строгий: сначала записать пару
  ``mo3``, затем -- ``crc32``.
* Байты ``0x174..0x1B3`` (64 байта подписи, счётчик ``0x00000010`` в
  ``0x170``) не покрыты ни одной из сумм и **никогда не изменяются**:
  это RSA-подпись калибровки.  Заголовок ``0x100`` описывает сам себя:
  ``0x104`` -- дескриптор области CRC (``00 00 00 01 | FFE40200 | FFE5CFFF``),
  ``0x130`` -- дескриптор подписи (две области ``FFF40000..FFF4016F`` и
  ``FFF40200..FFF5CFFF``, смещение подписи ``0x170``, длина ``0x1CE00``).
* Область ``0x00000..0x3FFFF`` (загрузчик/системная часть внешней флеш),
  программная область ``0x60000..0xFFFFF``, внутренняя флеш MPC (448 КиБ) и
  служебная запись программатора ``0xFFF40..0xFFF75`` не записываются
  никогда (см. :class:`OutsideCalRegionError`).

Суммы ``_mon`` у MS45 не используются; ничего, кроме двух описанных сумм, в
калибровке не найдено, поэтому третьих сумм инструмент не выдумывает.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MS45.tools.console import prepare_stdout  # noqa: E402

from MS45.tools import pathfmt  # noqa: E402  (после bootstrap-а)

# ---------------------------------------------------------------------------
# Геометрия образа
# ---------------------------------------------------------------------------

#: Размер калибровки (внешняя флеш, область параметров), байт.
CAL_SIZE = 0x1D000  # 118 784

#: Размер полного чтения внешней флеш, байт.
EXT_FLASH_SIZE = 0x100000  # 1 МиБ

#: Смещение калибровки в полном чтении внешней флеш.
EXT_CAL_OFFSET = 0x40000

#: Размер внутренней флеш MPC (никогда не записывается).
MPC_FLASH_SIZE = 0x70000  # 448 КиБ

#: Служебная область программатора (ZIF/ID) в полном образе -- только чтение.
PROGRAMMER_AREA = (0xFFF40, 0xFFF75)

#: База внешней флеш в адресном пространстве CPU.
EXT_BASE = 0x02000000

#: Адрес калибровки в адресном пространстве CPU.
CAL_BASE = 0x02040000

#: Начало области программы (никогда не записывается).
PROGRAM_AREA_BASE = 0x60000

# ---------------------------------------------------------------------------
# Основная сумма: CRC-32/MPEG-2
# ---------------------------------------------------------------------------

CRC32_POLY = 0x04C11DB7
CRC32_INIT = 0xFFFFFFFF
CRC32_XOROUT = 0x00000000
CRC32_STORE_OFFSET = 0x100
CRC32_STORE_SIZE = 4
CRC32_REGION = (0x200, 0x1D000)

# ---------------------------------------------------------------------------
# Дополнительная сумма: MO3
# ---------------------------------------------------------------------------

MO3_SEED = 0x0123456789ABCDEF
MO3_TERM = 0xFFFFFFFFFFFFFFFF
MO3_REGION = (0x240, 0x7C8)

#: Пара указателей диапазона MO3 в полном образе (смещения файла).
MO3_POINTER_OFFSETS = (0x60620, 0x60624)
#: Ожидаемые значения указателей (адреса CPU, база 0xFFE00000).
MO3_POINTER_EXPECTED = (0xFFE40240, 0xFFE407C8)

#: Значение переключателя ``val_mo3_swi_cal_cks``, которым сумма отключается.
MO3_SWITCH_DISABLE = 0xA5

#: Смещение хранения пары (старшая половина, затем младшая) по семействам.
#: Определено по данным: в каждом семействе пара найдена ровно в одном месте,
#: в одном и том же для всех файлов семейства.
MO3_STORE_OFFSET: dict[str, int] = {
    "4560BL00S": 0x587C,
    "4560BL30S": 0x589C,
    "4560BM00S": 0x5780,
    "4560BN00S": 0x57AC,
    "4560BO00S": 0x5820,
    "4570LL00S": 0x5A80,
    "4570LL30S": 0x5AA4,
    "4570LM00S": 0x5714,
    "4570LN00S": 0x5740,
    "4570LO00S": 0x57DC,
}

#: Смещение переключателя ``val_mo3_swi_cal_cks`` по семействам (0x00 -- сумма
#: включена, 0xA5 -- отключена).
#:
#: **Подтверждено только у 4560BO00S и 4570LO00S** (адрес даёт независимый
#: первоисточник определений; структурно байт стоит сразу за маркером
#: ``07 0e 1b 46 52 64 82 96`` в блоке дескриптора MO3). У остальных семейств
#: адрес выведен правилом «пара минус 0x1D» и не подтверждён: у 4560BN00S
#: структура дескриптора отличается, а у BL/LL/LM/LN байт по этому правилу даже
#: не равен нулю. Значение используется только в справке отчёта (на проверку
#: суммы не влияет) и помечается как неподтверждённое — см. ``switch_verified``.
MO3_SWITCH_OFFSET: dict[str, int] = {
    "4560BL00S": 0x585F,
    "4560BL30S": 0x587F,
    "4560BM00S": 0x5763,
    "4560BN00S": 0x578F,
    "4560BO00S": 0x5803,
    "4570LL00S": 0x5A63,
    "4570LL30S": 0x5A87,
    "4570LM00S": 0x56F7,
    "4570LN00S": 0x5723,
    "4570LO00S": 0x57BF,
}

#: Семейства, у которых адрес переключателя MO3 подтверждён первоисточником.
MO3_SWITCH_VERIFIED = ("4560BO00S", "4570LO00S")

#: Регулярное выражение семейства прошивки в ASCII-идентификаторе образа.
FAMILY_RE = re.compile(rb"45[67]0[BL][A-Z]\d\dS")

# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class MS45Error(Exception):
    """Базовая ошибка инструмента MS45."""

    code = "MS45_ERROR"


class ImageFormatError(MS45Error):
    """Неподдерживаемый размер/формат образа."""

    code = "IMAGE_SIZE_UNSUPPORTED"


class MpcImageError(MS45Error):
    """Внутренняя флеш MPC (448 КиБ) -- запись и пересчёт запрещены."""

    code = "MPC_IMAGE_UNSUPPORTED"


class OutsideCalRegionError(MS45Error):
    """Попытка выйти за пределы области калибровки."""

    code = "OUTSIDE_CAL_REGION"


class SourceChecksumError(MS45Error):
    """Исходный образ не проходит собственную проверку сумм."""

    code = "SOURCE_CRC_INVALID"


class ChecksumLayoutError(MS45Error):
    """Не удалось однозначно определить место хранения суммы."""

    code = "CHECKSUM_LAYOUT_UNKNOWN"


# ---------------------------------------------------------------------------
# Реализация CRC-32/MPEG-2
# ---------------------------------------------------------------------------


def _build_crc_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ CRC32_POLY) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def crc32_mpeg2_bitwise(
    data: bytes, init: int = CRC32_INIT, xorout: int = CRC32_XOROUT
) -> int:
    """Побитовая (эталонная) реализация CRC-32/MPEG-2."""
    crc = init & 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ CRC32_POLY) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc ^ xorout


def crc32_mpeg2(data: bytes, init: int = CRC32_INIT, xorout: int = CRC32_XOROUT) -> int:
    """Табличная CRC-32/MPEG-2 (полином 0x04C11DB7, без отражения)."""
    crc = init & 0xFFFFFFFF
    table = _CRC_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) ^ byte) & 0xFF]
    return crc ^ xorout


def crc32_stored(cal: bytes) -> int:
    """Хранимая основная сумма (big-endian u32 по смещению 0x100)."""
    return int.from_bytes(cal[CRC32_STORE_OFFSET : CRC32_STORE_OFFSET + CRC32_STORE_SIZE], "big")


def crc32_computed(cal: bytes) -> int:
    """Основная сумма, посчитанная по области 0x200..0x1D000."""
    start, end = CRC32_REGION
    return crc32_mpeg2(cal[start:end])


# ---------------------------------------------------------------------------
# Реализация MO3
# ---------------------------------------------------------------------------


def mo3_total(cal: bytes) -> int:
    """Аддитивная сумма MO3 (64 бита) по области 0x240..0x7C8."""
    start, end = MO3_REGION
    if end > len(cal):
        raise ImageFormatError(f"образ короче области MO3: {len(cal):#x} < {end:#x}")
    total = MO3_SEED
    for offset in range(start, end, 4):
        total = (total + int.from_bytes(cal[offset : offset + 4], "big")) & MO3_TERM
    return total


def mo3_pair(total: int) -> tuple[int, int]:
    """Разложение 64-битной суммы на пару (старшая, младшая)."""
    return (total >> 32) & 0xFFFFFFFF, total & 0xFFFFFFFF


def mo3_stored(cal: bytes, offset: int) -> tuple[int, int]:
    """Хранимая пара MO3 по указанному смещению."""
    return (
        int.from_bytes(cal[offset : offset + 4], "big"),
        int.from_bytes(cal[offset + 4 : offset + 8], "big"),
    )


def locate_mo3_store(cal: bytes, pair: tuple[int, int]) -> list[int]:
    """Все смещения, где в калибровке лежит ровно такая пара.

    В стоковых образах место всегда одно (239 из 239 файлов), поэтому длина
    списка -- самостоятельная проверка целостности.
    """
    pattern = pair[0].to_bytes(4, "big") + pair[1].to_bytes(4, "big")
    hits: list[int] = []
    start = 0
    while True:
        index = cal.find(pattern, start)
        if index < 0:
            return hits
        hits.append(index)
        start = index + 1


# ---------------------------------------------------------------------------
# Определение семейства / разбор образа
# ---------------------------------------------------------------------------


def read_family(image: bytes) -> str | None:
    """Семейство прошивки из ASCII-идентификатора в начале образа."""
    match = FAMILY_RE.search(image[:0x50])
    return match.group(0).decode("ascii") if match else None


def read_image_id(image: bytes) -> str:
    """Печатаемая часть ASCII-идентификатора (для отчётов)."""
    raw = image[:0x50]
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)


def image_kind(image: bytes) -> str:
    """``cal`` (116 КиБ), ``full`` (1 МиБ) или исключение."""
    size = len(image)
    if size == CAL_SIZE:
        return "cal"
    if size == EXT_FLASH_SIZE:
        return "full"
    if size == MPC_FLASH_SIZE:
        raise MpcImageError(
            "внутренняя флеш MPC (448 КиБ) не поддерживается: "
            "запись и пересчёт сумм калибровки для неё запрещены"
        )
    raise ImageFormatError(
        f"неподдерживаемый размер образа {size:#x}: ожидается калибровка "
        f"{CAL_SIZE:#x} или полное чтение внешней флеш {EXT_FLASH_SIZE:#x}"
    )


def extract_cal(image: bytes) -> tuple[bytes, int]:
    """Возвращает (калибровка, смещение калибровки в образе)."""
    kind = image_kind(image)
    offset = 0 if kind == "cal" else EXT_CAL_OFFSET
    return image[offset : offset + CAL_SIZE], offset


def check_mo3_pointers(image: bytes) -> tuple[bool, tuple[int, int]]:
    """Проверка пары указателей диапазона MO3 в полном образе."""
    if len(image) != EXT_FLASH_SIZE:
        return True, MO3_POINTER_EXPECTED
    values = tuple(
        int.from_bytes(image[off : off + 4], "big") for off in MO3_POINTER_OFFSETS
    )
    return values == MO3_POINTER_EXPECTED, values  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Отчёты
# ---------------------------------------------------------------------------


@dataclass
class RecomputeResult:
    """Результат пересчёта сумм (без записи на диск)."""

    family: str | None
    kind: str
    crc32_before: int
    crc32_after: int
    mo3_before: tuple[int, int]
    mo3_after: tuple[int, int]
    mo3_store_offset: int
    switch_offset: int | None
    switch_value: int | None
    changed_offsets: list[int] = field(default_factory=list)


@dataclass
class CheckReport:
    """Отчёт о проверке образа."""

    path: str | None
    size: int
    kind: str
    family: str | None
    image_id: str = ""
    crc32_stored: int | None = None
    crc32_computed: int | None = None
    crc32_ok: bool = False
    mo3_stored: tuple[int, int] | None = None
    mo3_computed: tuple[int, int] | None = None
    mo3_store_offset: int | None = None
    mo3_candidates: list[int] = field(default_factory=list)
    mo3_ok: bool = False
    switch_offset: int | None = None
    switch_value: int | None = None
    #: Подтверждён ли адрес переключателя первоисточником (None -- семейство без таблицы).
    switch_verified: bool | None = None
    pointers_ok: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.crc32_ok and self.mo3_ok

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "kind": self.kind,
            "family": self.family,
            "image_id": self.image_id,
            "crc32": {
                "stored": None if self.crc32_stored is None else f"{self.crc32_stored:08X}",
                "computed": None if self.crc32_computed is None else f"{self.crc32_computed:08X}",
                "offset": f"{CRC32_STORE_OFFSET:#x}",
                "ok": self.crc32_ok,
            },
            "mo3": {
                "stored": None if self.mo3_stored is None else [f"{v:08X}" for v in self.mo3_stored],
                "computed": None if self.mo3_computed is None else [f"{v:08X}" for v in self.mo3_computed],
                "store_offset": None if self.mo3_store_offset is None else f"{self.mo3_store_offset:#x}",
                "candidates": [f"{v:#x}" for v in self.mo3_candidates],
                "ok": self.mo3_ok,
            },
            "switch": {
                "offset": None if self.switch_offset is None else f"{self.switch_offset:#x}",
                "value": None if self.switch_value is None else f"{self.switch_value:#04x}",
                "verified": self.switch_verified,
            },
            "pointers_ok": self.pointers_ok,
            "ok": self.ok,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Проверка и пересчёт
# ---------------------------------------------------------------------------


def verify_bytes(image: bytes, path: str | None = None) -> CheckReport:
    """Проверить обе суммы, ничего не записывая (путь — всегда POSIX)."""
    cal, _ = extract_cal(image)
    report = CheckReport(
        path=None if path is None else pathfmt.to_posix(path),
        size=len(image),
        kind=image_kind(image),
        family=read_family(cal),
        image_id=read_image_id(cal),
    )

    report.crc32_stored = crc32_stored(cal)
    report.crc32_computed = crc32_computed(cal)
    report.crc32_ok = report.crc32_stored == report.crc32_computed

    total = mo3_total(cal)
    report.mo3_computed = mo3_pair(total)
    report.mo3_candidates = locate_mo3_store(cal, report.mo3_computed)
    expected = MO3_STORE_OFFSET.get(report.family or "")
    if expected is not None:
        report.mo3_store_offset = expected
        report.mo3_stored = mo3_stored(cal, expected)
        report.mo3_ok = report.mo3_stored == report.mo3_computed
        if not report.mo3_ok and report.mo3_candidates:
            report.notes.append(
                "пара найдена по другим смещениям, отличным от табличного: "
                + ", ".join(f"{v:#x}" for v in report.mo3_candidates)
            )
    elif len(report.mo3_candidates) == 1:
        report.mo3_store_offset = report.mo3_candidates[0]
        report.mo3_stored = mo3_stored(cal, report.mo3_store_offset)
        report.mo3_ok = True
        report.notes.append(
            f"семейство не в таблице, место пары определено поиском: {report.mo3_store_offset:#x}"
        )
    elif not report.mo3_candidates:
        report.mo3_ok = False
        report.notes.append("пара MO3 в калибровке не найдена -- сумма не подтверждена")
    else:
        report.mo3_ok = False
        report.notes.append(
            "пара MO3 встречается более одного раза -- место хранения неоднозначно"
        )

    if report.family:
        switch_offset = MO3_SWITCH_OFFSET.get(report.family)
        if switch_offset is not None:
            report.switch_offset = switch_offset
            report.switch_value = cal[switch_offset]
            report.switch_verified = report.family in MO3_SWITCH_VERIFIED
            if not report.switch_verified:
                report.notes.append(
                    "адрес переключателя MO3 у этого семейства не подтверждён "
                    "первоисточником — значение приведено для справки"
                )
            if report.switch_value == MO3_SWITCH_DISABLE:
                report.notes.append(
                    "val_mo3_swi_cal_cks = 0xA5: сумма MO3 в ЭБУ отключена"
                )

    pointers_ok, values = check_mo3_pointers(image)
    report.pointers_ok = pointers_ok
    if not pointers_ok:
        report.notes.append(
            "неожиданные указатели диапазона MO3: "
            + " / ".join(f"{v:#010x}" for v in values)
        )
    return report


def verify_image(path: str | Path) -> CheckReport:
    """Проверить файл образа."""
    data = Path(path).read_bytes()
    return verify_bytes(data, pathfmt.to_posix(path))


def recompute(
    image: bytes, *, base: bytes | None = None, allow_unknown_family: bool = True
) -> tuple[bytes, RecomputeResult]:
    """Пересчитать обе суммы и вернуть новый образ.

    Записываются только байты самих сумм (``0x100..0x104`` и место пары MO3).
    Больше ничего не меняется.

    Параметр ``base`` -- исходный (неправимый) образ.  Если он передан, то:

    * ``base`` обязан проходить собственную проверку обеих сумм, иначе
      ``SOURCE_CRC_INVALID``;
    * место хранения пары MO3 берётся из **проверенного** ``base``, а не из
      правимого образа (подмена места записи невозможна);
    * все различия между ``base`` и ``image`` обязаны лежать внутри области
      калибровки, иначе ``OUTSIDE_CAL_REGION``.

    Без ``base`` функция работает как низкоуровневый пересчёт (например,
    для восстановления файла после ручной правки) и место пары определяет по
    таблице семейства либо по единственному вхождению пары.
    """
    cal, cal_offset = extract_cal(image)
    family = read_family(cal)
    computed_crc = crc32_computed(cal)

    if base is not None:
        base_report = verify_bytes(base)
        if not base_report.ok:
            raise SourceChecksumError(
                f"исходный образ не проходит проверку сумм: "
                f"CRC {'ok' if base_report.crc32_ok else 'BAD'}, "
                f"MO3 {'ok' if base_report.mo3_ok else 'BAD'}"
            )
        if len(base) != len(image):
            raise ImageFormatError(
                f"размеры исходного и правленого образа различаются: {len(base):#x} != {len(image):#x}"
            )
        store_offset = base_report.mo3_store_offset
        assert store_offset is not None
        allowed = range(cal_offset, cal_offset + CAL_SIZE)
        escaped = [i for i in range(len(image)) if base[i] != image[i] and i not in allowed]
        if escaped:
            raise OutsideCalRegionError(
                f"правки выходят за пределы калибровки: {[hex(i) for i in escaped[:8]]}"
            )
        stored_crc = crc32_stored(cal)
        pair = mo3_pair(mo3_total(cal))
    else:
        stored_crc = crc32_stored(cal)
        total = mo3_total(cal)
        pair = mo3_pair(total)
        expected = MO3_STORE_OFFSET.get(family or "")
        candidates = locate_mo3_store(cal, pair)
        if expected is not None:
            store_offset = expected
        elif len(candidates) == 1 and allow_unknown_family:
            store_offset = candidates[0]
        elif len(candidates) > 1:
            raise ChecksumLayoutError(
                "пара MO3 встречается более одного раза: "
                + ", ".join(f"{v:#x}" for v in candidates)
            )
        else:
            raise ChecksumLayoutError(
                f"семейство {family!r} неизвестно и пара MO3 не найдена в калибровке"
            )

    buffer = bytearray(image)
    changed: list[int] = []

    def write(offset: int, payload: bytes) -> None:
        absolute = cal_offset + offset
        if not cal_offset <= absolute < cal_offset + CAL_SIZE:
            raise OutsideCalRegionError(f"смещение {offset:#x} вне калибровки")
        if buffer[absolute : absolute + len(payload)] != payload:
            buffer[absolute : absolute + len(payload)] = payload
            changed.append(absolute)

    # Порядок обязателен: пара MO3 лежит ВНУТРИ области CRC-32, поэтому CRC
    # считается заново уже после записи пары.
    write(store_offset, pair[0].to_bytes(4, "big") + pair[1].to_bytes(4, "big"))
    final_crc = crc32_mpeg2(buffer[cal_offset + CRC32_REGION[0] : cal_offset + CRC32_REGION[1]])
    write(CRC32_STORE_OFFSET, final_crc.to_bytes(4, "big"))

    result = RecomputeResult(
        family=family,
        kind=image_kind(image),
        crc32_before=stored_crc,
        crc32_after=final_crc,
        mo3_before=mo3_stored(cal, store_offset),
        mo3_after=pair,
        mo3_store_offset=store_offset,
        switch_offset=MO3_SWITCH_OFFSET.get(family or ""),
        switch_value=cal[MO3_SWITCH_OFFSET[family]] if family in MO3_SWITCH_OFFSET else None,
        changed_offsets=changed,
    )
    return bytes(buffer), result


def recompute_after_patch(source: bytes, patched: bytes) -> tuple[bytes, RecomputeResult]:
    """Строгий путь для ядра: проверить исходник, пересчитать суммы после правки.

    Именно эту функцию вызывает ядро генератора: исходный образ обязан
    проверяться полностью, правки обязаны лежать внутри калибровки.
    """
    return recompute(patched, base=source)


def repair_image(source: str | Path, destination: str | Path) -> RecomputeResult:
    """Пересчитать суммы в файле и записать результат."""
    data = Path(source).read_bytes()
    repaired, result = recompute(data)
    changed = [i for i in range(len(data)) if data[i] != repaired[i]]
    outside = [i for i in changed if not 0x100 <= i % CAL_SIZE < CAL_SIZE]
    if outside:  # pragma: no cover - защита от регрессий
        raise OutsideCalRegionError(f"изменения за пределами калибровки: {outside[:8]}")
    Path(destination).write_bytes(repaired)
    result.changed_offsets = changed
    return result


def checksum_cells(image: bytes) -> list[int]:
    """Абсолютные смещения всех байтов сумм (для пост-проверки ядра)."""
    _, cal_offset = extract_cal(image)
    family = read_family(image[cal_offset : cal_offset + CAL_SIZE])
    offset = MO3_STORE_OFFSET.get(family or "")
    cells = list(range(CRC32_STORE_OFFSET, CRC32_STORE_OFFSET + CRC32_STORE_SIZE))
    if offset is not None:
        cells += list(range(offset, offset + 8))
    return [cal_offset + cell for cell in cells]


# ---------------------------------------------------------------------------
# Отчёты и CLI
# ---------------------------------------------------------------------------


def report_as_markdown(reports: list[CheckReport]) -> str:
    """Таблица отчётов в Markdown."""
    lines = [
        "| файл | размер | тип | семейство | CRC-32 @0x100 | MO3 | смещение пары | примечания |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rep in reports:
        lines.append(
            "| {name} | {size} | {kind} | {family} | {crc} | {mo3} | {off} | {notes} |".format(
                name=Path(rep.path).name if rep.path else "-",
                size=f"{rep.size:#x}",
                kind=rep.kind,
                family=rep.family or "?",
                crc=("ok" if rep.crc32_ok else "BAD") + f" ({rep.crc32_computed:08X})",
                mo3="ok" if rep.mo3_ok else "BAD",
                off="-" if rep.mo3_store_offset is None else f"{rep.mo3_store_offset:#x}",
                notes="; ".join(rep.notes),
            )
        )
    return "\n".join(lines)


def _collect(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.bin") if p.is_file()))
        else:
            files.append(path)
    return files


def _print_report(report: CheckReport, explain: bool) -> None:
    status = "OK " if report.ok else "BAD"
    print(
        f"[{status}] {report.path}  {report.size:#x}  {report.kind}  "
        f"{report.family or '?'}  CRC={report.crc32_computed:08X}  "
        f"MO3={report.mo3_store_offset if report.mo3_store_offset is None else format(report.mo3_store_offset, '#x')}"
    )
    if explain:
        print(
            "        хранимая CRC={:08X} расчётная={:08X}; пара хранимая={} расчётная={}".format(
                report.crc32_stored or 0,
                report.crc32_computed or 0,
                "-".join(f"{v:08X}" for v in (report.mo3_stored or (0, 0))),
                "-".join(f"{v:08X}" for v in (report.mo3_computed or (0, 0))),
            )
        )
        for note in report.notes:
            print(f"        {note}")


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()

    parser = argparse.ArgumentParser(
        description="Проверка и пересчёт контрольных сумм MS45 (CRC-32 @0x100 и MO3)"
    )
    parser.add_argument("paths", nargs="+", help="файлы образов или каталоги с *.bin")
    parser.add_argument("--repair", metavar="КАТАЛОГ", help="пересчитать суммы в каталог")
    parser.add_argument("--json-out", metavar="ФАЙЛ", help="отчёт в JSON")
    parser.add_argument("--markdown", action="store_true", help="отчёт таблицей Markdown")
    parser.add_argument("--explain", action="store_true", help="подробности по каждому файлу")
    args = parser.parse_args(argv)

    files = _collect(args.paths)
    if not files:
        print("не найдено ни одного образа", file=sys.stderr)
        return 2

    reports: list[CheckReport] = []
    exit_code = 0
    for path in files:
        try:
            report = verify_image(path)
        except MS45Error as error:
            print(f"[ERR] {path}: {error.code}: {error}", file=sys.stderr)
            exit_code = 1
            continue
        reports.append(report)
        if not report.ok:
            exit_code = 1
        _print_report(report, args.explain)

    print(
        f"\nвсего {len(reports)}: OK {sum(1 for r in reports if r.ok)}, "
        f"BAD {sum(1 for r in reports if not r.ok)}"
    )

    if args.repair:
        target = Path(args.repair)
        target.mkdir(parents=True, exist_ok=True)
        for path in files:
            try:
                result = repair_image(path, target / path.name)
            except MS45Error as error:
                print(f"[ERR] {path}: {error.code}: {error}", file=sys.stderr)
                exit_code = 1
                continue
            print(
                f"[FIX] {path.name}: CRC {result.crc32_before:08X} -> {result.crc32_after:08X}, "
                f"MO3 @{result.mo3_store_offset:#x} -> "
                + "-".join(f"{v:08X}" for v in result.mo3_after)
            )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.as_dict() for r in reports], ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    if args.markdown:
        print(report_as_markdown(reports))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
