#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report stock/result cells for the round-5b pedal-response package.

Usage:
    python tools/pedal_report.py --stock STOCK.bin --result RESULT.bin
    python tools/pedal_report.py --stock STOCK.bin --result RESULT.bin --json

The tool reads both images with the same version policy.  It intentionally
reports the torque surfaces and every ``id_pvs_fl*`` threshold available in that
policy; it does not infer a tune from a different ECU/version.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import torquex  # noqa: E402
from MS42.tools import stages as ms42_stages  # noqa: E402
from MS42.tools.calibration import DEFAULT_POLICY as MS42_POLICY_PATH  # noqa: E402
from MS42.tools.calibration import Policy as MS42Policy  # noqa: E402
from MS42.tools.analyze_firmware import ADDRESS_BASES, identify_layout  # noqa: E402
from MS42.tools.xdf import get_object as get_ms42_object  # noqa: E402
from MS42.tools.xdf import load_definition as load_ms42_definition  # noqa: E402
from MS43.tools import stages as ms43_stages  # noqa: E402
from MS43.tools import policy as ms43_policy  # noqa: E402


MS43_TORQUE = ("ip_tqi_pvs__n__pvs", "ip_tqi_maf__n__maf")
MS42_TORQUE = ("ip_tqi_pvs_tco_2__n__pvs", "ip_tqi_maf__n__maf")


def _ms43_values(data: bytes, entry: Any) -> list[float]:
    return ms43_stages._read_all(data, entry)


def _ms42_objects() -> dict[str, list[Any]]:
    objects, problems = load_ms42_definition(ms42_stages.DEFAULT_XDF)
    if problems:
        raise ValueError("MS42 XDF не загрузился: " + "; ".join(str(item) for item in problems))
    return objects


def _ms42_values(data: bytes, symbol: str, policy: Any, objects: dict[str, list[Any]]) -> list[float]:
    entry = policy.entry(symbol)
    if entry is None or symbol not in objects:
        raise ValueError(f"MS42: символ {symbol} отсутствует в политике/XDF")
    obj = (get_ms42_object(objects, symbol) if entry.xdf_index is None
           else get_ms42_object(objects, symbol, entry.xdf_index))
    image, offset, _image_type, _address_base = load_ms42_calibration_from_bytes(data)
    return ms42_stages._ms42_values(image, offset, obj)


def load_ms42_calibration_from_bytes(data: bytes) -> tuple[bytearray, int, str, int]:
    """Mirror the documented MS42 file layout without writing a temporary file."""
    image_type, calibration_offset = identify_layout(len(data))
    if image_type is None or calibration_offset is None:
        raise ValueError("MS42: образ имеет неподдержанный размер")
    address_base = ADDRESS_BASES[image_type]
    return bytearray(data), calibration_offset, image_type, address_base


def _rows_for(platform: str, version: str, stock: bytes, result: bytes) -> list[dict[str, Any]]:
    if len(stock) != len(result):
        raise ValueError("сток и result должны иметь одинаковый размер")
    if platform == "MS43":
        policy = ms43_policy.load_policy_for_version(version)
        if policy is None:
            raise ValueError(f"MS43: нет политики для версии {version}")
        symbols = [symbol for symbol in MS43_TORQUE if symbol in policy.objects]
        symbols += sorted(symbol for symbol in policy.objects if symbol.startswith("id_pvs_fl"))
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            entry = policy.objects[symbol]
            before = _ms43_values(stock, entry)
            after = _ms43_values(result, entry)
            for index, (stock_value, result_value) in enumerate(zip(before, after)):
                rows.append({
                    "symbol": symbol, "index": index,
                    "stock": stock_value, "result": result_value,
                    "delta": result_value - stock_value,
                    "changed": abs(result_value - stock_value) > 1e-12,
                })
        return rows
    if platform == "MS42":
        policy = MS42Policy.load(MS42_POLICY_PATH)
        objects = _ms42_objects()
        symbols = [symbol for symbol in MS42_TORQUE if policy.entry(symbol) is not None]
        symbols += sorted(symbol for symbol in policy.entries if symbol.startswith("id_pvs_fl"))
        stock_image, stock_offset, _kind, _base = load_ms42_calibration_from_bytes(stock)
        result_image, result_offset, _kind, _base = load_ms42_calibration_from_bytes(result)
        rows = []
        for symbol in symbols:
            entry = policy.entry(symbol)
            obj = (get_ms42_object(objects, symbol) if entry.xdf_index is None
                   else get_ms42_object(objects, symbol, entry.xdf_index))
            before = ms42_stages._ms42_values(stock_image, stock_offset, obj)
            after = ms42_stages._ms42_values(result_image, result_offset, obj)
            for index, (stock_value, result_value) in enumerate(zip(before, after)):
                rows.append({
                    "symbol": symbol, "index": index,
                    "stock": stock_value, "result": result_value,
                    "delta": result_value - stock_value,
                    "changed": abs(result_value - stock_value) > 1e-12,
                })
        return rows
    raise ValueError(f"педальный отчёт не поддерживает {platform}")


def build_report(stock_path: Path, result_path: Path) -> dict[str, Any]:
    stock = stock_path.read_bytes()
    result = result_path.read_bytes()
    detection = torquex.detect(stock_path)
    result_detection = torquex.detect(result_path)
    if detection.platform not in {"MS42", "MS43"}:
        raise ValueError(f"педальный отчёт доступен только для MS42/MS43, получено {detection.platform}")
    if (detection.platform, detection.version) != (result_detection.platform, result_detection.version):
        raise ValueError("сток и result имеют разные ECU/версии")
    rows = _rows_for(detection.platform, detection.version, stock, result)
    return {
        "platform": detection.platform,
        "version": detection.version,
        "stock": str(stock_path),
        "result": str(result_path),
        "symbols": sorted({row["symbol"] for row in rows}),
        "changed_cells": sum(1 for row in rows if row["changed"]),
        "rows": rows,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Pedal response: {report['platform']} {report['version']}",
        "",
        "Stock/result table. Values come from the same version policy; this is not a power claim.",
        "",
        f"Changed cells: **{report['changed_cells']}**",
        "",
        "| symbol | index | stock | result | delta | changed |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| `{row['symbol']}` | {row['index']} | {row['stock']:.6g} | "
            f"{row['result']:.6g} | {row['delta']:+.6g} | "
            f"{'yes' if row['changed'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.stock, args.result)
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n" if args.as_json else markdown(report)
        if args.out:
            args.out.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
    except (OSError, ValueError, torquex.TorquexError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
