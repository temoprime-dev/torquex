# -*- coding: utf-8 -*-
"""Потолки добавки зажигания — единый источник констант (раунд 4).

Задание ``docs/TASK_STAGE1_RU.md`` §1: Stage 1 (v1, без логов) — до +3,0° на
карте RON98 для профиля RON98, до +1,5° для RON95, RON91 — 0; Stage 2 (v2+, с
логов детонации) — до +5,0°. Числа живут **только** в
``docs/pack_log_requirements.json`` (секции ``packages.ignition.gates``), этот
модуль — единственный читатель: генераторы MS43/MS45, октан-профиль
``tools/variant1.py``, советчик ``tools/pack_recommendation.py`` и CLI
``tools/torquex.py`` берут потолки отсюда. Тест ``tests/test_ignition_caps.py``
сверяет json с таблицей задания и следит, чтобы ни один модуль не хардкодил
своё число.

Смысл «профиль ограничивает запись в карту RON98»: ЭБУ MS4x считает базу
интерполяцией между картами RON91 и RON98 по выученному октану
(``docs/VARIANT1_WEB_ANALYSIS_RU.md`` §7). Поэтому запись +X° в карту RON98
при профиле RON95 даёт на рабочей точке только часть добавки (вес
интерполяции < 1) — потолок 1,5° для RON95 означает «не более ~0,9° на точке
95-го топлива», что и раньше соответствовало «максимуму без лога».
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = REPO_ROOT / "docs" / "pack_log_requirements.json"

#: Значения по умолчанию — только для сообщения об ошибке, если файл
#: specifications не читается: они намеренно не подставляются в работу.
FALLBACK_NOTE = (
    "docs/pack_log_requirements.json не читается — потолки зажигания взять "
    "неоткуда; почините файл спецификаций (он единый источник), молчаливых "
    "дефолтов здесь нет"
)

_UNSET = object()
_CACHE: dict | None = _UNSET


class CapSpecError(RuntimeError):
    """Спецификация потолков повреждена или противоречива."""


def _spec() -> dict:
    global _CACHE
    if _CACHE is _UNSET:
        try:
            _CACHE = json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # pragma: no cover - защита от поломки
            raise CapSpecError(f"{FALLBACK_NOTE}: {exc}") from exc
    return _CACHE


def reset_cache_for_tests() -> None:
    """Перечитать json (тестам — после подмены файла)."""
    global _CACHE
    _CACHE = _UNSET


def gates() -> dict:
    spec = _spec()
    try:
        return spec["packages"]["ignition"]["gates"]
    except KeyError as exc:
        raise CapSpecError(f"в спецификации нет packages.ignition.gates: {exc}") from exc


def layout() -> dict:
    spec = _spec()
    try:
        return spec["packages"]["ignition"]["layout"]
    except KeyError as exc:
        raise CapSpecError(f"в спецификации нет packages.ignition.layout: {exc}") from exc


def delta_without_log_deg() -> float:
    """Абсолютный потолок Stage 1 (без лога) для профиля RON98."""
    return float(gates()["delta_without_log_deg"])


def delta_with_log_deg() -> float:
    """Абсолютный потолок Stage 2 (с логом детонации) для профилей 98/95."""
    return float(gates()["delta_with_log_deg"])


def _profile_key(profile: str | None) -> str:
    if profile in (None, ""):
        return "unspecified"
    key = str(profile).strip().lower()
    return key if key in ("ron98", "ron95", "ron91") else "unspecified"


def cap_no_log_deg(profile: str | None) -> float:
    """Потолок без лога для октан-профиля клиента (Stage 1)."""
    table = gates()["octane_no_log_deg"]
    return min(float(table[_profile_key(profile)]), delta_without_log_deg())


def cap_with_log_deg(profile: str | None) -> float:
    """Потолок с проверенным логом детонации (Stage 2) для профиля клиента."""
    table = gates()["octane_with_log_deg"]
    return min(float(table[_profile_key(profile)]), delta_with_log_deg())


def cap_for(profile: str | None, has_verified_log: bool) -> float:
    """Действующий потолок добавки для профиля и наличия принятого лога."""
    return cap_with_log_deg(profile) if has_verified_log else cap_no_log_deg(profile)


def is_profile_rejected(profile: str | None) -> bool:
    """Профиль, в котором добавка зажигания запрещена в принципе (RON91)."""
    return cap_with_log_deg(profile) <= 0.0 and cap_no_log_deg(profile) <= 0.0


#: Имена профилей для текстов (RU).
PROFILE_TITLES = {"ron98": "RON98", "ron95": "RON95", "ron91": "RON91",
                  "unspecified": "без профиля"}


def table_ru() -> str:
    """Одна строка с текущими потолками — для нарядов и текстов отказа.

    Числа подставляются из спецификации, поэтому текст не может разойтись с
    воротами.
    """
    return (
        f"Stage 1 (без логов): до +{delta_without_log_deg():g}° на карте RON98 "
        f"при профиле RON98, до +{cap_no_log_deg('ron95'):g}° при RON95, при "
        f"RON91 карта не трогается; Stage 2 (с логом детонации): "
        f"до +{delta_with_log_deg():g}°"
    )


def expected_table() -> dict:
    """Ожидаемая таблица потолков — ровно §1 задания docs/TASK_STAGE1_RU.md.

    Нужна тестам: любое расхождение json с заданием падает здесь, а не в
    «правке числа по файлам».
    """
    return {
        "delta_without_log_deg": 3.0,
        "delta_with_log_deg": 5.0,
        "octane_no_log_deg": {"ron98": 3.0, "ron95": 1.5, "ron91": 0.0,
                              "unspecified": 1.5},
        "octane_with_log_deg": {"ron98": 5.0, "ron95": 5.0, "ron91": 0.0,
                                "unspecified": 5.0},
    }


def check_matches_task() -> None:
    """Бросает CapSpecError, если json разошёлся с таблицей задания."""
    g = gates()
    expected = expected_table()
    problems: list[str] = []
    for key in ("delta_without_log_deg", "delta_with_log_deg"):
        if float(g[key]) != float(expected[key]):
            problems.append(
                "%s=%r, ожидается %g" % (key, g[key], float(expected[key]))
            )
    for key in ("octane_no_log_deg", "octane_with_log_deg"):
        got = {k: float(v) for k, v in g[key].items()}
        want = {k: float(v) for k, v in expected[key].items()}
        if got != want:
            problems.append(f"{key}: {got} != {want}")
    if problems:
        raise CapSpecError("потолки в docs/pack_log_requirements.json разошлись "
                           "с таблицей §1 задания: " + "; ".join(problems))
