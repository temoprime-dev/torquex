#!/usr/bin/env python3
"""Контрольные суммы MS43 (Siemens MS43, M52TU / M54): проверка и пересчёт.

Схема доказана заново на 51 образе из ``MS43/firmware`` и
``MS43/firmware_archive`` (512 КиБ полные образы и 64 КиБ чтения калибровки,
версии 430037/430055/430056/430064/430066/430069).  Схему MS42 сюда не
переносили.

Алгоритм
--------

* CRC-16/ARC: полином 0x8005, отражённая форма 0xA001 (сдвиг вправо), xor-out
  0, начальное значение у каждой записи своё (см. ниже).  Табличная и
  побитовая реализации сверены тестами.
* Каждая запись описывает сама себя::

      u16 crc (LE)   -- хранимая сумма
      u16 count (LE) -- число сегментов, 1..16
      count * (u32 start, u32 end)  -- обе границы включительно

  CRC считается по конкатенации сегментов в объявленном порядке.

Где лежат записи
----------------

**Полный образ 512 КиБ** (``.bin`` 0x80000)::

    boot  @ 0x03C24  -- смещения файла,  0x00000..0x3FE6 у стока
    prog  @ 0x6FDE0  -- АДРЕСА ПАМЯТИ,   файл = адрес - 0x80000
    cal   @ 0x73FE0  -- смещения файла,  0x70000..0x7FFFF

**Чтение калибровки 64 КиБ** (``.bin`` 0x10000): только cal, но по смещению
``0x03FE0`` и адреса сегментов в ней тоже адреса памяти калибровки
(файл = адрес - 0x70000).  boot/prog в таком чтении отсутствуют.

Начальные значения (init)
-------------------------

Init не константа: прошивка читает его из строк идентификации образа как
ASCII-пару символов, прочитанную младшим байтом вперёд (LE16).  Формула
найдена по данным и проверена на всех 51 образе:

* boot  -- LE16 @ 0x3FE6, в стоке там ``--`` (0x2D2D);
* prog  -- LE16 @ 0x6FFB6, поле кода прошивки ``00``/``10``/``30``
  (0x3030/0x3031/0x3033) в записи ZIF @ 0x6FFB2;
* cal   -- LE16 из двух последних цифр шестизначного кода версии
  (``4300xx``) в строке идентификации калибровки: 0x7000C для 512 КиБ,
  0x0000C для 64 КиБ.  Например ``430069`` -> ``69`` -> 0x3936,
  ``430037`` -> ``37`` -> 0x3733.

  Это уточнение к описанию в ``v-arapidis/openremap-core``: там для
  калибровки указан BE16 @ 0x6FFBF, что совпадает с нашим значением только
  для 430069 (в строке ``...6965...`` соседние байты образуют палиндром).
  На 430037/0055/0056/0064/0066 правило BE16 даёт неверный init, LE16 из
  цифр версии -- верный.

Суммы ``_mon`` (0x6FDAE, 0x72FFC) -- это аддитивные суммы, которые прошивка
сверяет во время работы через XRAM; статически из дампа они не проверяются
(см. ``MS43/docs/CHECKSUM_RU.md``).  Инструмент их только показывает, со
статусом ``unverified``, и никогда не пересчитывает.

Ремонт односторонний и параноидальный: байты пишутся только в позиции
хранимых CRC, результат перепроверяется независимым проходом, и если после
записи изменился любой другой байт, инструмент падает, а не отдаёт файл.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from MS43.tools.console import prepare_stdout
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    from console import prepare_stdout  # type: ignore[no-redef]

# --------------------------------------------------------------------------
# Алгоритм
# --------------------------------------------------------------------------

CRC16_ARC_REFLECTED_POLY = 0xA001

#: Максимальное число сегментов в записи (по формату и по всем образам).
MAX_SEGMENTS = 16

#: Пределы файловых офсетов, чтобы повреждённая запись не читала мусор.
IMAGE_SIZE_FULL = 0x80000
IMAGE_SIZE_CAL64 = 0x10000

#: prog-сегменты записаны адресами памяти: файл = адрес - PROGRAM_ADDRESS_BASE.
PROGRAM_ADDRESS_BASE = 0x80000

#: cal-сегменты в 64 КиБ чтении записаны адресами памяти калибровки.
CAL64_ADDRESS_BASE = 0x70000


def _build_crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ CRC16_ARC_REFLECTED_POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_crc_table()

#: Буфер нулей для ``crc16_zeros`` / ``solve_init``.
_ZERO_CHUNK = bytes(4096)


def crc16_arc(data: bytes, init: int = 0) -> int:
    """CRC-16/ARC побитово (эталон для тестов)."""
    crc = init & 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ CRC16_ARC_REFLECTED_POLY if crc & 1 else crc >> 1
    return crc & 0xFFFF


def crc16_arc_table(data: bytes, init: int = 0) -> int:
    """CRC-16/ARC по таблице; для байтов эквивалентно :func:`crc16_arc`."""
    crc = init & 0xFFFF
    table = _CRC_TABLE
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc


def crc16_zeros(length: int, init: int = 0) -> int:
    """CRC-16/ARC по ``length`` нулевым байтам, без выделения буфера."""
    crc = init & 0xFFFF
    table = _CRC_TABLE
    chunk = _ZERO_CHUNK
    while length > 0:
        part = min(length, len(chunk))
        for byte in chunk[:part]:
            crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
        length -= part
    return crc


class MS43Error(Exception):
    """Образ не соответствует известной раскладке MS43."""


# --------------------------------------------------------------------------
# Раскладка и записи
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordSpec:
    """Где лежит запись контрольной суммы и как читать её сегменты."""

    name: str
    offset: int
    #: None -- адреса в записи это смещения файла; иначе файл = адрес - база.
    address_base: int | None
    #: Человекочитаемое описание источника init.
    init_note: str


RECORD_BOOT = RecordSpec("boot", 0x03C24, None, "LE16 @ 0x3FE6 ('--' в стоке)")
RECORD_PROG = RecordSpec("prog", 0x6FDE0, PROGRAM_ADDRESS_BASE, "LE16 @ 0x6FFB6 (код '00'/'10'/'30')")
RECORD_CAL = RecordSpec("cal", 0x73FE0, None, "LE16 из цифр версии '4300xx' в ID калибровки")
RECORD_CAL_64K = RecordSpec("cal", 0x03FE0, CAL64_ADDRESS_BASE, "LE16 из цифр версии '4300xx' в ID калибровки")

#: Строка идентификации калибровки: код версии ``4300xx`` (ASCII), по нему же
#: считается init cal-суммы.  В полном образе 512 КиБ -- 0x70008, в 64 КиБ
#: чтении калибровки -- 0x00008.
CAL_VERSION_OFFSET_FULL = 0x70008
CAL_VERSION_OFFSET_CAL64 = 0x00008
CAL_VERSION_LENGTH = 6

#: Адреса, которые в MS43 всегда содержат суммы, проверяемые во время работы.
MONITOR_PROG_OFFSET = 0x6FDAE
MONITOR_CAL_OFFSET = 0x72FFC
MONITOR_CAL_OFFSET_64K = 0x02FFC

#: Известные расхождения корпуса: файл -> (запись, подтверждённая причина).
#: Держим их списком, чтобы тест падал, если появится новое расхождение.
KNOWN_ANOMALIES: dict[str, dict[str, str]] = {
    "Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin": {
        "cal": (
            "калибровка сообщества: отличается от E46_M54B25 тремя байтами "
            "(0x70AB1, 0x70B1A, 0x70B1E), а хранимая сумма оставлена от "
            "исходной калибровки (0x5ADF) -> провал проверки. Файл заявлен "
            "как сток, но в него правки внесены без пересчёта CRC."
        )
    },
    "E39_525__b137b56g_Ca430037_2.5L_Stok.bin": {
        "prog": (
            "поле кода прошивки @ 0x6FFB6 = '10' (init 0x3031), но хранимая "
            "сумма не сходится ни с одним ASCII-init: содержимое prog "
            "меняли после расчёта CRC."
        )
    },
    "E53__E39_b137b56g_Ca430037_2.5L_Full_Stok.bin": {
        "prog": (
            "побайтный дубликат E39_525__b137b56g_Ca430037_2.5L_Stok.bin, "
            "то же расхождение prog-суммы."
        )
    },
    "E39_525__b137b57g_Ca430037_Stok.bin": {
        "prog": (
            "поле кода прошивки @ 0x6FFB6 = '30' (init 0x3033), но хранимая "
            "сумма не сходится: содержимое prog меняли после расчёта CRC."
        )
    },
}


@dataclass
class RecordResult:
    """Результат проверки одной записи."""

    name: str
    offset: int
    stored: int
    computed: int
    init: int
    segments: list[tuple[int, int]]
    segments_as_addresses: bool

    @property
    def ok(self) -> bool:
        return self.stored == self.computed


@dataclass
class ImageReport:
    """Результат проверки одного образа."""

    path: Path
    size: int
    sha256: str
    kind: str  # 'full512' | 'cal64'
    records: list[RecordResult] = field(default_factory=list)
    monitor: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(record.ok for record in self.records)

    @property
    def failures(self) -> list[RecordResult]:
        return [record for record in self.records if not record.ok]


def cal_version_offset(size: int) -> int | None:
    """Смещение строки версии калибровки для размера образа (None -- не наш образ)."""
    if size == IMAGE_SIZE_FULL:
        return CAL_VERSION_OFFSET_FULL
    if size == IMAGE_SIZE_CAL64:
        return CAL_VERSION_OFFSET_CAL64
    return None


def read_cal_version(image: bytes) -> str | None:
    """Код версии калибровки (``4300xx``) из строки идентификации образа.

    Полный образ 512 КиБ: ASCII по смещению 0x70008; 64 КиБ чтение калибровки:
    по смещению 0x00008.  Возвращается ``None``, если размер не наш или строка
    не похожа на код версии (например ``MS43X001``) -- вызывающая сторона
    обязана трактовать это как «версия неизвестна» и отказать в записи.
    """
    offset = cal_version_offset(len(image))
    if offset is None or offset + CAL_VERSION_LENGTH > len(image):
        return None
    try:
        text = image[offset : offset + CAL_VERSION_LENGTH].decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if text.isdigit() else None


def identify_layout(size: int) -> tuple[str, list[RecordSpec]]:
    """Определить тип образа по размеру и вернуть список записей."""
    if size == IMAGE_SIZE_FULL:
        return "full512", [RECORD_BOOT, RECORD_PROG, RECORD_CAL]
    if size == IMAGE_SIZE_CAL64:
        return "cal64", [RECORD_CAL_64K]
    raise MS43Error(
        f"неизвестный размер образа: 0x{size:X} (ожидается 0x80000 или 0x10000)"
    )


def parse_record(image: bytes, spec: RecordSpec) -> tuple[int, list[tuple[int, int]]]:
    """Прочитать хранимую сумму и таблицу сегментов записи."""
    if spec.offset + 4 > len(image):
        raise MS43Error(f"{spec.name}: запись @ 0x{spec.offset:X} вне образа")
    stored = int.from_bytes(image[spec.offset : spec.offset + 2], "little")
    count = int.from_bytes(image[spec.offset + 2 : spec.offset + 4], "little")
    if not 1 <= count <= MAX_SEGMENTS:
        raise MS43Error(
            f"{spec.name}: число сегментов {count} вне 1..{MAX_SEGMENTS} "
            "-> таблица повреждена (инструмент не угадывает)"
        )
    need = spec.offset + 4 + 8 * count
    if need > len(image):
        raise MS43Error(f"{spec.name}: таблица сегментов выходит за образ")
    segments = []
    for index in range(count):
        base = spec.offset + 4 + 8 * index
        start = int.from_bytes(image[base : base + 4], "little")
        end = int.from_bytes(image[base + 4 : base + 8], "little")
        segments.append((start, end))
    return stored, segments


def _segment_to_offsets(
    spec: RecordSpec, segments: list[tuple[int, int]], size: int
) -> list[tuple[int, int]]:
    """Перевести сегменты записи в файловые офсеты и проверить границы."""
    offsets = []
    for start, end in segments:
        if spec.address_base is not None:
            if start < spec.address_base or end < start:
                raise MS43Error(
                    f"{spec.name}: сегмент 0x{start:X}..0x{end:X} не переводится "
                    f"в файл (база 0x{spec.address_base:X})"
                )
            begin = start - spec.address_base
            finish = end - spec.address_base
        else:
            begin, finish = start, end
        if finish >= size or begin < 0:
            raise MS43Error(
                f"{spec.name}: сегмент 0x{start:X}..0x{end:X} выходит за образ"
            )
        offsets.append((begin, finish))
    return offsets


def compute_record(image: bytes, spec: RecordSpec, init: int) -> int:
    """CRC-16/ARC по сегментам записи (обе границы включительно)."""
    stored, segments = parse_record(image, spec)
    del stored
    crc = init & 0xFFFF
    for begin, finish in _segment_to_offsets(spec, segments, len(image)):
        crc = crc16_arc_table(image[begin : finish + 1], crc)
    return crc


# --------------------------------------------------------------------------
# Источники init
# --------------------------------------------------------------------------

#: Строка кода версии MS43 начинается с '43' и содержит четыре цифры.
_VERSION_RE = re.compile(rb"43\d{4}")


def read_init_boot(image: bytes) -> int:
    """Init загрузчика: LE16 из строки идентификации @ 0x3FE6."""
    if len(image) <= 0x3FE7:
        raise MS43Error("boot: нет строки идентификации @ 0x3FE6")
    return int.from_bytes(image[0x3FE6:0x3FE8], "little")


def read_init_program(image: bytes) -> int:
    """Init программы: LE16 поля кода прошивки @ 0x6FFB6 (ASCII-цифры)."""
    if len(image) <= 0x6FFB7:
        raise MS43Error("prog: нет поля кода прошивки @ 0x6FFB6")
    raw = image[0x6FFB6:0x6FFB8]
    if not all(0x30 <= byte <= 0x39 for byte in raw):
        raise MS43Error(
            f"prog: поле @ 0x6FFB6 = {raw!r} не ASCII-цифры -> раскладка не та"
        )
    return int.from_bytes(raw, "little")


def read_init_cal(image: bytes, search_from: int = 0x70000, window: int = 0x40) -> int:
    """Init калибровки: LE16 из двух последних цифр кода версии ``4300xx``."""
    window_end = min(len(image), search_from + window)
    if search_from >= len(image):
        search_from = 0
        window_end = min(len(image), window)
    chunk = image[search_from:window_end]
    match = _VERSION_RE.search(chunk)
    if match is None:
        raise MS43Error(
            f"cal: не найден код версии 43xxxx в первых 0x{window:X} байтах "
            f"строки @ 0x{search_from:X}"
        )
    digits = match.group(0)[-2:]
    if not all(0x30 <= byte <= 0x39 for byte in digits):
        raise MS43Error(f"cal: последние символы кода версии {digits!r} не цифры")
    return int.from_bytes(digits, "little")


def read_init(image: bytes, spec: RecordSpec) -> int:
    """Init для записи по её описанию."""
    if spec.name == "boot":
        return read_init_boot(image)
    if spec.name == "prog":
        return read_init_program(image)
    if spec.name == "cal":
        if len(image) == IMAGE_SIZE_FULL:
            return read_init_cal(image, 0x70000)
        return read_init_cal(image, 0x00000)
    raise MS43Error(f"нет источника init для записи {spec.name}")


def _record_body_length(image: bytes, spec: RecordSpec) -> int:
    _, segments = parse_record(image, spec)
    return sum(finish - begin + 1 for begin, finish in _segment_to_offsets(spec, segments, len(image)))


def solve_init(image: bytes, spec: RecordSpec) -> int | None:
    """Показать, при каком init хранимая сумма сошлась бы с содержимым.

    Решение ищется обращением CRC как линейного отображения GF(2)^16
    (16 уравнений).  Если решения нет -- сегменты не покрывают всё, что
    считала прошивка.  Диагностика: показывает, правился ли образ после
    расчёта суммы.
    """
    stored, segments = parse_record(image, spec)
    offsets = _segment_to_offsets(spec, segments, len(image))
    length = sum(finish - begin + 1 for begin, finish in offsets)

    crc_data_zero_init = 0
    for begin, finish in offsets:
        crc_data_zero_init = crc16_arc_table(image[begin : finish + 1], crc_data_zero_init)

    zero_init = crc16_zeros(length, 0)
    columns = [crc16_zeros(length, 1 << bit) ^ zero_init for bit in range(16)]

    rows = []
    for row in range(16):
        value = 0
        for column in range(16):
            if (columns[column] >> row) & 1:
                value |= 1 << column
        rows.append(value)

    rhs = stored ^ crc_data_zero_init
    matrix = [rows[row] | (((rhs >> row) & 1) << 16) for row in range(16)]
    pivots: dict[int, int] = {}
    for column in range(16):
        pivot = None
        for row in range(16):
            if (matrix[row] >> column) & 1 and row not in pivots.values():
                pivot = row
                break
        if pivot is None:
            return None
        pivots[column] = pivot
        for row in range(16):
            if row != pivot and ((matrix[row] >> column) & 1):
                matrix[row] ^= matrix[pivot]

    init = 0
    for column in range(16):
        if (matrix[pivots[column]] >> 16) & 1:
            init |= 1 << column
    if crc16_arc_table(b"", 0) != 0:  # страховка от опечаток
        raise MS43Error("внутренняя ошибка: проверка CRC пустого входа")
    return init if compute_record(image, spec, init) == stored else None


# --------------------------------------------------------------------------
# Проверка и ремонт
# --------------------------------------------------------------------------


def verify_image(path: str | Path) -> ImageReport:
    """Проверить все записи образа и собрать отчёт (без записи на диск)."""
    path = Path(path)
    image = path.read_bytes()
    kind, specs = identify_layout(len(image))
    report = ImageReport(
        path=path,
        size=len(image),
        sha256=hashlib.sha256(image).hexdigest(),
        kind=kind,
    )
    for spec in specs:
        stored, segments = parse_record(image, spec)
        try:
            init = read_init(image, spec)
            computed = compute_record(image, spec, init)
        except MS43Error as error:
            report.notes.append(f"{spec.name}: {error}")
            continue
        report.records.append(
            RecordResult(
                name=spec.name,
                offset=spec.offset,
                stored=stored,
                computed=computed,
                init=init,
                segments=segments,
                segments_as_addresses=spec.address_base is not None,
            )
        )
    if kind == "full512":
        report.monitor["prog_mon @ 0x6FDAE"] = int.from_bytes(
            image[MONITOR_PROG_OFFSET : MONITOR_PROG_OFFSET + 4], "little"
        )
        report.monitor["cal_mon @ 0x72FFC"] = int.from_bytes(
            image[MONITOR_CAL_OFFSET : MONITOR_CAL_OFFSET + 4], "little"
        )
    else:
        report.monitor["cal_mon @ 0x2FFC"] = int.from_bytes(
            image[MONITOR_CAL_OFFSET_64K : MONITOR_CAL_OFFSET_64K + 4], "little"
        )
    report.notes.append(
        "суммы _mon прошивка сверяет во время работы (XRAM); статически не "
        "проверяются и не пересчитываются (статус unverified)"
    )
    return report


def repair_image(source: str | Path, destination: str | Path) -> ImageReport:
    """Пересчитать CRC и записать образ, изменив только позиции сумм.

    Падает, если после записи не сходится перепроверка или если изменился
    любой байт вне позиций хранимых CRC.
    """
    source = Path(source)
    destination = Path(destination)
    image = bytearray(source.read_bytes())
    kind, specs = identify_layout(len(image))

    original = bytes(image)
    written: list[tuple[RecordSpec, int]] = []
    for spec in specs:
        init = read_init(image, spec)
        computed = compute_record(bytes(image), spec, init)
        image[spec.offset : spec.offset + 2] = computed.to_bytes(2, "little")
        written.append((spec, computed))

    if len(image) != len(original):
        raise MS43Error("внутренняя ошибка: размер образа изменился")

    # 1. Разрешаем изменения только в двух байтах каждой записи.
    allowed = set()
    for spec, _ in written:
        allowed.update((spec.offset, spec.offset + 1))
    for index, (before, after) in enumerate(zip(original, image)):
        if before != after and index not in allowed:
            raise MS43Error(
                f"ремонт изменил байт 0x{index:06X} вне позиций CRC -> отказ"
            )

    # 2. Независимая перепроверка записанного файла.
    result = ImageReport(
        path=destination,
        size=len(image),
        sha256=hashlib.sha256(bytes(image)).hexdigest(),
        kind=kind,
    )
    for spec, _ in written:
        stored, segments = parse_record(bytes(image), spec)
        init = read_init(bytes(image), spec)
        computed = compute_record(bytes(image), spec, init)
        result.records.append(
            RecordResult(
                name=spec.name,
                offset=spec.offset,
                stored=stored,
                computed=computed,
                init=init,
                segments=segments,
                segments_as_addresses=spec.address_base is not None,
            )
        )
    if not result.ok:
        raise MS43Error("после ремонта образ не проходит проверку -> файл не записан")
    destination.write_bytes(bytes(image))
    return result


# --------------------------------------------------------------------------
# Отчёт и командная строка
# --------------------------------------------------------------------------


def report_as_dict(report: ImageReport) -> dict:
    return {
        "file": report.path.name,
        "size": report.size,
        "kind": report.kind,
        "sha256": report.sha256,
        "ok": report.ok,
        "records": [
            {
                "name": record.name,
                "offset": f"0x{record.offset:X}",
                "stored": f"0x{record.stored:04X}",
                "computed": f"0x{record.computed:04X}",
                "init": f"0x{record.init:04X}",
                "ok": record.ok,
                "segments": [
                    [f"0x{start:05X}", f"0x{end:05X}"] for start, end in record.segments
                ],
            }
            for record in report.records
        ],
        "monitor": {key: f"0x{value:08X}" for key, value in report.monitor.items()},
        "notes": report.notes,
    }


def report_as_markdown(reports: list[ImageReport]) -> str:
    lines = [
        "# Проверка контрольных сумм MS43",
        "",
        f"Сформировано: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| файл | тип | boot | prog | cal | _mon (unverified) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def verdict(name: str) -> str:
        for record in report.records:
            if record.name == name:
                return "OK" if record.ok else f"FAIL (0x{record.stored:04X} vs 0x{record.computed:04X})"
        return "--"

    for report in reports:
        monitor = ", ".join(f"{key} = 0x{value:08X}" for key, value in report.monitor.items())
        lines.append(
            f"| {report.path.name} | {report.kind} | {verdict('boot')} | "
            f"{verdict('prog')} | {verdict('cal')} | {monitor} |"
        )
    failed = [report for report in reports if not report.ok]
    lines += ["", f"Итог: {len(reports) - len(failed)} из {len(reports)} образов сходятся полностью.", ""]
    if failed:
        lines += ["## Расхождения", ""]
        for report in failed:
            for record in report.failures:
                known = KNOWN_ANOMALIES.get(report.path.name, {}).get(record.name)
                status = "известное" if known else "НОВОЕ"
                lines.append(
                    f"* `{report.path.name}` / {record.name}: хранимая "
                    f"0x{record.stored:04X}, расчётная 0x{record.computed:04X} "
                    f"(init 0x{record.init:04X}) -- {status}"
                )
                if known:
                    lines.append(f"  * причина: {known}")
        lines.append("")
    return "\n".join(lines)


def _print_report(report: ImageReport, explain: bool) -> None:
    status = "OK" if report.ok else "FAIL"
    print(f"{report.path.name} [{report.kind}] {status}")
    for record in report.records:
        mark = "OK  " if record.ok else "FAIL"
        table = "адреса" if record.segments_as_addresses else "офсеты"
        print(
            f"  {record.name:4s} @0x{record.offset:X} {mark} "
            f"stored=0x{record.stored:04X} computed=0x{record.computed:04X} "
            f"init=0x{record.init:04X} сегментов={len(record.segments)} ({table})"
        )
        if not record.ok and explain:
            spec = next(s for s in (RECORD_BOOT, RECORD_PROG, RECORD_CAL, RECORD_CAL_64K)
                        if s.name == record.name and s.offset == record.offset)
            derived = solve_init(report.path.read_bytes(), spec)
            if derived is None:
                print("       диагностика: решения для init нет (сегменты не покрывают сумму)")
            else:
                print(
                    f"       диагностика: сумма сойдётся при init=0x{derived:04X} "
                    f"-> содержимое правили после расчёта CRC"
                )
    for key, value in report.monitor.items():
        print(f"  {key}: 0x{value:08X} (unverified: runtime)")
    for note in report.notes:
        print(f"  примечание: {note}")


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()  # Windows: не падать на символах вне cp1251
    parser = argparse.ArgumentParser(
        description="Проверка и пересчёт контрольных сумм MS43 (CRC-16/ARC).",
        epilog=(
            "Суммы _mon (0x6FDAE, 0x72FFC) не проверяются статически: прошивка "
            "сверяет их во время работы. Образы, попавшие в KNOWN_ANOMALIES, "
            "перечислены в MS43/docs/CHECKSUM_RU.md."
        ),
    )
    parser.add_argument("images", nargs="+", help="образ(ы) MS43 (512 КиБ или 64 КиБ cal)")
    parser.add_argument("--out", help="пересчитать CRC и записать образ по этому пути")
    parser.add_argument("--report", help="файл отчёта: .md или .json")
    parser.add_argument("--explain", action="store_true", help="для FAIL искать требуемый init")
    args = parser.parse_args(argv)

    reports: list[ImageReport] = []
    exit_code = 0
    for image in args.images:
        path = Path(image)
        if not path.is_file():
            print(f"{path}: нет такого файла", file=sys.stderr)
            exit_code = 2
            continue
        try:
            report = verify_image(path)
            if args.out:
                if len(args.images) > 1:
                    raise MS43Error("--out допустим только для одного образа")
                report = repair_image(path, args.out)
                print(f"{path.name} -> {args.out}: записаны только позиции CRC")
            _print_report(report, args.explain)
            reports.append(report)
            if not report.ok:
                exit_code = 1
        except MS43Error as error:
            print(f"{path}: {error}", file=sys.stderr)
            exit_code = 2

    if args.report and reports:
        target = Path(args.report)
        if target.suffix.lower() == ".json":
            payload = {
                "generated": datetime.now(timezone.utc).isoformat(),
                "images": [report_as_dict(report) for report in reports],
            }
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            target.write_text(report_as_markdown(reports), encoding="utf-8")
        print(f"отчёт: {target}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
