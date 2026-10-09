#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать ``config/gs8_factory.json`` (хеши) и ``config/gs8_factory_cal.json`` (окна калибровки).

Источники (читаются с диска разработчика):

* ``--spdaten DIR`` — фабричные данные BMW SP-Daten: ``*/data/GD8600``,
  ``GD8602``, ``GD8604`` с файлами ``.0PA`` (программа) и ``.0DA`` (калибровка);
* ``--alpina DIR`` — клон ``github.com/ms4x-net/alpina``: файлы
  ``Bosch_GS8.60.x_*_Full.bin``.

В ``gs8_factory.json`` попадают только хеши программ (по блокам 256 байт) и
хеши калибровок с названиями машин. В ``gs8_factory_cal.json`` лежат окна
калибровки (сжатые) для отката на сток; программы BMW и загрузчики в базы не
входят.

Пример::

    python tools/build_gs8_factory.py --spdaten D:/new_spdaten --alpina %TEMP%/alpina_src
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import gs8_state  # noqa: E402

GROUPS = {"GD8600": "GS8.60.0", "GD8602": "GS8.60.2", "GD8604": "GS8.60.4"}


def _read(path: Path):
    return gs8_state.parse_intel_hex(path.read_bytes().decode("latin-1"))


def collect_spdaten(root: Path, database: dict, payloads: dict) -> None:
    for group, system in GROUPS.items():
        for path in sorted(root.glob(f"*/data/{group}/*")):
            suffix = path.suffix.upper()
            if suffix not in (".0PA", ".0DA"):
                continue
            meta, memory = _read(path)
            if suffix == ".0PA":
                blocks = gs8_state.block_hashes(memory)
                key = gs8_state.short_hash(json.dumps(blocks).encode())
                database[system]["programs"].setdefault(key, {
                    "name": path.stem, "reference": meta.get("ZL_REFERENZ"), "blocks": blocks,
                })
            else:
                try:
                    window = gs8_state.memory_window(memory, system)
                except gs8_state.Gs8Error:
                    continue
                digest = gs8_state.short_hash(window)
                vehicle = (meta.get("K_V2") or meta.get("K_F2") or "").strip()
                database[system]["calibrations"].setdefault(digest, {
                    "hash": digest, "name": path.stem, "source": "bmw",
                    "reference": meta.get("ZL_REFERENZ"), "vehicle": vehicle,
                })
                payloads[system].setdefault(digest, {
                    "vehicle": vehicle, "source": "bmw", "data": gs8_state.encode_payload(window),
                })


def collect_alpina(root: Path, database: dict, payloads: dict) -> None:
    for path in sorted(root.rglob("Bosch_GS8.60.*_Full.bin")):
        found = re.match(r"Bosch_(GS8\.60\.\d)_(\w+?)_(.*?)_Alpina_(.*?)_(\d{7})_", path.name)
        if not found:
            continue
        system, program, _chassis_gearbox, model, number = found.groups()
        if system not in gs8_state.LAYOUTS:
            continue
        data = path.read_bytes()
        if len(data) != gs8_state.LAYOUTS[system][0]:
            continue
        window = gs8_state.calibration_window(data, system)
        digest = gs8_state.short_hash(window)
        vehicle = f"Alpina {model.replace('_', ' ')} (24 10 {number[4:]})"
        # к какой фабричной программе подходит эта калибровка Alpina
        best_name, best_score = None, 0.0
        for item in database[system]["programs"].values():
            score = gs8_state._program_score(data, item)
            if score > best_score:
                best_name, best_score = item["name"], score
        if best_score < gs8_state.PROGRAM_MATCH:
            continue
        database[system]["calibrations"].setdefault(digest, {
            "hash": digest, "name": f"Alpina {number[:2]} {number[2:4]} {number[4:]}",
            "source": "alpina", "reference": program, "vehicle": vehicle,
            "program": best_name,
        })
        payloads[system].setdefault(digest, {
            "vehicle": vehicle, "source": "alpina", "data": gs8_state.encode_payload(window),
        })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spdaten", required=True)
    parser.add_argument("--alpina", default="")
    parser.add_argument("--out", default=str(ROOT / "config" / "gs8_factory.json"))
    parser.add_argument("--out-cal", default=str(ROOT / "config" / "gs8_factory_cal.json"))
    args = parser.parse_args()
    database = {system: {"programs": {}, "calibrations": {}} for system in gs8_state.LAYOUTS}
    payloads: dict[str, dict] = {system: {} for system in gs8_state.LAYOUTS}
    collect_spdaten(Path(args.spdaten), database, payloads)
    if args.alpina:
        collect_alpina(Path(args.alpina), database, payloads)
    final = {"version": 2}
    for system, item in database.items():
        final[system] = {
            "programs": list(item["programs"].values()),
            "calibrations": list(item["calibrations"].values()),
        }
        print(f"{system}: программ {len(final[system]['programs'])}, "
              f"калибровок {len(final[system]['calibrations'])}")
    Path(args.out).write_text(json.dumps(final, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    Path(args.out_cal).write_text(json.dumps(payloads, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size = Path(args.out_cal).stat().st_size
    print(f"окна калибровки: {size // 1024} КиБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
