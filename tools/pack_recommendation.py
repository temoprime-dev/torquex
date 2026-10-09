# -*- coding: utf-8 -*-
"""Анализатор-советчик: рекомендации генератору по клиентскому логу.

Задание `docs/TASK_VARIANT1_RU.md` §3. Обычный анализатор отвечает «да/нет»;
здесь по тому же логу считаются **рекомендации** — что и на сколько можно
изменить, по зонам (нагрузка × обороты), с объяснением и уровнем уверенности.
Числа выводятся только из лога и порогов `docs/pack_log_requirements.json`:

* зажигание — запас до детонации по зонам: коррекция/напряжения детонации и
  стабильность фактического угла; добавка не выше границ проекта
  (Stage 1 ≤ +3.0° без лога для RON98 и ≤ +1.5° для RON95, Stage 2 ≤ +5.0° с
  логом детонации; единый источник — packages.ignition.gates);
* топливо — измеренная λ по зонам против целевого пола 0.82: насколько можно
  обогатить, не пробивая пол; узкополосная λ коррекций не даёт;
* VANOS — рассогласование факт/цель по зонам: сдвиг только там, где механика
  держит цель и удержание устойчиво.

Рекомендация **ничего не разрешает сама**: генератор применяет её только вместе
с sha256 исходного лога и версией анализатора и всё равно ограничивает своими
воротами (`pack_log_analyzer.verify_evidence` + политика версии). Сомнительные
или неполные данные дают «недостаточно данных», а не «можно».

Модуль ничего не пишет на диск и не знает про образы ЭБУ — только про лог.
"""

from __future__ import annotations

import statistics

#: Границы зон по оборотам и нагрузке (мг/такт) — те же, что в требованиях к
#: заездам (`docs/pack_log_requirements.json` §3/§5: 2000..6000+ об/мин,
#: 150..600 мг/такт). Зоны — «полуоткрытые» [lo, hi).
RPM_EDGES = (2000.0, 3000.0, 4000.0, 5000.0, 6500.0)
LOAD_EDGES = (150.0, 300.0, 450.0, 700.0)

#: Минимум выборок в зоне, чтобы вообще что-то рекомендовать.
MIN_ZONE_SAMPLES = 5

#: Во сколько раз «почти порог» снижает запас (мягкая шкала).
NEAR_LIMIT_RATIO = 0.6


def _zone_of(value: float, edges: tuple[float, ...]) -> int | None:
    if value is None or value < edges[0] or value >= edges[-1]:
        return None
    for index in range(len(edges) - 1):
        if edges[index] <= value < edges[index + 1]:
            return index
    return None


def _zone_name(rpm_index: int, load_index: int) -> str:
    return (f"n {RPM_EDGES[rpm_index]:g}–{RPM_EDGES[rpm_index + 1]:g} / "
            f"load {LOAD_EDGES[load_index]:g}–{LOAD_EDGES[load_index + 1]:g} "
            "мг/такт")


def _zone_grid() -> dict[tuple[int, int], dict]:
    grid: dict[tuple[int, int], dict] = {}
    for rpm_index in range(len(RPM_EDGES) - 1):
        for load_index in range(len(LOAD_EDGES) - 1):
            grid[(rpm_index, load_index)] = {
                "index": {"rpm": rpm_index, "load": load_index},
                "zone_ru": _zone_name(rpm_index, load_index),
                "samples": 0,
                "runs": set(),
                "ignition": [],
                "knock_retard": [],
                "knock_signal": [],
                "lambda": [],
                "track_error": [],
                "iat": [],
                "coolant": [],
            }
    return grid


def _segments(table: dict, spec: dict, pack: str) -> list[tuple[int, int]]:
    """Участки лога, по которым считаются зоны.

    Для зажигания/топлива — только «газ в пол» (полный ход); для VANOS —
    установившиеся удержания точек (обороты ≥ 1500 и нагрузка ≥ 150 мг/такт),
    потому что сетка фаз снимается на частичных нагрузках, а не на полном ходу.
    """
    from tools import pack_log_analyzer as analyzer

    if pack in ("ignition", "fuel"):
        return analyzer._wot_runs(table, spec, pack)
    rpm = table["channels"].get("rpm") or []
    load = table["channels"].get("load") or []
    segments: list[tuple[int, int]] = []
    start = None
    for index in range(len(rpm)):
        ok = (rpm[index] is not None and load[index] is not None
              and rpm[index] >= 1500 and load[index] >= 150)
        if ok and start is None:
            start = index
        if (not ok or index == len(rpm) - 1) and start is not None:
            end = index if not ok else index + 1
            if end - start >= 5:
                segments.append((start, end))
            start = None
    return segments


def _fill_zones(table: dict, spec: dict, pack: str) -> dict[tuple[int, int], dict]:
    """Раскладывает выборки лога по зонам (участки — см. :func:`_segments`)."""
    grid = _zone_grid()
    runs = _segments(table, spec, pack)
    channels = table["channels"]
    for run_index, (start, end) in enumerate(runs):
        for index in range(start, end):
            rpm = channels["rpm"][index]
            load = channels["load"][index]
            rpm_index = _zone_of(rpm, RPM_EDGES)
            load_index = _zone_of(load, LOAD_EDGES)
            if rpm_index is None or load_index is None:
                continue
            zone = grid[(rpm_index, load_index)]
            zone["samples"] += 1
            zone["runs"].add(run_index)
            for name, key in (("ignition", "ignition"),
                              ("knock_retard", "knock_retard"),
                              ("lambda_wideband", "lambda"),
                              ("intake_temp", "iat"),
                              ("coolant_temp", "coolant")):
                series = channels.get(name) or []
                if index < len(series) and series[index] is not None:
                    zone[key].append(series[index])
            for bank in ("knock_voltage_bank1", "knock_voltage_bank2"):
                series = channels.get(bank) or []
                if index < len(series) and series[index] is not None:
                    zone["knock_signal"].append(series[index])
            for cylinder in range(1, 7):
                series = channels.get(f"knock_cyl_{cylinder}") or []
                if index < len(series) and series[index] is not None:
                    zone["knock_signal"].append(series[index])
            if pack == "vanos":
                pairs = (
                    ("vanos_intake_actual", "vanos_intake_target"),
                    ("vanos_exhaust_actual", "vanos_exhaust_target"),
                )
                for actual_name, target_name in pairs:
                    actual = channels.get(actual_name) or []
                    target = channels.get(target_name) or []
                    if (index < len(actual) and index < len(target)
                            and actual[index] is not None
                            and target[index] is not None):
                        zone["track_error"].append(abs(actual[index] - target[index]))
    return grid


def _warnings(table: dict, spec: dict, pack: str) -> list[str]:
    """Предупреждения «не рекомендую»: перегрев, обеднение, детонация, ХХ."""
    warnings: list[str] = []
    conditions = spec["conditions"]
    channels = table["channels"]

    def compact(name: str) -> list[float]:
        return [v for v in (channels.get(name) or []) if v is not None]

    coolant = compact("coolant_temp")
    if coolant and max(coolant) > float(conditions["coolant_max_c"]):
        warnings.append(
            f"перегрев: ОЖ до {max(coolant):.1f} °C выше "
            f"{conditions['coolant_max_c']} °C — сначала система охлаждения"
        )
    if coolant and min(coolant) < float(conditions["coolant_min_c"]):
        warnings.append(
            f"мотор не прогрет по фрагменту: ОЖ от {min(coolant):.1f} °C "
            f"(нужно ≥ {conditions['coolant_min_c']} °C)"
        )
    iat = compact("intake_temp")
    if iat and max(iat) > float(conditions["intake_temp_max_c"]):
        warnings.append(
            f"воздух на впуске до {max(iat):.1f} °C — выше "
            f"{conditions['intake_temp_max_c']} °C, добавку по такому логу "
            "не рекомендую"
        )
    lamb = compact("lambda_wideband")
    if lamb:
        floor = float(spec["packages"]["fuel"]["criteria"]["lambda_target_min"])
        if min(lamb) < floor - 0.02:
            warnings.append(
                f"переобогащение: λ до {min(lamb):.3f} ниже пола {floor:.2f}"
            )
        rpm = channels.get("rpm") or []
        load = channels.get("load") or []
        load_min = float(spec["packages"]["ignition"]["wot_run"]["load_min_mg_stroke"])
        wot_lambdas = [
            value for index, value in enumerate(lamb)
            if index < len(load) and load[index] is not None and load[index] >= load_min
        ]
        if wot_lambdas and min(wot_lambdas) > floor + 0.06:
            warnings.append(
                f"на полном ходу λ не опускается ниже {min(wot_lambdas):.3f} — "
                "смесь беднее типичной для полной нагрузки, нужен разбор причин"
            )
    knock_retard = compact("knock_retard")
    if knock_retard:
        no_knock = spec["packages"]["ignition"]["no_knock"]
        bad = sum(1 for value in knock_retard
                  if value > float(no_knock["max_retard_deg"]))
        if bad:
            warnings.append(
                f"детонация: {bad} выборок с коррекцией выше "
                f"{no_knock['max_retard_deg']}° — добавку не рекомендую"
            )
    rpm = compact("rpm")
    if pack in ("ignition", "limits") and rpm:
        # Отсечка: длительное удержание у предела — признак срабатывания.
        top = max(rpm)
        if top >= 6700:
            warnings.append(
                f"обороты до {top:.0f} об/мин: проверьте, что это не "
                "срабатывание ограничителя (мягкая отсечка видна как плато)"
            )
    idle_actuator = compact("idle_actuator")
    if idle_actuator:
        spread = max(idle_actuator) - min(idle_actuator)
        if spread > 25.0:
            warnings.append(
                f"регулятор холостого хода ходит на {spread:.0f} % — "
                "нестабильный ХХ, сначала механика/герметичность"
            )
    return warnings


def _ignition_zones(grid: dict, spec: dict, fuel_ron, table: dict) -> tuple[list[dict], dict]:
    from tools import pack_log_analyzer as analyzer

    pack_spec = spec["packages"]["ignition"]
    no_knock = pack_spec["no_knock"]
    gates = pack_spec["gates"]
    delta_no_log = float(gates["delta_without_log_deg"])
    delta_with_log = float(gates["delta_with_log_deg"])

    has_retard = bool(analyzer._compact(table["channels"].get("knock_retard") or []))
    has_knock_signal = any(zone["knock_signal"] for zone in grid.values())
    ratio_limit = float(no_knock["knock_spike_ratio_vs_median"])

    # Опорный уровень шума тракта: медиана напряжений по всему логу.
    all_signals = [value for zone in grid.values() for value in zone["knock_signal"]]
    reference = statistics.median(all_signals) if all_signals else 0.0

    zones: list[dict] = []
    limit_used = delta_with_log if has_retard else delta_no_log
    for (rpm_index, load_index), zone in sorted(grid.items()):
        if zone["samples"] < MIN_ZONE_SAMPLES:
            continue
        entry = {
            "index": {"rpm": rpm_index, "load": load_index},
            "zone_ru": zone["zone_ru"],
            "samples": zone["samples"],
            "wot_runs": len(zone["runs"]),
            "recommendation_ru": "",
            "delta_deg": 0.0,
            "confidence": "low",
        }
        if has_retard:
            worst = max(zone["knock_retard"])
            mean = statistics.fmean(zone["knock_retard"])
            entry["knock_retard_max_deg"] = round(worst, 3)
            entry["knock_retard_mean_deg"] = round(mean, 3)
            if worst > float(no_knock["max_retard_deg"]):
                entry["recommendation_ru"] = (
                    "детонация в зоне: добавка не рекомендуется (0°)"
                )
                zones.append(entry)
                continue
            if mean > float(no_knock["max_retard_deg"]) * NEAR_LIMIT_RATIO:
                delta = limit_used / 2.0
                entry["recommendation_ru"] = (
                    f"следы коррекции в зоне (средняя {mean:.2f}°): "
                    f"половинная добавка +{delta:g}°"
                )
            else:
                delta = limit_used
                entry["recommendation_ru"] = (
                    f"коррекции в зоне нет — допустима полная добавка +{delta:g}°"
                )
        elif has_knock_signal and reference > 0:
            peak = max(zone["knock_signal"])
            mean = statistics.fmean(zone["knock_signal"])
            ratio = peak / reference
            entry["knock_signal_peak_v"] = round(peak, 3)
            entry["knock_signal_ratio_vs_median"] = round(ratio, 3)
            if ratio >= ratio_limit:
                entry["recommendation_ru"] = (
                    f"пик напряжения детонации {ratio:.1f}× медианы — "
                    "добавка не рекомендуется (0°), нужен лог вердикта ЭБУ"
                )
                zones.append(entry)
                continue
            if ratio >= ratio_limit * NEAR_LIMIT_RATIO:
                delta = delta_no_log / 2.0
                entry["recommendation_ru"] = (
                    f"пики {ratio:.1f}× медианы — только половинная добавка "
                    f"(+{delta:g}°, нижняя граница: канала вердикта детонации нет)"
                )
            else:
                delta = delta_no_log
                entry["recommendation_ru"] = (
                    f"тракт спокоен (пики {ratio:.1f}× медианы) — допустима "
                    f"добавка до +{delta:g}° без лога вердикта"
                )
        else:
            delta = delta_no_log
            entry["recommendation_ru"] = (
                f"каналов детонации в зоне нет — только добавка без лога "
                f"(+{delta:g}°, граница проекта)"
            )
        if zone["iat"] and max(zone["iat"]) > float(spec["conditions"]["intake_temp_max_c"]):
            entry["recommendation_ru"] += "; впуск горячее порога — добавку отложить"
            delta = 0.0
        entry["delta_deg"] = round(delta, 3)
        entry["confidence"] = (
            "high" if (has_retard and len(zone["runs"]) >=
                       int(pack_spec["min_wot_runs"]))
            else "medium" if len(zone["runs"]) >= 1 else "low"
        )
        zones.append(entry)

    caps = {
        "max_add_deg_with_retard_log": delta_with_log,
        "max_add_deg_without_retard_log": delta_no_log,
        "knock_spike_ratio_vs_median": ratio_limit,
        "note_ru": (
            "рекомендация не обходит ворота генератора: RON91-карта не "
            "изменяется, защита по детонации сохраняется, сетка 0.375°"
        ),
    }
    return zones, caps


def _fuel_zones(grid: dict, spec: dict, table: dict) -> tuple[list[dict], dict]:
    criteria = spec["packages"]["fuel"]["criteria"]
    floor = float(criteria["lambda_target_min"])
    zones: list[dict] = []
    for (rpm_index, load_index), zone in sorted(grid.items()):
        if zone["samples"] < MIN_ZONE_SAMPLES or not zone["lambda"]:
            continue
        values = zone["lambda"]
        measured_min = min(values)
        measured_median = statistics.median(values)
        entry = {
            "index": {"rpm": rpm_index, "load": load_index},
            "zone_ru": zone["zone_ru"],
            "samples": zone["samples"],
            "wot_runs": len(zone["runs"]),
            "lambda_min": round(measured_min, 4),
            "lambda_median": round(measured_median, 4),
            "recommendation_ru": "",
            "enrichment_factor": 1.0,
            "confidence": "high" if len(zone["runs"]) >= 2 else "medium",
        }
        if measured_min < floor:
            entry["recommendation_ru"] = (
                f"λ min {measured_min:.3f} ниже пола {floor:.2f}: обогащать "
                "нельзя, нужен разбор переобогащения"
            )
            entry["enrichment_factor"] = 1.0
        else:
            # Насколько можно обогатить, пока худшая точка остаётся над полом.
            factor = min(measured_min / floor, 1.05)
            entry["enrichment_factor"] = round(factor, 4)
            entry["recommendation_ru"] = (
                f"λ min {measured_min:.3f} против пола {floor:.2f}: запас "
                f"×{factor:.3f} — обогащение в этих пределах, но не ниже пола"
            )
        zones.append(entry)
    caps = {
        "lambda_floor": floor,
        "lambda_stdev_max_wot": float(criteria["lambda_stdev_max_wot"]),
        "partial_load_trim_max_percent": float(criteria["partial_load_trim_max_percent"]),
        "note_ru": (
            "узкополосная лямбда коррекций не даёт: рекомендация строится "
            "только по внешней широкополосной λ"
        ),
    }
    return zones, caps


def _vanos_zones(grid: dict, spec: dict) -> tuple[list[dict], dict]:
    criteria = spec["packages"]["vanos"]["criteria"]
    track_limit = float(criteria["max_track_error_deg"])
    # Границы сдвига — из требований: ±max_shift_from_stock_deg от стока.
    shift_limit = float(criteria["max_shift_from_stock_deg"])
    zones: list[dict] = []
    for (rpm_index, load_index), zone in sorted(grid.items()):
        if zone["samples"] < MIN_ZONE_SAMPLES or not zone["track_error"]:
            continue
        errors = zone["track_error"]
        maximum = max(errors)
        median = statistics.median(errors)
        entry = {
            "index": {"rpm": rpm_index, "load": load_index},
            "zone_ru": zone["zone_ru"],
            "samples": zone["samples"],
            "wot_runs": len(zone["runs"]),
            "track_error_max_deg": round(maximum, 3),
            "track_error_median_deg": round(median, 3),
            "recommendation_ru": "",
            "shift_deg": 0.0,
            "confidence": "high" if len(zone["runs"]) >= 2 else "medium",
        }
        if maximum > track_limit:
            entry["recommendation_ru"] = (
                f"рассогласование до {maximum:.2f}° (предел {track_limit:g}°) — "
                "сдвигать цели нельзя, сначала механика VANOS"
            )
        else:
            delta = min(max(median, -shift_limit), shift_limit)
            entry["shift_deg"] = round(delta, 3)
            entry["recommendation_ru"] = (
                f"механика держит цель (максимум {maximum:.2f}°, медиана "
                f"{median:.2f}°) — допустим сдвиг цели {delta:+.2f}° в стабильной зоне"
            )
        zones.append(entry)
    caps = {
        "max_track_error_deg": track_limit,
        "max_shift_deg": shift_limit,
        "note_ru": "сдвиг только в зонах с устойчивым отслеживанием цели",
    }
    return zones, caps


def zone_delta(recommendation: dict, rpm: float, load: float,
               fallback_deg: float = 0.0) -> float:
    """Добавка для точки (rpm, load) по зонам рекомендации.

    Зона без данных не выдумывается: возвращается ``fallback_deg`` (по
    умолчанию 0 — «в этой зоне лог ничего не показал»).
    """
    rpm_index = _zone_of(rpm, RPM_EDGES)
    load_index = _zone_of(load, LOAD_EDGES)
    if rpm_index is None or load_index is None:
        return fallback_deg
    for zone in recommendation.get("zones") or []:
        index = zone.get("index")
        if not index:
            continue
        if (index.get("rpm") == rpm_index and index.get("load") == load_index
                and "delta_deg" in zone):
            return float(zone["delta_deg"])
    return fallback_deg


def deltas_for_map(recommendation: dict, rpm_axis: list[float],
                   load_axis: list[float],
                   fallback_deg: float = 0.0) -> list[float]:
    """Добавки к ячейкам карты (строки — обороты, столбцы — нагрузка).

    Порядок значений — построчный (row-major), как в политике версии.
    """
    deltas: list[float] = []
    for rpm in rpm_axis:
        for load in load_axis:
            deltas.append(zone_delta(recommendation, float(rpm), float(load),
                                     fallback_deg))
    return deltas


def load_band_index(load: float) -> int | None:
    """Номер полосы нагрузки в сетке зон рекомендации (``None`` — вне полос)."""
    return _zone_of(float(load), LOAD_EDGES)


def trusted_recommendation(evidence: dict | None, package: str,
                           analyzer_version: str = "") -> tuple[dict | None, str]:
    """Рекомендация доказательства, пригодная для записи в карты, и причина отказа.

    В запись идёт только рекомендация, посчитанная тем же анализатором и по тому
    же логу, что и само доказательство (``docs/PACKS_LOG_REQUIREMENTS_RU.md``
    §7.2: числа рекомендации переносить руками нельзя). Возвращает
    ``(рекомендация | None, причина отказа)`` — причина идёт в наряд/находки.
    """
    if not isinstance(evidence, dict):
        return None, "доказательство не приложено"
    recommendation = evidence.get("pack_recommendation")
    if not isinstance(recommendation, dict):
        return None, "в доказательстве нет pack_recommendation"
    if recommendation.get("kind") != "pack_recommendation":
        return None, (
            f"kind рекомендации {recommendation.get('kind')!r}, нужен 'pack_recommendation'"
        )
    if recommendation.get("package") != package:
        return None, (
            f"рекомендация для пакета {recommendation.get('package')!r}, нужен {package!r}"
        )
    version = recommendation.get("analyzer_version")
    if not version or version != evidence.get("analyzer_version"):
        return None, "версия анализатора рекомендации не совпадает с доказательством"
    if analyzer_version and version != analyzer_version:
        return None, (
            f"рекомендация посчитана версией {version!r}, текущая {analyzer_version!r}"
        )
    digest = recommendation.get("source_sha256")
    if not digest or digest != evidence.get("source_sha256"):
        return None, "рекомендация не привязана к sha256 лога доказательства"
    if recommendation.get("confidence") == "insufficient":
        return None, "в логе мало данных для рекомендации по зонам (confidence insufficient)"
    if not recommendation.get("zones"):
        return None, "в рекомендации нет зон с данными"
    return recommendation, ""


def zone_for_point(recommendation: dict, rpm: float, load: float) -> dict | None:
    """Зона рекомендации для точки (rpm, load) или ``None`` (зоны нет/нет данных)."""
    rpm_index = _zone_of(float(rpm), RPM_EDGES)
    load_index = _zone_of(float(load), LOAD_EDGES)
    if rpm_index is None or load_index is None:
        return None
    for zone in recommendation.get("zones") or []:
        index = zone.get("index") or {}
        if index.get("rpm") == rpm_index and index.get("load") == load_index:
            return zone
    return None


def zone_field(recommendation: dict, rpm: float, load: float, field: str):
    """Поле зоны для точки (rpm, load) или ``None`` (зоны/данных нет)."""
    zone = zone_for_point(recommendation, rpm, load)
    return None if zone is None else zone.get(field)


def lambda_margins_for_map(recommendation: dict, rpm_axis: list[float],
                           load_axis: list[float], floor: float,
                           requested: float) -> list[float]:
    """Допустимое обогащение λ по ячейкам карты (строки — обороты, столбцы — нагрузка).

    Запас зоны — измеренная λ min минус пол (``lambda_target_min``): насколько
    смесь может стать богаче, не пробив пол. Зона с λ ниже пола и зона без
    данных не обогащаются вовсе (0): у топлива «нет данных» — это не «можно
    консервативно», а «нельзя ничего», в отличие от зажигания (см. §7.3).
    """
    limits: list[float] = []
    for rpm in rpm_axis:
        for load in load_axis:
            measured = zone_field(recommendation, rpm, load, "lambda_min")
            if measured is None:
                limits.append(0.0)
                continue
            margin = max(float(measured) - float(floor), 0.0)
            # округление до 4 знаков: значения уходят в карту с более грубой сеткой
            limits.append(round(min(float(requested), margin), 4))
    return limits


def full_load_delta_by_rpm(recommendation: dict, rpm_axis: list[float],
                           floor: float, requested: float) -> list[float]:
    """Обогащение для карты полной нагрузки, индексированной оборотами (1×N).

    Берётся **минимум** по зонам с данными при этих оборотах (все полосы
    нагрузки): правка полной нагрузки не должна вывести за пол ни одну из
    измеренных полос. Обороты без данных не обогащаются (0) — карта полной
    нагрузки не покрывается безлоговой консервативной добавкой.
    """
    limits: list[float] = []
    zones = recommendation.get("zones") or []
    for rpm in rpm_axis:
        rpm_index = _zone_of(float(rpm), RPM_EDGES)
        if rpm_index is None:
            limits.append(0.0)
            continue
        margins = [
            max(float(zone["lambda_min"]) - float(floor), 0.0)
            for zone in zones
            if (zone.get("index") or {}).get("rpm") == rpm_index
            and "lambda_min" in zone
        ]
        limits.append(
            round(min(float(requested), min(margins)), 4) if margins else 0.0)
    return limits


def shift_limits_for_map(recommendation: dict, rpm_axis: list[float],
                         load_axis: list[float], requested: float,
                         opposite_margin_deg: float = 0.5) -> list[float]:
    """Допустимый сдвиг фаз по ячейкам карты (строки — обороты, столбцы — нагрузка).

    Лог фаз даёт зоне два доказательства: механика там держит цель
    (``track_error_max_deg`` в пределах предела) и какова медиана
    рассогласования. Правило:

    * зона без данных — сдвига нет (0): без лога фаз карта не двигается;
    * зона, где механика не держит цель, — сдвига нет: сначала ремонт, потом
      цели (веб-анализ §3.3);
    * если медиана рассогласования значимо указывает в **другую** сторону
      (больше ``opposite_margin_deg``), сдвиг в сторону заявки не подтверждён
      логом — 0; при медиане около нуля лог не различает стороны, и заявленная
      величина разрешена: доказано, что механика тянет цель в этом диапазоне
      нагрузок и оборотов, а предел ±5° от стока проверяет генератор.
    """
    sign = 1.0 if float(requested) >= 0 else -1.0
    cap = float((recommendation.get("limits") or {}).get("max_track_error_deg", 3.0))
    limits: list[float] = []
    for rpm in rpm_axis:
        for load in load_axis:
            zone = zone_for_point(recommendation, rpm, load)
            if zone is None or "shift_deg" not in zone:
                limits.append(0.0)
                continue
            if float(zone.get("track_error_max_deg") or 0.0) > cap:
                limits.append(0.0)
                continue
            median = float(zone.get("shift_deg") or 0.0)
            if abs(median) > float(opposite_margin_deg) and (median > 0) != (sign > 0):
                limits.append(0.0)
                continue
            limits.append(round(abs(float(requested)), 4))
    return limits


def deltas_for_load_axis(recommendation: dict, load_axis: list[float],
                         fallback_deg: float = 0.0) -> list[float]:
    """Добавки по столбцу нагрузки, когда ось оборотов карты не доказана.

    Ось оборотов карты может быть не найдена (например, MS45: доказана только
    ось нагрузки). Тогда берётся **минимум** по зонам этой полосы нагрузки —
    осторожный вариант: добавка не выше самой слабой зоны полосы, а полоса без
    данных не добавляет ничего.
    """
    result: list[float] = []
    for load in load_axis:
        load_index = _zone_of(float(load), LOAD_EDGES)
        if load_index is None:
            result.append(fallback_deg)
            continue
        values = [
            float(zone["delta_deg"]) for zone in recommendation.get("zones") or []
            if (zone.get("index") or {}).get("load") == load_index
            and "delta_deg" in zone
        ]
        result.append(min(values) if values else fallback_deg)
    return result


def build_recommendation(table: dict, pack: str, spec: dict,
                         fuel_ron: float | None = None) -> dict:
    """Рекомендация генератору по логу (JSON-совместимый словарь).

    Все числа — из лога и порогов спецификации; при неполных данных
    ``confidence`` падает, а зоны без выборок не попадают в результат вовсе
    (лучше «нет данных», чем выдуманное число).
    """
    from tools import pack_log_analyzer as analyzer

    if pack not in ("ignition", "fuel", "vanos"):
        raise ValueError(f"рекомендаций для пакета {pack!r} нет")
    warnings = _warnings(table, spec, pack)
    grid = _fill_zones(table, spec, pack)
    if pack == "ignition":
        zones, caps = _ignition_zones(grid, spec, fuel_ron, table)
    elif pack == "fuel":
        zones, caps = _fuel_zones(grid, spec, table)
    else:
        zones, caps = _vanos_zones(grid, spec)
    if pack == "ignition" and fuel_ron is not None and \
            float(fuel_ron) < float(spec["conditions"]["min_ron_for_ignition_evidence"]) \
            and any(zone.get("delta_deg", 0.0) > 0 for zone in zones):
        warnings.append(
            f"топливо RON {fuel_ron:g} ниже RON "
            f"{spec['conditions']['min_ron_for_ignition_evidence']} — "
            "добавка к карте RON98 по такому логу не рекомендуется"
        )
        for zone in zones:
            if zone.get("delta_deg"):
                zone["delta_deg"] = 0.0
                zone["recommendation_ru"] += "; топливо ниже RON 98 — добавка снята"
    enough = [zone for zone in zones if zone.get("delta_deg") or zone.get("shift_deg")
              or zone.get("enrichment_factor", 1.0) != 1.0]
    if not zones:
        confidence = "insufficient"
    elif pack == "ignition" and (warnings or not enough):
        confidence = "low"
    elif len(enough) == len(zones) and all(zone["confidence"] == "high" for zone in zones):
        confidence = "high"
    else:
        confidence = "medium"
    return {
        "kind": "pack_recommendation",
        "schema_version": 1,
        "analyzer_version": analyzer.ANALYZER_VERSION,
        "package": pack,
        "confidence": confidence,
        "zones": zones,
        "warnings": warnings,
        "limits": caps,
        "source_sha256": None,  # заполняет анализатор: хэш исходного лога
        "notes_ru": [
            "рекомендация действительна только вместе с исходным логом: "
            "генератор сверяет sha256 и версию анализатора",
            "генератор применяет свои границы: рекомендация их не обходит",
        ],
    }
