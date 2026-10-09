#!/usr/bin/env python3
"""Патчи MS4X Community Patchlist для MS430069: каталог, проверка, применение.

Источник — community patchlist MS4X для MS430069 (TunerPro XDF, v2.9.2). В
отличие от калибровочных таблиц, часть возможностей MS43 (launch control, NLS,
RAL, отсечка по зажиганию, Alpha/N, MAF hack, расширение логирования DS2,
индикаторы M-кластера, boost control) реализована **патчем кода прошивки**.
Патчлист описывает каждый патч явно: адрес, размер, какие байты должны лежать
в стоке (``basedata``) и какие должны получиться (``patchdata``).

Поэтому наш движок не «примеряет» патч, а **проверяет** его:

1. для каждой записи патча байты в образе обязаны совпадать с ``basedata`` —
   иначе патч не применяется (это защита от чужого/правленого файла);
2. проверяются все записи патча до единой записи (атомарность: либо весь патч,
   либо отказ);
3. после записи контрольные суммы пересчитываются нашим ядром
   (`MS43/tools/checksum.py`), и результат перепроверяется им же;
4. ``revert`` умеет вернуть сток обратно и обязан дать побайтно исходный файл.

Проверено на 18 стоках `MS43/firmware`: 26 из 27 патчей совпадают по
``basedata`` во всех 18 образах; 27-й — ``[PATCH] Remove Deprecated Launch
Control`` — ожидаемо неприменим к чистому стоку (он снимает старый LC-патч
v2.5 и требует уже пропатченных байтов).

Границы продукта остаются нашими: у патчей есть ворота (см. ``GATES``) —
иммобилайзер требует отдельного подтверждения, checksum bypass нам не нужен
(мы считаем суммы), устаревший LC помечен как неприменимый.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    from MS43.tools import checksum as ck
    from MS43.tools.console import prepare_stdout
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    import checksum as ck  # type: ignore[no-redef]
    from console import prepare_stdout  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = ROOT / "firmware"
PATCH_DIR = ROOT / "patches"
DEFAULT_CATALOG = PATCH_DIR / "ms430069_community_patchlist_v2.9.2.json"

#: Ожидаемое число патчей и записей в patchlist v2.9.2 (контроль целостности).
EXPECTED_PATCHES = 27
EXPECTED_ENTRIES = 67

def unique_paths(
    paths: Iterable[Path], *, key: Callable[[Path], str] | None = None
) -> list[Path]:
    """Пути без повторов. ``key`` по умолчанию нормализует регистр и путь.

    Нужен из-за Windows: файловая система там регистронезависима, поэтому
    ``glob("*.bin")`` и ``glob("*.Bin")`` возвращают **одни и те же** файлы, и
    счётчики стоков удваивались (430056 давала 20 образов вместо 10).
    ``os.path.normcase`` на Windows приводит путь к нижнему регистру, на Linux
    ничего не меняет — то есть на Linux два файла, отличающиеся только регистром
    расширения, остаются разными (это разные файлы).  ``key`` переопределяется в
    тестах, чтобы воспроизвести обе семантики на любой ОС.
    """
    normalizer = key or (lambda path: os.path.normcase(os.path.realpath(path)))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = normalizer(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(path)
    return unique


def bin_files(directory: Path, *, key: Callable[[Path], str] | None = None) -> list[Path]:
    """Стоковые файлы каталога (``.bin``/``.Bin``) без повторов.

    Фильтр по ``suffix.lower()`` вместо двух glob-шаблонов: так файл с любым
    регистром расширения попадает ровно один раз независимо от регистроза-
    висимости файловой системы.
    """
    if not directory.is_dir():
        return []
    candidates = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".bin"
    )
    return unique_paths(candidates, key=key)


#: Границы областей 512 КиБ образа MS43 (адрес в патчлисте = смещение файла).
REGION_BOOT = (0x00000, 0x0FFFF)
REGION_PROGRAM = (0x10000, 0x6FFFF)
REGION_CAL = (0x70000, 0x7FFFF)


class PatchError(Exception):
    """Патч не применяется: данные не сходятся."""


def region_of(address: int) -> str:
    """Область образа по адресу патчлиста (адрес = смещение файла)."""
    if REGION_BOOT[0] <= address <= REGION_BOOT[1]:
        return "boot"
    if REGION_PROGRAM[0] <= address <= REGION_PROGRAM[1]:
        return "program"
    if REGION_CAL[0] <= address <= REGION_CAL[1]:
        return "cal"
    raise PatchError(f"адрес 0x{address:X} вне образа MS43")


@dataclass(frozen=True)
class PatchEntry:
    name: str
    address: int
    size: int
    patch_data: bytes
    #: Ожидаемые байты стока. ``None`` — в патчлисте их нет (запись без
    #: проверки, например очистка области ISN): применить можно, вернуть
    #: исходное состояние нельзя, поэтому revert такие патчи отклоняет.
    base_data: bytes | None

    @property
    def region(self) -> str:
        return region_of(self.address)

    @property
    def end(self) -> int:
        return self.address + self.size


@dataclass
class Patch:
    title: str
    description: str
    category: str
    entries: list[PatchEntry] = field(default_factory=list)
    #: Сколько образов из проверенных совпали по basedata (заполняет extract).
    verified_images: int = 0
    checked_images: int = 0

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({entry.region for entry in self.entries}))

    @property
    def group(self) -> str:
        """Наша группа фичи (не зависит от позиционной категории патчлиста)."""
        return PATCH_GROUPS.get(self.title, "")

    @property
    def addresses(self) -> tuple[int, int]:
        return (
            min(entry.address for entry in self.entries),
            max(entry.end for entry in self.entries),
        )


@dataclass
class PatchSymbol:
    """Параметр фичи (таблица/константа/флаг из патчлиста)."""

    title: str
    kind: str  # table | constant | flag
    address: int
    size_bits: int
    rows: int
    columns: int
    equation: str
    units: str
    description: str
    category: str
    requires_patch: str | None
    stock_min: float | None = None
    stock_max: float | None = None
    stock_uniform: bool = False
    stock_note: str = ""


@dataclass
class PatchCatalog:
    source_name: str
    source_file: str
    source_url: str
    source_sha256: str
    extracted: str
    applies_to: str
    image_size: int
    patches: list[Patch]
    symbols: list[PatchSymbol]

    def patch(self, title: str) -> Patch:
        for item in self.patches:
            if item.title == title:
                return item
        raise PatchError(f"патча {title!r} нет в каталоге")

    def titles(self) -> list[str]:
        return [item.title for item in self.patches]

    def as_dict(self) -> dict:
        return {
            "source": {
                "name": self.source_name,
                "file": self.source_file,
                "url": self.source_url,
                "sha256": self.source_sha256,
                "extracted": self.extracted,
            },
            "applies_to": self.applies_to,
            "image_size": self.image_size,
            "notes": CATALOG_NOTES,
            "patches": [
                {
                    "title": patch.title,
                    "group": patch.group,
                    "category": patch.category,
                    "description": patch.description,
                    "regions": list(patch.regions),
                    "address_range": [f"0x{patch.addresses[0]:05X}", f"0x{patch.addresses[1]:05X}"],
                    "verified_images": patch.verified_images,
                    "checked_images": patch.checked_images,
                    "entries": [
                        {
                            "name": entry.name,
                            "address": f"0x{entry.address:05X}",
                            "size": entry.size,
                            "region": entry.region,
                            "base_data": (
                                entry.base_data.hex().upper() if entry.base_data is not None else None
                            ),
                            "patch_data": entry.patch_data.hex().upper(),
                        }
                        for entry in patch.entries
                    ],
                }
                for patch in self.patches
            ],
            "symbols": [
                {
                    "title": symbol.title,
                    "kind": symbol.kind,
                    "address": f"0x{symbol.address:05X}",
                    "size_bits": symbol.size_bits,
                    "shape": f"{symbol.rows}x{symbol.columns}",
                    "equation": symbol.equation,
                    "units": symbol.units,
                    "category": symbol.category,
                    "requires_patch": symbol.requires_patch,
                    "description": symbol.description,
                    "stock": {
                        "min": symbol.stock_min,
                        "max": symbol.stock_max,
                        "uniform": symbol.stock_uniform,
                        "note": symbol.stock_note,
                    },
                }
                for symbol in self.symbols
            ],
        }


#: Продуктовые ворота: какие патчи мы предлагаем и что нужно для применения.
#: Оговорки к каталогу — то, что нельзя потерять при переносе данных.
CATALOG_NOTES = [
    "requires_patch у символов взят из патчлиста как есть (CATEGORYMEM). Поле "
    "позиционное: например, таблицы CAN привязаны там к «Immobilizer Bypass», а "
    "id_maf_tab__v_maf — к «Boost Control». Рабочая привязка символа к фиче "
    "задана в MS43/policies/stage_policy.json (SYMBOL_OVERRIDES) и проверяется тестами.",
    "Категории патчей в патчлисте перемешаны (см. PATCH_GROUPS) — source category "
    "хранится только для провенанса.",
    "basedata отсутствует у 5 патчей (12 записей): снять их нельзя, только "
    "применить поверх стокового бэкапа.",
    "[PATCH] Remove Deprecated Launch Control на чистом стоке 430069 — no-op "
    "(patchdata совпадает со стоком), нужен только для образов со старым LC v2.5.",
]

#: Наша группировка патчей по фичам. Категории в самом патчлисте проставлены
#: позиционно и перемешаны (``CATEGORYMEM`` ссылается на индекс, а не на имя),
#: поэтому на них опираться нельзя — источник сохраняем в ``category``, а
#: рабочую группу задаём здесь. Пустая группа "" остаётся у служебных патчей
#: (суммы, иммобилайзер, миграция), они не фичи тюнинга.
PATCH_GROUPS: dict[str, str] = {
    "[PATCH] Checksum Bypass": "patch_checksum_bypass",
    "[PATCH] Immobilizer Bypass": "patch_immobilizer",
    "[PATCH] Immobilizer Bypass DTC Fix": "patch_immobilizer",
    "[PATCH] Clear Immobilizer Data": "patch_immobilizer",
    "[PATCH] Launch Control & Rolling Anti Lag": "patch_lc",
    "[DATA] LC & RAL Default Values": "patch_lc",
    "[PATCH] Ignition Cut Limiter": "patch_ign_cut",
    "[PATCH] Alpha/N": "patch_alpha_n",
    "[PATCH] Map Reduction": "patch_map_reduction",
    "[PATCH] MAF Based Full Load Enrichment": "patch_maf_fl",
    "[DATA] MAF Based FL Default Values - M54B30": "patch_maf_fl",
    "[PATCH] MAF Sensor Hack 2048kg/h": "patch_maf_hack",
    "[PATCH] MAF Sensor Hack 4096kg/h": "patch_maf_hack",
    "[PATCH] M Cluster LED Support": "patch_mcluster",
    "[DATA] M3 Cluster Default Values": "patch_mcluster",
    "[PATCH] DS2 Logging Feature Enhancement": "patch_ds2",
    "[PATCH] DS2 Speed Increase": "patch_ds2",
    "[PATCH] DS2 Baudrate Switch Engine Stopped Bypass": "patch_ds2",
    "[PATCH] MIL Knock Indicator": "patch_mil_knk",
    "[DATA] MIL Knock Indicator": "patch_mil_knk",
    "[PATCH] Boost Control": "patch_boost",
    "[PATCH] Custom CAN - OLM Boost Gauge": "patch_can",
    "[PATCH] E3x Vehicle Speed Sensor Accuracy Improvement": "patch_vss",
    "[PATCH] Fuel Cut Table Switch Over Cruise Control": "patch_fuel_cut_sw",
    "[PATCH] Remove Deprecated Launch Control": "patch_legacy_cleanup",
    "[DATA] Remove Deprecated Launch Control": "patch_legacy_cleanup",
    "[PATCH] Daniel_F. 430056 Update Fix": "patch_migration_fix",
}

GATES: dict[str, dict] = {
    "[PATCH] Checksum Bypass": {
        "offered": False,
        "reason": (
            "не предлагаем: суммы мы всегда пересчитываем сами "
            "(MS43/tools/checksum.py), а bypass нужен только старым "
            "инструментам и делает калибровку несовместимой с проверкой"
        ),
    },
    "[PATCH] Remove Deprecated Launch Control": {
        "offered": False,
        "reason": (
            "на чистом стоке 430069 это no-op: patchdata совпадает со стоковыми "
            "байтами (basedata — состояние после старого LC-патча v2.5, ни с одним "
            "из 18 стоков не совпала). Нужен только для образов, где уже стоял LC v2.5"
        ),
    },
    "[DATA] Remove Deprecated Launch Control": {
        "offered": False,
        "reason": "пара к снятию старого LC-патча v2.5; на чистом стоке не нужна",
    },
    "[PATCH] Immobilizer Bypass": {
        "offered": True,
        "declaration": "immo_change",
        "reason": (
            "отключение иммобилайзера: только по письменному подтверждению "
            "владельца (соревновательное авто / EWS удалён). Штатный путь — "
            "оставить EWS и синхронизировать его после обновления"
        ),
    },
    "[PATCH] Immobilizer Bypass DTC Fix": {
        "offered": True,
        "declaration": "immo_change",
        "reason": "только вместе с Immobilizer Bypass, то же подтверждение",
    },
    "[PATCH] Clear Immobilizer Data": {
        "offered": True,
        "declaration": "immo_virginize",
        "reason": (
            "виргинизация ЭБУ: сбрасывает привязку к EWS и требует bootmode "
            "для записи; делать только сознательно, иначе синхронизация "
            "с EWS через INPA обязательна"
        ),
    },
}


def gate(patch: Patch) -> dict:
    """Ворота патча: предлагаем ли и что требуется для применения."""
    return GATES.get(patch.title, {"offered": True})


#: Каково соответствие категорий патчлиста и патчей (для символов).
#: Символ нужен только тогда, когда применён соответствующий патч.
CATEGORY_REQUIRES_PATCH: dict[str, str] = {
    "Launch Control Parameters": "[PATCH] Launch Control & Rolling Anti Lag",
    "LC / NLS / RAL General Settings": "[PATCH] Launch Control & Rolling Anti Lag",
    "Rolling Anti Lag Parameters": "[PATCH] Launch Control & Rolling Anti Lag",
    "No Lift Shift Parameters - Not Implemented Yet": "[PATCH] Launch Control & Rolling Anti Lag",
    "OBD CVN Fix": "[PATCH] Launch Control & Rolling Anti Lag",
    "MAF Based Full Load Enrichment": "[PATCH] MAF Based Full Load Enrichment",
    "MAF Sensor Hack": "[PATCH] MAF Sensor Hack 2048kg/h",
    "MIL Knock Indicator": "[PATCH] MIL Knock Indicator",
    "Ignition Cut Limiter": "[PATCH] Ignition Cut Limiter",
    "Alpha/N": "[PATCH] Alpha/N",
    "M3/M5 Cluster LEDs": "[PATCH] M Cluster LED Support",
    "DS2 Logging Extensions": "[PATCH] DS2 Logging Feature Enhancement",
    "Boost Control Over Canister Purge Output": "[PATCH] Boost Control",
    "Map Reduction": "[PATCH] Map Reduction",
    "E3x Vehicle Speed Sensor Accuracy Improvement": "[PATCH] E3x Vehicle Speed Sensor Accuracy Improvement",
    "Fuel Cut Table Switch Over Cruise Control": "[PATCH] Fuel Cut Table Switch Over Cruise Control",
    "CAN Bus Addons": "[PATCH] Custom CAN - OLM Boost Gauge",
    "Immobilizer Bypass": "[PATCH] Immobilizer Bypass",
}

#: Символы, которые не нужны как записываемые параметры (только информация).
INFORMATIONAL_CATEGORIES = {"ECU Information", "DS2 Logging Extensions"}


# --------------------------------------------------------------------------
# Извлечение каталога из патчлиста
# --------------------------------------------------------------------------


def _parse_int(value: str | None, default: int = 0) -> int:
    if not value:
        return default
    try:
        return int(value, 0)
    except ValueError:
        return default


def _field(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return match.group(1).strip() if match else ""


def _embedded(block: str) -> dict[str, str]:
    match = re.search(r"<EMBEDDEDDATA([^/>]*)/>", block)
    return dict(re.findall(r'(\w+)="([^"]*)"', match.group(1))) if match else {}


def extract_catalog(
    xdf_path: Path,
    firmware_dir: Path = FIRMWARE_DIR,
    extracted: str | None = None,
) -> PatchCatalog:
    """Разобрать патчлист XDF и проверить его against стоковые образы."""
    raw = Path(xdf_path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    categories = {
        int(index, 16): name
        for index, name in re.findall(r'<CATEGORY index="([^"]+)" name="([^"]*)"', text)
    }
    # заголовок: базовый адрес и размер региона
    header = re.search(r"<XDFHEADER>(.*?)</XDFHEADER>", text, re.S)
    header_body = header.group(1) if header else ""

    images = {path.name: path.read_bytes() for path in bin_files(firmware_dir)}
    if not images:
        raise PatchError(f"нет стоковых образов в {firmware_dir}")

    patches: list[Patch] = []
    for block in re.findall(r"<XDFPATCH\b(.*?)</XDFPATCH>", text, re.S):
        title = re.sub(r"&amp;", "&", _field(block, "title"))
        category = ""
        category_match = re.search(r'<CATEGORYMEM index="0" category="([^"]+)"', block)
        if category_match:
            category = categories.get(int(category_match.group(1), 16), "")
        entries: list[PatchEntry] = []
        for entry_raw in re.findall(r"<XDFPATCHENTRY\b([^>]*)/>", block):
            attributes = dict(re.findall(r'(\w+)="([^"]*)"', entry_raw))
            address = _parse_int(attributes.get("address"))
            size = _parse_int(attributes.get("datasize"))
            patch_data = bytes.fromhex(attributes.get("patchdata", ""))
            base_data = (
                bytes.fromhex(attributes["basedata"]) if attributes.get("basedata") else None
            )
            if not address or not size or len(patch_data) != size:
                raise PatchError(
                    f"{title}: запись {attributes.get('name')!r} неполна "
                    f"(адрес 0x{address:X}, размер {size}, "
                    f"patchdata {len(patch_data)}Б)"
                )
            if base_data is not None and len(base_data) != size:
                raise PatchError(
                    f"{title}: запись {attributes.get('name')!r} — basedata "
                    f"{len(base_data)}Б против размера {size}"
                )
            region_of(address)  # проверка границ
            region_of(address + size - 1)
            entries.append(
                PatchEntry(
                    name=attributes.get("name", ""),
                    address=address,
                    size=size,
                    patch_data=patch_data,
                    base_data=base_data,
                )
            )
        if not entries:
            raise PatchError(f"{title}: нет ни одной записи — разбор патчлиста сломан")
        if title not in PATCH_GROUPS:
            raise PatchError(
                f"{title}: нет в PATCH_GROUPS — новая версия патчлиста, разберите её вручную"
            )
        patch = Patch(
            title=title,
            description=_field(block, "description").replace("&#013;&#010;", " | "),
            category=category,
            entries=entries,
            checked_images=len(images),
        )
        patch.verified_images = sum(
            1
            for image in images.values()
            if all(
                e.base_data is None or image[e.address : e.end] == e.base_data
                for e in entries
            )
        )
        patches.append(patch)

    symbols: list[PatchSymbol] = []
    for kind, tag in (("table", "TABLE"), ("constant", "CONSTANT"), ("flag", "FLAG")):
        for block in re.findall(rf"<XDF{tag}\b(.*?)</XDF{tag}>", text, re.S):
            title = re.sub(r"&amp;", "&", _field(block, "title"))
            if not title or title.startswith("-----"):
                continue
            embedded = _embedded(block)
            address = _parse_int(embedded.get("mmedaddress"), -1)
            if address < 0 and kind == "table":
                # у таблиц адрес данных лежит в оси z
                z_axis = re.search(r'<XDFAXIS id="z">(.*?)</XDFAXIS>', block, re.S)
                if z_axis:
                    embedded = _embedded(z_axis.group(1))
                    address = _parse_int(embedded.get("mmedaddress"), -1)
            if address < 0:
                continue  # подписи и оси без своего адреса
            size_bits = _parse_int(embedded.get("mmedelementsizebits"), 8)
            rows = _parse_int(embedded.get("mmedrowcount"), 1) or 1
            columns = _parse_int(embedded.get("mmedcolcount"), 1) or 1
            equation_match = re.search(r'<MATH equation="([^"]*)"', block)
            category = ""
            category_match = re.search(r'<CATEGORYMEM index="0" category="([^"]+)"', block)
            if category_match:
                category = categories.get(int(category_match.group(1), 16), "")
            symbol = PatchSymbol(
                title=title,
                kind=kind,
                address=address,
                size_bits=size_bits,
                rows=rows,
                columns=columns,
                equation=equation_match.group(1) if equation_match else "X",
                units=_field(block, "units"),
                description=_field(block, "description").replace("&#013;&#010;", " | "),
                category=category,
                requires_patch=CATEGORY_REQUIRES_PATCH.get(category),
            )
            _annotate_stock(symbol, images)
            symbols.append(symbol)

    if len(patches) != EXPECTED_PATCHES:
        raise PatchError(
            f"патчей {len(patches)}, ожидалось {EXPECTED_PATCHES} — патчлист другой версии"
        )
    total_entries = sum(len(patch.entries) for patch in patches)
    if total_entries != EXPECTED_ENTRIES:
        raise PatchError(
            f"записей {total_entries}, ожидалось {EXPECTED_ENTRIES} — патчлист другой версии"
        )

    return PatchCatalog(
        source_name="MS4X Community Patchlist for TunerPro",
        source_file=Path(xdf_path).name,
        source_url="https://www.ms4x.net/index.php?title=TunerPro_MS43_Community_Patchlist",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        extracted=extracted or date.today().isoformat(),
        applies_to="MS430069 (полный образ 512 КиБ)",
        image_size=0x80000,
        patches=patches,
        symbols=symbols,
    )


def _annotate_stock(symbol: PatchSymbol, images: dict[str, bytes]) -> None:
    """Прочитать стоковые значения символа прямо из образов (без масштаба)."""
    size = max(1, symbol.size_bits // 8)
    total = symbol.rows * symbol.columns
    values: list[tuple[int, ...]] = []
    for image in images.values():
        chunk = image[symbol.address : symbol.address + size * total]
        if len(chunk) != size * total:
            return
        values.append(
            tuple(
                int.from_bytes(chunk[index * size : (index + 1) * size], "little")
                for index in range(total)
            )
        )
    unique = sorted(set(values))
    symbol.stock_min = float(min(min(v) for v in unique))
    symbol.stock_max = float(max(max(v) for v in unique))
    symbol.stock_uniform = len(unique) == 1
    symbol.stock_note = (
        "одинаково во всех образах"
        if symbol.stock_uniform
        else f"{len(unique)} вариантов, диапазон {symbol.stock_min:g}..{symbol.stock_max:g}"
    )


def save_catalog(catalog: PatchCatalog, path: Path = DEFAULT_CATALOG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(catalog.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_catalog(path: Path = DEFAULT_CATALOG) -> PatchCatalog:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    patches = []
    for item in payload["patches"]:
        entries = [
            PatchEntry(
                name=entry["name"],
                address=int(entry["address"], 16),
                size=int(entry["size"]),
                patch_data=bytes.fromhex(entry["patch_data"]),
                base_data=(
                    bytes.fromhex(entry["base_data"]) if entry.get("base_data") else None
                ),
            )
            for entry in item["entries"]
        ]
        group = PATCH_GROUPS.get(item["title"], "")
        if item.get("group", group) != group:
            raise PatchError(
                f"{item['title']}: группа {item.get('group')!r} в каталоге расходится "
                f"с PATCH_GROUPS ({group!r}) — каталог надо пересобрать"
            )
        patches.append(
            Patch(
                title=item["title"],
                description=item["description"],
                category=item["category"],
                entries=entries,
                verified_images=int(item.get("verified_images", 0)),
                checked_images=int(item.get("checked_images", 0)),
            )
        )
    symbols = [
        PatchSymbol(
            title=item["title"],
            kind=item["kind"],
            address=int(item["address"], 16),
            size_bits=int(item["size_bits"]),
            rows=int(item["shape"].split("x")[0]),
            columns=int(item["shape"].split("x")[1]),
            equation=item["equation"],
            units=item["units"],
            description=item["description"],
            category=item["category"],
            requires_patch=item["requires_patch"],
            stock_min=item["stock"]["min"],
            stock_max=item["stock"]["max"],
            stock_uniform=bool(item["stock"]["uniform"]),
            stock_note=item["stock"].get("note", ""),
        )
        for item in payload["symbols"]
    ]
    if payload.get("notes") != CATALOG_NOTES:
        raise PatchError("оговорки каталога (notes) разошлись с CATALOG_NOTES — пересоберите каталог")
    source = payload["source"]
    return PatchCatalog(
        source_name=source["name"],
        source_file=source["file"],
        source_url=source["url"],
        source_sha256=source["sha256"],
        extracted=source["extracted"],
        applies_to=payload["applies_to"],
        image_size=int(payload["image_size"]),
        patches=patches,
        symbols=symbols,
    )


# --------------------------------------------------------------------------
# Проверка и применение
# --------------------------------------------------------------------------


@dataclass
class PatchStatus:
    patch: Patch
    state: str  # applicable | applied | inapplicable | conflict | partial
    detail: str
    mismatched: list[str] = field(default_factory=list)


def check_patch(image: bytes, patch: Patch) -> PatchStatus:
    """Состояние патча в образе: можно применить, уже применён, не подходит."""
    if len(image) != 0x80000:
        raise PatchError(f"нужен полный образ 512 КиБ, а не {len(image)} байт")
    base_checks = [e for e in patch.entries if e.base_data is not None]
    base_ok = [image[e.address : e.end] == e.base_data for e in base_checks]
    patch_ok = [image[e.address : e.end] == e.patch_data for e in patch.entries]
    if all(patch_ok):
        return PatchStatus(patch, "applied", "все записи уже содержат patchdata")
    if all(base_ok):
        detail = "все basedata на месте — можно применять"
        if not base_checks:
            detail = "проверять нечего (в патчлисте нет basedata) — применяется как есть"
        return PatchStatus(patch, "applicable", detail)
    mismatched = [
        f"{e.name} @0x{e.address:05X}: {image[e.address:e.end].hex().upper()} "
        f"вместо {(e.base_data or b'').hex().upper()}"
        for e, ok in zip(base_checks, base_ok)
        if not ok
    ]
    if any(patch_ok) and any(base_ok):
        return PatchStatus(patch, "partial", "часть записей пропатчена, часть нет", mismatched)
    return PatchStatus(patch, "conflict", "basedata не совпала — образ правили", mismatched)


def check_image(image: bytes, catalog: PatchCatalog) -> list[PatchStatus]:
    return [check_patch(image, patch) for patch in catalog.patches]


def check_supported_images(catalog: PatchCatalog, firmware_dir: Path = FIRMWARE_DIR) -> dict:
    """Сводка: сколько стоковых образов поддерживают каждый патч."""
    summary = {}
    for path in bin_files(firmware_dir):
        image = path.read_bytes()
        summary[path.name] = {
            status.patch.title: status.state for status in check_image(image, catalog)
        }
    return summary


#: Версии калибровки, на которых проверена basedata патчлиста сообщества.  Это
#: **не** то же самое, что версии, покрытые политиками калибровки: патчлист
#: v2.9.2 написан и сверен по 18 стокам MS430069, его байтовые маски на других
#: версиях не доказаны, поэтому он остаётся покрытием только 430069 (см.
#: ``MS43/docs/VERSION_COVERAGE_RU.md`` §5).
PATCHLIST_SOFTWARE: tuple[str, ...] = ("430069",)


def supported_software() -> tuple[str, ...]:
    """Версии, на которых проверена basedata патчлиста (только 430069).

    Раньше значение читалось из политики; теперь, когда политик несколько, связь
    с конкретной версией задана явно: расширение покрытия калибровки не должно
    молча включать патчлист на новых версиях.  Импорт ``policy`` не нужен.
    """
    return PATCHLIST_SOFTWARE


def refuse_unsupported_version(
    image: bytes, *, supported: tuple[str, ...] | None = None
) -> str:
    """Отказать, если версия калибровки образа не покрыта патчлистом.

    Basedata патчлиста снята с 18 стоков MS430069: на других версиях по тем же
    адресам лежат другие данные, поэтому запись патчей запрещена — даже если для
    версии есть политика калибровки (покрытие калибровки и патчлист разделены,
    см. ``MS43/docs/VERSION_COVERAGE_RU.md`` §5).  Возвращает прочитанную версию.
    """
    version = ck.read_cal_version(image)
    versions = tuple(supported) if supported is not None else supported_software()
    if version is None or version not in versions:
        raise PatchError(
            f"CAL_VERSION_UNSUPPORTED: версия калибровки {version or 'не прочитана'} "
            f"вне поддерживаемых ({', '.join(versions) or 'пусто'}) — патчи не применяются"
        )
    return version


def _refuse_gated(patches: list[Patch], allow_gated: bool, declarations: set[str]) -> None:
    for patch in patches:
        rules = gate(patch)
        if rules.get("offered", True):
            required = rules.get("declaration")
            if required and required not in declarations:
                raise PatchError(
                    f"{patch.title}: требуется подтверждение {required!r} — {rules['reason']}"
                )
            continue
        if not allow_gated:
            raise PatchError(f"{patch.title}: не предлагается — {rules['reason']}")


def _recompute_checksums(image: bytes) -> bytes:
    """Пересчитать все CRC нашим ядром и перепроверить результат."""
    with tempfile.TemporaryDirectory(prefix="ms43-patch-") as directory:
        source = Path(directory) / "patched.bin"
        target = Path(directory) / "checked.bin"
        source.write_bytes(image)
        ck.repair_image(source, target)  # падает, если проверка не сошлась
        return target.read_bytes()


@dataclass
class PatchResult:
    image: bytes
    applied: list[str]
    changed_entries: int
    changed_bytes: int
    regions: tuple[str, ...]


def _apply(
    image: bytes,
    patches: list[Patch],
    from_data: str,
    to_data: str,
) -> PatchResult:
    if len(image) != 0x80000:
        raise PatchError(f"нужен полный образ 512 КиБ, а не {len(image)} байт")
    out = bytearray(image)
    changed_entries = 0
    changed_bytes = 0
    regions: set[str] = set()
    for patch in patches:
        for entry in patch.entries:
            current = bytes(out[entry.address : entry.end])
            expected = entry.base_data if from_data == "base_data" else entry.patch_data
            target = entry.patch_data if to_data == "patch_data" else entry.base_data
            if target is None:
                raise PatchError(
                    f"{patch.title} / {entry.name} @0x{entry.address:05X}: в патчлисте "
                    "нет basedata — восстановить исходные байты нечем, "
                    "держите стоковый бэкап и не снимайте этот патч"
                )
            if current == target:
                # запись уже в целевом состоянии — идемпотентность, ничего не пишем
                regions.add(entry.region)
                continue
            if expected is not None and current != expected:
                raise PatchError(
                    f"{patch.title} / {entry.name} @0x{entry.address:05X}: в образе "
                    f"{current.hex().upper()}, ожидалось {expected.hex().upper()} — отказ"
                )
            if current != target:
                out[entry.address : entry.end] = target
                changed_bytes += sum(1 for a, b in zip(current, target) if a != b)
                changed_entries += 1
            regions.add(entry.region)
    patched = _recompute_checksums(bytes(out))
    # контроль: изменились только байты записей патча и позиции CRC
    allowed = {0x3C24, 0x3C25, 0x6FDE0, 0x6FDE1, 0x73FE0, 0x73FE1}
    for patch in patches:
        for entry in patch.entries:
            allowed.update(range(entry.address, entry.end))
    for index, (before, after) in enumerate(zip(image, patched)):
        if before != after and index not in allowed:
            raise PatchError(
                f"после пересчёта сумм изменился байт 0x{index:05X} вне патча — отказ"
            )
    return PatchResult(
        image=patched,
        applied=[patch.title for patch in patches],
        changed_entries=changed_entries,
        changed_bytes=changed_bytes,
        regions=tuple(sorted(regions)),
    )


def conflicts(patches: list[Patch]) -> list[str]:
    """Взаимные пересечения патчей: одна и та же запись, разные данные.

    Пример из патчлиста: ``[PATCH] MAF Sensor Hack 2048kg/h`` и
    ``[PATCH] MAF Sensor Hack 4096kg/h`` правят одну и ту же ячейку разными
    байтами — вместе они не применяются, это альтернативы.
    """
    problems: list[str] = []
    for index, first in enumerate(patches):
        for second in patches[index + 1 :]:
            for a in first.entries:
                for b in second.entries:
                    if a.address < b.end and b.address < a.end and a.patch_data != b.patch_data:
                        problems.append(
                            f"{first.title} и {second.title} пересекаются на "
                            f"0x{max(a.address, b.address):05X} с разными данными"
                        )
    return problems


def apply_patches(
    image: bytes,
    catalog: PatchCatalog,
    titles: list[str],
    *,
    allow_gated: bool = False,
    declarations: set[str] | None = None,
) -> PatchResult:
    """Применить патчи к стоку: сначала проверить всё, потом записать, потом CRC."""
    refuse_unsupported_version(image)
    patches = [catalog.patch(title) for title in titles]
    _refuse_gated(patches, allow_gated, declarations or set())
    overlapping = conflicts(patches)
    if overlapping:
        raise PatchError("; ".join(overlapping[:3]))
    for patch in patches:
        status = check_patch(image, patch)
        if status.state == "applied":
            continue
        if status.state != "applicable":
            raise PatchError(
                f"{patch.title}: состояние {status.state} — {status.detail}; "
                + ("; ".join(status.mismatched[:3]) if status.mismatched else "")
            )
    return _apply(image, patches, from_data="base_data", to_data="patch_data")


def revert_patches(
    image: bytes,
    catalog: PatchCatalog,
    titles: list[str],
) -> PatchResult:
    """Снять патчи: вернуть basedata. Обратная операция обязана быть точной."""
    refuse_unsupported_version(image)
    patches = [catalog.patch(title) for title in titles]
    for patch in patches:
        status = check_patch(image, patch)
        if status.state == "applicable":
            continue
        if status.state != "applied":
            raise PatchError(
                f"{patch.title}: снять нельзя (состояние {status.state}) — {status.detail}"
            )
    return _apply(image, patches, from_data="patch_data", to_data="base_data")


# --------------------------------------------------------------------------
# Отчёт и командная строка
# --------------------------------------------------------------------------


def report_catalog(catalog: PatchCatalog) -> str:
    lines = [
        f"{catalog.source_name} ({catalog.source_file})",
        f"  источник: {catalog.source_url}",
        f"  sha256 источника: {catalog.source_sha256}",
        f"  извлечено: {catalog.extracted}; применимо к: {catalog.applies_to}",
        f"  патчей: {len(catalog.patches)}, записей: "
        f"{sum(len(p.entries) for p in catalog.patches)}, параметров: {len(catalog.symbols)}",
        "",
        f"{'патч':56s} {'группа':20s} {'зн':>3s} {'области':>16s} "
        f"{'проверка':>9s} {'ворота':>26s}",
    ]
    for patch in catalog.patches:
        rules = gate(patch)
        if not rules.get("offered", True):
            state = "не предлагаем"
        elif rules.get("declaration"):
            state = f"подтверждение: {rules['declaration']}"
        else:
            state = "предлагаем"
        lines.append(
            f"{patch.title[:56]:56s} {patch.group:20s} {len(patch.entries):3d} "
            f"{','.join(patch.regions):>16s} "
            f"{patch.verified_images}/{patch.checked_images:>7d} {state:>26s}"
        )
    return "\n".join(lines)


def report_image(image: bytes, catalog: PatchCatalog) -> str:
    version = ck.read_cal_version(image)
    supported = supported_software()
    verdict = "поддерживается" if version in supported else "НЕ поддерживается: запись запрещена"
    lines = [
        f"версия калибровки: {version or 'не прочитана'} ({verdict}; "
        f"политика: {', '.join(supported) or 'пусто'})"
    ]
    if version not in supported:
        # Патчлист — только 430069: на чужой версии даже разбор basedata даёт
        # мусорные «конфликты» и может быть неверно прочитан как «патчи почти
        # подходят». Поэтому дальше не идём, а честно отказываем.
        lines.append(
            "патчи не проверяются: basedata патчлиста v2.9.2 снята с 18 стоков "
            "MS430069, на других версиях её байтовые маски не доказаны "
            "(MS43/docs/VERSION_COVERAGE_RU.md §5)"
        )
        return "\n".join(lines)
    for status in check_image(image, catalog):
        lines.append(
            f"{status.patch.title[:56]:56s} {status.state:14s} {status.detail}"
        )
        for item in status.mismatched[:2]:
            lines.append(f"    {item}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()  # Windows: не падать на символах вне cp1251
    parser = argparse.ArgumentParser(
        description=(
            "Патчи MS4X Community Patchlist для MS430069: каталог, проверка, "
            "применение и снятие (с пересчётом контрольных сумм)."
        )
    )
    parser.add_argument("--extract", metavar="XDF", help="разобрать патчлист XDF в каталог JSON")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="путь каталога JSON")
    parser.add_argument("--list", action="store_true", help="напечатать каталог патчей")
    parser.add_argument("--check", metavar="IMAGE", help="проверить образ: какие патчи применимы")
    parser.add_argument("--apply", metavar="IMAGE", help="применить патчи к образу")
    parser.add_argument("--revert", metavar="IMAGE", help="снять патчи с образа")
    parser.add_argument("--patch", action="append", default=[], help="название патча (можно несколько)")
    parser.add_argument("--out", help="куда записать результат")
    parser.add_argument("--allow-gated", action="store_true", help="разрешить патчи, которые мы не предлагаем")
    parser.add_argument("--declaration", action="append", default=[], help="подтверждение (immo_change, immo_virginize)")
    args = parser.parse_args(argv)

    if args.extract:
        catalog = extract_catalog(Path(args.extract))
        path = save_catalog(catalog, Path(args.catalog))
        print(f"каталог записан: {path}")
        print(report_catalog(catalog))
        return 0

    catalog = load_catalog(Path(args.catalog))
    if args.list or (not args.check and not args.apply and not args.revert):
        print(report_catalog(catalog))
        return 0

    if args.check:
        image = Path(args.check).read_bytes()
        print(report_image(image, catalog))
        # Ненулевой код, если образ не покрыт патчлистом: скрипты обязаны
        # замечать отказ, а не считать «конфликты» результатом проверки.
        return 0 if ck.read_cal_version(image) in supported_software() else 2

    for mode, source in (("apply", args.apply), ("revert", args.revert)):
        if not source:
            continue
        if not args.patch:
            print("нужно указать хотя бы один --patch", file=sys.stderr)
            return 2
        if not args.out:
            print("нужен --out", file=sys.stderr)
            return 2
        image = Path(source).read_bytes()
        try:
            if mode == "apply":
                result = apply_patches(
                    image,
                    catalog,
                    args.patch,
                    allow_gated=args.allow_gated,
                    declarations=set(args.declaration),
                )
            else:
                result = revert_patches(image, catalog, args.patch)
        except PatchError as error:
            print(f"{mode}: {error}", file=sys.stderr)
            return 2
        Path(args.out).write_bytes(result.image)
        print(
            f"{mode}: {', '.join(result.applied)}; байт изменено {result.changed_bytes}, "
            f"записей {result.changed_entries}, области {','.join(result.regions)} -> {args.out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
