#!/usr/bin/env python3
"""Ядро патчинга калибровок MS45 (SW 456 / 457).

Ядро — единственный путь к записи в образ. Порядок работы:

1. **План** (:func:`plan_changes`): образ проверяется целиком (размер, семейство,
   обе контрольные суммы), каждый запрошенный символ проходит через allowlist
   политики; считаются ячейки, квантование, дельта к стоку. План либо ``PLANNED``,
   либо ``REJECTED`` со списком отказов — «предупреждений о записи» здесь нет.
2. **Применение** (:func:`apply_plan`): пишутся только ячейки плана, затем
   пересчитываются суммы (пара MO3 → CRC-32), затем **пост-проверка**: измениться
   обязаны только ячейки плана и байты сумм. Всё остальное — отказ
   ``OUTSIDE_PLAN_CHANGE``.

Жёсткие отказы (не предупреждения):

* ``OUTSIDE_CAL_REGION`` — попытка правки вне ``0x40000..0x5CFFF``;
* ``CAL_VERSION_UNSUPPORTED`` — семейство не поддержано определениями;
* ``MPC_IMAGE_UNSUPPORTED`` / ``IMAGE_SIZE_UNSUPPORTED`` — не тот вход;
* ``SOURCE_CRC_INVALID`` — исходник не проходит проверку сумм;
* ``SYMBOL_NOT_ALLOWED`` / ``READ_ONLY`` — символа нет в allowlist / он защищён;
* ``POLICY_RANGE_VIOLATION`` / ``VALUE_NOT_REPRESENTABLE`` / ``QUANTIZATION_TOO_COARSE``;
* ``DELTA_TOO_LARGE`` — правка больше разрешённого шага от стока;
* ``CELL_COUNT`` / ``DUPLICATE_SYMBOL`` / ``OVERLAPPING_WRITES`` / ``TOO_MANY_CELLS``;
* ``ACKNOWLEDGEMENT_REQUIRED`` — нет подтверждения (например «нет катализатора»);
* ``NOTHING_TO_DO`` — план пустой.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MS45.tools.console import prepare_stdout  # noqa: E402
from MS45.tools import checksums as ck
from MS45.tools import policy as pl

#: Код отказа, если после пересчёта сумм изменилось что-то вне плана.
CODE_OUTSIDE_PLAN_CHANGE = "OUTSIDE_PLAN_CHANGE"
#: Значение в исходнике вне измеренного стока семейства: чужая версия/тюнинг.
CODE_SOURCE_NOT_STOCK = "SOURCE_NOT_STOCK"
CODE_ACKNOWLEDGEMENT_REQUIRED = "ACKNOWLEDGEMENT_REQUIRED"
CODE_CELL_COUNT = "CELL_COUNT"
CODE_DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
CODE_OVERLAPPING_WRITES = "OVERLAPPING_WRITES"
CODE_NOTHING_TO_DO = "NOTHING_TO_DO"

#: Подтверждения, которые клиент обязан дать для отдельных групп символов.
ACKNOWLEDGEMENTS = {
    "no-catalyst": "подтверждение: катализатор отсутствует (pops разрешены только без него)",
}

#: Максимальное число ячеек в одном патче (защита от «перезапиши всё»).
MAX_CELLS_PER_PATCH = 2048


class PatchError(ValueError):
    """Отказ ядра (не предупреждение)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Finding:
    """Замечание плана: ``reject`` — отказ, ``info`` — к сведению."""

    code: str
    message: str
    severity: str = "reject"
    symbol: str | None = None

    def as_dict(self) -> dict:
        data = {"code": self.code, "message": self.message, "severity": self.severity}
        if self.symbol:
            data["symbol"] = self.symbol
        return data


@dataclass
class PlannedCell:
    """Одна ячейка, которая будет изменена."""

    symbol: str
    index: int
    cal_offset: int
    image_offset: int
    size_bytes: int
    raw_before: int
    raw_after: int
    value_before: float
    value_after: float
    quantization_error_percent: float = 0.0

    @property
    def changed(self) -> bool:
        return self.raw_before != self.raw_after

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "cal_offset": f"0x{self.cal_offset:05X}",
            "image_offset": f"0x{self.image_offset:05X}",
            "size_bytes": self.size_bytes,
            "raw_before": self.raw_before,
            "raw_after": self.raw_after,
            "value_before": round(self.value_before, 6),
            "value_after": round(self.value_after, 6),
            "quantization_error_percent": round(self.quantization_error_percent, 6),
            "changed": self.changed,
        }


@dataclass
class PlannedSymbol:
    """Запрошенное изменение одного символа."""

    symbol: str
    group: str
    entry: pl.PolicyEntryData
    requested: tuple[float | None, ...]
    cells: list[PlannedCell] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "group": self.group,
            "address": f"0x{self.entry.address:05X}",
            "size_bits": self.entry.element_size_bits,
            "shape": f"{self.entry.rows}x{self.entry.columns}",
            "units": self.entry.units,
            "requested": list(self.requested),
            "changed_cells": sum(1 for cell in self.cells if cell.changed),
            "cells": [cell.as_dict() for cell in self.cells],
        }


@dataclass
class PatchPlan:
    """План правки: либо ``PLANNED``, либо ``REJECTED``."""

    source_sha256: str
    image_size: int
    kind: str
    family: str | None
    policy_version: int
    symbols: list[PlannedSymbol] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    checksum_cells: list[int] = field(default_factory=list)
    status: str = "PLANNED"

    @property
    def cells(self) -> list[PlannedCell]:
        return [cell for item in self.symbols for cell in item.cells]

    @property
    def planned_cells(self) -> list[PlannedCell]:
        return [cell for cell in self.cells if cell.changed]

    @property
    def rejections(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "reject"]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "source_sha256": self.source_sha256,
            "image_size": self.image_size,
            "kind": self.kind,
            "family": self.family,
            "policy_version": self.policy_version,
            "changed_cells": len(self.planned_cells),
            "checksum_cells": [f"0x{offset:05X}" for offset in self.checksum_cells],
            "symbols": [item.as_dict() for item in self.symbols],
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass
class PatchResult:
    """Результат применения плана."""

    image: bytes
    sha256: str
    changed_cells: int
    changed_bytes: int
    checksum_words: tuple[tuple[str, int, int, int], ...]
    plan: PatchPlan

    def as_dict(self) -> dict:
        return {
            "status": "APPLIED",
            "sha256": self.sha256,
            "changed_cells": self.changed_cells,
            "changed_bytes": self.changed_bytes,
            "checksum_words": [
                {"name": name, "offset": f"0x{offset:05X}", "before": before, "after": after}
                for name, offset, before, after in self.checksum_words
            ],
            "plan": self.plan.as_dict(),
        }


# ---------------------------------------------------------------------------
# План
# ---------------------------------------------------------------------------


def _parse_requests(requests: list[dict]) -> list[tuple[str, tuple[float | None, ...]]]:
    parsed: list[tuple[str, tuple[float | None, ...]]] = []
    for request in requests:
        symbol = request.get("symbol")
        if not symbol:
            raise PatchError(pl.CODE_SYMBOL_NOT_ALLOWED, "в запросе нет символа")
        values = request.get("values") or []
        parsed.append((symbol, tuple(values)))
    return parsed


def _cells_for(entry: pl.PolicyEntryData, values: tuple[float | None, ...], cal_offset: int) -> list[tuple[int, int]]:
    """Разложить запрос по ячейкам: (индекс, сырое значение)."""
    count = entry.element_count
    if len(values) == 1:
        targets = [(index, values[0]) for index in range(count)]
    elif len(values) == count:
        targets = list(enumerate(values))
    else:
        raise PatchError(
            CODE_CELL_COUNT,
            f"{entry.symbol}: значений {len(values)}, а ячеек {count} "
            "(допустимо одно значение на все ячейки или ровно по числу ячеек)",
        )
    return [(index, value) for index, value in targets if value is not None]  # type: ignore[misc]


def plan_changes(
    image: bytes,
    requests: list[dict],
    *,
    policy: pl.Policy,
    acknowledgements: set[str] | None = None,
) -> PatchPlan:
    """Построить план правки. Исключений не бросает: отказы идут в ``findings``."""
    acknowledgements = acknowledgements or set()
    findings: list[Finding] = []
    family: str | None = None
    kind = "?"
    cal = b""
    cal_offset = 0

    try:
        cal, cal_offset = ck.extract_cal(image)
        kind = ck.image_kind(image)
        family = ck.read_family(cal)
    except ck.MS45Error as error:
        findings.append(Finding(error.code, str(error)))
    else:
        report = ck.verify_bytes(image)
        if not report.ok:
            findings.append(
                Finding(
                    ck.SourceChecksumError.code,
                    "исходный образ не проходит проверку сумм: "
                    f"CRC-32 {'ok' if report.crc32_ok else 'BAD'}, "
                    f"MO3 {'ok' if report.mo3_ok else 'BAD'}",
                )
            )

    plan = PatchPlan(
        source_sha256=hashlib.sha256(image).hexdigest(),
        image_size=len(image),
        kind=kind,
        family=family,
        policy_version=policy.version,
        findings=findings,
    )

    family_policy: pl.FamilyPolicy | None = None
    if not findings:
        # Вход уже отвергнут (размер/суммы) — семейство не разбираем, чтобы не
        # выдавать вторичные отказы.
        try:
            family_policy = policy.for_family(family)
        except pl.PolicyError as error:
            findings.append(Finding(error.code, str(error)))

    seen_symbols: set[str] = set()
    claimed: dict[int, str] = {}

    if family_policy is not None and not findings:
        for symbol, values in _parse_requests(requests):
            if symbol in seen_symbols:
                findings.append(
                    Finding(CODE_DUPLICATE_SYMBOL, f"{symbol}: символ запрошен дважды", symbol=symbol)
                )
                continue
            seen_symbols.add(symbol)
            try:
                entry = family_policy.entry(symbol)
            except pl.PolicyError as error:
                findings.append(Finding(error.code, str(error), symbol=symbol))
                continue

            if entry.element_count > MAX_CELLS_PER_PATCH:
                findings.append(
                    Finding(
                        CODE_TOO_MANY_CELLS,
                        f"{symbol}: {entry.element_count} ячеек больше предела {MAX_CELLS_PER_PATCH}",
                        symbol=symbol,
                    )
                )
                continue

            ack = family_policy.acknowledgement(symbol)
            if ack and ack not in acknowledgements:
                findings.append(
                    Finding(
                        CODE_ACKNOWLEDGEMENT_REQUIRED,
                        f"{symbol}: нужно подтверждение «{ack}» ({ACKNOWLEDGEMENTS.get(ack, '')})",
                        symbol=symbol,
                    )
                )
                continue

            try:
                pairs = _cells_for(entry, values, cal_offset)
            except PatchError as error:
                findings.append(Finding(error.code, str(error), symbol=symbol))
                continue

            planned = PlannedSymbol(symbol=symbol, group=entry.group, entry=entry, requested=values)
            size = entry.element_size_bytes
            for index, value in pairs:
                cell_offset = entry.address + index * size
                absolute = cal_offset + cell_offset
                raw_before = int.from_bytes(cal[cell_offset : cell_offset + size], "big")

                for message in entry.check_value(value):
                    code = message.split(":", 1)[0]
                    findings.append(Finding(code, message, symbol=symbol))

                try:
                    raw_after, error_percent = entry.quantize(value)
                except pl.PolicyError as error:
                    findings.append(Finding(error.code, str(error), symbol=symbol))
                    continue

                if error_percent > policy.max_quantization_error_percent:
                    findings.append(
                        Finding(
                            pl.CODE_QUANTIZATION_TOO_COARSE,
                            f"{symbol}[{index}]: запрошено {value:g}, получится "
                            f"{entry.to_engineering(raw_after):g} — ошибка {error_percent:.3f}% "
                            f"больше предела {policy.max_quantization_error_percent:g}%",
                            symbol=symbol,
                        )
                    )
                    continue

                if entry.max_delta_percent is not None and entry.stock.minimum is not None:
                    stock_low = entry.to_engineering(entry.stock.minimum)
                    stock_high = entry.to_engineering(entry.stock.maximum)
                    reference = max(abs(stock_low), abs(stock_high))
                    achieved = entry.to_engineering(raw_after)
                    delta = max(abs(achieved - stock_low), abs(achieved - stock_high))
                    percent = delta / reference * 100 if reference else 0.0
                    if percent > entry.max_delta_percent:
                        findings.append(
                            Finding(
                                pl.CODE_DELTA_TOO_LARGE,
                                f"{symbol}[{index}]: {achieved:g} отличается от стока "
                                f"({stock_low:g}..{stock_high:g}) на {percent:.2f}% — "
                                f"предел {entry.max_delta_percent:g}%",
                                symbol=symbol,
                            )
                        )
                        continue

                if raw_after != raw_before and entry.stock.minimum is not None:
                    stock_low = entry.stock.minimum
                    stock_high = entry.stock.maximum
                    if not (stock_low <= raw_before <= stock_high):
                        findings.append(
                            Finding(
                                CODE_SOURCE_NOT_STOCK,
                                f"{symbol}[{index}]: в исходнике "
                                f"{entry.to_engineering(raw_before):g} (сырое {raw_before}), "
                                f"а измеренный сток семейства "
                                f"{entry.to_engineering(stock_low):g}.."
                                f"{entry.to_engineering(stock_high):g} — исходник не похож "
                                "на сток этой версии; берём только стоковую калибровку",
                                symbol=symbol,
                            )
                        )
                        continue

                for offset in range(absolute, absolute + size):
                    if offset in claimed:
                        findings.append(
                            Finding(
                                CODE_OVERLAPPING_WRITES,
                                f"{symbol}[{index}]: байт 0x{offset:05X} уже занят символом "
                                f"{claimed[offset]}",
                                symbol=symbol,
                            )
                        )
                    else:
                        claimed[offset] = symbol

                planned.cells.append(
                    PlannedCell(
                        symbol=symbol,
                        index=index,
                        cal_offset=cell_offset,
                        image_offset=absolute,
                        size_bytes=size,
                        raw_before=raw_before,
                        raw_after=raw_after,
                        value_before=entry.to_engineering(raw_before),
                        value_after=entry.to_engineering(raw_after),
                        quantization_error_percent=error_percent,
                    )
                )
            plan.symbols.append(planned)

    try:
        plan.checksum_cells = ck.checksum_cells(image)
    except ck.MS45Error:
        plan.checksum_cells = []

    if not findings and not plan.planned_cells:
        findings.append(Finding(CODE_NOTHING_TO_DO, "план не меняет ни одной ячейки: значения уже такие"))

    plan.status = "REJECTED" if findings else "PLANNED"
    return plan


# ---------------------------------------------------------------------------
# Применение
# ---------------------------------------------------------------------------


def apply_plan(image: bytes, plan: PatchPlan) -> PatchResult:
    """Применить план: ячейки -> суммы -> пост-проверка «ничего лишнего»."""
    if plan.status != "PLANNED":
        raise PatchError(
            plan.rejections[0].code if plan.rejections else "PLAN_REJECTED",
            "план не принят: " + "; ".join(f.message for f in plan.rejections[:5]),
        )
    if hashlib.sha256(image).hexdigest() != plan.source_sha256:
        raise PatchError(
            ck.SourceChecksumError.code, "образ не тот, для которого построен план (sha256 не совпал)"
        )

    out = bytearray(image)
    allowed: set[int] = set(plan.checksum_cells)
    changed_cells = 0
    for cell in plan.planned_cells:
        out[cell.image_offset : cell.image_offset + cell.size_bytes] = cell.raw_after.to_bytes(
            cell.size_bytes, "big"
        )
        allowed.update(range(cell.image_offset, cell.image_offset + cell.size_bytes))
        changed_cells += 1

    # Суммы: строгий путь, исходник — проверенный образ.
    patched, _ = ck.recompute_after_patch(image, bytes(out))

    changed = [index for index, (a, b) in enumerate(zip(image, patched)) if a != b]
    outside = [index for index in changed if index not in allowed]
    if outside:
        raise PatchError(
            CODE_OUTSIDE_PLAN_CHANGE,
            "после пересчёта сумм изменились байты вне плана: "
            + ", ".join(f"0x{index:05X}" for index in outside[:8]),
        )

    crc_offset = None
    if plan.kind == "cal":
        crc_offset = ck.CRC32_STORE_OFFSET
    else:
        crc_offset = ck.EXT_CAL_OFFSET + ck.CRC32_STORE_OFFSET

    checksum_words: list[tuple[str, int, int, int]] = []
    for name, offset in (
        ("CRC-32", crc_offset),
        ("MO3-high", None),
        ("MO3-low", None),
    ):
        if offset is None:
            continue
        before = int.from_bytes(image[offset : offset + 4], "big")
        after = int.from_bytes(patched[offset : offset + 4], "big")
        if before != after:
            checksum_words.append((name, offset, before, after))

    # Пара MO3 ищется по факту: её место зависит от семейства.
    if plan.family:
        store = ck.MO3_STORE_OFFSET.get(plan.family)
        if store is not None:
            base = 0 if plan.kind == "cal" else ck.EXT_CAL_OFFSET
            for index, name in enumerate(("MO3-high", "MO3-low")):
                offset = base + store + index * 4
                before = int.from_bytes(image[offset : offset + 4], "big")
                after = int.from_bytes(patched[offset : offset + 4], "big")
                if before != after:
                    checksum_words.append((name, offset, before, after))

    return PatchResult(
        image=patched,
        sha256=hashlib.sha256(patched).hexdigest(),
        changed_cells=changed_cells,
        changed_bytes=len(changed),
        checksum_words=tuple(checksum_words),
        plan=plan,
    )


# ---------------------------------------------------------------------------
# Отчёты и CLI
# ---------------------------------------------------------------------------


def report_plan(plan: PatchPlan) -> str:
    lines = [
        f"план: {plan.status} | образ {plan.image_size} байт ({plan.kind}) | "
        f"семейство {plan.family or 'не прочитано'} | политика v{plan.policy_version} | "
        f"исходник {plan.source_sha256[:16]}…",
    ]
    for item in plan.symbols:
        lines.append(
            f"  {item.symbol} [{item.group}] ячеек {len(item.cells)} "
            f"({sum(1 for c in item.cells if c.changed)} меняется), "
            f"{item.entry.element_size_bits} бит {item.entry.rows}x{item.entry.columns}, "
            f"{item.entry.units or '-'}"
        )
        for cell in item.cells:
            # Стрелка «->», а не «→»: символ U+2192 отсутствует в cp1251,
            # и на русской Windows консольный вывод падал (проверяет
            # MS45/tests/test_cli_console.py).
            mark = "->" if cell.changed else "="
            lines.append(
                f"      [{cell.index}] 0x{cell.cal_offset:05X}: {cell.value_before:g} {mark} "
                f"{cell.value_after:g} (сырое {cell.raw_before} -> {cell.raw_after})"
            )
    for finding in plan.findings:
        lines.append(f"  [{finding.severity.upper()}] {finding.code}: {finding.message}")
    return "\n".join(lines)


def parse_set(item: str) -> dict:
    if "=" not in item:
        raise PatchError(pl.CODE_SYMBOL_NOT_ALLOWED, f"--set должен быть вида СИМВОЛ=ЗНАЧЕНИЕ, а не {item!r}")
    symbol, raw = item.split("=", 1)
    values: list[float | None] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk in ("", "None", "none", "-"):
            values.append(None)
        else:
            try:
                values.append(float(chunk))
            except ValueError as error:
                raise PatchError(
                    pl.CODE_VALUE_NOT_REPRESENTABLE, f"{symbol}: {chunk!r} не число"
                ) from error
    return {"symbol": symbol.strip(), "values": values}


def run(args: argparse.Namespace) -> int:
    image_path = Path(args.image)
    if not image_path.exists():
        print(f"нет файла {image_path}", file=sys.stderr)
        return 2
    image = image_path.read_bytes()
    policy = pl.load_policy(Path(args.policy)) if args.policy else pl.load_policy()

    try:
        requests = [parse_set(item) for item in args.set]
    except PatchError as error:
        print(f"ошибка запроса: {error}", file=sys.stderr)
        return 2

    plan = plan_changes(image, requests, policy=policy, acknowledgements=set(args.acknowledge))
    print(report_plan(plan))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(plan.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if plan.status != "PLANNED":
        return 2
    if args.dry_run:
        print("(dry-run: файл не записан)")
        return 0
    if not args.out:
        print("нужен --out, чтобы записать результат", file=sys.stderr)
        return 2
    try:
        result = apply_plan(image, plan)
    except (PatchError, ck.MS45Error) as error:
        print(f"отказ: {error}", file=sys.stderr)
        return 2
    Path(args.out).write_bytes(result.image)
    print(
        f"записано: {args.out} (sha256 {result.sha256[:16]}…, ячеек {result.changed_cells}, "
        f"байт {result.changed_bytes})"
    )
    for name, offset, before, after in result.checksum_words:
        print(f"  сумма {name} @0x{offset:05X}: 0x{before:08X} -> 0x{after:08X}")
    return 0


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()

    parser = argparse.ArgumentParser(
        description="Ядро патчинга MS45: план правки калибровки и запись файла"
    )
    parser.add_argument("--image", required=True, help="калибровка 116 КиБ или полный образ 1 МиБ")
    parser.add_argument("--set", action="append", default=[], help="СИМВОЛ=ЗНАЧЕНИЕ[,ЗНАЧЕНИЕ…]")
    parser.add_argument("--acknowledge", action="append", default=[],
                        help="подтверждение, например no-catalyst")
    parser.add_argument("--out", help="куда записать результат")
    parser.add_argument("--json-out", help="план/результат в JSON")
    parser.add_argument("--dry-run", action="store_true", help="только план")
    parser.add_argument("--policy", help="свой файл политики")
    args = parser.parse_args(argv)
    if not args.set:
        print("нужен хотя бы один --set", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
