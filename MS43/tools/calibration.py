#!/usr/bin/env python3
"""Ядро патчинга MS43: калибровка пишется только через allowlist и только с CRC.

Правила (совпадают с MS42-ядром, `MS42/tools/calibration.py`):

* **default deny** — символ пишется только если он есть в `stage_policy.json`;
* только калибровочные адреса (0x70000..) — код, загрузчик и идентификация не
  трогаются; символы фич из патчлиста требуют применённого патча
  (`requires_patch`), иначе запись отклоняется;
* исходный образ обязан проходить **все** свои CRC-записи до планирования;
* значение обязано влезать в объявленные границы и в сырое поле с точностью не
  хуже `max_quantization_error_percent`;
* для таблиц — лимит числа правок за пакет (`max_cells_per_patch`) и проверка
  монотонности там, где она объявлена (`require_monotonic`);
* после записи CRC пересчитываются нашим ядром (`MS43/tools/checksum.py`), а
  затем пост-проверка доказывает, что изменились **только** запланированные
  байты и позиции сумм.

Порядок применения важен: сначала патчи (`MS43/tools/patches.py`), затем
калибровка. Так дефолты из `[DATA] …` патчей не затирают то, что просил клиент.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from MS43.tools import checksum as ck
    from MS43.tools import patches as pt
    from MS43.tools.console import prepare_stdout
    from MS43.tools.policy import (
        DEFAULT_POLICY,
        KNOWN_SOFTWARE,
        Policy,
        PolicyEntryData,
        covered_software,
        load_policy,
        resolve_policy,
    )
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    import checksum as ck  # type: ignore[no-redef]
    import patches as pt  # type: ignore[no-redef]
    from console import prepare_stdout  # type: ignore[no-redef]
    from policy import (  # type: ignore[no-redef]
        DEFAULT_POLICY,
        KNOWN_SOFTWARE,
        Policy,
        PolicyEntryData,
        covered_software,
        load_policy,
        resolve_policy,
    )

IMAGE_SIZE = ck.IMAGE_SIZE_FULL
FIRMWARE_DIR = Path(__file__).resolve().parents[1] / "firmware"

#: Коды находок плана.
FINDING_SEVERITIES = ("reject", "warning", "info")


class PatchError(ValueError):
    """Запрос не выполняется: данные или политика не сходятся."""


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class PlannedCell:
    index: int
    address: int
    before_raw: int
    after_raw: int
    before: float
    after: float
    quant_error_percent: float


@dataclass(frozen=True)
class PlannedSymbol:
    symbol: str
    group: str
    entry: PolicyEntryData
    requires_patch: str | None
    source: str
    note: str
    requested: tuple[float | None, ...]
    cells: tuple[PlannedCell, ...]


@dataclass
class PatchPlan:
    status: str
    source_sha256: str
    image_size: int
    policy_version: int
    software: tuple[str, ...]
    patches: tuple[str, ...]
    declarations: tuple[str, ...]
    acknowledgements: tuple[str, ...]
    #: Версия калибровки, прочитанная из образа (``4300xx``); None -- не прочитана.
    cal_version: str | None = None
    symbols: tuple[PlannedSymbol, ...] = ()
    findings: list[Finding] = field(default_factory=list)

    @property
    def changed_cells(self) -> int:
        return sum(len(item.cells) for item in self.symbols)

    def problems(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "reject"]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "source_sha256": self.source_sha256,
            "image_size": self.image_size,
            "policy_version": self.policy_version,
            "cal_version": self.cal_version,
            "software": list(self.software),
            "patches": list(self.patches),
            "declarations": list(self.declarations),
            "acknowledgements": list(self.acknowledgements),
            "changed_cells": self.changed_cells,
            "symbols": [
                {
                    "symbol": item.symbol,
                    "group": item.group,
                    "requires_patch": item.requires_patch,
                    "source": item.source,
                    "note": item.note,
                    "cells": [
                        {
                            "index": cell.index,
                            "address": f"0x{cell.address:05X}",
                            # Размер ячейки нужен проверке выпущенного файла по
                            # манифесту (MS43/tools/stages.py --verify-manifest):
                            # по адресу и размеру сверяются байты после записи.
                            "size_bytes": max(1, item.entry.element_size_bits // 8),
                            "before": cell.before,
                            "after": cell.after,
                            "before_raw": cell.before_raw,
                            "after_raw": cell.after_raw,
                            "quant_error_percent": round(cell.quant_error_percent, 6),
                        }
                        for cell in item.cells
                    ],
                }
                for item in self.symbols
            ],
            "findings": [
                {"code": item.code, "severity": item.severity, "message": item.message}
                for item in self.findings
            ],
        }


@dataclass
class PatchResult:
    image: bytes
    sha256: str
    symbols: tuple[str, ...]
    patches: tuple[str, ...]
    changed_cells: int
    changed_bytes: int
    crc_words: tuple[tuple[str, int, int, int], ...]
    plan: PatchPlan

    def as_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "symbols": list(self.symbols),
            "patches": list(self.patches),
            "changed_cells": self.changed_cells,
            "changed_bytes": self.changed_bytes,
            "crc_words": [
                {"record": name, "offset": f"0x{offset:05X}", "before": before, "after": after}
                for name, offset, before, after in self.crc_words
            ],
            "plan": self.plan.as_dict(),
        }


# ---------------------------------------------------------------------------
# Проверка образа и вспомогательные функции
# ---------------------------------------------------------------------------


def crc_storage_offsets(image_size: int) -> dict[int, str]:
    """Файловые смещения хранимых CRC (по два байта на запись)."""
    _, specs = ck.identify_layout(image_size)
    offsets: dict[int, str] = {}
    for spec in specs:
        offsets[spec.offset] = spec.name
        offsets[spec.offset + 1] = spec.name
    return offsets


def verify_image_bytes(image: bytes) -> tuple[bool, list[str]]:
    """Проверить CRC образа в памяти, не трогая диск."""
    kind, specs = ck.identify_layout(len(image))
    problems: list[str] = []
    verified = 0
    for spec in specs:
        try:
            stored, _segments = ck.parse_record(image, spec)
            init = ck.read_init(image, spec)
            computed = ck.compute_record(image, spec, init)
        except ck.MS43Error as error:
            problems.append(f"{spec.name}: {error}")
            continue
        verified += 1
        if stored != computed:
            problems.append(f"{spec.name}: 0x{stored:04X} != 0x{computed:04X}")
    if verified == 0:
        problems.append(f"образ {len(image)} байт: ни одной проверяемой записи")
    return (not problems, problems)


def read_cal_version(image: bytes) -> str | None:
    """Версия калибровки из образа (единая точка чтения для ядра и генератора)."""
    return ck.read_cal_version(image)


def check_version_supported(
    image: bytes, supported: tuple[str, ...] | None = None
) -> Finding | None:
    """Защита по версии калибровки: None — версия покрыта политикой.

    Адреса, границы и стоки каждой политики сняты только со стоков **её** версии;
    на чужих версиях по тем же адресам лежат другие данные (например, в 430069
    ``ip_iga_puc__n__tco`` начинается с ``2D 28 23…``, а в 430056 — с ``8E 8B 87…``;
    таблица ПХХ в 430037 стоит по 0x77180, а не по 0x7717D 430069).  Поэтому
    проверка обязана стоять до планирования и до любых записей.

    ``supported`` — версии, покрытые политикой; по умолчанию — все версии, для
    которых есть файл политики (``policy.covered_software()``).
    """
    version = ck.read_cal_version(image)
    if supported is None:
        supported = covered_software()
    supported_text = ", ".join(supported) or "нет"
    known = ", ".join(KNOWN_SOFTWARE)
    if len(image) != IMAGE_SIZE:
        if len(image) == ck.IMAGE_SIZE_CAL64:
            return Finding(
                "IMAGE_SIZE",
                "reject",
                f"это чтение калибровки 64 КиБ (версия {version or 'не прочитана'}); "
                "патчинг возможен только по полному образу 512 КиБ — в нём есть "
                "boot/prog и все записи CRC",
            )
        return Finding("IMAGE_SIZE", "reject", f"нужен полный образ 512 КиБ, а не {len(image)} байт")
    if version is None:
        return Finding(
            "CAL_VERSION_UNSUPPORTED",
            "reject",
            "не удалось прочитать код версии калибровки "
            f"@0x{ck.CAL_VERSION_OFFSET_FULL:05X}; политики собраны для "
            f"{supported_text} — отказ до любых записей",
        )
    if version not in supported:
        return Finding(
            "CAL_VERSION_UNSUPPORTED",
            "reject",
            f"версия калибровки {version} не поддерживается: политики собраны и "
            f"проверены только на {supported_text}; известные версии MS43: {known}. "
            "На других версиях по тем же адресам лежат другие данные, поэтому "
            "адреса одной версии на другую не переносятся: нужно отдельное покрытие "
            "на версию (см. MS43/docs/VERSION_COVERAGE_RU.md)",
        )
    return None


def check_cal_version(image: bytes, policy: Policy) -> Finding | None:
    """Защита по версии калибровки для конкретной политики (совместимость API)."""
    return check_version_supported(image, tuple(policy.software))


def _quantization_error_percent(requested: float, achieved: float) -> float:
    if requested == 0:
        return 0.0 if achieved == 0 else float("inf")
    return abs(achieved - requested) / abs(requested) * 100.0


def _scaler(entry: PolicyEntryData) -> tuple[float, float]:
    return entry.scale_a, entry.scale_b


def _is_monotonic(values: list[float]) -> str | None:
    """Вернуть 'non-decreasing' / 'non-increasing' / None (не монотонна)."""
    increasing = all(b <= a for a, b in zip(values, values[1:]))
    decreasing = all(b >= a for a, b in zip(values, values[1:]))
    if increasing:
        return "non-decreasing"  # значения хранятся от «горячего» конца
    if decreasing:
        return "non-increasing"
    return None


# ---------------------------------------------------------------------------
# Планирование
# ---------------------------------------------------------------------------


def plan_changes(
    image: bytes,
    requests: list[dict],
    *,
    policy: Policy | None = None,
    patch_catalog: pt.PatchCatalog | None = None,
    patches: tuple[str, ...] = (),
    declarations: set[str] | tuple[str, ...] = (),
    acknowledgements: set[str] | tuple[str, ...] = (),
) -> PatchPlan:
    """Построить план правок. План с находками уровня reject не применяется.

    Политика выбирается по версии калибровки из образа (``policy.resolve_policy``):
    каждая версия пишется только своей политикой.  Явно переданная ``policy``
    используется как есть (тесты, свои политики) — тогда версия сверяется с её
    ``software``.
    """
    if policy is None:
        policy = resolve_policy(image)
    patch_catalog = patch_catalog or pt.load_catalog()
    declarations = tuple(sorted(declarations))
    acknowledgements = tuple(sorted(acknowledgements))
    if policy is None:
        # Версия не покрыта ни одной политикой: план-отказ (даже CRC не считаем —
        # адреса чужой политики к этому образу неприменимы).
        plan = PatchPlan(
            status="REJECTED",
            source_sha256=hashlib.sha256(image).hexdigest(),
            image_size=len(image),
            policy_version=0,
            software=covered_software(),
            cal_version=ck.read_cal_version(image),
            patches=tuple(patches),
            declarations=declarations,
            acknowledgements=acknowledgements,
        )
        guard = check_version_supported(image)
        plan.findings.append(
            guard
            or Finding(
                "CAL_VERSION_UNSUPPORTED",
                "reject",
                "для этой версии калибровки нет политики — отказ до любых записей",
            )
        )
        return plan
    plan = PatchPlan(
        status="PLANNED",
        source_sha256=hashlib.sha256(image).hexdigest(),
        image_size=len(image),
        policy_version=policy.version,
        software=policy.software,
        cal_version=ck.read_cal_version(image),
        patches=tuple(patches),
        declarations=declarations,
        acknowledgements=acknowledgements,
    )

    def reject(code: str, message: str) -> None:
        plan.findings.append(Finding(code, "reject", message))

    def warn(code: str, message: str) -> None:
        plan.findings.append(Finding(code, "warning", message))

    # Защита по версии калибровки — до CRC и до планирования правок, чтобы
    # обойти её было нельзя (см. REVIEW_CLAUDE_RU.md, задание 1).
    guard = check_cal_version(image, policy)
    if guard is not None:
        reject(guard.code, guard.message)
    else:
        ok, problems = verify_image_bytes(image)
        if not ok:
            reject("SOURCE_CRC_INVALID", "исходный образ не проходит CRC: " + "; ".join(problems))

    # патчи: патчлист собран под 430069 (basedata снята с 18 стоков этой версии),
    # на других версиях его маски не доказаны — отказываем ещё на плане, чтобы
    # «PLANNED» на чужой версии не выглядел разрешением (см.
    # MS43/docs/VERSION_COVERAGE_RU.md §5).
    patched_versions = tuple(pt.supported_software())
    version_for_patches = ck.read_cal_version(image)
    offered: list[pt.Patch] = []
    if patches and version_for_patches not in patched_versions:
        reject(
            "PATCHLIST_FEATURE_UNSUPPORTED",
            f"патчи патчлиста доступны только на {', '.join(patched_versions)}; "
            f"версия {version_for_patches or 'не прочитана'} их не принимает — "
            "байтовые маски на ней не доказаны",
        )
    for title in patches:
        try:
            patch = patch_catalog.patch(title)
        except pt.PatchError as error:
            reject("PATCH_UNKNOWN", str(error))
            continue
        rules = pt.gate(patch)
        if not rules.get("offered", True):
            reject("PATCH_NOT_OFFERED", f"{title}: не предлагается — {rules['reason']}")
            continue
        required = rules.get("declaration")
        if required and required not in declarations:
            reject(
                "PATCH_DECLARATION_MISSING",
                f"{title}: требуется подтверждение {required!r} — {rules['reason']}",
            )
            continue
        offered.append(patch)
    for problem in pt.conflicts(offered):
        reject("PATCH_CONFLICT", problem)

    used: dict[int, str] = {}
    seen: set[str] = set()
    planned: list[PlannedSymbol] = []
    for request in requests:
        symbol = request.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            reject("REQUEST_INVALID", f"запрос без символа: {request!r}")
            continue
        if symbol in seen:
            reject("DUPLICATE_SYMBOL", f"{symbol}: символ запрошен дважды в одном пакете")
            continue
        seen.add(symbol)
        entry = policy.objects.get(symbol)
        if entry is None:
            reject(
                "SYMBOL_NOT_ALLOWED",
                f"{symbol}: символа нет в политике — по умолчанию не меняем ничего",
            )
            continue
        if entry.read_only:
            # A read-only symbol is never made generally writable.  The sole
            # exception is a policy-declared linked context (currently the
            # warm MBT map raised together with the RON98 map), and both the
            # context and the parent symbol must be explicit in the request.
            context_ok = (
                entry.write_context is not None
                and request.get("write_context") == entry.write_context
            )
            link_ok = (
                entry.write_context != "ignition_optm_link"
                or request.get("linked_to") == "ip_iga_ron_98_pl_ivvt__n__maf"
            )
            if not (context_ok and link_ok):
                reject("READ_ONLY", f"{symbol}: помечен «только чтение» ({entry.note[:80]})")
                continue
        elif entry.write_context is not None and request.get("write_context") != entry.write_context:
            reject(
                "WRITE_CONTEXT_REQUIRED",
                f"{symbol}: запись разрешена только в контексте {entry.write_context!r}",
            )
            continue
        if entry.requires_patch and entry.requires_patch not in (patch.title for patch in offered):
            reject(
                "PATCH_REQUIRED",
                f"{symbol}: действует только после патча {entry.requires_patch!r}",
            )
            continue

        total = entry.rows * entry.columns
        values = request.get("values", request.get("value"))
        if values is None:
            reject("REQUEST_INVALID", f"{symbol}: нет значений")
            continue
        if isinstance(values, (int, float)):
            values = [float(values)]
        else:
            values = list(values)
        if len(values) == 1 and total > 1:
            reject(
                "CELL_COUNT",
                f"{symbol}: одна ячейка передана для таблицы {entry.rows}x{entry.columns} — "
                "нужен полный список значений (None = оставить как есть)",
            )
            continue
        if len(values) != total:
            reject(
                "CELL_COUNT",
                f"{symbol}: передано {len(values)} значений, в таблице {total}",
            )
            continue

        scaler_a, scaler_b = _scaler(entry)
        if scaler_a == 0:
            reject("SCALE_INVALID", f"{symbol}: нулевой масштаб в политике")
            continue
        raw_max = (1 << entry.element_size_bits) - 1
        tolerance = policy.max_quantization_error_percent
        cells: list[PlannedCell] = []
        achieved_values: list[float] = []
        failures = 0
        for index, value in enumerate(values):
            size = max(1, entry.element_size_bits // 8)
            address = entry.address + index * size
            current = int.from_bytes(image[address : address + size], "little")
            current_value = scaler_a * current + scaler_b
            if value is None:
                achieved_values.append(current_value)
                continue
            requested = float(value)
            raw_float = (requested - scaler_b) / scaler_a
            raw = int(round(raw_float))
            if not 0 <= raw <= raw_max:
                reject(
                    "VALUE_NOT_REPRESENTABLE",
                    f"{symbol}[{index}]={requested:g} требует сырое {raw_float:.2f}, "
                    f"допустимо 0..{raw_max} для {entry.element_size_bits} бит",
                )
                failures += 1
                continue
            achieved = scaler_a * raw + scaler_b
            error = _quantization_error_percent(requested, achieved)
            if error > tolerance:
                reject(
                    "QUANTIZATION_TOO_COARSE",
                    f"{symbol}[{index}]={requested:g} превращается в {achieved:.6g} "
                    f"({error:.2f}% против порога {tolerance:.2f}%)",
                )
                failures += 1
                continue
            if achieved < entry.minimum or achieved > entry.maximum:
                reject(
                    "POLICY_RANGE_VIOLATION",
                    f"{symbol}[{index}]={achieved:.6g} вне границ политики "
                    f"{entry.minimum:g}..{entry.maximum:g}",
                )
                failures += 1
                continue
            # Относительный предел считаем от стока ячейки; при нулевом стоке
            # (сыром или инженерном — например, переход знака в картах угла)
            # проценты не определены, и проверка пропускается: абсолютный сдвиг
            # в этом случае ограничен границами политики и воротами генератора.
            if entry.max_delta_percent is not None and current != 0 and current_value != 0:
                delta = abs(achieved - current_value) / abs(current_value) * 100.0
                if delta > entry.max_delta_percent:
                    reject(
                        "DELTA_TOO_LARGE",
                        f"{symbol}[{index}]: {current_value:.6g} -> {achieved:.6g} "
                        f"это {delta:.1f}% при лимите {entry.max_delta_percent:g}%",
                    )
                    failures += 1
                    continue
            achieved_values.append(achieved)
            if raw != current:
                cells.append(
                    PlannedCell(
                        index=index,
                        address=address,
                        before_raw=current,
                        after_raw=raw,
                        before=current_value,
                        after=achieved,
                        quant_error_percent=error,
                    )
                )
        if failures:
            continue
        if entry.max_cells_per_patch is not None and len(cells) > entry.max_cells_per_patch:
            reject(
                "TOO_MANY_CELLS",
                f"{symbol}: за пакет меняется {len(cells)} ячеек, лимит "
                f"{entry.max_cells_per_patch}",
            )
            continue
        clash = next((used[cell.address] for cell in cells if cell.address in used), None)
        if clash:
            reject("OVERLAPPING_WRITES", f"{symbol}: пересекается с правкой {clash}")
            continue
        if entry.require_monotonic:
            orientation = _is_monotonic(achieved_values)
            if orientation is None:
                reject(
                    "NOT_MONOTONIC",
                    f"{symbol}: после правки таблица не монотонна по инженерным значениям",
                )
                continue
        for cell in cells:
            used[cell.address] = symbol
        if not cells:
            warn("NO_CHANGE", f"{symbol}: значения уже такие, как запрошено")
        planned.append(
            PlannedSymbol(
                symbol=symbol,
                group=entry.group,
                entry=entry,
                requires_patch=entry.requires_patch,
                source=entry.source,
                note=entry.note,
                requested=tuple(values),
                cells=tuple(cells),
            )
        )

    if plan.problems():
        plan.status = "REJECTED"
        return plan
    plan.symbols = tuple(planned)
    if not plan.symbols and not plan.patches:
        plan.status = "REJECTED"
        plan.findings.append(
            Finding("NOTHING_TO_DO", "reject", "план пуст: ни правок, ни патчей")
        )
    return plan


# ---------------------------------------------------------------------------
# Применение
# ---------------------------------------------------------------------------


def apply_plan(
    image: bytes,
    plan: PatchPlan,
    *,
    patch_catalog: pt.PatchCatalog | None = None,
) -> PatchResult:
    """Применить план: патчи -> калибровка -> CRC -> пост-проверка."""
    if plan.status != "PLANNED":
        raise PatchError(
            "план не принят: " + "; ".join(item.message for item in plan.problems())
        )
    if hashlib.sha256(image).hexdigest() != plan.source_sha256:
        raise PatchError("образ не тот, для которого построен план (sha256 не совпал)")
    patch_catalog = patch_catalog or pt.load_catalog()

    # 1. Патчи (они сами пересчитывают CRC и проверяют, что не тронули лишнего).
    allowed: set[int] = set(crc_storage_offsets(len(image)))
    if plan.patches:
        patch_result = pt.apply_patches(
            image,
            patch_catalog,
            list(plan.patches),
            declarations=set(plan.declarations),
        )
        image = patch_result.image
        for patch in patch_catalog.patches:
            if patch.title in plan.patches:
                for entry in patch.entries:
                    allowed.update(range(entry.address, entry.end))

    # 2. Калибровка.
    out = bytearray(image)
    changed_cells = 0
    for planned in plan.symbols:
        for cell in planned.cells:
            size = max(1, planned.entry.element_size_bits // 8)
            out[cell.address : cell.address + size] = cell.after_raw.to_bytes(size, "little")
            allowed.update(range(cell.address, cell.address + size))
            changed_cells += 1

    # 3. CRC нашим ядром.
    patched = _repair_bytes(bytes(out))

    # 4. Пост-проверка: ничего лишнего.
    changed = [index for index, (a, b) in enumerate(zip(image, patched)) if a != b]
    outside = [index for index in changed if index not in allowed]
    if outside:
        raise PatchError(
            "после пересчёта сумм изменились байты вне плана: "
            + ", ".join(f"0x{index:05X}" for index in outside[:8])
        )

    # 5. Какие именно CRC-слова изменились (для наряда).
    crc_words: list[tuple[str, int, int, int]] = []
    for offset, name in sorted(crc_storage_offsets(len(image)).items()):
        if offset % 2 == 0 and offset + 1 < len(image):
            before = int.from_bytes(image[offset : offset + 2], "little")
            after = int.from_bytes(patched[offset : offset + 2], "little")
            if before != after:
                crc_words.append((name, offset, before, after))
    return PatchResult(
        image=patched,
        sha256=hashlib.sha256(patched).hexdigest(),
        symbols=tuple(item.symbol for item in plan.symbols if item.cells),
        patches=plan.patches,
        changed_cells=changed_cells,
        changed_bytes=len(changed),
        crc_words=tuple(crc_words),
        plan=plan,
    )


def _repair_bytes(image: bytes) -> bytes:
    """Пересчитать CRC в памяти (ядро работает через файлы, поэтому через temp)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ms43-cal-") as directory:
        source = Path(directory) / "in.bin"
        target = Path(directory) / "out.bin"
        source.write_bytes(image)
        report = ck.repair_image(source, target)
        if not report.ok:
            raise PatchError("CRC не сошлись после правки: " + "; ".join(report.failures))
        return target.read_bytes()


def run(args: argparse.Namespace) -> int:
    image_path = Path(args.image)
    if not image_path.exists():
        print(f"нет файла {image_path}", file=sys.stderr)
        return 2
    image = image_path.read_bytes()
    # Политика по умолчанию — по версии калибровки из образа; --policy — свой файл.
    policy = load_policy(Path(args.policy)) if args.policy else resolve_policy(image)
    patch_catalog = pt.load_catalog(Path(args.catalog)) if args.catalog else pt.load_catalog()

    requests = [_parse_set(item) for item in args.set]
    try:
        plan = plan_changes(
            image,
            requests,
            policy=policy,
            patch_catalog=patch_catalog,
            patches=tuple(args.patch),
            declarations=set(args.declaration),
            acknowledgements=set(args.acknowledge),
        )
    except PatchError as error:
        print(f"ошибка плана: {error}", file=sys.stderr)
        return 2
    print(_report_plan(plan))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(plan.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
        result = apply_plan(image, plan, patch_catalog=patch_catalog)
    except PatchError as error:
        print(f"отказ: {error}", file=sys.stderr)
        return 2
    Path(args.out).write_bytes(result.image)
    print(
        f"записано: {args.out} (sha256 {result.sha256[:16]}…, "
        f"ячеек {result.changed_cells}, байт {result.changed_bytes})"
    )
    for name, offset, before, after in result.crc_words:
        print(f"  CRC {name} @0x{offset:05X}: 0x{before:04X} -> 0x{after:04X}")
    return 0


def _parse_set(item: str) -> dict:
    if "=" not in item:
        raise PatchError(f"--set должен быть вида СИМВОЛ=ЗНАЧЕНИЕ, а не {item!r}")
    symbol, raw = item.split("=", 1)
    values = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk in ("", "None", "none", "-"):
            values.append(None)
        else:
            values.append(float(chunk))
    return {"symbol": symbol.strip(), "values": values}


def _report_plan(plan: PatchPlan) -> str:
    lines = [
        f"план: {plan.status} | образ {plan.image_size} байт | калибровка "
        f"{plan.cal_version or 'не прочитана'} | sha256 "
        f"{plan.source_sha256[:16]}… | политика v{plan.policy_version}",
    ]
    if plan.patches:
        lines.append(f"  патчи: {', '.join(plan.patches)}")
    for item in plan.symbols:
        lines.append(
            f"  {item.symbol} [{item.group}] ячеек {len(item.cells)}"
            + (f" (+патч {item.requires_patch})" if item.requires_patch else "")
        )
        for cell in item.cells[:6]:
            lines.append(
                f"     [{cell.index}] 0x{cell.address:05X}: {cell.before:g} -> {cell.after:g}"
                f" (сырое {cell.before_raw} -> {cell.after_raw}, ошибка "
                f"{cell.quant_error_percent:.3f}%)"
            )
        if len(item.cells) > 6:
            lines.append(f"     … ещё {len(item.cells) - 6} ячеек")
    for finding in plan.findings:
        lines.append(f"  {finding.severity.upper():7s} {finding.code}: {finding.message}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()  # Windows: не падать на символах вне cp1251
    parser = argparse.ArgumentParser(
        description="Патчинг калибровки MS43 через allowlist (по умолчанию ничего не меняет)."
    )
    parser.add_argument("--image", required=True, help="полный стоковый образ 512 КиБ")
    parser.add_argument("--set", action="append", default=[], help="СИМВОЛ=ЗНАЧЕНИЕ (можно списком)")
    parser.add_argument("--patch", action="append", default=[], help="патч из патчлиста")
    parser.add_argument("--declaration", action="append", default=[], help="подтверждение патча")
    parser.add_argument("--acknowledge", action="append", default=[], help="acknowledge_risk / …")
    parser.add_argument("--out", help="куда записать результат")
    parser.add_argument("--json-out", help="куда записать план в JSON")
    parser.add_argument("--dry-run", action="store_true", help="только показать план")
    parser.add_argument("--policy", help="свой файл политики")
    parser.add_argument("--catalog", help="свой каталог патчей")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
