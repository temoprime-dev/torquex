# -*- coding: utf-8 -*-
"""Раскладка добавки зажигания по ячейкам карты (раунд 4, задание §2а).

Плоская «+N° ко всем ячейкам» — это то, что делают массовые тюнеры (средняя
дельта RON91/98-карт у MOD4 — +3.4° на всех ячейках,
``docs/TUNER_SCOPE_ANALYSIS_RU.md`` §1), и она плоха: там, где сток уже у
оптимума, прироста момента нет, а риск остаётся; на границах зон появляются
скачки. Здесь — «грамотная» раскладка, общая для MS43 и MS45:

    add_cell = min(потолок стадии, max(0, MBT − сток) × k) × форма(нагрузка)

* ``потолок стадии`` — из ``tools/ignition_caps.py`` (единый источник:
  Stage 1 ≤ +3,0°, Stage 2 с логом ≤ +5,0°, профиль RON95 ≤ +1,5° без лога,
  RON91 — 0);
* ``MBT`` — карта оптимального угла: в старом режиме это
  ``ip_iga_optm_tco_2``. Для связанного MS42/MS43 Stage 1 вызывающая сторона
  передаёт ``mbt=None`` намеренно: старый потолок не обнуляет заявку, потому что
  ``ip_iga_optm_tco_2`` получает ту же интерполированную добавку отдельной
  contextual-записью; policy maximum всё равно действует. Для MS45 аналога с
  доказанной семантикой нет — там отсутствие MBT честно остаётся в наряде.
* ``k`` и форма по нагрузке — из ``docs/pack_log_requirements.json``
  (``packages.ignition.layout``), без подгонки под конкретный образ;
* после расчёта добавки прижимаются к сетке ВНИЗ и сглаживаются: соседние
  ячейки (по горизонтали и вертикали) отличаются не более чем на
  ``neighbour_max_step_deg``. Сглаживание умеет только уменьшать добавки,
  поэтому никакая запретная зона (потолок, зона лога с детонацией, низ
  нагрузки) не «подтягивается» вверх.

Модуль не знает про образы и политики: принимает списки чисел, возвращает
списки. Значения в карту пишет вызывающая сторона.
"""

from __future__ import annotations

from tools import ignition_caps


def shape_factor(load_mg_stroke: float, low: float | None = None,
                 high: float | None = None) -> float:
    """Доля добавки для данной нагрузки: 0 ниже ``low``, линейный рост, 1 с ``high``.

    Пороги — из спецификации (200/600 мг/такт, обоснование — веб-анализ §7:
    ниже 200 мг/такт живут режимы ХХ/ПХХ и хлопков, где добавка даёт рост NOx
    и риск нестабильности без прироста момента).
    """
    layout = ignition_caps.layout()
    if low is None:
        low = float(layout["shape_low_mg_stroke"])
    if high is None:
        high = float(layout["shape_high_mg_stroke"])
    if high <= low:
        raise ValueError("shape_high_mg_stroke должен быть больше low")
    value = float(load_mg_stroke)
    if value <= low:
        return 0.0
    if value >= high:
        return 1.0
    return (value - low) / (high - low)


def default_k(stage1: bool) -> float:
    """Коэффициент k из спецификации (0.5 — Stage 1, 1.0 — Stage 2 с логом)."""
    layout = ignition_caps.layout()
    return float(layout["mbt_gain_k"]["stage1" if stage1 else "stage2"])


def default_step() -> float:
    """Максимальный скачок добавки между соседними ячейками (°)."""
    return float(ignition_caps.layout()["neighbour_max_step_deg"])


def default_quantum() -> float:
    """Квант сетки угла (0.375°) — размер шага прижатия добавки."""
    return float(ignition_caps.layout()["quantum_deg"])


def mbt_margins(stock: list[float], mbt: list[float] | None) -> list[float]:
    """Запас до MBT по ячейкам; ``mbt=None`` — запаса нет (поле None не выдумываем).

    Ячейки, где сток уже на оптимуме или выше него (запас ≤ 0), получают 0:
    прироста момента там нет, и добавлять нечего.
    """
    if mbt is None:
        return []
    if len(mbt) != len(stock):
        raise ValueError(f"MBT {len(mbt)} ячеек, карты {len(stock)}")
    return [max(0.0, float(m) - float(s)) for m, s in zip(mbt, stock)]


def raw_additions(margins: list[float], ceiling_by_cell: list[float], k: float,
                  shape_by_cell: list[float] | None = None) -> list[float]:
    """add = min(потолок ячейки, запас × k) × форма (до прижатия к сетке).

    ``ceiling_by_cell`` — действующий потолок (Stage 1 — безлоговый; Stage 2 —
    минимум из потолка стадии и предела зоны лога); ``shape_by_cell`` — доля
    по нагрузке (1.0 — без формы).
    """
    if margins:
        if len(margins) != len(ceiling_by_cell):
            raise ValueError("размеры запасов и потолков не совпадают")
        adds = [min(c, m * k) for m, c in zip(margins, ceiling_by_cell)]
    else:
        adds = list(ceiling_by_cell)
    if shape_by_cell is not None:
        if len(shape_by_cell) != len(adds):
            raise ValueError("размеры формы и добавок не совпадают")
        adds = [a * f for a, f in zip(adds, shape_by_cell)]
    return adds


def _axis_position(axis: list[float], value: float) -> tuple[int, int, float]:
    """Bilinear interpolation position on an ascending calibration axis."""
    if not axis:
        raise ValueError("interpolation axis is empty")
    if value <= axis[0]:
        return 0, 0, 0.0
    if value >= axis[-1]:
        last = len(axis) - 1
        return last, last, 0.0
    for index, (left, right) in enumerate(zip(axis, axis[1:])):
        if left <= value <= right:
            span = right - left
            return index, index + 1, 0.0 if span == 0 else (value - left) / span
    last = len(axis) - 1
    return last, last, 0.0


def interpolate_grid(values: list[float], source_rows: int, source_columns: int,
                     source_row_axis: list[float], source_column_axis: list[float],
                     target_row_axis: list[float], target_column_axis: list[float]) -> list[float]:
    """Bilinearly interpolate a row-major grid onto another map's axes.

    This is used for the linked warm-MBT addition: it interpolates an *addition*,
    never an absolute angle, and therefore keeps the RON98/MBT relationship
    explicit in the manifest.
    """
    if len(values) != source_rows * source_columns:
        raise ValueError("source grid shape does not match values")
    if len(source_row_axis) != source_rows or len(source_column_axis) != source_columns:
        raise ValueError("source axes do not match source grid")
    result: list[float] = []
    for row_value in target_row_axis:
        r0, r1, rf = _axis_position(source_row_axis, float(row_value))
        for column_value in target_column_axis:
            c0, c1, cf = _axis_position(source_column_axis, float(column_value))
            v00 = values[r0 * source_columns + c0]
            v01 = values[r0 * source_columns + c1]
            v10 = values[r1 * source_columns + c0]
            v11 = values[r1 * source_columns + c1]
            result.append(
                (v00 * (1.0 - cf) + v01 * cf) * (1.0 - rf)
                + (v10 * (1.0 - cf) + v11 * cf) * rf
            )
    return result


def quantize_target_down(value: float, stock: float, quantum: float) -> float:
    """Quantize an absolute target down so its realised addition never exceeds plan."""
    if quantum <= 0:
        return value
    steps = int((float(value) - float(stock)) / quantum + 1e-9)
    return float(stock) + max(0, steps) * quantum


def quantize_down(adds: list[float], quantum: float | None = None) -> list[float]:
    """Прижать добавки к сетке ВНИЗ (полный квант), чтобы запись не превысила потолок."""
    if quantum is None:
        quantum = default_quantum()
    out: list[float] = []
    for value in adds:
        steps = int(value / quantum + 1e-9)
        out.append(steps * quantum)
    return out


def smooth_down(adds: list[float], rows: int, columns: int,
                max_step: float | None = None) -> list[float]:
    """Ограничитель перепада добавки между соседями (только уменьшение).

    Результат: ``|a[i] − a[j]| ≤ max_step`` для горизонтальных и вертикальных
    соседей сетки ``rows × columns`` (row-major). Реализация — две проходы
    обхода (прямой и обратный) с расстоянием_Manхэттена: дают точный
    результат ``a'_i = min_j (a_j + max_step × dist(i, j))`` для 4-связной
    сетки; значения только понижаются, поэтому потолок ячейки, зоны лога и
    нули запретных зон сохраняются.
    """
    if not adds:
        return []
    if max_step is None:
        max_step = default_step()
    if rows * columns != len(adds):
        raise ValueError(f"сетка {rows}×{columns} не совпала с {len(adds)} ячейками")
    grid = [list(adds[r * columns:(r + 1) * columns]) for r in range(rows)]
    # Прямой и обратный проходы (chamfer- distance transform, веса 1).
    for r in range(rows):
        for c in range(columns):
            best = grid[r][c]
            if r:
                best = min(best, grid[r - 1][c] + max_step)
            if c:
                best = min(best, grid[r][c - 1] + max_step)
            grid[r][c] = best
    for r in range(rows - 1, -1, -1):
        for c in range(columns - 1, -1, -1):
            best = grid[r][c]
            if r + 1 < rows:
                best = min(best, grid[r + 1][c] + max_step)
            if c + 1 < columns:
                best = min(best, grid[r][c + 1] + max_step)
            grid[r][c] = best
    return [grid[r][c] for r in range(rows) for c in range(columns)]


def smooth_1d(adds: list[float], max_step: float | None = None) -> list[float]:
    """То же для одной оси (столбцы нагрузки MS45: раскладка по оборотам не доказана)."""
    if not adds:
        return []
    if max_step is None:
        max_step = default_step()
    out = list(adds)
    for i in range(1, len(out)):
        out[i] = min(out[i], out[i - 1] + max_step)
    for i in range(len(out) - 2, -1, -1):
        out[i] = min(out[i], out[i + 1] + max_step)
    return out


def plan_additions(stock: list[float], mbt: list[float] | None,
                   ceiling: float, load_axis: list[float] | None,
                   columns: int, rows: int, stage1: bool = True,
                   ceiling_by_cell: list[float] | None = None,
                   quantize: bool = True) -> tuple[list[float], str]:
    """Полный план: запасы → потолок → форма → квант → сглаживание.

    ``ceiling`` — общий потолок стадии; ``ceiling_by_cell`` (пределы зон Stage
    2) учитывается, если задан: потолок ячейки = min(ceiling, предел зоны).
    Возвращает ``(добавки по ячейкам, строка в наряд)``. Если ``mbt`` или
    ``load_axis`` нет, соответствующий шаг честно пропускается и это отражено
    в строке наряда (никаких молчаливых упрощений).
    """
    notes: list[str] = []
    margins = mbt_margins(stock, mbt)
    if mbt is None:
        notes.append(
            "запас до старого MBT не используется: потолок задан policy/связкой "
            "вызывающего пакета"
        )
    if load_axis is None:
        notes.append("оси нагрузки нет — форма по нагрузке не применена")
        shape = None
    else:
        shape = [shape_factor(float(load)) for load in load_axis] * rows
    caps = [ceiling] * len(stock)
    if ceiling_by_cell is not None:
        caps = [min(ceiling, float(v)) for v in ceiling_by_cell]
    k = default_k(stage1=stage1)
    adds = raw_additions(margins, caps, k, shape)
    if quantize:
        adds = quantize_down(adds)
    adds = smooth_down(adds, rows, columns) if rows > 1 else smooth_1d(adds)
    return adds, "; ".join(notes)


def additions_stats(adds: list[float], ceiling: float,
                    zero_by_margin: int = 0) -> dict:
    """Сводка раскладки для наряда и отчёта (числа — факт, не обещание)."""
    changed = [a for a in adds if a > 0]
    at_ceiling = sum(1 for a in adds if a >= ceiling - 1e-9)
    return {
        "cells": len(adds),
        "written_cells": len(changed),
        "at_ceiling": at_ceiling,
        "zero_by_mbt": zero_by_margin,
        "mean_add": round(sum(changed) / len(changed), 3) if changed else 0.0,
        "max_add": max(changed, default=0.0),
    }


def max_neighbour_jump(adds: list[float], rows: int, columns: int) -> float:
    """Максимальный скачок между соседями (тесты и отчёт)."""
    if not adds or rows * columns != len(adds):
        return 0.0
    best = 0.0
    for r in range(rows):
        for c in range(columns):
            i = r * columns + c
            if c + 1 < columns:
                best = max(best, abs(adds[i] - adds[i + 1]))
            if r + 1 < rows:
                best = max(best, abs(adds[i] - adds[i + columns]))
    return round(best, 6)
