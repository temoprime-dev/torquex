# -*- coding: utf-8 -*-
"""Офлайн-индекс ожидаемого момента по кривой КПД зажигания ЭБУ (раунд 4, §2а.2).

Считает, что ЭБУ сам считает «эффективностью зажигания»: карта
``ip_eff_iga_mon`` («ignition efficiency», MS43 и MS45) переводит отставание
угла от оптимума в долю момента. Для каждой ячейки карты зажигания:

    delta_stock = clamp(stock − MBT, ось_eff.min, 0)
    delta_new   = clamp(patched − MBT, ось_eff.min, 0)
    прирост_i   = eff(delta_new) − eff(delta_stock)        (в долях момента)

MBT для MS43 — стоковая ``ip_iga_optm_tco_2`` (та же интерполяция 16×12 →
сетку карты, что и в генераторе; в старом сравнительном индексе это опора
кривой). Для связанного патча дополнительно считается ``linked_model`` с
пост-патчевой MBT-картой: он проверяет, что подъём опоры не создаёт ложного
рассогласования. Индекс — среднее арифметическое по ячейкам: ВЕСОВ ПО
НАГРУЗКЕ НЕТ (честное ограничение: карта работает на всех режимах, а
средний по тахометру драйвцикл здесь не воспроизводится). Это ОЖИДАНИЕ по
математике ЭБУ, а не обещание: дино не мерилось, мощность не обещается.

Для MS45 опорной карты момента нет (аналог MBT не доказан — см.
``docs/STAGE1_REPORT_RU.md``), поэтому там печатается только кривая
``ip_eff_iga_mon`` и честно сказано «по ячейкам не считается».

Запуск (из корня репозитория):
    python3 tools/torque_index.py MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin
    python3 tools/torque_index.py STOCK.bin --patched PATCHED.bin --md out.md --json out.json
    python3 tools/torque_index.py STOCK.bin --flat 3.0     # сравнение «плоской» добавки

Модуль ничего не пишет в байты — только читает образы.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from bisect import bisect_left
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

IGNITION_SYMBOL_MS43 = "ip_iga_ron_98_pl_ivvt__n__maf"


def interpolate_curve(xs: list[float], ys: list[float], x: float) -> float:
    """Кусочно-линейная интерполяция: ``xs`` упорядочены (в любую сторону), вне диапазона — крайнее значение."""
    if len(xs) != len(ys) or not xs:
        raise ValueError("кривая задана некорректно")
    pairs = sorted(zip((float(v) for v in xs), (float(v) for v in ys)))
    xs_s = [p[0] for p in pairs]
    ys_s = [p[1] for p in pairs]
    x = float(x)
    if x <= xs_s[0]:
        return ys_s[0]
    if x >= xs_s[-1]:
        return ys_s[-1]
    i = bisect_left(xs_s, x)
    if i == 0:
        return ys_s[0]
    x0, x1 = xs_s[i - 1], xs_s[i]
    y0, y1 = ys_s[i - 1], ys_s[i]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def deltas_index(stock: list[float], patched: list[float], mbt: list[float],
                 curve_x: list[float], curve_y: list[float]) -> list[float]:
    """Прирост доли момента по ячейкам (0 — там, где менять было нечего)."""
    if not (len(stock) == len(patched) == len(mbt)):
        raise ValueError("карты разной длины")
    axis_min = min(curve_x)
    out: list[float] = []
    for s, p, m in zip(stock, patched, mbt):
        d0 = max(axis_min, 0.0 if s >= m else s - m)
        d1 = max(axis_min, 0.0 if p >= m else p - m)
        out.append(interpolate_curve(curve_x, curve_y, d1)
                   - interpolate_curve(curve_x, curve_y, d0))
    return out


def linked_deltas_index(stock: list[float], patched: list[float],
                        stock_mbt: list[float], patched_mbt: list[float],
                        curve_x: list[float], curve_y: list[float]) -> list[float]:
    """Moment-model delta when the MBT reference is also patched.

    This is separate from :func:`deltas_index`, which intentionally retains the
    historical stock-MBT reference for comparison.  Linked MS42/MS43 packages
    raise RON98 and warm MBT together, so the field-facing model check must use
    the matching pair rather than counting the same angle twice.
    """
    if not (len(stock) == len(patched) == len(stock_mbt) == len(patched_mbt)):
        raise ValueError("linked maps have different lengths")
    axis_min = min(curve_x)
    out: list[float] = []
    for before, after, before_mbt, after_mbt in zip(
            stock, patched, stock_mbt, patched_mbt):
        d0 = max(axis_min, 0.0 if before >= before_mbt else before - before_mbt)
        d1 = max(axis_min, 0.0 if after >= after_mbt else after - after_mbt)
        out.append(
            interpolate_curve(curve_x, curve_y, d1)
            - interpolate_curve(curve_x, curve_y, d0)
        )
    return out


def summarize(deltas: list[float], rows: int, columns: int) -> dict:
    changed = [d for d in deltas if d > 1e-12]
    zero = len(deltas) - len(changed)
    per_row = []
    for r in range(rows):
        band = deltas[r * columns:(r + 1) * columns]
        per_row.append({
            "cells": len(band),
            "changed": sum(1 for d in band if d > 1e-12),
            "mean_pct": round(100.0 * sum(band) / len(band), 3) if band else 0.0,
        })
    per_col = []
    for c in range(columns):
        band = [deltas[r * columns + c] for r in range(rows)]
        per_col.append({
            "cells": len(band),
            "changed": sum(1 for d in band if d > 1e-12),
            "mean_pct": round(100.0 * sum(band) / len(band), 3) if band else 0.0,
        })
    return {
        "cells": len(deltas),
        "changed_cells": len(changed),
        "unchanged_cells": zero,
        "mean_pct": round(100.0 * sum(deltas) / len(deltas), 3) if deltas else 0.0,
        "mean_changed_pct": round(100.0 * sum(changed) / len(changed), 3) if changed else 0.0,
        "max_pct": round(100.0 * max(deltas), 3) if deltas else 0.0,
        "by_rpm_row": per_row,
        "by_load_column": per_col,
    }


# ---------------------------------------------------------------------------
# Чтение кривой КПД из образов (только через штатные загрузчики репозитория)
# ---------------------------------------------------------------------------

MS43_DEFINITIONS = REPO_ROOT / "MS43" / "definitions"


def _ms43_definition(version: str):
    """512K-определение конкретной версии (именование бывает двух видов:
    ``Siemens_MS43_430069_...`` и ``MS430037_...``). Без него кривую читать
    нельзя: адреса объектов в разных версиях разные, чужие адреса дают
    мусор, а не приблизительную правду."""
    from MS43.tools import xdf as xdf43

    matches = sorted(MS43_DEFINITIONS.glob(f"*{version}*_Extended_Load_512K.xdf"))
    if not matches:
        raise LookupError(
            f"для версии {version} нет 512K-определения в {MS43_DEFINITIONS.name}/ — "
            "кривая КПД не декодируется, индекс не публикуется")
    return xdf43.load_definition(matches[0])


def _ms43_curve(image: bytes, version: str) -> tuple[list[float], list[float], str]:
    definition = _ms43_definition(version)
    curve_obj = definition.objects["ip_eff_iga_mon"][0]
    axis_obj = definition.objects["ldp_iga_dif_av__eff_iga_mon"][0]
    ys = definition.read_engineering(image, curve_obj)
    xs = definition.read_engineering(image, axis_obj)
    if len(xs) != len(ys):
        raise ValueError(f"ось {len(xs)} и кривая {len(ys)} не совпали")
    # Проверка правдоподобия: кривая КПД — убывающая, от 1.0 вниз; ось —
    # неположительные углы задержки, строго убывающая. Читалка на неверном
    # адресе даёт константы или значения вне диапазона — молча публиковать
    # такое нельзя.
    if not ys or abs(ys[0] - 1.0) > 0.02:
        raise ValueError(f"кривая {curve_obj.title} не начинается с 1.0 "
                         f"(первое {ys[:1]!r}) — похоже на неверный адрес")
    if any(y2 > y1 + 0.02 for y1, y2 in zip(ys, ys[1:])):
        raise ValueError("кривая КПД не убывает — данные недостоверны")
    if any(x > 1.0 for x in xs) or min(xs) < -60.0:
        raise ValueError("ось угла вне физдиапазона −60..+1° — данные недостоверны")
    if any(x2 >= x1 for x1, x2 in zip(xs, xs[1:])):
        raise ValueError("ось угла не убывает — данные недостоверны")
    return xs, ys, f"{curve_obj.title}+{axis_obj.title}"


def _ms45_curve(image: bytes, family: str) -> tuple[list[float], list[float], str]:
    from MS45.tools import xdf as xdf45
    from MS45.tools import checksums as ck45

    cal, cal_offset = ck45.extract_cal(image)
    definition = xdf45.load_definition(family=family)
    obj = definition.objects["ip_eff_iga_mon"]
    ys = xdf45.read_engineering(image, obj, base_offset=cal_offset)
    axis = obj.axis("x")
    if axis is None or axis.scaler is None:
        raise ValueError("ось кривой ip_eff_iga_mon не определена")
    xs = []
    for i in range(axis.count):
        start = cal_offset + axis.address + i * axis.element_size_bytes
        raw = int.from_bytes(image[start:start + axis.element_size_bytes], "big")
        xs.append(axis.scaler.to_engineering(raw))
    return xs, ys, "ip_eff_iga_mon"


def _detect_platform(image: bytes) -> str:
    """MS43 — по номеру версии в калибровке; остальное отдаём MS45."""
    from MS43.tools import calibration as cal43

    try:
        if cal43.read_cal_version(image):
            return "MS43"
    except Exception:
        pass
    return "MS45"


def _ms43_maps(image: bytes, version_policy) -> tuple[list[float], list[float], int, int]:
    from MS43.tools import stages as stages43

    entry = version_policy.objects[IGNITION_SYMBOL_MS43]
    stock = stages43._read_all(image, entry)
    mbt = stages43._optm_caps(image, version_policy)
    if mbt is None:
        raise ValueError("ip_iga_optm_tco_2 или её оси не найдены в политике версии")
    return stock, mbt, entry.rows, entry.columns


def compute_ms43(stock_image: bytes, patched_image: bytes | None,
                 flat_deg: float | None) -> dict:
    from MS43.tools import calibration as cal43
    from MS43.tools import policy as pl43
    from MS43.tools import stages as stages43

    version = cal43.read_cal_version(stock_image)
    policy = pl43.load_policy_for_version(version) or pl43.load_policy()
    stock, mbt, rows, columns = _ms43_maps(stock_image, policy)
    result = {
        "platform": "MS43",
        "version": version,
        "symbol": IGNITION_SYMBOL_MS43,
        "mbt_symbol": "ip_iga_optm_tco_2 (интерполяция на сетку карты)",
        "curve": None,
        "grid": {"rows": rows, "columns": columns},
    }
    try:
        xs, ys, curve_name = _ms43_curve(stock_image, version)
        result["curve"] = {"name": curve_name, "x_deg": [round(v, 4) for v in xs],
                           "eff": [round(v, 5) for v in ys]}
    except (LookupError, ValueError) as error:
        # кривой для этой версии нет или она читается недостоверно: индекс
        # не публикуем, но статистику записанных угостей считаем честно
        result["curve_error"] = str(error)
        xs = ys = None
    patched = None
    if patched_image is not None:
        patched, patched_mbt, _, _ = _ms43_maps(patched_image, policy)
        if result["curve"] is not None:
            # Keep the original stock-reference index for backward-compatible
            # comparisons, and add the paired index used by linked RON98/MBT
            # packages.  The latter should stay near zero when both maps move
            # together; neither value is a dyno or power claim.
            result["patched"] = summarize(deltas_index(stock, patched, mbt, xs, ys),
                                          rows, columns)
            result["linked_model"] = summarize(
                linked_deltas_index(stock, patched, mbt, patched_mbt, xs, ys),
                rows, columns,
            )
            # Control: the realized patched RON98 angle must not exceed the
            # realized patched MBT reference.  Existing stock cells above MBT
            # are reported separately and are never lowered by the generator.
            over = sum(1 for p, m in zip(patched, patched_mbt) if p > m + 1e-9)
            result["patched"]["cells_above_mbt"] = over
            result["patched"]["cells_above_mbt_stock"] = sum(
                1 for s, m in zip(stock, mbt) if s > m + 1e-9)
        else:
            diffs = [b - a for a, b in zip(stock, patched)]
            changed = [d for d in diffs if d > 1e-9]
            result["patched"] = {
                "cells": len(diffs),
                "changed_cells": len(changed),
                "unchanged_cells": len(diffs) - len(changed),
                "mean_add_deg": round(sum(changed) / len(changed), 3) if changed else 0.0,
                "max_add_deg": round(max(changed), 3) if changed else 0.0,
            }
    if flat_deg is not None and result["curve"] is not None:
        flat = [min(s + float(flat_deg), m) for s, m in zip(stock, mbt)]
        result["flat_reference"] = summarize(
            deltas_index(stock, flat, mbt, xs, ys), rows, columns)
        result["flat_reference"]["requested_deg"] = float(flat_deg)
    return result


def compute_ms45(stock_image: bytes) -> dict:
    from MS45.tools import policy as pl45
    from MS45.tools import checksums as ck45

    cal, _ = ck45.extract_cal(stock_image)
    family = ck45.read_family(cal)
    xs, ys, curve_name = _ms45_curve(stock_image, family)
    return {
        "platform": "MS45",
        "family": family,
        "curve": {"name": curve_name, "x_deg": [round(v, 4) for v in xs],
                  "eff": [round(v, 5) for v in ys]},
        "per_cell": None,
        "why_no_per_cell": (
            "индекс по ячейкам не считается: опорной карты момента (аналога "
            "ip_iga_optm_tco_2) в MS45 нет — кандидат ip_iga_ref__n__maf "
            "отклонён (семантика не доказана), см. docs/STAGE1_REPORT_RU.md; "
            "кривая ниже показывает только форму потери момента от "
            "задержки угла относительно оптимума"
        ),
    }


# ---------------------------------------------------------------------------
# Markdown для отчёта
# ---------------------------------------------------------------------------

def to_markdown(result: dict) -> str:
    lines = [f"### Индекс ожидаемого момента — {result['platform']}"
             + (f" {result.get('version', '')}" if result.get("version") else ""), ""]
    curve = result.get("curve")
    if curve is None:
        lines.append(f"Кривая КПД не декодируется: {result.get('curve_error', '?')}")
        lines.append("")
    else:
        lines.append(f"Кривая ЭБУ `{curve['name']}` (X — отставание от оптимума, °):")
        lines.append("")
        lines.append("| дельта от MBT, ° | " + " | ".join(f"{v:+.1f}" for v in curve["x_deg"]) + " |")
        lines.append("| --- |" + " ---: |" * len(curve["x_deg"]))
        lines.append("| доля момента | " + " | ".join(f"{v:.3f}" for v in curve["eff"]) + " |")
        lines.append("")
    for key, title in (("patched", "запись генератора"),
                       ("flat_reference", "сравнение: плоская добавка")):
        data = result.get(key)
        if not data:
            continue
        extra = (f" (+{data['requested_deg']:g}° всем ячейкам)"
                 if "requested_deg" in data else "")
        lines.append(f"**{title}{extra}**")
        lines.append("")
        lines.append(f"- ячейки: {data['cells']}; с приростом: {data['changed_cells']}; "
                     f"без прироста (сток у MBT/выше): {data['unchanged_cells']}"
                     + (f"; в итоговой карте выше MBT: {data['cells_above_mbt']} "
                        f"(столько же было и в стоке: {data['cells_above_mbt_stock']})"
                        if "cells_above_mbt" in data else ""))
        if "mean_pct" in data:
            lines.append(f"- средний индекс: **{data['mean_pct']:+.3f}%**; по изменённым: "
                         f"{data['mean_changed_pct']:+.3f}%; максимум: {data['max_pct']:+.3f}%")
        elif "mean_add_deg" in data:
            lines.append(f"- средний записанный угол (по изменённым): "
                         f"+{data['mean_add_deg']:.3f}°; максимум +{data['max_add_deg']:.3f}° "
                         "(индекс момента — см. причину выше)")
        lines.append("")
    if result["platform"] == "MS43" and result.get("patched") \
            and "by_rpm_row" in result["patched"]:
        data = result["patched"]
        rows = result["grid"]["rows"]
        cols = result["grid"]["columns"]
        lines.append(f"Средний прирост по строкам оборотов (карта {rows}×{cols}, "
                     "столбцы нагрузки агрегированы)")
        lines.append("")
        header = "| строка (ряд 20 ячеек) | прирост, % |"
        lines.append(header)
        lines.append("| ---: | ---: |")
        for i, row in enumerate(data["by_rpm_row"]):
            lines.append(f"| {i + 1} | {row['mean_pct']:+.3f} |")
        lines.append("")
    if result["platform"] == "MS45":
        lines.append(result["why_no_per_cell"])
        lines.append("")
    lines.append("> Ограничения: не дино; среднее по ячейкам без весов по нагрузке; "
                 "мощность не обещается. Это то, что ЭБУ сам считает «эффективностью "
                 "зажигания», применённое к плановым углам.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Офлайн-индекс ожидаемого момента по ip_eff_iga_mon (раунд 4 §2а)")
    parser.add_argument("stock", help="стоковый .bin (MS43 или MS45)")
    parser.add_argument("--patched", help="файл после генератора для сравнения")
    parser.add_argument("--flat", type=float, default=None,
                        help="сравнить с гипотетической плоской добавкой +N° (все "
                             "ячейки, потолок — MBT)")
    parser.add_argument("--json", help="куда положить машиночитаемый результат")
    parser.add_argument("--md", help="куда положить markdown-блок для отчёта")
    args = parser.parse_args(argv)

    stock_path = Path(args.stock)
    image = stock_path.read_bytes()
    platform = _detect_platform(image)
    if platform == "MS43":
        patched = Path(args.patched).read_bytes() if args.patched else None
        result = compute_ms43(image, patched, args.flat)
    else:
        if args.patched or args.flat:
            print("MS45: индекс по ячейкам не считается (MBT-опора не доказана) — "
                  "печатаю только кривую", file=sys.stderr)
        result = compute_ms45(image)

    markdown = to_markdown(result)
    print(markdown)
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    if args.md:
        Path(args.md).write_text(markdown + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
