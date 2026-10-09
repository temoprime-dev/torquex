# -*- coding: utf-8 -*-
"""Блоки АКПП Bosch GS8.60.x (ZF 5HP): определение файла, контрольные суммы, откат калибровки.

Данные для сверки хранятся как хеши (``config/gs8_factory.json``) и, отдельно,
как фабричные калибровки BMW и Alpina (``config/gs8_factory_cal.json``, только
окно калибровки по ~32–64 КиБ, сжатое). Источники: фабричные данные BMW
(SP-Daten: ``.0PA`` — программа, ``.0DA`` — калибровка под машину, оба Intel HEX)
и публичный каталог Alpina (``github.com/ms4x-net/alpina``). Строит базы
``tools/build_gs8_factory.py``.

Раскладка файла чтения: GS8.60.0 и GS8.60.2 — 256 КиБ, калибровка по адресам
0x8000..0xFFEF; GS8.60.4 — 512 КиБ, калибровка по адресам 0x70000..0x7FFEF.
В окне калибровки перед меткой есть 14 байт заполнителя (0xFFC0..0xFFCD или
0x7FFC0..0x7FFCD): в разных файлах там 0xFF или 0x00, в фабричных ``.0DA`` их нет,
поэтому в хеш они не входят, а при откате остаются теми, что в файле клиента.

Контрольные суммы (три CRC-16/XMODEM, алгоритм описан в открытом репозитории
``therudywolf/gs860-5hp19``, проверено нами на всех опубликованных файлах Alpina и
на стоковой сумме 0x47DB калибровки KA20): загрузчик, программа, калибровка.

``identify(data)`` возвращает словарь с ключами ``system``, ``program``, ``state``
(``stock`` / ``alpina`` / ``modified`` / ``unknown``), ``vehicle``, ``reference``.
``restore(data, calibration_hash)`` собирает файл с выбранной калибровкой.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "config" / "gs8_factory.json"
CAL_PATH = ROOT / "config" / "gs8_factory_cal.json"

#: система → (размер файла чтения, начало калибровки, длина окна ``.0DA``).
LAYOUTS = {
    "GS8.60.0": (262144, 0x8000, 0x7FF0),
    "GS8.60.2": (262144, 0x8000, 0x7FF0),
    "GS8.60.4": (524288, 0x70000, 0xFFF0),
}
#: заполнитель перед меткой: [начало, конец) в адресах файла.
PADDING = {
    "GS8.60.0": (0xFFC0, 0xFFCE),
    "GS8.60.2": (0xFFC0, 0xFFCE),
    "GS8.60.4": (0x7FFC0, 0x7FFCE),
}
#: где лежит метка калибровки (три копии по 16 символов) и сумма калибровки.
LABEL_AT = {"GS8.60.0": 0xFFCE, "GS8.60.2": 0xFFCE, "GS8.60.4": 0x7FFCE}
LABEL_LEN = 16
#: области контрольных сумм: (начало, конец исключая, где хранится, имя).
REGIONS = {
    "GS8.60.0": [(0x00000, 0x04300, 0x05FFE, "loader"),
                 (0x10000, 0x3F77C, 0x3F7FE, "program"),
                 (0x08000, 0x0FFCE, 0x0FFFE, "calibration")],
    "GS8.60.2": [(0x00000, 0x04300, 0x05FFE, "loader"),
                 (0x10000, 0x3F77C, 0x3F7FE, "program"),
                 (0x08000, 0x0FFCE, 0x0FFFE, "calibration")],
    "GS8.60.4": [(0x00000, 0x04400, 0x05FFE, "loader"),
                 (0x08000, 0x6FF7C, 0x6FFFE, "program"),
                 (0x70000, 0x7FFCE, 0x7FFFE, "calibration")],
}
BLOCK = 256
#: Доля совпавших блоков программы, с которой считаем программу опознанной.
PROGRAM_MATCH = 0.995


class Gs8Error(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Разбор Intel HEX и хеши
# ---------------------------------------------------------------------------


def parse_intel_hex(text: str) -> tuple[dict[str, str], dict[int, int]]:
    """Разобрать ``.0PA``/``.0DA``: метаданные ``;;`` и байты по адресам."""
    meta: dict[str, str] = {}
    memory: dict[int, int] = {}
    base = 0
    segment = 0
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    for line in lines:
        if line.startswith(";;"):
            found = re.match(r";;(\w+):?\s*(.*)", line)
            if found:
                meta.setdefault(found.group(1), found.group(2).strip())
        elif line.startswith(":"):
            count = int(line[1:3], 16)
            address = int(line[3:7], 16)
            kind = int(line[7:9], 16)
            data = bytes.fromhex(line[9:9 + 2 * count])
            if kind == 0:
                start = (segment << 4) + base + address
                for index, value in enumerate(data):
                    memory[start + index] = value
            elif kind == 2:
                segment = int.from_bytes(data, "big")
            elif kind == 4:
                base = int.from_bytes(data, "big") << 16
    return meta, memory


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:20]


def block_hashes(memory: dict[int, int]) -> list[list[Any]]:
    """Хеши полных блоков по 256 байт, целиком лежащих в данных ``memory``."""
    out: list[list[Any]] = []
    for start in sorted({address - address % BLOCK for address in memory}):
        if all(start + i in memory for i in range(BLOCK)):
            chunk = bytes(memory[start + i] for i in range(BLOCK))
            out.append([start, short_hash(chunk)[:12]])
    return out


def calibration_window(data: bytes, system: str) -> bytes:
    """Окно калибровки файла без заполнителя (как в фабричных ``.0DA``)."""
    _size, start, length = LAYOUTS[system]
    pad_start, pad_end = PADDING[system]
    return data[start:pad_start] + data[pad_end:start + length]


def memory_window(memory: dict[int, int], system: str) -> bytes:
    """То же окно из разобранной ``.0DA`` (все адреса должны быть, кроме заполнителя)."""
    _size, start, length = LAYOUTS[system]
    pad_start, pad_end = PADDING[system]
    addresses = [a for a in range(start, start + length) if not pad_start <= a < pad_end]
    missing = [a for a in addresses if a not in memory]
    if missing:
        raise Gs8Error("GS8_DATASET_INCOMPLETE", f"в наборе нет {len(missing)} байт окна калибровки")
    return bytes(memory[a] for a in addresses)


def calibration_hash(data: bytes, system: str) -> str:
    return short_hash(calibration_window(data, system))


# ---------------------------------------------------------------------------
# Контрольные суммы
# ---------------------------------------------------------------------------


def _table() -> list[int]:
    out = []
    for i in range(256):
        value = i << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) if value & 0x8000 else (value << 1)
            value &= 0xFFFF
        out.append(value)
    return out


_TABLE = _table()


def crc16(data: bytes, start: int, end: int) -> int:
    """CRC-16/XMODEM по ``data[start:end]`` (правая граница не входит)."""
    crc = 0
    for byte in data[start:end]:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def checksums(data: bytes, system: str) -> list[dict[str, Any]]:
    rows = []
    for start, end, stored_at, name in REGIONS[system]:
        stored = int.from_bytes(data[stored_at:stored_at + 2], "big")
        computed = crc16(data, start, end)
        rows.append({"name": name, "stored": stored, "computed": computed, "ok": stored == computed})
    return rows


# ---------------------------------------------------------------------------
# База и определение
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_db(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else DB_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _load_cal(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else CAL_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _program_score(data: bytes, program: dict[str, Any]) -> float:
    blocks = program["blocks"]
    if not blocks:
        return 0.0
    same = sum(1 for start, digest in blocks if short_hash(data[start:start + BLOCK])[:12] == digest)
    return same / len(blocks)


def identify(data: bytes, db: dict[str, Any] | None = None) -> dict[str, Any]:
    """Определить систему, программу и состояние калибровки файла GS8."""
    database = db if db is not None else load_db()
    result: dict[str, Any] = {
        "system": None, "program": None, "state": "unknown",
        "vehicle": None, "reference": None, "size": len(data),
    }
    best: tuple[float, str, dict[str, Any]] | None = None
    for system, (size, _start, _length) in LAYOUTS.items():
        if len(data) != size:
            continue
        for program in (database.get(system) or {}).get("programs", []):
            score = _program_score(data, program)
            if best is None or score > best[0]:
                best = (score, system, program)
    if best is None or best[0] < PROGRAM_MATCH:
        return result
    _score, system, program = best
    result.update(system=system, program=program["name"], program_reference=program.get("reference"))
    digest = calibration_hash(data, system)
    result["calibration_hash"] = digest
    for entry in (database.get(system) or {}).get("calibrations", []):
        if entry["hash"] == digest:
            result.update(
                state="alpina" if entry.get("source") == "alpina" else "stock",
                vehicle=entry.get("vehicle"), reference=entry.get("reference"),
                dataset=entry.get("name"),
            )
            return result
    result["state"] = "modified"
    return result


def factory_options(system: str, db: dict[str, Any] | None = None, source: str = "bmw",
                    program: str | None = None) -> list[dict[str, str]]:
    """Список калибровок, которые можно подставить (для выбора машины или модели Alpina).

    Для Alpina ``program`` — программа файла клиента: подходит только калибровка,
    снятая с той же фабричной программы.
    """
    database = db if db is not None else load_db()
    out = []
    for entry in (database.get(system) or {}).get("calibrations", []):
        if entry.get("source") != source:
            continue
        if source == "alpina" and program is not None and entry.get("program") != program:
            continue
        out.append({"hash": entry["hash"], "vehicle": entry.get("vehicle") or entry.get("name") or "",
                    "reference": entry.get("reference") or ""})
    return out


def catalog_summary(db: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сводка для страницы «Что поддерживается»: что есть в базах по каждой системе."""
    database = db if db is not None else load_db()
    out: dict[str, Any] = {}
    for system in LAYOUTS:
        item = database.get(system) or {}
        cals = item.get("calibrations", [])
        out[system] = {
            "image_kib": LAYOUTS[system][0] // 1024,
            "programs": len(item.get("programs", [])),
            "factory": sum(1 for c in cals if c.get("source") == "bmw"),
            "alpina": [c.get("vehicle") for c in cals if c.get("source") == "alpina"],
        }
    return out


# ---------------------------------------------------------------------------
# Откат калибровки
# ---------------------------------------------------------------------------


def encode_payload(window: bytes) -> str:
    return base64.b64encode(zlib.compress(window, 9)).decode("ascii")


def decode_payload(text: str) -> bytes:
    return zlib.decompress(base64.b64decode(text))


def restore(data: bytes, calibration_hash_value: str, system: str | None = None,
            cal_db: dict[str, Any] | None = None, db: dict[str, Any] | None = None,
            source: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """Файл клиента с подставленной фабричной калибровкой и верными суммами.

    Программа и загрузчик остаются клиентскими: они должны быть опознаны и иметь
    верные суммы, иначе отказ (повреждённое или чужое чтение).
    """
    found = identify(data, db=db)
    system = system or found["system"]
    if system is None or found["system"] != system:
        raise Gs8Error("GS8_NOT_RECOGNISED", "программа блока не опознана")
    for row in checksums(data, system):
        if row["name"] != "calibration" and not row["ok"]:
            raise Gs8Error("GS8_SOURCE_CRC_INVALID",
                           f"сумма {row['name']} не сходится: чтение повреждено или другая программа")
    cal = (cal_db if cal_db is not None else _load_cal()).get(system, {})
    entry = cal.get(calibration_hash_value)
    if entry is None:
        raise Gs8Error("GS8_DATASET_UNKNOWN", "такой калибровки нет в каталоге")
    if source is not None and entry.get("source") != source:
        raise Gs8Error("GS8_DATASET_KIND", "калибровка не того вида (фабричная BMW или Alpina)")
    if entry.get("source") == "alpina":
        meta = next((c for c in ((db if db is not None else load_db()).get(system) or {}).get("calibrations", [])
                     if c["hash"] == calibration_hash_value), {})
        if meta.get("program") != found["program"]:
            raise Gs8Error("GS8_ALPINA_INCOMPATIBLE",
                           "эта калибровка Alpina снята с другой программы блока: подстановка запрещена")
    window = decode_payload(entry["data"])
    _size, start, length = LAYOUTS[system]
    pad_start, pad_end = PADDING[system]
    head = pad_start - start
    expected = head + (start + length - pad_end)
    if len(window) != expected:
        raise Gs8Error("GS8_DATASET_INCOMPLETE", "размер сохранённой калибровки не сходится")
    out = bytearray(data)
    out[start:pad_start] = window[:head]
    out[pad_end:start + length] = window[head:]
    # метка калибровки: три копии одной и той же строки (в наборе есть две, третью достраиваем)
    label_at = LABEL_AT[system]
    label = bytes(out[label_at:label_at + LABEL_LEN])
    out[label_at:label_at + 3 * LABEL_LEN] = label * 3
    for start_c, end_c, stored_at, name in REGIONS[system]:
        if name == "calibration":
            out[stored_at:stored_at + 2] = crc16(bytes(out), start_c, end_c).to_bytes(2, "big")
    rows = checksums(bytes(out), system)
    if not all(row["ok"] for row in rows):
        raise Gs8Error("GS8_RESULT_CRC_INVALID", "после подстановки суммы не сходятся (ошибка генератора)")
    changed = sum(a != b for a, b in zip(data, out))
    report = {"system": system, "calibration": calibration_hash_value, "label": label.decode("latin-1"),
              "source": entry.get("source"),
              "vehicle": entry.get("vehicle"), "changed_bytes": changed,
              "checksums": [{"name": r["name"], "value": f"0x{r['computed']:04X}"} for r in rows]}
    return bytes(out), report
