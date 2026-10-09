"""Разбор TunerPro-определений MS45 (XML с несколькими ROM) — исследовательский инструмент.

Сообщество (pazi88 и др.) собрало в одном файле определения для нескольких
заводских вариантов MS45. Инструмент извлекает таблицы конкретного ROM, чтобы
проверить их по стоковым образам и, если проверка проходит, добавить семью.
"""

from __future__ import annotations

import sys

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MS45.tools.console import prepare_stdout  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XML = ROOT / "definitions/MS45 ECU Definitions.xml"

# TunerPro: сколько байт занимает элемент каждого storagetype
_STORAGE_BITS = {
    "uint8": 8, "int8": 8, "uint16": 16, "int16": 16,
    "uint32": 32, "int32": 32, "float32": 32,
    "lo8hi8": 16, "hi8lo8": 16, "bits": 8,
}


@dataclass
class TableDef:
    name: str
    address: int
    storage: str
    #: выражение TunerPro: value = (raw - offset) / factor  (или наоборот, см. expression)
    expression: str
    units: str
    rows: int
    columns: int
    category: str
    description: str

    @property
    def bits(self) -> int:
        return _STORAGE_BITS.get(self.storage, 16)

    @property
    def byte_length(self) -> int:
        return (self.bits // 8) * max(1, self.rows) * max(1, self.columns)


@dataclass
class RomDef:
    xml_id: str
    internal_id: str
    filesize: str
    model: str
    year: str
    ecuid: str
    tables: list[TableDef]

    def find(self, pattern: str) -> list[TableDef]:
        rx = re.compile(pattern, re.I)
        return [t for t in self.tables if rx.search(t.name)]


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_definitions_xml(path: Path | str | None = None) -> list[RomDef]:
    path = Path(path or DEFAULT_XML)
    text = path.read_text(encoding="utf-8")
    text = text.replace("&deg;", "°").replace("&micro;", "µ")
    text = re.sub(r"<!DOCTYPE.*?\]>", "", text, flags=re.S)
    root = ET.fromstring(text)

    roms: list[RomDef] = []
    for rom in root.findall("rom"):
        rid = rom.find("romid")
        if rid is None:
            continue
        tables: list[TableDef] = []

        def walk(node: ET.Element) -> None:
            for child in node.findall("table"):
                scaling = child.find("scaling")
                tables.append(
                    TableDef(
                        name=child.get("name", ""),
                        address=int(child.get("storageaddress", "0"), 16)
                        if child.get("storageaddress", "").lower().startswith("0x")
                        else int(child.get("storageaddress", "0")),
                        storage=child.get("storagetype", ""),
                        expression=scaling.get("expression", "") if scaling is not None else "",
                        units=scaling.get("units", "") if scaling is not None else "",
                        rows=int(child.get("sizey") or 1),
                        columns=int(child.get("sizex") or 1),
                        category=child.get("category", ""),
                        description=_text(child, "description"),
                    )
                )
                walk(child)

        walk(rom)
        roms.append(
            RomDef(
                xml_id=_text(rid, "xmlid"),
                internal_id=_text(rid, "internalidstring"),
                filesize=_text(rid, "filesize"),
                model=_text(rid, "model"),
                year=_text(rid, "year"),
                ecuid=_text(rid, "ecuid"),
                tables=tables,
            )
        )
    return roms


def load_rom(path: Path | str | None = None, internal_id: str = "", *, filesize: str = "116kb") -> RomDef:
    """Найти ROM по ``internalidstring`` (и размеру образа) в XML-определениях."""
    candidates = [rom for rom in parse_definitions_xml(path) if rom.internal_id == internal_id]
    if filesize:
        exact = [rom for rom in candidates if rom.filesize == filesize]
        if exact:
            return exact[0]
    if not candidates:
        raise KeyError(f"в определениях нет ROM {internal_id!r}")
    return candidates[0]


def main() -> int:
    prepare_stdout()

    import argparse

    ap = argparse.ArgumentParser(description="разбор XML-определений MS45")
    ap.add_argument("--rom", default="", help="подстрока internalidstring")
    ap.add_argument("--grep", default="", help="регулярка по имени таблицы")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    for rom in parse_definitions_xml():
        if args.rom and args.rom not in rom.internal_id:
            continue
        tables = rom.tables
        if args.grep:
            tables = rom.find(args.grep)
        print(f"=== {rom.internal_id or rom.xml_id} ({rom.filesize}, {rom.model}, {rom.year}): "
              f"{len(rom.tables)} таблиц" + (f", по фильтру {len(tables)}" if args.grep else ""))
        for t in tables[: args.limit]:
            print(f"    {t.name[:44]:44s} 0x{t.address:05X} {t.storage:8s} {t.rows}x{t.columns} "
                  f"{t.units:10s} {t.expression}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
