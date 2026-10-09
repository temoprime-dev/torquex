#!/usr/bin/env python3
"""Turn a customer request into a patch plan for the calibration core.

This is the missing link between a work order (which names objects but carries
no values) and ``calibration.py`` (which refuses to invent values). It expands a
request file into concrete symbol/value changes, refuses everything that is not
backed by evidence, and dry-runs the result through the real planner so the
output cannot be rejected later.

Honesty rules implemented here:

* A value only enters the plan when the customer stated it (an intent) or when
  it comes from a file we hold. Nothing is guessed from a forum post.
* Raising the engine speed limit, hardening the overrun cutoff and removing the
  speed limiter are intent-only changes: they are marked
  ``requires_validation`` with a hint of what to check after flashing.
* Anything whose target value would have to be derived from data we do not have
  (ignition advance, fuelling, MAF rescaling, popcorn) is reported as NOT
  SUPPORTED or REQUIRES DATA instead of being approximated.
* A hardware profile that itself fails its own checks blocks generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from MS41.tools.calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from MS41.tools.hardware_profile import evaluate, parse_profile
    from MS41.tools.injector_scaling import (
        STATUS_READY as SCALING_READY,
        ScalingError,
        build_report as build_scaling_report,
    )
    from MS41.tools.log_evidence import Evidence, EvidenceError, load_evidence
    from MS41.tools.maf_limits import build_report as build_limits_report
    from MS41.tools.vanos_switch import (
        STATUS_READY as VANOS_READY,
        load_comparison,
        TRANSFER_TABLES,
        ThresholdError,
        build_report as build_vanos_report,
        dump_stock as vanos_dump,
    )
    from MS41.tools.maf_curve import CurveError, build_report, load_curve
    from MS41.tools.xdf import XdfError, get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from hardware_profile import evaluate, parse_profile
    from injector_scaling import (
        STATUS_READY as SCALING_READY,
        ScalingError,
        build_report as build_scaling_report,
    )
    from log_evidence import Evidence, EvidenceError, load_evidence
    from maf_limits import build_report as build_limits_report
    from vanos_switch import (
        STATUS_READY as VANOS_READY,
        load_comparison,
        TRANSFER_TABLES,
        ThresholdError,
        build_report as build_vanos_report,
        dump_stock as vanos_dump,
    )
    from maf_curve import CurveError, build_report, load_curve
    from xdf import XdfError, get_object, load_definition, read_engineering

STATUS_READY = "READY"
STATUS_EMPTY = "EMPTY"
STATUS_BLOCKED = "BLOCKED"

#: Reasons attached to every generated change. ``customer_intent`` means the
#: value came from the request itself; the others name a document or a file.
EVIDENCE_INTENT = "customer_intent"


@dataclass(frozen=True)
class Rule:
    """How one intent kind becomes symbol/value changes."""

    kind: str
    description: str
    validate: tuple[str, ...] = ()


RULES: dict[str, Rule] = {
    "vmax": Rule(
        "vmax",
        "vehicle speed limiter",
        ("road test: confirm the limiter releases at the requested speed and that "
         "the ECU does not re-limit during the pull",),
    ),
    "rpm_limit": Rule(
        "rpm_limit",
        "engine speed limiter (fuel cut)",
        ("log the pull and confirm the limiter engages where expected and that the "
         "engine still makes power up to the new limit",),
    ),
    "maf_curve": Rule(
        "maf_curve",
        "MAF transfer curve",
        ("log MAF voltage, load, lambda and the fuel trims at several points and "
         "compare them with the supplier curve",),
    ),
    "launch_limiter": Rule(
        "launch_limiter",
        "vehicle-speed launch limit (cylinder shut-off)",
        ("log a launch: the limiter must hold the set speed without misfire or "
         "cylinder-shut-off codes, and the car must still drive normally away from "
         "the limit",),
    ),
    "vanos": Rule(
        "vanos",
        "VANOS switching threshold",
        ("log the switch: the threshold should trigger on the measured crossing "
         "line, without a jolt at the transition, and the cold columns should keep "
         "their stock behaviour",),
    ),
    "injectors": Rule(
        "injectors",
        "base injection map rescale for documented injectors",
        ("log fuel trims and wideband lambda at idle, cruise and full load; the "
         "long-term trims should sit near zero across the range, not just at idle",),
    ),
    "hardcut": Rule(
        "hardcut",
        "overrun fuel-cut hysteresis",
        ("log the transition: a smaller hysteresis makes the cut harder and can "
         "bring a jerky resume",),
    ),
}

#: Kinds that are not generated because they need evidence no software can create.
#: Unlike a bare "not supported", each entry names the tables it would write, the
#: log sections that have to be filled, and the bench work that has to precede it,
#: so a customer request comes back as an actionable plan instead of a refusal.
EVIDENCE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "ignition": {
        "title": "Ignition advance (Stage 1 / RON 91-95-98)",
        "reason": (
            "углы зажигания под нагрузкой не имеют универсального значения: их "
            "подбирают по логам детонации и стенду на конкретном топливе"
        ),
        "tables": ["ip_igab__n__maf", "ip_iga_ivvt__n__maf", "ip_iga_max_knk__n__maf",
                   "c_iga_max_ad", "c_iga_min_ad", "c_iga_knk_dif_max_min",
                   "c_fac_tra_knk"],
        "logs": ["knock.knock_retard_deg_max", "knock.rpm_at_max_knock",
                 "lambda.afr_error_percent_max"],
        "bench": [
            "стенд: свип по нагрузке и оборотам на целевом RON с прослушиванием детонации",
            "лог: величина и обороты максимального ретарда штатного knock-контроля",
            "проверить, что штатная защита не ослабляется ни на одном шаге",
        ],
        "gates": [
            "ни одного шага без подтверждённого отсутствия детонации на этом топливе",
            "штатные knock- и термозащиты остаются включёнными",
        ],
    },
    "fueling": {
        "title": "WOT fuelling (lambda target)",
        "reason": (
            "топливо под нагрузкой задаётся в лямбде и подтверждается широкополосным "
            "датчиком: у стокового MS41 нет широкополосного входа, нужен внешний"
        ),
        "tables": ["ip_ti_fl__n", "ip_tib__n__maf", "ip_ti_ivvt__n__maf",
                   "c_ti_ad_fac_max", "c_ti_ad_add_max"],
        "logs": ["lambda.afr_error_percent_max", "lambda.wideband_available",
                 "trims.stft_percent", "trims.ltft_percent"],
        "bench": [
            "внешняя wideband: целевая лямбда по оборотам на полной нагрузке",
            "контроль обеих банок, IPW и давления топлива",
            "коррекция строится гладкой кривой по оборотам, без ступеней",
        ],
        "gates": [
            "без wideband шаг не выполняется ни в каком виде",
            "адаптационные пределы остаются стоковыми, пока база не подтверждена",
        ],
    },
    "octane": {
        "title": "Октановый профиль RON91/95/98",
        "reason": (
            "в MS41 нет пары карт RON91/RON98: база считается одной картой с "
            "температурно-октановым фактором `ip_iga_tra_fac__tia__ron_fac` и "
            "детонационной поправкой, а семантику этого фактора на стоке мы не "
            "доказывали (веб-анализ docs/VARIANT1_WEB_ANALYSIS_RU.md §7). Отдельные "
            "профили RON91/95/98 поэтому не выпускаются; допустима только добавка "
            "внутри уже влитого пакета зажигания и с сохранением штатных защит"
        ),
        "tables": ["ip_igab__n__maf", "ip_iga_ivvt__n__maf",
                   "ip_iga_tra_fac__tia__ron_fac", "ip_iga_max_knk__n__maf",
                   "ip_iga_aj__n__maf", "id_iga_dec_knk_1__n", "id_iga_inc_knk__n",
                   "ip_iga_tra_knk__n__maf"],
        "logs": ["knock.knock_retard_deg_max", "knock.rpm_at_max_knock",
                 "lambda.afr_error_percent_max"],
        "bench": [
            "стенд: свип по нагрузке и оборотам на целевом RON с прослушиванием детонации",
            "снять и разложить октановый фактор: как он меняется на этом топливе",
            "проверить, что детонационный ретард и потолок ip_iga_max_knk__n__maf остались штатными",
        ],
        "gates": [
            "без доказанной семантики ip_iga_tra_fac__tia__ron_fac профили не выпускаются",
            "штатные knock- и термозащиты не ослабляются ни на одном шаге",
        ],
    },
    "isa_off": {
        "title": "Обход клапана холостого хода (ISA off)",
        "reason": (
            "ISA/ICV держит холостой ход: удаление клапана без заглушки канала ХХ "
            "даёт неустойчивый или глохнущий холостой ход (сообщество: Renovelo, "
            "E46Fanatics; веб-анализ §6). Подавление диагностики функцию не "
            "отключает, поэтому нужен лог холостого хода до/после и решение по "
            "диагностическому слою"
        ),
        "tables": ["ip_isapwm__tco", "ip_n_sp_is__tco", "ip_isapwm_tia_add__tia",
                   "ip_isapwm_tia_fac__tia", "c_isapwm_isa_diag", "c_n_sp_lgrd_is",
                   "ip_maf_isapwm_diag_1__n__isapwm_mmv"],
        "logs": ["idle_actuator", "rpm", "coolant_temp", "battery_voltage"],
        "bench": [
            "лог холостого хода до и после вмешательства: устойчивость оборотов",
            "подтверждение механики: клапан снят или заглушен, чем закрыт канал ХХ",
            "отдельное решение по коду ошибки (подавление — только с правовой справкой)",
        ],
        "gates": [
            "без заглушки канала ХХ опция не выпускается",
            "диагностический слой не подавляет передний зонд и детонацию",
        ],
    },
    "idle": {
        "title": "Обороты холостого хода (уставка и прогрев)",
        "reason": (
            "уставка холостого хода поднимается вместе со скважностью РХХ, иначе "
            "регулятор упирается в предел; значение подбирается по логу "
            "установившегося холостого хода, а не из форумов"
        ),
        "tables": ["ip_n_sp_is__tco", "ip_isapwm__tco", "c_n_sp_lgrd_is",
                   "c_n_sp_add_ch", "ip_isapwm_tia_add__tia",
                   "c_isapwm_ad_max_isa"],
        "logs": ["idle_actuator", "rpm", "coolant_temp", "lambda_bank1_voltage"],
        "bench": [
            "лог установившегося холостого хода на прогретом двигателе (разброс оборотов)",
            "проверка пуска и прогрева: уставка по tco остаётся плавной",
            "контроль, что регулятор не упирается в предел скважности",
        ],
        "gates": [
            "без лога холостого хода уставка не меняется",
            "прогревочные столбцы не сглаживаются «на глаз»",
        ],
    },
    "popcorn": {
        "title": "Controlled overrun popcorn",
        "reason": (
            "хлопки делаются поздним зажиганием и топливом на сбросе газа; это "
            "термическая нагрузка на выпуск и требует проверки на конкретной машине"
        ),
        "tables": ["ip_iga_puc__n", "ip_n_min_puc__tco", "c_n_hys_max_puc",
                   "c_n_hys_min_puc", "c_tco_min_pu", "c_pvs_grd_pu"],
        "logs": [],
        "bench": [
            "проверка на конкретном выпуске: температура и отсутствие прогара",
            "подтвердить, что отсечка на сбросе газа включается и корректно "
            "восстанавливается (STATUS_SCHUB_AB в диагностике)",
            "только закрытая площадка, не на дороге общего пользования",
        ],
        "gates": [
            "при катализаторе — отказ, а не предупреждение",
        ],
    },
}


@dataclass
class Outcome:
    id: str
    kind: str
    status: str
    detail: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "changes": self.changes,
            "findings": self.findings,
        }


@dataclass
class GenerationResult:
    status: str
    patches: list[dict[str, Any]]
    outcomes: list[Outcome]
    notes: list[str]
    source_sha256: str
    image_type: str
    blocked_reasons: list[str]
    dry_run: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_sha256": self.source_sha256,
            "image_type": self.image_type,
            "patches": self.patches,
            "outcomes": [outcome.to_json() for outcome in self.outcomes],
            "notes": self.notes,
            "blocked_reasons": self.blocked_reasons,
            "dry_run": self.dry_run,
        }


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _change(symbol: str, value: float, intent: dict[str, Any], rule: Rule,
            evidence: str = EVIDENCE_INTENT, index: int | None = None,
            reason: str | None = None, hint: str | None = None,
            units: str | None = None) -> dict[str, Any]:
    change = {
        "symbol": symbol,
        "value": value,
        "reason": reason or intent.get("reason")
        or f"{rule.description}: requested by the customer",
        "evidence": evidence,
        "intent": intent.get("id") or rule.kind,
        "requires_validation": True,
        "validation_hint": hint if hint is not None else (
            rule.validate[0] if rule.validate else ""),
    }
    if index is not None:
        change["index"] = index
    if units is not None:
        # A tool that read the table knows better than a mislabelled XDF field;
        # the reason travels in the report, not silently in the patch file.
        change["units"] = units
    return change


def _expand_vmax(intent: dict[str, Any], rule: Rule, context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "vmax"), rule.kind, STATUS_EMPTY, "")
    mode = str(intent.get("mode", "raise")).lower()
    if mode == "remove":
        outcome.changes.append(_change("c_vs_max", 255, intent, rule))
        outcome.status = STATUS_READY
        outcome.detail = "speed limiter set to 255 km/h, i.e. above any reachable speed"
        outcome.findings.append(_finding(
            "LEGAL_NOTE", "info",
            "removing the speed limiter is a road-legal decision for the owner; "
            "record it in the delivery note",
        ))
        return outcome
    if mode != "raise":
        outcome.status = STATUS_BLOCKED
        outcome.detail = f"unknown mode {mode!r}; use 'raise' or 'remove'"
        return outcome
    value = _number(intent.get("value_kmh"))
    if value is None:
        outcome.status = STATUS_BLOCKED
        outcome.detail = "raise needs value_kmh"
        return outcome
    outcome.changes.append(_change("c_vs_max", value, intent, rule))
    outcome.status = STATUS_READY
    outcome.detail = f"speed limiter raised to {value:g} km/h"
    return outcome


def _expand_rpm_limit(intent: dict[str, Any], rule: Rule,
                      context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "rpm_limit"), rule.kind, STATUS_EMPTY, "")
    value = _number(intent.get("value_rpm"))
    if value is None:
        outcome.status = STATUS_BLOCKED
        outcome.detail = "rpm_limit needs value_rpm"
        return outcome
    stock = context.get("stock", {}).get("c_n_max")
    raising = stock is None or value > stock
    evidence = context.get("evidence")
    logged = None
    if raising and evidence is not None:
        candidate = evidence.require("rpm_limit", "validated", "max_safe_rpm")
        if candidate is not None and candidate["validated"] is True:
            logged = candidate
        elif candidate is not None:
            outcome.findings.append(_finding(
                "LOG_NOT_VALIDATED", "warning",
                f"the log evidence from {evidence.source} records "
                f"max_safe_rpm={candidate['max_safe_rpm']} but marks it as not validated, "
                "so it was not used",
            ))
    if raising and not intent.get("acknowledge_risk") and logged is None:
        evidence_note = ""
        if evidence is not None:
            evidence_note = f" (log evidence: {evidence.status('rpm_limit')})"
        outcome.status = STATUS_REQUIRES_DATA
        outcome.detail = (
            f"raising the limiter to {value:g} rpm changes engine stress; it needs an "
            "explicit acknowledge_risk: true or log evidence that the engine is happy "
            "at that speed" + evidence_note
        )
        return outcome
    change = _change("c_n_max", value, intent, rule)
    if logged is not None:
        change["evidence"] = f"log_evidence:{evidence.source}"
        change["found"] = f"max_safe_rpm={logged['max_safe_rpm']}"
    outcome.changes.append(change)
    outcome.status = STATUS_READY
    outcome.detail = f"engine speed limiter set to {value:g} rpm"
    if logged is not None:
        outcome.findings.append(_finding(
            "LOG_BACKED", "info",
            f"the raise is backed by log evidence from {evidence.source}",
        ))
    elif raising:
        outcome.findings.append(_finding(
            "RISK_ACKNOWLEDGED", "warning",
            "the customer acknowledged the stress increase of a higher limiter",
        ))
    return outcome


def _derive_hysteresis(mode: str, context: dict[str, Any]) -> tuple[float | None, str]:
    """One raw step away from stock, which is the smallest representable move.

    Engineering-unit guesses do not work here: c_n_max_hys has a 32 rpm quantum,
    so "half of stock" is not representable and the planner would reject it.
    """
    objects = context.get("objects")
    calibration = context.get("calibration")
    if not objects or calibration is None:
        return None, "the hysteresis table could not be read; supply hysteresis_rpm"
    try:
        obj = get_object(objects, "c_n_max_hys")
    except PatchError as exc:
        return None, f"hysteresis_rpm could not be derived ({exc}); supply it explicitly"
    raw_stock = int.from_bytes(
        calibration[obj.address:obj.address + obj.element_size_bytes],
        "little" if obj.lsb_first else "big",
    )
    limit = (1 << obj.element_size_bits) - 1
    raw_target = max(0, min(raw_stock - 1 if mode == "harden" else raw_stock + 1, limit))
    if raw_target == raw_stock:
        return None, (
            f"stock already sits at the {'hardest' if mode == 'harden' else 'softest'} "
            f"representable value (raw {raw_stock}); there is no one-step move, so the "
            "new value has to come from a log"
        )
    return obj.to_engineering(raw_target), (
        f"hysteresis_rpm was not given, so it was derived from stock raw {raw_stock} as "
        f"one raw step ({obj.scaler.a:g} rpm) {mode}; this is a starting point for a "
        "log, not a measured optimum"
    )


def _expand_evidence_request(kind: str, intent: dict[str, Any],
                             context: dict[str, Any]) -> Outcome:
    """Answer a request for a kind that needs evidence, with a real plan."""
    requirement = EVIDENCE_REQUIREMENTS[kind]
    outcome = Outcome(str(intent.get("id") or kind), kind, STATUS_REQUIRES_DATA, "")
    objects = context.get("objects") or {}
    resolved: list[dict[str, Any]] = []
    for symbol in requirement["tables"]:
        try:
            obj = get_object(objects, symbol)
        except (PatchError, XdfError) as exc:
            resolved.append({"symbol": symbol, "error": str(exc)})
            continue
        entry: dict[str, Any] = {
            "symbol": symbol,
            "address": f"0x{obj.address:04X}",
            "units": obj.units,
            "description": obj.description,
        }
        if obj.element_count > 1:
            entry["shape"] = f"{obj.rows}x{obj.columns}"
            entry["cells"] = obj.element_count
        resolved.append(entry)
    outcome.findings.append(_finding(
        "EVIDENCE_REQUIRED", "info",
        requirement["reason"] + "; нужны: " + "; ".join(requirement["bench"]),
    ))
    for step in requirement["gates"]:
        outcome.findings.append(_finding("GATE", "warning", step))
    if requirement["logs"]:
        outcome.findings.append(_finding(
            "LOG_SECTIONS_REQUIRED", "info",
            "секции лог-контракта, которые должны быть заполнены: "
            + ", ".join(requirement["logs"]),
        ))
    outcome.findings.append(_finding(
        "NOTHING_WRITTEN", "info",
        "ничего не записывается: значение для этих таблиц может дать только стенд",
    ))
    tables = ", ".join(
        f"{item['symbol']} {item.get('address', '?')}"
        + (f" ({item['shape']})" if item.get("shape") else "")
        for item in resolved
    )
    outcome.detail = (
        f"{requirement['title']}: {requirement['reason']}. Таблицы, которые понадобятся: "
        f"{tables}"
    )
    outcome.changes = []
    outcome.findings.append(_finding("TABLES_RESOLVED", "info", tables))
    return outcome


#: Below this speed a limit is a launch aid rather than a road-speed limit: the car
#: spends its time on the limiter under full load, which is what makes it harsh on
#: the exhaust and the driveline.
LAUNCH_CEILING_KMH = 100.0


def _expand_launch_limiter(intent: dict[str, Any], rule: Rule,
                           context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "launch_limiter"), rule.kind,
                      STATUS_EMPTY, "")
    value = _number(intent.get("value_kmh"))
    if value is None:
        outcome.status = STATUS_BLOCKED
        outcome.detail = "launch_limiter needs value_kmh: the speed the car may not exceed"
        return outcome
    if value < 1 or value > 255:
        outcome.status = STATUS_BLOCKED
        outcome.detail = (
            f"value_kmh={value:g} is outside the 1..255 km/h the 8-bit cell can hold"
        )
        return outcome
    launch = value < LAUNCH_CEILING_KMH
    catalyst = context.get("catalyst")

    if launch and catalyst == "present":
        outcome.status = STATUS_BLOCKED
        outcome.detail = (
            f"LAUNCH_WITH_CATALYST: {value:g} km/h means the limiter is held on under "
            "full load, and MS41 limits speed by shutting cylinders off, so unburnt "
            "mixture reaches the exhaust. Same product rule as popcorn: no launch "
            "limit on a catalyst-equipped car"
        )
        outcome.findings.append(_finding(
            "LAUNCH_WITH_CATALYST", "reject",
            "product rule: the cylinder shut-off limiter must not be used as a launch "
            "aid while a catalyst is fitted",
        ))
        return outcome
    if launch and not intent.get("acknowledge_risk"):
        outcome.status = STATUS_REQUIRES_DATA
        outcome.detail = (
            f"a {value:g} km/h limit turns the car into one that cannot exceed "
            f"{value:g} km/h anywhere, and it does that by cutting cylinders under "
            "load. Confirm the owner understands both, then set acknowledge_risk: true"
        )
        return outcome

    objects = context.get("objects") or {}
    calibration = context.get("calibration") or b""
    switch = get_object(objects, "c_var_vs_max_act")
    switch_raw = (int.from_bytes(
        calibration[switch.address:switch.address + switch.element_size_bytes],
        "little" if switch.lsb_first else "big") if calibration else None)
    if switch_raw == 0 or switch_raw is None:
        outcome.changes.append(_change(
            "c_var_vs_max_act", 1, intent, rule,
            reason="включить штатный вариант «ограничение скорости активно»: со "
                   "стоковым 0 значение c_vs_max не читается вообще",
            hint="проверить по диагностике, что ограничение действительно работает, "
                 "а не осталось выключенным",
        ))
    outcome.changes.append(_change(
        "c_vs_max", value, intent, rule, units="km/h",
        reason=f"ограничитель скорости {value:g} км/ч как launch-лимит "
               f"({'отсечка цилиндров под нагрузкой' if launch else 'дорожный лимит'})",
        hint=rule.validate[0],
    ))

    escalation = get_object(objects, "id_n_max_cycnr__vs")
    axis = get_object(objects, "ldp_vs__n_max_cycnr")
    detail_bits = []
    if calibration:
        cells = read_engineering(calibration, escalation)
        speeds = read_engineering(calibration, axis)
        detail_bits.append(
            "эскалация отключения цилиндров (id_n_max_cycnr__vs @ "
            f"0x{escalation.address:04X}) остаётся стоковой: "
            + ", ".join(f"{speed:g} км/ч->{value:g}" for speed, value in
                        zip(speeds, cells))
        )
    outcome.findings.append(_finding(
        "LAUNCH_LIMIT_MECHANISM", "warning",
        "MS41 ограничивает скорость отключением цилиндров, а не зажиганием: это не "
        "RPM-отсечка и не двухступенчатый лимитер, а дорожный лимит, который "
        "работает везде, а не только на старте",
    ))
    if detail_bits:
        outcome.findings.append(_finding("ESCALATION_KEPT", "info", detail_bits[0]))
    if not launch:
        outcome.findings.append(_finding(
            "ROAD_LIMIT", "info",
            f"{value:g} км/ч это обычный дорожный лимит: поведение как у интента "
            "vmax, launch-режим здесь не включается",
        ))
    outcome.status = STATUS_READY
    outcome.detail = (
        f"ограничитель скорости {value:g} км/ч"
        + (" (launch-лимит через отсечку цилиндров)" if launch else "")
    )
    return outcome


def _expand_vanos(intent: dict[str, Any], rule: Rule,
                  context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "vanos"), rule.kind, STATUS_EMPTY, "")
    profile = context.get("profile")
    if profile is None:
        outcome.status = STATUS_BLOCKED
        outcome.detail = (
            "vanos needs the Stage 2 hardware profile: the manual and the automatic "
            "car use different threshold tables, and the powertrain has to decide "
            "which one is calibrated"
        )
        return outcome
    transmission = str(profile.get("transmission") or "").strip().lower()
    if transmission not in TRANSFER_TABLES:
        outcome.status = STATUS_BLOCKED
        outcome.detail = (
            f"the hardware profile does not state a usable transmission "
            f"({transmission or 'missing'}); the two VANOS tables are not "
            "interchangeable"
        )
        return outcome

    reference = intent.get("threshold_file")
    if not reference:
        dump = vanos_dump(context["bin_path"], transmission)
        outcome.status = STATUS_REQUIRES_DATA
        outcome.detail = (
            f"nothing confirms a new VANOS threshold: measure the load with the cam "
            f"advanced and retarded and supply threshold_file. The {transmission} table "
            f"is {dump['symbol']} ({dump['shape']} @ {dump['address']}), axes "
            f"{dump['rpm_axis'][0]:g}…{dump['rpm_axis'][-1]:g} rpm by "
            f"{dump['tco_axis_c'][0]:g}…{dump['tco_axis_c'][-1]:g} °C; "
            f"{dump['never_switch']['cells']} of {dump['cells_total']} cells are the "
            "'never switch' sentinel and stay stock"
        )
        return outcome

    path = Path(reference)
    if not path.is_absolute():
        path = Path(context.get("base_dir") or ".") / path
    if not path.exists():
        outcome.status = STATUS_BLOCKED
        outcome.detail = f"threshold file {path} does not exist"
        return outcome
    try:
        comparison = load_comparison(path)
        report = build_vanos_report(context["bin_path"], transmission, comparison,
                                    context["policy"], engine=profile.get("engine"))
    except (ThresholdError, PatchError, XdfError, json.JSONDecodeError) as exc:
        outcome.status = STATUS_BLOCKED
        outcome.detail = str(exc)
        return outcome
    for finding in report.findings:
        outcome.findings.append(_finding(finding["code"], finding["severity"],
                                         finding["message"]))
    if report.status == "EMPTY":
        outcome.status = STATUS_EMPTY
        outcome.detail = "the measured threshold already matches the table"
        return outcome
    if report.status != VANOS_READY:
        outcome.status = STATUS_BLOCKED
        outcome.detail = ("the measured threshold was refused: " + "; ".join(
            finding["message"] for finding in report.findings
            if finding["severity"] == "reject") or "see the findings")
        return outcome
    hint = report.patches[0]["validation_hint"] if report.patches else ""
    for patch in report.patches:
        outcome.changes.append(_change(
            patch["symbol"], patch["value"], intent, rule,
            evidence=patch["evidence"], index=patch["index"],
            reason=patch["reason"], hint=hint, units=patch.get("units"),
        ))
    outcome.status = STATUS_READY
    outcome.detail = (
        f"{len(outcome.changes)} VANOS threshold cells moved to the measured line "
        f"({report.details['comparison_source']}), crossing at "
        f"{report.details['crossing']['computed_rpm']:.0f} rpm"
    )
    return outcome


def _expand_injectors(intent: dict[str, Any], rule: Rule,
                      context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "injectors"), rule.kind, STATUS_EMPTY, "")
    profile = context.get("profile")
    if profile is None:
        outcome.status = STATUS_BLOCKED
        outcome.detail = (
            "injectors needs the Stage 2 hardware profile: pass --profile with the "
            "documented injector part number and both flow values"
        )
        return outcome
    try:
        report = build_scaling_report(context["bin_path"], profile,
                                      context.get("evidence"), context["policy"])
    except (ScalingError, PatchError, XdfError) as exc:
        outcome.status = STATUS_BLOCKED
        outcome.detail = str(exc)
        return outcome
    for finding in report.findings:
        outcome.findings.append(_finding(finding["code"], finding["severity"],
                                         finding["message"]))
    factor = report.factor_from_flows
    if report.status == "REQUIRES_DATA":
        outcome.status = STATUS_REQUIRES_DATA
        cells = report.details.get("cells")
        outcome.detail = (
            f"the base map ({cells} cells) would be rescaled by {factor:.4f} from the "
            "documented flows, but the factor is unconfirmed: "
            + "a log with the new injectors fitted has to confirm it before a fuel "
            "map is written"
        )
        return outcome
    if report.status == "EMPTY":
        outcome.status = STATUS_EMPTY
        outcome.detail = (f"the declared flow ratio is within 0.5% of stock "
                          f"(factor {factor:.4f}); nothing to rescale")
        return outcome
    if report.status != SCALING_READY:
        outcome.status = STATUS_BLOCKED
        outcome.detail = ("the rescale was rejected: " + "; ".join(
            finding["message"] for finding in report.findings
            if finding["severity"] == "reject") or "see the findings")
        return outcome
    for patch in report.patches:
        outcome.changes.append(dict(patch))
    outcome.status = STATUS_READY
    outcome.detail = (
        f"{len(outcome.changes)} injection cells rescaled by {factor:.4f} "
        f"({report.details.get('stock_flow_cc_min')} -> "
        f"{report.details.get('new_flow_cc_min')} cc/min), confirmed by the logged "
        "trims"
    )
    return outcome


def _expand_hardcut(intent: dict[str, Any], rule: Rule,
                    context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "hardcut"), rule.kind, STATUS_EMPTY, "")
    mode = str(intent.get("mode", "harden")).lower()
    if mode not in ("harden", "soften"):
        outcome.status = STATUS_BLOCKED
        outcome.detail = f"unknown mode {mode!r}; use 'harden' or 'soften'"
        return outcome
    value = _number(intent.get("hysteresis_rpm"))
    if value is None:
        value, note = _derive_hysteresis(mode, context)
        if value is None:
            outcome.status = STATUS_REQUIRES_DATA
            outcome.detail = note
            return outcome
        outcome.findings.append(_finding("DERIVED_FROM_STOCK", "info", note))
    outcome.changes.append(_change("c_n_max_hys", value, intent, rule))
    outcome.status = STATUS_READY
    outcome.detail = f"overrun fuel-cut hysteresis set to {value:g} rpm ({mode})"
    if mode == "harden":
        outcome.findings.append(_finding(
            "FUEL_CUT_NOTE", "warning",
            "a smaller hysteresis makes the fuel cut harder and shortens the resume "
            "delay; it also makes the transition more abrupt",
        ))
    return outcome


def _expand_maf_curve(intent: dict[str, Any], rule: Rule,
                      context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "maf_curve"), rule.kind, STATUS_EMPTY, "")
    reference = intent.get("curve_file")
    if not reference:
        outcome.status = STATUS_BLOCKED
        outcome.detail = (
            "maf_curve needs curve_file: a documented voltage -> kg/h curve for the "
            "replacement sensor"
        )
        return outcome
    path = Path(reference)
    if not path.is_absolute():
        path = Path(context.get("base_dir") or ".") / path
    if not path.exists():
        outcome.status = STATUS_BLOCKED
        outcome.detail = f"curve file {path} does not exist"
        return outcome
    stock_path = context.get("bin_path")
    try:
        curve = load_curve(path)
        report = build_report(stock_path, curve, context["policy"])
    except (CurveError, PatchError) as exc:
        outcome.status = STATUS_BLOCKED
        outcome.detail = f"{exc}"
        outcome.findings.append(_finding("CURVE_REFUSED", "reject", str(exc)))
        return outcome

    outcome.findings.append(_finding(
        "CURVE_RESAMPLED", "info",
        f"{curve.sensor} ({curve.provenance}) sampled on {report.details['columns']} "
        f"columns; ceiling {report.details['deviation_vs_stock']['stock_ceiling_kgh']} "
        f"-> {report.details['deviation_vs_stock']['new_ceiling_kgh']} kg/h; max "
        f"deviation {report.details['deviation_vs_stock']['max_absolute_kgh']} kg/h "
        f"({report.details['deviation_vs_stock']['max_relative_percent']}%)",
    ))
    for finding in report.findings:
        outcome.findings.append(_finding(finding["code"], finding["severity"],
                                         finding["message"]))
    if report.status == "EMPTY":
        outcome.status = STATUS_EMPTY
        outcome.detail = "the resampled curve already matches this binary"
        return outcome
    if report.status != "READY":
        outcome.status = STATUS_BLOCKED
        outcome.detail = "the curve was refused by the patch planner"
        return outcome

    hint = report.patches[0]["validation_hint"] if report.patches else ""
    for patch in report.patches:
        outcome.changes.append(_change(
            patch["symbol"], patch["value"], intent, rule,
            evidence=patch["evidence"], index=patch["index"],
            reason=patch["reason"], hint=hint, units=patch.get("units"),
        ))
    curve_columns = len(outcome.changes)
    limits = _append_maf_limits(outcome, curve, context, intent, rule)
    outcome.status = STATUS_READY
    outcome.detail = (
        f"{curve_columns} MAF columns resampled from {curve.sensor} "
        f"({curve.provenance})"
        + (f" + {limits} air-mass limit(s) aligned with the new ceiling"
           if limits else "")
    )
    if curve_columns and not limits:
        outcome.findings.append(_finding(
            "LIMITS_UNCHANGED", "warning",
            "the air-mass limits (c_maf_max*) stay stock, so the extra sensor "
            "range is not reachable yet: the limit raise needs a log showing the "
            "stock sensor saturated"
            + ("" if context.get("evidence") is None
               else f" (log evidence: {context['evidence'].status('maf')})"),
        ))
    return outcome


def _append_maf_limits(outcome: Outcome, curve, context: dict[str, Any],
                       intent: dict[str, Any], rule: Rule) -> int:
    """Plan the air-mass limits that must follow a raised MAF ceiling.

    Returns the number of planned limits. A limit raise that is not proven yet
    must not block the curve: it is reported as a warning instead, and the whole
    reasoning lives in MS41/tools/maf_limits.py.
    """
    try:
        report = build_limits_report(context["bin_path"], curve,
                                     context.get("evidence"), context["policy"])
    except (PatchError, XdfError, ValueError) as exc:
        outcome.findings.append(_finding(
            "LIMITS_NOT_ALIGNED", "warning",
            f"the air-mass limits could not be aligned with this curve: {exc}",
        ))
        return 0
    for finding in report.findings:
        outcome.findings.append(_finding(finding["code"], finding["severity"],
                                         finding["message"]))
    if report.status == "EMPTY":
        return 0
    if report.status != "READY":
        return 0
    for patch in report.patches:
        outcome.changes.append(_change(
            patch["symbol"], patch["value"], intent, rule,
            evidence=patch["evidence"], reason=patch["reason"],
            hint=patch["validation_hint"],
        ))
    return len(report.patches)


EXPANDERS: dict[str, Callable[[dict[str, Any], Rule, dict[str, Any]], Outcome]] = {
    "launch_limiter": _expand_launch_limiter,
    "maf_curve": _expand_maf_curve,
    "injectors": _expand_injectors,
    "vanos": _expand_vanos,
    "vmax": _expand_vmax,
    "rpm_limit": _expand_rpm_limit,
    "hardcut": _expand_hardcut,
}


def load_request(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request file must be a JSON object")
    intents = data.get("intents")
    if intents is None:
        intents = []
    if not isinstance(intents, list):
        raise ValueError("'intents' must be a list")
    return data


def stock_values(image: bytearray, offset: int, objects) -> dict[str, float]:

    calibration = bytes(image[offset:offset + 0x6000])
    values: dict[str, float] = {}
    for symbol in ("c_n_max", "c_n_max_hys", "c_vs_max", "c_vs_max_hys",
                   "c_n_hys_max_puc", "c_n_hys_min_puc"):
        try:
            obj = get_object(objects, symbol)
        except PatchError:
            continue
        values[symbol] = read_engineering(calibration, obj)[0]
    return values


#: Намерения, которых у MS41 нет, и причина. Отдельно от «неизвестного kind»:
#: это не опечатка клиента, а честный ответ «на MS41 так нельзя» (веб-анализ
#: docs/VARIANT1_WEB_ANALYSIS_RU.md §5/§6/§8).
UNAVAILABLE_INTENTS: dict[str, tuple[str, str]] = {
    "disa_off": (
        "DISA off",
        "MS41: функции DISA нет вовсе — в определениях и job-ах PRG нет ни "
        "объектов VIM/DISA, ни управления заслонкой (веб-анализ §5); опция "
        "неприменима, а не «ждёт данных»",
    ),
    "fan_thermostat": (
        "вентилятор и термостат",
        "MS41: в определениях нет ни объектов электровентилятора (ECF), ни "
        "электронного термостата (ECT) — трек-режим по калибровке недоступен "
        "(веб-анализ §8)",
    ),
    "launch": (
        "launch control",
        "MS41: патча launch control для MS41 не существует; лимит по скорости "
        "на старте — намерение launch_limiter и только по логу (веб-анализ §8)",
    ),
    "dtc": (
        "подавление DTC",
        "MS41: подавление кодов ошибок проект не выпускает (правовая справка — "
        "docs/VARIANT1_WEB_ANALYSIS_RU.md §4)",
    ),
    "cat_delete": (
        "удаление катализатора",
        "MS41: удаление катализатора проект не выпускает; хлопки — намерение "
        "popcorn и только с подтверждением, что катализатора нет",
    ),
    "sap_delete": (
        "удаление СВП",
        "MS41: удаление системы вторичного воздуха проект не выпускает",
    ),
}


def expand_intents(request: dict[str, Any], objects, stock: dict[str, float],
                   calibration: bytes | None = None,
                   bin_path: Path | None = None, policy: Policy | None = None,
                   base_dir: Path | None = None,
                   evidence: "Evidence | None" = None,
                   profile: dict[str, Any] | None = None,
                   catalyst: str | None = None) -> list[Outcome]:
    context = {"stock": stock, "objects": objects, "calibration": calibration,
               "bin_path": bin_path, "policy": policy, "base_dir": base_dir,
               "evidence": evidence, "profile": profile, "catalyst": catalyst}
    outcomes: list[Outcome] = []
    for intent in request.get("intents", []):
        if not isinstance(intent, dict):
            outcomes.append(Outcome("?", "?", STATUS_BLOCKED, "intent must be an object"))
            continue
        kind = str(intent.get("kind", "")).lower()
        ident = str(intent.get("id") or kind or "?")
        if kind in UNAVAILABLE_INTENTS:
            title, reason = UNAVAILABLE_INTENTS[kind]
            outcomes.append(Outcome(ident, kind, STATUS_BLOCKED, f"{title} — {reason}"))
            continue
        if kind in EVIDENCE_REQUIREMENTS:
            outcomes.append(_expand_evidence_request(kind, intent, context))
            continue
        rule = RULES.get(kind)
        if rule is None:
            outcomes.append(Outcome(
                ident, kind, STATUS_BLOCKED,
                f"unknown kind {kind!r}; known: "
                f"{sorted(list(RULES) + list(EVIDENCE_REQUIREMENTS))}",
            ))
            continue
        outcomes.append(EXPANDERS[kind](intent, rule, context))
    return outcomes


# Outcome status for a readable request-level answer.
STATUS_REQUIRES_DATA = "REQUIRES_DATA"


def generate(
    bin_path: Path,
    request: dict[str, Any],
    policy: Policy | None = None,
    profile: dict[str, Any] | None = None,
    measurements: dict[str, Any] | None = None,
    base_dir: Path | None = None,
    evidence: "Evidence | None" = None,
) -> GenerationResult:
    policy = policy or Policy.load(DEFAULT_POLICY)
    objects, problems = load_definition()
    image, offset, image_type = load_calibration(bin_path)
    notes: list[str] = []
    blocked: list[str] = []

    if evidence is not None:
        notes.append("log evidence: " + evidence.summary())
        if evidence.is_stub:
            notes.append(
                "the log evidence file is a stub, so every log-dependent decision "
                "stays REQUIRES_DATA; nothing was assumed from it"
            )
    if profile is not None:
        report = evaluate(parse_profile(profile), measurements, stage2_requested=True)
        notes.append(f"hardware profile status: {report.status}")
        if report.status == "REJECTED":
            rejected = [f"{f.code}: {f.message}" for f in report.findings
                        if f.severity == "reject"]
            return GenerationResult(
                status=STATUS_BLOCKED, patches=[], outcomes=[], notes=notes,
                source_sha256=hashlib.sha256(bytes(image)).hexdigest(),
                image_type=image_type,
                blocked_reasons=[f"HARDWARE_PROFILE_REJECTED: {reason}" for reason in rejected],
            )
        for work in report.work_order:
            notes.append(f"work order: {work.get('job') or work.get('id') or work}")
        if report.required_logging:
            notes.append("required logging: " + "; ".join(report.required_logging))

    catalyst = None
    if profile is not None:
        catalyst = profile.get("catalyst")
    catalyst = request.get("catalyst", catalyst)

    stock = stock_values(image, offset, objects)
    outcomes = expand_intents(
        request, objects, stock, bytes(image[offset:offset + 0x6000]),
        bin_path=bin_path, policy=policy, base_dir=base_dir, evidence=evidence,
        profile=profile, catalyst=catalyst,
    )

    wanted = {intent.get("kind") for intent in request.get("intents", [])
              if isinstance(intent, dict)}
    if "popcorn" in wanted and catalyst == "present":
        for outcome in outcomes:
            if outcome.kind == "popcorn":
                outcome.status = STATUS_BLOCKED
                outcome.detail = (
                    "POPCORN_WITH_CATALYST: popcorn is refused on a car that still has "
                    "a catalyst; the afterfire destroys it"
                )
                outcome.findings.append(_finding(
                    "POPCORN_WITH_CATALYST", "reject",
                    "product rule: no popcorn preset on a catalyst-equipped car",
                ))

    requests: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for outcome in outcomes:
        for change in outcome.changes:
            request = {"symbol": change["symbol"], "value": change["value"]}
            if "index" in change:
                request["index"] = change["index"]
            requests.append(request)

    plan = plan_patches(image, offset, image_type, objects, problems, requests, policy)

    for outcome in outcomes:
        if outcome.status != STATUS_READY:
            continue
        kept: list[dict[str, Any]] = []
        for change in outcome.changes:
            key = (change["symbol"], change.get("index") or 0)
            planned = any((item.symbol, item.index) == key for item in plan.changes)
            if not planned:
                reason = _reason_for(change["symbol"], plan.findings)
                outcome.findings.append(_finding("PLANNER_REJECTED", "reject", reason))
                outcome.status = STATUS_BLOCKED
                outcome.detail = reason
                continue
            kept.append(change)
        outcome.changes = kept
        if outcome.status == STATUS_READY:
            accepted.extend(kept)

    planned_map = {(item.symbol, item.index): item for item in plan.changes}
    patches: list[dict[str, Any]] = []
    for change in accepted:
        planned = planned_map.get((change["symbol"], change.get("index") or 0))
        if planned is None:
            continue
        entry = dict(change)
        entry["planned"] = {
            "index": planned.index,
            "old_raw": planned.old_raw,
            "new_raw": planned.new_raw,
            "old_engineering": planned.old_engineering,
            "new_engineering": planned.new_engineering,
            "quantization_error_percent": round(planned.quantization_error_percent, 4),
            "delta_percent": (round(planned.delta_percent, 4)
                              if planned.delta_percent is not None else None),
            "address": f"0x{planned.address:04X}",
            "units": change.get("units") or planned.units,
        }
        patches.append(entry)

    blocked = [f"{outcome.id}: {outcome.detail}"
               for outcome in outcomes if outcome.status == STATUS_BLOCKED]

    if blocked:
        status = STATUS_BLOCKED
    elif patches:
        status = STATUS_READY
    else:
        status = STATUS_EMPTY

    waiting = [o.id for o in outcomes if o.status == STATUS_REQUIRES_DATA]
    if waiting:
        notes.append("not planned, waiting on data: " + ", ".join(waiting))

    return GenerationResult(
        status=status,
        patches=patches,
        outcomes=outcomes,
        notes=notes,
        source_sha256=hashlib.sha256(bytes(image)).hexdigest(),
        image_type=image_type,
        blocked_reasons=blocked,
        dry_run={
            "plan_status": plan.status,
            "planned_symbols": sorted({symbol for symbol, _ in planned_map}),
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message}
                for f in plan.findings
            ],
        },
    )


def _reason_for(symbol: str, findings) -> str:
    for finding in findings:
        if finding.severity == "reject" and symbol in finding.message:
            return f"{finding.code}: {finding.message}"
    rejected = [f for f in findings if f.severity == "reject"]
    return rejected[0].message if rejected else f"{symbol} was not planned"


def patches_payload(result: GenerationResult, request: dict[str, Any],
                    profile: dict[str, Any] | None) -> dict[str, Any]:
    unfulfilled = [
        {"id": outcome.id, "kind": outcome.kind, "status": outcome.status,
         "detail": outcome.detail}
        for outcome in result.outcomes
        if outcome.status != STATUS_READY
    ]
    return {
        "version": 1,
        "status": result.status,
        "generator": "MS41/tools/generate_patches.py",
        "generated_from_sha256": result.source_sha256,
        "image_type": result.image_type,
        "customer": request.get("customer"),
        "stage": request.get("stage"),
        "ron": request.get("ron"),
        "hardware_profile_supplied": profile is not None,
        "patches": [
            {k: v for k, v in change.items() if k != "planned"}
            for change in result.patches
        ],
        "unfulfilled": unfulfilled,
        "blocked_reasons": result.blocked_reasons,
        "planned_details": {
            f"{change['symbol']}[{change['planned']['index']}]": change["planned"]
            for change in result.patches
        },
    }


def run(args: argparse.Namespace) -> int:
    request = load_request(args.request)
    profile = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile else None
    measurements = (json.loads(args.measurements.read_text(encoding="utf-8"))
                    if args.measurements else None)
    evidence = None
    if args.evidence:
        try:
            evidence = load_evidence(args.evidence)
        except (EvidenceError, json.JSONDecodeError) as exc:
            raise SystemExit(f"error: {exc}")
    try:
        result = generate(args.bin, request, Policy.load(args.policy), profile,
                          measurements, base_dir=args.request.parent, evidence=evidence)
    except PatchError as exc:
        raise SystemExit(f"error: {exc}")

    payload = patches_payload(result, request, profile)
    if args.out and result.status == STATUS_READY:
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    if args.report:
        args.report.write_text(
            json.dumps({"generation": result.to_json(), "patch_file": payload},
                       indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({"generation": result.to_json(), "patch_file": payload},
                         indent=2, ensure_ascii=False))
    else:
        print(f"{result.status} {args.bin}")
        for outcome in result.outcomes:
            print(f"  [{outcome.status}] {outcome.id} ({outcome.kind}): {outcome.detail}")
            for finding in outcome.findings:
                print(f"      {finding['severity'].upper()} {finding['code']}: "
                      f"{finding['message']}")
        shown: set[tuple[str, str]] = set()
        for change in result.patches:
            planned = change["planned"]
            key = (change["symbol"], change["validation_hint"])
            if key in shown:
                continue
            shown.add(key)
            if planned.get("index"):
                columns = sum(1 for other in result.patches
                              if other["symbol"] == change["symbol"])
                print(f"  -> {change['symbol']}: {columns} cell(s) "
                      f"planned, e.g. [{planned['index']}] "
                      f"{planned['old_engineering']:g} -> "
                      f"{planned['new_engineering']:g} {planned['units']} "
                      f"@ {planned['address']} ({change['reason']})")
            else:
                print(f"  -> {change['symbol']}: {planned['old_engineering']:g} -> "
                      f"{planned['new_engineering']:g} {planned['units']} "
                      f"@ {planned['address']} ({change['reason']})")
            print(f"     requires validation: {change['validation_hint']}")
        for reason in result.blocked_reasons:
            print(f"  [BLOCKED] {reason}")
        for note in result.notes:
            print(f"  [NOTE] {note}")
        if result.status == STATUS_READY and args.out:
            print(f"  patch file: {args.out}")
        elif result.status == STATUS_BLOCKED and result.patches:
            print(f"  {len(result.patches)} change(s) passed the planner, but the request is "
                  "blocked: resolve the blocked intents and regenerate before writing a "
                  "patch file")
    return 1 if result.status == STATUS_BLOCKED else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expand a customer request into a patch plan for MS41.")
    parser.add_argument("--bin", type=Path, required=True,
                        help="stock binary the patch will be applied to")
    parser.add_argument("--request", type=Path, required=True,
                        help="customer request JSON")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the patch file here (only when something is planned)")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the full generation report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--profile", type=Path, default=None,
                        help="optional Stage 2 hardware profile JSON")
    parser.add_argument("--measurements", type=Path, default=None,
                        help="optional Stage 2 measurements JSON")
    parser.add_argument("--evidence", type=Path, default=None,
                        help="log evidence file (a stub until real logs exist)")
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
