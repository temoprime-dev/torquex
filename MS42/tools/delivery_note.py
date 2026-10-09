#!/usr/bin/env python3
"""Render a delivery note for a generated MS42 calibration from the tool reports.

The JSON reports are for machines; the customer gets this. Everything in the
note comes from what the tools actually did:

* every change lists its file address, the raw and engineering values it moved
  between, and the evidence behind the new value;
* the calibration CRC record (and only it) is the one MS42 recomputes after an
  edit; boot and program records are reported as untouched;
* everything the request asked for and did not get is listed with the reason;
* validation that has not happened is stated as outstanding, never implied to
  be done;
* the hashes of the exact source and output files are part of the note.

The MS42 reports have a different shape from the MS41 ones, so this module
reads the MS42 keys: intents (not outcomes), the calibration report's
``crc_records_recomputed`` / ``crc_valid_after`` fields, and the per-change
``quantization_error_percent`` instead of an MS41-style delta.
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
    "PLANNED": "план построен",
}

#: Russian labels for the intent kinds, used when the customer gave no reason.
KIND_RU = {
    "vmax": "ограничитель скорости",
    "rpm_limit": "ограничитель оборотов",
    "hardcut": "гистерезис отсечки на сбросе газа",
    "ignition": "зажигание",
    "fueling": "топливоподача",
    "popcorn": "хлопки на сбросе газа",
    "vanos": "VANOS",
    "maf_curve": "кривая MAF",
    "launch_limiter": "лимиттер на старте",
}

DEFAULT_REASON = "customer_intent"

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
    values = [change for change in changes if isinstance(change.get("new_engineering"),
                                                        (int, float))]
    old = [change["old_engineering"] for change in changes
           if isinstance(change.get("old_engineering"), (int, float))]
    if not values or not old:
        return [symbol, "—", "—", "—", f"{len(changes)} столбцов", "—"]
    new = [change["new_engineering"] for change in values]
    errors = [change.get("quantization_error_percent") for change in changes
              if isinstance(change.get("quantization_error_percent"), (int, float))]
    units = values[0].get("units", "")
    addresses = [change.get("file_address") or change.get("address", "")
                 for change in changes]
    span = f"{min(addresses)}…{max(addresses)}" if addresses else "—"
    return [
        f"{symbol} (сводно)",
        span,
        f"{min(old):.4g} … {max(old):.4g} {units}".strip(),
        f"{min(new):.4g} … {max(new):.4g} {units}".strip(),
        f"{len(changes)} столбцов",
        (f"макс. {max(errors):.3g} %" if errors else "—"),
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
            name = change["symbol"] + (f"[{index}]" if index else "")
            old = change.get("old_engineering")
            new = change.get("new_engineering")
            delta = None
            if (isinstance(old, (int, float)) and isinstance(new, (int, float))
                    and old != 0):
                delta = (new - old) / abs(old) * 100.0
            rows.append([
                name,
                change.get("file_address") or change.get("address", "—"),
                f"{_fmt(old)} {change.get('units', '')}".strip(),
                f"{_fmt(new)} {change.get('units', '')}".strip(),
                f"{change.get('old_raw')} -> {change.get('new_raw')}",
                f"{float(delta):+.2f} %" if delta is not None else "—",
            ])
    if not rows:
        return "_Изменений нет._"
    table = _table(rows, ["Объект", "Адрес в файле", "Было", "Стало", "Сырые значения",
                          "Дельта от стока"])
    if any(len(changes) > MAX_ROWS_PER_SYMBOL for changes in grouped.values()):
        table += ("\n\n_Для таблиц, изменённых целиком, приведена сводка; полный "
                  "постолбцовый список — в JSON-отчётах._")
    return table


def _evidence_table(generation: dict[str, Any]) -> str:
    rows = []
    seen: set[tuple[Any, Any]] = set()
    per_symbol: dict[str, int] = {}
    capped: set[str] = set()
    for intent in generation.get("intents", []):
        for change in intent.get("changes", []):
            key = (change.get("symbol"), change.get("value"))
            if key in seen:
                continue
            seen.add(key)
            symbol = str(change.get("symbol", ""))
            per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
            if per_symbol[symbol] > MAX_ROWS_PER_SYMBOL:
                # A whole table (a popcorn delay, an injector scaling) can carry
                # dozens of values; the note lists a few and points at the JSON.
                capped.add(symbol)
                continue
            reason = (change.get("reason") or change.get("validation_hint") or "").replace("|", "/")
            if DEFAULT_REASON in str(change.get("evidence", "")):
                label = KIND_RU.get(str(change.get("intent_kind")), change.get("intent_kind", ""))
                reason = f"типовое изменение ({label}): значение задано клиентом"
            rows.append([
                change.get("symbol", "—"),
                _fmt(change.get("value")),
                change.get("evidence", "—"),
                "да" if change.get("requires_validation") else "нет",
                reason or "—",
            ])
    if not rows:
        return "_Значений не задано._"
    table = _table(rows, ["Объект", "Запрошено", "Источник значения", "Нужна проверка",
                          "Что проверять"])
    if capped:
        table += ("\n\n_Для таблиц, записанных целиком, показаны первые "
                  f"{MAX_ROWS_PER_SYMBOL} значения по объекту; полный список — в манифесте: "
                  + ", ".join(sorted(capped)) + "._")
    return table


def _requested_but_missing(generation: dict[str, Any]) -> str:
    rows = []
    for intent in generation.get("intents", []):
        if intent.get("status") in ("READY", None):
            continue
        reasons = "; ".join(
            finding.get("message", "")
            for finding in intent.get("findings", []) if finding.get("message")
        ) or intent.get("summary", "")
        requirements = intent.get("requirements") or {}
        summary = requirements.get("summary")
        if summary and summary not in reasons:
            reasons = f"{reasons} {summary}".strip()
        rows.append([
            str(intent.get("id", "—")),
            KIND_RU.get(str(intent.get("kind")), str(intent.get("kind", "—"))),
            str(intent.get("status", "—")),
            reasons.replace("|", "/") or "—",
        ])
    if not rows:
        return "_Все пункты запроса выполнены._"
    return _table(rows, ["Пункт", "Тип", "Статус", "Причина"])


def build_note(generation_report: dict[str, Any],
               patch_report: dict[str, Any] | None = None,
               customer: str | None = None) -> str:
    generation = generation_report.get("generation", generation_report)
    status = generation.get("status", "?")
    intents = generation.get("intents", [])

    title = "Наряд на калибровку MS42"
    if customer:
        title += f" — {customer}"

    parts: list[str] = [f"# {title}", ""]
    trust = generation.get("trust_level")
    if trust:
        parts += [f"> Уровень доверия: **{trust}**.", ""]
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
            f"`{patch_report.get('source')}` ({patch_report.get('image_type')}).",
            "",
            f"- Выполнение: **{patch_report.get('status')}** — "
            f"{STATUS_RU.get(str(patch_report.get('status')), 'см. отчёт')}.",
            f"- Изменено байтов значений: {patch_report.get('value_bytes_changed')}, "
            f"байтов контрольных сумм: {patch_report.get('crc_bytes_changed')}, "
            f"всего: {patch_report.get('total_bytes_changed')}.",
            f"- Пересчитано записей CRC: {patch_report.get('crc_records_recomputed')}; "
            f"все записи валидны после записи: {patch_report.get('crc_valid_after')}. "
            f"Правка лежит внутри блока калибровки 0x48000..0x4F843, поэтому меняется "
            f"только запись `cal`; записи `boot` и `prog` не затрагиваются.",
            f"- Байт вне плана изменено: {len(patch_report.get('unplanned_byte_changes') or [])}.",
            f"- SHA-256 исходника: `{patch_report.get('source_sha256')}`",
            f"- SHA-256 результата: `{patch_report.get('output_sha256')}`",
            "",
        ]

    parts += ["## Изменения", "", _changes_table(patch_report or {}), "",
              "### Почему именно эти значения", "",
              _evidence_table(generation), ""]

    parts += ["## Что не выполнено", "", _requested_but_missing(generation), ""]

    hints: list[str] = []
    for intent in intents:
        for change in intent.get("changes", []):
            hint = change.get("validation_hint")
            if hint and hint not in hints:
                hints.append(hint)
    parts += ["## Что нужно проверить после прошивки", ""]
    if hints:
        parts += [f"- {hint}" for hint in hints]
    else:
        parts += ["- Нечего проверять: изменений нет."]
    parts += ["", f"Статус проверки на момент выпуска наряда: **{VALIDATION_PENDING}** — "
                  "наряд построен по расчёту, а не по результатам заездов.", ""]

    parts += [
        "## Границы",
        "",
        "- Зажигание и топливные карты в наряд не входят: для них нужны логи, "
        "широкополосная лямбда и стенд (см. `docs/EVIDENCE_REQUIREMENTS_RU.md`).",
        "- Ограничитель оборотов поднимается только выше стока и только с явным "
        "подтверждением риска: клапанный механизм, масляный насос и штатная защита "
        "рассчитаны на заводской предел. Выше 6750 об/мин требуется отдельное "
        "подтверждение по масляному насосу.",
        "- Режим «(MAF Hack 2048kg/h)» не применяется: он требует патча программной "
        "части, а не правки калибровки.",
        "- Файл подготовлен для записи внешним флешером; этот наряд прошивку не "
        "выполняет и не заменяет проверку на машине.",
        "- Значения с источником `customer_intent` — это пожелание клиента, а не "
        "измеренный оптимум.",
        "- Хлопки попадают в файл только с письменным подтверждением, что катализатора нет, "
        "и планом дорожной проверки; агрессивный пресет не предлагается.",
        "- Карты впуска, перенесённые с донорской калибровки MS43, помечены источником "
        "`donor_ms43:<dataset>:<символ>`: это значения донорского мотора, а не замер этой "
        "машины.",
        f"- Проверены только те записи CRC, которые есть в образе: для полного чтения "
        f"512 КиБ это `boot`, `cal` и `prog`.",
        "",
    ]
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
