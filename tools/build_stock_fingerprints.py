#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать ``config/stock_fingerprints.json`` для ``tools/cal_state.py``.

Источники:

* стоки репозитория (``MS43/firmware``, ``MS43/firmware_archive``): в базу идут
  хеши и значения карт (для сравнения «ближе всего к какому варианту»);
* дополнительные каталоги с заводскими чтениями (``--extra DIR``): берутся только
  ХЕШИ карт и границы ограничителей, значения чужих файлов не сохраняются.

Запуск (из корня репозитория)::

    python tools/build_stock_fingerprints.py --extra D:/eurotun_picks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cal_state  # noqa: E402


def _is_original(path: Path) -> bool:
    return path.suffix.lower() in {".original", ".bin"} and "stage" not in path.name.lower()


def collect(platform: str, stock_files: list[Path], extra_files: list[Path]) -> dict:
    adapter = cal_state.ADAPTERS[platform]
    maps: dict[str, dict] = {}
    used = 0
    for path, keep_values in [(p, True) for p in stock_files] + [(p, False) for p in extra_files]:
        found = adapter(path.read_bytes())
        if found is None:
            continue
        used += 1
        for name, values in found.items():
            slot = maps.setdefault(name, {"hashes": set(), "variants": {}})
            digest = cal_state.fingerprint(values)
            slot["hashes"].add(digest)
            if keep_values:
                slot["variants"][digest] = [round(v, 4) for v in values]
    return {
        "maps": {
            name: {"hashes": sorted(item["hashes"]), "variants": list(item["variants"].values())}
            for name, item in sorted(maps.items())
        },
        "files_used": used,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra", action="append", default=[], help="каталог с заводскими чтениями (только хеши)")
    parser.add_argument("--out", default=str(ROOT / "config" / "stock_fingerprints.json"))
    args = parser.parse_args()

    sources = {
        "MS43": (
            [p for p in sorted((ROOT / "MS43" / "firmware").glob("*.bin"))]
            + [p for p in sorted((ROOT / "MS43" / "firmware_archive").glob("*")) if p.is_file()],
            (524288,),
        ),
        "MS45": (
            [p for p in sorted((ROOT / "MS45" / "firmware").rglob("*.bin"))],
            (118784, 1048576, 1048580),
        ),
        "MS42": (
            [p for p in sorted((ROOT / "MS42" / "firmware").rglob("*.bin"))],
            (32768, 65536, 524288),
        ),
        "MS41": (
            [p for p in sorted((ROOT / "MS41" / "firmware").rglob("*.bin"))],
            (24576, 32768, 262144),
        ),
    }
    database: dict = {"version": 2}
    for platform, (files, sizes) in sources.items():
        files = [p for p in files if p.stat().st_size in sizes]
        extra_files: list[Path] = []
        for folder in args.extra:
            for path in sorted(Path(folder).rglob("*.Original")):
                if path.is_file() and path.stat().st_size in sizes and platform.lower() in str(path.parent.name).lower():
                    extra_files.append(path)
        database[platform] = collect(platform, files, extra_files)
    Path(args.out).write_text(json.dumps(database, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for platform in sources:
        item = database[platform]
        sizes = {name: len(slot["hashes"]) for name, slot in item["maps"].items()}
        print(f"{platform}: файлов {item['files_used']}; хешей по картам {sizes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
