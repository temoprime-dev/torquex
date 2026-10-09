#!/usr/bin/env python3
"""Render a delivery note for a generated calibration from the tool reports.

The JSON reports are for machines; the customer gets this. The note is built
only from what the other two tools actually did, so it cannot claim more than
the run proved:

* every change lists the address, the raw and engineering values it moved
  between, and the evidence behind the new value;
* everything the request asked for and did not get is listed with the reason;
* validation that has not happened is stated as outstanding, never implied to
  be done;
* the hashes of the exact source and output files are part of the note.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

VALIDATION_PENDING = "НЕ ВЫПОЛНЕНА"

#: Internal run statuses explained in customer terms.
STATUS_RU = {
    "WRITTEN": "файл записан на диск (в ЭБУ не записан)",
    "READY": "план проверен, файл ещё не записывался",
    "REJECTED": "отказано, файл не писался",
    "EMPTY": "изменений нет",
}

#: Russian labels for the intent kinds, used when the customer gave no reason.
KIND_RU = {
    "vmax": "ограничитель скорости",
    "rpm_limit": "ограничитель оборотов",
    "hardcut": "гистерезис отсечки на сбросе газа",
}

DEFAULT_REASON = "requested by the customer"

#: Above this many changes for one symbol the note summarises instead of listing.
MAX_ROWS_PER_SYMBOL = 8


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value)


def _table(rows: list[list[str]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _grouped_summary(symbol: str, changes: list[dict[str, Any]]) -> list[str]:
    """One line for a whole-table change instead of hundreds of rows."""
    values = [change for change in changes if isinstance(change.get("new_engineering"),
                                                         (int, float))]
    old = [change["old_engineering"] for change in changes
           if isinstance(change.get("old_engineering"), (int, float))]
    if not values or not old:
        return [symbol, "—", "—", "—", f"{len(changes)} столбцов", "—"]
    new = [change["new_engineering"] for change in values]
    deltas = [
        (change["new_engineering"] - change["old_engineering"])
        for change in changes
        if isinstance(change.get("old_engineering"), (int, float))
        and isinstance(change.get("new_engineering"), (int, float))
    ]
    units = values[0].get("units", "")
    worst = max(deltas, key=abs) if deltas else 0.0
    addresses = [change.get("address", "") for change in changes]
    span = f"{min(addresses)}…{max(addresses)}" if addresses else "—"
    return [
        f"{symbol} (сводно)",
        span,
        f"{min(old):.4g} … {max(old):.4g} {units}".strip(),
        f"{min(new):.4g} … {max(new):.4g} {units}".strip(),
        f"{len(changes)} столбцов",
        f"макс. {worst:+.4g}",
    ]


def _changes_table(patch_report: dict[str, Any]) -> str:
    rows = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for change in patch_report.get("changes", []):
        symbol = change["symbol"]
        if symbol not in grouped:
            grouped[symbol] = []
            order.append(symbol)
        grouped[symbol].append(change)
    for symbol in order:
        changes = grouped[symbol]
        if len(changes) > MAX_ROWS_PER_SYMBOL:
            rows.append(_grouped_summary(symbol, changes))
            continue
        for change in changes:
            index = change.get("index")
            symbol = change["symbol"] + (f"[{index}]" if index else "")
            delta = change.get("delta_percent")
            if delta is None:
                old = change.get("old_engineering")
                new = change.get("new_engineering")
                if (isinstance(old, (int, float)) and isinstance(new, (int, float))
                        and old != 0):
                    delta = (new - old) / abs(old) * 100.0
            rows.append([
                symbol,
                change.get("address", "—"),
                f"{_fmt(change.get('old_engineering'))} {change.get('units', '')}".strip(),
                f"{_fmt(change.get('new_engineering'))} {change.get('units', '')}".strip(),
                f"{change.get('old_raw')} -> {change.get('new_raw')}",
                (f"{float(delta):+.2f} %" if delta is not None
                 else "н/д" if isinstance(change.get("old_engineering"), (int, float))
                 and change.get("old_engineering") == 0 else "—"),
            ])
    if not rows:
        return "_Изменений нет._"
    table = _table(rows, ["Объект", "Адрес", "Было", "Стало", "Сырые значения",
                          "Дельта от стока"])
    if any(len(changes) > MAX_ROWS_PER_SYMBOL for changes in grouped.values()):
        table += ("\n\n_Для таблиц, изменённых целиком, приведена сводка; полный "
                  "постолбцовый список — в JSON-отчётах._")
    return table


def _evidence_column(patches: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {patch["symbol"]: patch for patch in patches}


def _requested_but_missing(generation: dict[str, Any]) -> str:
    rows = []
    for outcome in generation.get("outcomes", []):
        if outcome.get("status") in ("READY", None):
            continue
        rows.append([
            outcome.get("id", "—"),
            outcome.get("kind", "—"),
            outcome.get("status", "—"),
            outcome.get("detail", "").replace("|", "/"),
        ])
    if not rows:
        return "_Все пункты запроса выполнены._"
    return _table(rows, ["Пункт", "Тип", "Статус", "Причина"])


def build_note(generation_report: dict[str, Any],
               patch_report: dict[str, Any] | None = None,
               customer: str | None = None) -> str:
    generation = generation_report.get("generation", generation_report)
    patch_file = generation_report.get("patch_file", {})
    status = generation.get("status", "?")
    notes = generation.get("notes", [])
    blocked = generation.get("blocked_reasons", [])
    patches = generation.get("patches", [])
    evidence = _evidence_column(patches)

    title = "Наряд на калибровку MS41"
    if customer:
        title += f" — {customer}"
    elif patch_file.get("customer"):
        title += f" — {patch_file['customer']}"

    parts: list[str] = [f"# {title}", ""]
    if status == "BLOCKED":
        parts += ["> **Наряд не полон.** Часть пунктов отказана или ждёт данных; "
                  "см. раздел «Что не выполнено».", ""]
    elif status == "EMPTY":
        parts += ["> **Записывать нечего:** запрос не дал ни одного изменения.", ""]

    parts += ["## Что сделано", ""]
    if patch_report is None:
        parts += ["BIN ещё не собран: ниже только план изменений.", ""]
    else:
        parts += [
            f"Собран файл `{patch_report.get('output')}` из стокового "
            f"`{patch_report.get('source')}`.",
            "",
            f"- Выполнение: **{patch_report.get('status')}** — "
            f"{STATUS_RU.get(str(patch_report.get('status')), 'см. отчёт')}.",
            f"- Изменено байтов значений: {patch_report.get('value_bytes_changed')}, "
            f"контрольных слов: {patch_report.get('crc_bytes_changed')}, "
            f"всего: {patch_report.get('total_bytes_changed')}.",
            f"- Пересчитано блоков CRC: {patch_report.get('crc_blocks_recomputed')}; "
            f"цепочка после записи валидна: {patch_report.get('crc_valid_after')}. "
            f"Контрольная цепочка покрывает блок до "
            f"{patch_report.get('crc_covered_end')}.",
            f"- Байт вне плана изменено: {len(patch_report.get('unplanned_byte_changes') or [])}.",
            f"- SHA-256 исходника: `{patch_report.get('source_sha256')}`",
            f"- SHA-256 результата: `{patch_report.get('output_sha256')}`",
            "",
        ]

    parts += ["## Изменения", "", _changes_table(patch_report or {}), ""]

    if evidence:
        counts: dict[str, int] = {}
        ranges: dict[str, tuple[float, float]] = {}
        for change in (patch_report or {}).get("changes", []) or []:
            name = change["symbol"]
            counts[name] = counts.get(name, 0) + 1
            old = change.get("old_engineering")
            new = change.get("new_engineering")
            if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                low, high = ranges.get(name, (old, new))
                ranges[name] = (min(low, old), max(high, new))
        rows = []
        for symbol, patch in evidence.items():
            if counts.get(symbol, 0) > MAX_ROWS_PER_SYMBOL:
                low, high = ranges.get(symbol, (0.0, 0.0))
                rows.append([
                    f"{symbol} (сводно)",
                    _fmt(low, 4),
                    _fmt(high, 4),
                    patch.get("evidence", "—"),
                    "да",
                    f"{counts[symbol]} столбцов: "
                    + (patch.get("reason") or "").replace("|", "/"),
                ])
                continue
            planned = patch.get("planned", {})
            reason = (patch.get("reason") or "").replace("|", "/")
            if DEFAULT_REASON in reason:
                label = KIND_RU.get(str(patch.get("intent")), patch.get("intent", ""))
                reason = (f"типовое изменение ({label}): обоснование клиентом "
                          "не указано")
            rows.append([
                symbol,
                _fmt(planned.get("old_engineering")),
                _fmt(planned.get("new_engineering")),
                patch.get("evidence", "—"),
                "да" if patch.get("requires_validation") else "нет",
                reason,
            ])
        parts += ["### Почему именно эти значения", "",
                  _table(rows, ["Объект", "Было", "Стало", "Источник значения",
                                "Нужна проверка", "Обоснование"]), ""]
        if any(counts.get(symbol, 0) > MAX_ROWS_PER_SYMBOL for symbol in evidence):
            parts += ["_Для таблиц значения приведены диапазоном; обоснование "
                      "одинаково для всех столбцов._", ""]

    parts += ["## Что не выполнено", "", _requested_but_missing(generation), ""]

    hints = [patch.get("validation_hint") for patch in patches
             if patch.get("validation_hint")]
    parts += ["## Что нужно проверить после прошивки", ""]
    if hints:
        parts += [f"- {hint}" for hint in hints]
    else:
        parts += ["- Нечего проверять: изменений нет."]
    parts += ["", f"Статус проверки на момент выпуска наряда: **{VALIDATION_PENDING}** — "
                  "наряд построен по расчёту, а не по результатам заездов.", ""]

    logging = [note for note in notes if "required logging" in note]
    if logging:
        parts += ["## Требования к логированию", ""]
        parts += [f"- {line.strip()}" for line in " ".join(logging).split(";") if line.strip()]
        parts += [""]

    if blocked:
        parts += ["## Отказы", ""]
        parts += [f"- {reason}" for reason in blocked]
        parts += [""]

    parts += [
        "## Границы",
        "",
        "- Зажигание и топливные карты в наряд не входят: для них нужны логи, "
        "широкополосная лямбда и стенд (см. `docs/WORKFLOW_RU.md`).",
        "- Кривая MAF переписывается только по документированной кривой датчика; "
        "источник `documented_curve:*` — это файл кривой, а не измерения на машине, "
        "поэтому проверка по логам обязательна.",
        "- Файл подготовлен для записи внешним флешером; этот наряд прошивку не "
        "выполняет и не заменяет проверку на машине.",
        "- Значения из пунктов с источником `customer_intent` — это пожелание "
        "клиента, а не измеренный оптимум.",
    ]
    other_notes = [n for n in notes if "required logging" not in n]
    if other_notes:
        parts += ["", "## Примечания", ""]
        parts += [f"- {note}" for note in other_notes]
    parts.append("")
    return "\n".join(parts)


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a delivery note from generate_patches/calibration reports.")
    parser.add_argument("--generation", type=Path, required=True,
                        help="report written by generate_patches.py --report")
    parser.add_argument("--patch", type=Path, default=None,
                        help="report written by calibration.py --report")
    parser.add_argument("--customer", default=None)
    parser.add_argument("--out", type=Path, default=None,
                        help="write the note here; stdout if omitted")
    args = parser.parse_args()

    note = build_note(load_report(args.generation),
                      load_report(args.patch) if args.patch else None,
                      args.customer)
    if args.out:
        args.out.write_text(note, encoding="utf-8")
        print(f"note written to {args.out}")
    else:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
