#!/usr/bin/env python3
"""Генератор MS45: Stage 1 / Stage 2 из заявки клиента.

Маршрут задания: **запрос клиента → план патча с обоснованием → готовый файл →
наряд**.  Здесь собирается всё вместе поверх ядра (``calibration.py``) и политики
(``policy.py``): генератор не пишет байты сам, он только формирует запросы к ядру.

Пакеты MS45 (в отличие от MS43, где есть патчлист кода, здесь **только**
калибровка):

=====================  ==================================================
пакет                  что делает
=====================  ==================================================
``limits``             ограничитель скорости ``c_vs_max`` и ограничитель
                       оборотов ``id_n_max_*`` (пара «basic» + «increased»)
``pops``               задержка отсечки ПХХ ``c_t_min_pu``, ``c_t_min_pu_vs``,
                       ``c_t_min_pu_rng_l`` («timered», без агрессии)
``launch_control``     **не реализуется**: калибровочного пути нет, а патч кода
                       заданием запрещён → ``REQUIRES_DATA``
``hardware`` (Stage 2) момент/впуск/форсунки под конкретное железо → набор
                       требований к стенду и логам, чисел нет
=====================  ==================================================

Ворота продукта (здесь, а не в ядре):

* выше стокового значения отсечки → ``acknowledge_risk``;
* отсечка выше 6900 rpm → ``acknowledge_damper`` (M54B30: нужен демпфер, ms4x.net);
* pops → ``no-catalyst`` + ``acknowledge_temperature``; агрессивный пресет запрещён;
* Stage 2 без блока ``hardware`` → отказ ``HARDWARE_REQUIRED``;
* ни одной цифры прироста мощности: без стенда обещаний нет.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from MS45.tools import calibration as cal
from MS45.tools import checksums as ck
from MS45.tools import pathfmt
from MS45.tools import policy as pl
from MS45.tools.console import prepare_stdout
# Общий анализатор логов (задание 1 ревью): доказательства пакетов принимает
# только ворота, сверяющие вывод анализатора с исходным CSV.
from tools import pack_log_analyzer  # noqa: E402
# Общие ворота варианта 1 (задание docs/TASK_VARIANT1_RU.md): профиль топлива,
# правовая справка, откат на сток, опции, ждущие данных.
from tools import variant1  # noqa: E402
# Рекомендация анализатора по зонам (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.2):
# добавка раскладывается по полосам нагрузки, а не по всей карте.
from tools import pack_recommendation  # noqa: E402
# Единый источник потолков добавки зажигания (задание docs/TASK_STAGE1_RU.md
# §2.1) и общая математика раскладки/сглаживания (§2а). MS45 не находит
# аналога MBT-карты с доказанной семантикой — раскладка по запасу до MBT в
# этом семействе честно REQUIRES_DATA, форма по нагрузке применяется.
from tools import ignition_caps  # noqa: E402
from tools import ignition_layout  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = ROOT / "firmware"
SPDATEN_DIR = ROOT / "firmware_spdaten" / "cal"

#: Стадии и их пакеты. Всё, что не выпускается, честно отвечает ``REQUIRES_DATA``
#: со списком того, что нужно измерить (``REQUIRES_DATA_CATALOG``).
STAGE_PACKAGES: dict[str, tuple[str, ...]] = {
    "stage1": (
        "limits",
        "pops",
        "pops_intensity",
        "cat_delete",
        "sap_delete",
        "idle_speed",
        "disa",
        "injectors",
        "e85",
        "launch_control",
        # Пакеты задания 3 (зажигание/топливо/ВАНос): выпускаются с воротами
        # по логам из docs/pack_log_requirements.json.
        "ignition",
        "fuel",
        "vanos",
        # Опции варианта 1 (docs/TASK_VARIANT1_RU.md §1): запрашиваются
        # отдельными намерениями и сегодня честно ждут данных клиента.
        "disa_off",
        "isa_off",
        "fan_thermostat",
        # Слой диагностики привода DISA (VIM) и клапана ХХ (ISA): доказанные
        # приращения счётчиков дребезга в калибровочном регионе (шаг 2 задания).
        "dtc_driver",
    ),
    "stage2": (
        "limits",
        "pops",
        "pops_intensity",
        "cat_delete",
        "sap_delete",
        "idle_speed",
        "disa",
        "injectors",
        "e85",
        "launch_control",
        "ignition",
        "fuel",
        "vanos",
        # Опции варианта 1 — и на Stage 2 (там они тем более ждут железа/логов).
        "disa_off",
        "isa_off",
        "fan_thermostat",
        # Слой диагностики привода DISA (VIM) и клапана ХХ (ISA): на Stage 2
        # доступен так же, как на Stage 1 (доказанные счётчики дребезга).
        "dtc_driver",
        "hardware",
    ),
}

PACKAGE_TITLES = {
    "limits": "Ограничители (скорость и обороты)",
    "vmax": "Ограничитель скорости",
    "rev_limit": "Ограничитель оборотов",
    "pops": "Хлопки на сбросе газа (задержка отсечки ПХХ)",
    "pops_intensity": "Интенсивность хлопков (углы зажигания на ПХХ)",
    "cat_delete": "Удаление катализатора: диагностика",
    "sap_delete": "Удаление системы продувки (SAP)",
    "idle_speed": "Обороты холостого хода",
    "disa": "Управление DISA (изменяемая геометрия впуска)",
    "injectors": "Форсунки: масштабирование под другое железо",
    "e85": "Топливо E85",
    "launch_control": "Launch control",
    "ignition": "Зажигание (базовая карта RON98)",
    "fuel": "Топливо (лямбда полной нагрузки)",
    "vanos": "VANOS (фазы распредвалов, частичная нагрузка)",
    "hardware": "Stage 2: момент, впуск, форсунки под железо",
    "disa_off": "DISA off (фиксация заслонок + диагностика)",
    "isa_off": "ISA off / обход клапана холостого хода",
    "fan_thermostat": "Вентилятор и термостат (трек-режим)",
    "dtc_driver": "Слой диагностики привода DISA (VIM) и клапана ХХ (ISA)",
}

#: Намерения заявки -> пакет.
INTENT_PACKAGES = {
    "vmax": "limits",
    "rev_limit": "limits",
    "pops": "pops",
    "pops_intensity": "pops_intensity",
    "cat_delete": "cat_delete",
    "sap_delete": "sap_delete",
    "idle_speed": "idle_speed",
    "disa": "disa",
    "injectors": "injectors",
    "e85": "e85",
    "launch_control": "launch_control",
    "ignition": "ignition",
    "fuel": "fuel",
    "vanos": "vanos",
    "stage2": "hardware",
    # Вариант 1 (docs/TASK_VARIANT1_RU.md): «off»-опции — пакеты, ждущие данных.
    "disa_off": "disa_off",
    "dtc_driver": "dtc_driver",
    "isa_off": "isa_off",
    "fan_thermostat": "fan_thermostat",
}

#: Намерения-профили: не пишут сами, а задают выбор клиента (ворота считаются
#: до пакетов). ``octane`` — профиль топлива RON91/95/98 из варианта 1.
PROFILE_KINDS = frozenset({"octane"})

#: Задержка ПХХ по умолчанию (с) и допустимый диапазон генератора.
DEFAULT_POPS_DELAY_S = 0.5
POPS_DELAY_RANGE_S = (0.05, 1.5)

#: Выше этой отсечки нужен демпфер крутильных колебаний (ms4x.net про M54B30).
DAMPER_ACK_RPM = 6900

#: Каталог функций, которые мы НЕ выпускаем, и что именно нужно, чтобы выпустить.
#: Причина всегда конкретная; символы и адреса — из ``docs/CALIBRATION_MAP_RU.md``
#: (раздел «осознанно не разрешено») и ``docs/ANALYSIS_WEB_RU.md``.
REQUIRES_DATA_CATALOG: dict[str, dict] = {
    "vmax": {
        "why": (
            "для этого SW-ID не доказаны собственные адрес и масштаб ограничителя скорости; "
            "адреса других семейств и неподписанных по версии XDF запрещено переносить"
        ),
        "needs": (
            "версионно привязанный кандидат c_vs_max или независимое структурное доказательство",
            "проверка байта/скейла на всех стоках именно этого семейства",
            "отказ на чужих версиях и подтверждение, что запись остаётся внутри калибровки",
        ),
    },
    "rev_limit": {
        "why": (
            "для выбранной трансмиссии не доказана полная пара обычного и повышенного "
            "лимитера в стоках этого семейства"
        ),
        "needs": (
            "две связанные 9-элементные BE-u16 таблицы выбранной трансмиссии",
            "парное сравнение всех передач на каждом стоке семьи и проверка относительно c_n_lim_min",
            "версионный отказ на чужих SW-ID и тест записи только плановых ячеек",
        ),
    },
    "pops": {
        "why": (
            "для этого SW-ID не доказана собственная группа задержек ПХХ; значения и адреса "
            "других семейств переносить нельзя"
        ),
        "needs": (
            "три задержки ПХХ и отдельный read-only таймер MSR в непрерывной группе",
            "проверка единиц/скейла и стоковых границ на всех файлах семьи",
            "подтверждения no_catalyst и температуры остаются обязательными",
        ),
    },
    "pops_intensity": {
        "why": (
            "углы зажигания на частичных нагрузках (ip_iga_bas_ron_91/98__n__maf) "
            "влияют на всю область и на мониторинг момента; интенсивность «на глаз» "
            "означала бы выдумывание"
        ),
        "needs": (
            "логи зажигания и момента до/после (INPA/Testo), чтобы увидеть, какие "
            "ячейки реально участвуют при сбросе газа",
            "стендовый замер температуры выпуска и нагрузки на детали при выбранной "
            "интенсивности",
            "подтверждение, что штатный контроль детонации остаётся активен",
        ),
    },
    "idle_speed": {
        "why": (
            "таблицы холостого хода (ip_n_sp_is и родственные) правятся только вместе "
            "с проверкой устойчивости холостого хода и адаптаций"
        ),
        "needs": (
            "замер холостого хода на прогретом и холодном двигателе (до/после)",
            "проверка адаптаций и отсутствия «плавания» оборотов",
            "подтверждение, что климат/кондиционер и нагрузка не выводят обороты за пределы",
        ),
    },
    "disa": {
        "why": (
            "управление DISA (c_fac_maf_max_vim, c_n_vim_l/h, c_vimpwm_*) привязано к "
            "конкретному коллектору; без замера наполнения — выдумывание"
        ),
        "needs": (
            "артикул коллектора и подтверждение, что заслонка исправна",
            "логи наполнения (MAF) на низких и высоких оборотах до/после",
            "решение: оставить заводскую логику переключения или менять пороги",
        ),
    },
    "injectors": {
        "why": (
            "масштабирование форсунок (c_fac_mff_ti_stnd_1/2, c_ti_as) требует точных "
            "характеристик новых форсунок; ошибка здесь = бедная/богатая смесь"
        ),
        "needs": (
            "артикул форсунок и их производительность (cc/min или lb/h) с источником",
            "логи топливных коррекций (краткосрочные/долгосрочные) до/после",
            "замер смеси широкополосным датчиком на нагрузке (или стенд)",
        ),
    },
    "e85": {
        "why": (
            "перевод на E85 требует правки состава смеси (c_mff_fac) и пусковых "
            "обогащений; без датчика состава и замеров это опасно для двигателя"
        ),
        "needs": (
            "датчик состава топлива (или решение работать на смеси фиксированной пропорции)",
            "замеры смеси под нагрузкой и на пуске при низких температурах",
            "подтверждение, что холодный пуск и прогрев остаются в допуске",
        ),
    },
    "launch_control": {
        "why": (
            "калибровочного пути launch control для MS45 не найдено: все lc_* в "
            "определениях — это Learning/Control, а сама функция в MS43 делается "
            "патчем программного кода, что запрещено заданием"
        ),
        "needs": (
            "калибровочный путь launch control для MS45 не подтверждён; в MS45 нет "
            "доказанного символа, а в MS43 функция делается патчем кода",
            "отдельное решение владельца о патче кода (выходит за рамки «правим только "
            "калибровку»)",
            "если решение принято — своя линия доказательств для программы, а не для "
            "калибровки",
        ),
    },
    "hardware": {
        "why": (
            "Stage 2 привязан к конкретному железу: без профиля и замеров любые "
            "значения момента и состава смеси были бы выдуманы"
        ),
        "needs": (
            "профиль железа (впуск/ДМРВ/форсунки/наддув) с точными артикулами",
            "стендовые замеры или логи нагрузки/смеси до и после установки железа",
            "подтверждение допустимости по детонации и температуре выпуска",
        ),
    },
    # Опции варианта 1 (docs/TASK_VARIANT1_RU.md §1, веб-анализ §5/§6/§8):
    # формулировки «почему» и «что нужно» — общие с MS43 (tools/variant1.py).
    "disa_off": {
        "why": (
            "в MS45 положения заслонки в калибровке нет: сообщество отключает VIM "
            "не «константой», а сведением vim- и не-vim-таблиц (наполнение, "
            "эффективный объём) и гашением счётчика ошибки привода. Пары таблиц "
            "«vim / не-vim» на стоках 4560BO00S и 4570LO00S мы ещё не доказали, "
            "а переносить адреса с MS43 нельзя. Что уже сделано: подавление "
            "счётчика ошибки привода — отдельный слой "
            "(намерение dtc_driver, группы vim/isa, с правовой справкой и "
            "подтверждением механики)"
        ),
        "needs": (
            "доказательство пар vim/не-vim таблиц на стоках семейства (какие "
            "именно таблицы наполнения и эффективного объёма свести и к какому "
            "значению) — процедура проекта: адреса проверяются по образам "
            "семейства, а не переносятся",
            "подтверждение механики: заслонка снята с заглушкой окна или "
            "зафиксирована, каким способом и в каком положении",
            "лог наполнения/момента до и после (у MS45 положение заслонки "
            "измеряется job-ом STATUS_VIMDISA — пресет disa_off)",
            "отдельное решение о подавлении диагностики (слой dtc_driver): оно "
            "выпускается, но функцию не отключает",
        ),
    },
    "isa_off": {
        "why": variant1.option_why("isa_off"),
        "needs": variant1.option_needs("isa_off"),
    },
    "fan_thermostat": {
        "why": variant1.option_why("fan_thermostat"),
        "needs": variant1.option_needs("fan_thermostat"),
    },
}


@dataclass
class PackageResult:
    """Результат одного пакета."""

    id: str
    title: str
    status: str  # written | requires_data | not_requested | rejected
    detail: str = ""
    needs: tuple[str, ...] = ()
    requests: tuple[dict, ...] = ()
    #: Для ``requires_data``: код требования и причина (машинно-читаемо).
    manifest: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "needs": list(self.needs),
            "requests": list(self.requests),
            "requires_data": self.manifest or None,
        }


@dataclass
class StageResult:
    """Результат сборки стадии."""

    stage: str
    status: str  # BUILT | REQUIRES_DATA | REJECTED
    source_sha256: str
    policy_version: int
    customer: str
    notes: str
    packages: tuple[PackageResult, ...]
    family: str | None = None
    #: Профиль топлива варианта 1 (``ron91``/``ron95``/``ron98``); None — не заявлен.
    octane_profile: str | None = None
    plan: cal.PatchPlan | None = None
    result: cal.PatchResult | None = None
    findings: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    @property
    def written_packages(self) -> tuple[PackageResult, ...]:
        return tuple(item for item in self.packages if item.status == "written")

    @property
    def pending(self) -> tuple[PackageResult, ...]:
        return tuple(item for item in self.packages if item.status == "requires_data")


def _finding(items: list[dict], code: str, severity: str, message: str) -> None:
    items.append({"code": code, "severity": severity, "message": message})


def _entry(policy: pl.FamilyPolicy, symbol: str, findings: list[dict]):
    try:
        return policy.entry(symbol)
    except pl.PolicyError as error:
        _finding(findings, error.code, "reject", str(error))
        return None


def _current(image: bytes, entry: pl.PolicyEntryData, cal_offset: int, index: int = 0) -> float:
    size = entry.element_size_bytes
    start = cal_offset + entry.address + index * size
    raw = int.from_bytes(image[start : start + size], "big")
    return entry.to_engineering(raw)


def limits_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    findings: list[dict],
) -> PackageResult:
    """Ограничители: скорость и обороты."""
    requests: list[dict] = []
    details: list[str] = []
    failed = False
    for intent in intents:
        kind = intent.get("kind")
        if kind == "vmax":
            entry = _entry(family_policy, "c_vs_max", findings)
            if entry is None:
                failed = True
                continue
            value = intent.get("value")
            if value is None:
                _finding(findings, "VALUE_REQUIRED", "reject", "vmax: не указано значение (км/ч)")
                failed = True
                continue
            before = _current(image, entry, cal_offset)
            requests.append({"symbol": "c_vs_max", "values": [float(value)]})
            details.append(f"c_vs_max: {before:g} -> {float(value):g} км/ч")
        elif kind == "rev_limit":
            transmission = str(intent.get("transmission", "mt")).lower()
            if transmission not in ("mt", "at", "amt"):
                _finding(
                    findings,
                    "TRANSMISSION_INVALID",
                    "reject",
                    f"rev_limit: неизвестная КПП {transmission!r} (нужно mt, at или amt)",
                )
                failed = True
                continue
            value = intent.get("value")
            if value is None:
                _finding(findings, "VALUE_REQUIRED", "reject", "rev_limit: не указаны обороты")
                failed = True
                continue
            value = float(value)
            stock_high = max(
                _current(image, family_policy.entry(f"id_n_max_{transmission}"), cal_offset),
                _current(image, family_policy.entry(f"id_n_max_h_{transmission}"), cal_offset),
            )
            if value > stock_high and not intent.get("acknowledge_risk"):
                _finding(
                    findings,
                    "ACKNOWLEDGE_REQUIRED",
                    "reject",
                    f"rev_limit: {value:g} rpm выше стока ({stock_high:g} rpm) — "
                    "нужно acknowledge_risk: true у намерения",
                )
                failed = True
                continue
            if value > DAMPER_ACK_RPM and not intent.get("acknowledge_damper"):
                _finding(
                    findings,
                    "ACKNOWLEDGE_REQUIRED",
                    "reject",
                    f"rev_limit: {value:g} rpm выше {DAMPER_ACK_RPM} rpm — для M54B30 нужен "
                    "демпфер крутильных колебаний (ms4x.net), нужно acknowledge_damper: true",
                )
                failed = True
                continue
            for symbol in (f"id_n_max_{transmission}", f"id_n_max_h_{transmission}"):
                entry = _entry(family_policy, symbol, findings)
                if entry is None:
                    continue
                before = _current(image, entry, cal_offset)
                requests.append({"symbol": symbol, "values": [value]})
                details.append(f"{symbol}: {before:g} -> {value:g} rpm")
    if not requests:
        status = "rejected" if failed else "not_requested"
        return PackageResult("limits", PACKAGE_TITLES["limits"], status)
    return PackageResult(
        "limits", PACKAGE_TITLES["limits"], "written", "; ".join(details), requests=tuple(requests)
    )


def pops_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    request: dict,
    findings: list[dict],
) -> PackageResult:
    """Хлопки: задержка отсечки ПХХ (неагрессивный режим)."""
    pops = [intent for intent in intents if intent.get("kind") == "pops"]
    if not pops:
        return PackageResult("pops", PACKAGE_TITLES["pops"], "not_requested")
    profile = str(pops[0].get("profile", "light")).lower()
    forbidden = ("aggressive", "extreme", "flames", "hard", "gunshot")
    if profile in forbidden:
        _finding(
            findings,
            "POPS_PROFILE_FORBIDDEN",
            "reject",
            f"pops: профиль {profile!r} не выпускается — только «light»/«medium» "
            "(агрессивные хлопки не выпускаются)",
        )
        return PackageResult("pops", PACKAGE_TITLES["pops"], "rejected", "профиль запрещён")
    delay = float(pops[0].get("delay_s", DEFAULT_POPS_DELAY_S))
    low, high = POPS_DELAY_RANGE_S
    if not low <= delay <= high:
        _finding(
            findings,
            pl.CODE_POLICY_RANGE_VIOLATION,
            "reject",
            f"pops: задержка {delay:g} с вне диапазона {low:g}..{high:g} с",
        )
        return PackageResult("pops", PACKAGE_TITLES["pops"], "rejected", "задержка вне диапазона")
    if not request.get("no_catalyst") and not pops[0].get("no_catalyst"):
        _finding(
            findings,
            "ACKNOWLEDGE_REQUIRED",
            "reject",
            "pops: нужно подтверждение no_catalyst: true (хлопки только без катализатора)",
        )
        return PackageResult("pops", PACKAGE_TITLES["pops"], "rejected", "нет подтверждения")
    if not request.get("acknowledge_temperature"):
        _finding(
            findings,
            "ACKNOWLEDGE_REQUIRED",
            "reject",
            "pops: нужно acknowledge_temperature: true (следить за температурой выпуска)",
        )
        return PackageResult("pops", PACKAGE_TITLES["pops"], "rejected", "нет подтверждения")

    symbols = family_policy.packs.get("pops", ())
    if not symbols or any(symbol not in family_policy.objects for symbol in symbols):
        item = requires_data_package("pops")
        _finding(
            findings,
            str(item.manifest["code"]),
            "info",
            f"{PACKAGE_TITLES['pops']}: не выпускается — {item.detail}",
        )
        return item

    requests = []
    details = []
    for symbol in symbols:
        entry = _entry(family_policy, symbol, findings)
        if entry is None:
            continue
        before = _current(image, entry, cal_offset)
        requests.append({"symbol": symbol, "values": [delay]})
        details.append(f"{symbol}: {before:g} -> {delay:g} с")
    if not requests:
        return PackageResult("pops", PACKAGE_TITLES["pops"], "rejected", "символы недоступны")
    return PackageResult(
        "pops", PACKAGE_TITLES["pops"], "written", "; ".join(details), requests=tuple(requests)
    )


def requires_data_package(package: str) -> PackageResult:
    """Пакет, который не собирается без стенда/логов: причина и список требований."""
    item = REQUIRES_DATA_CATALOG.get(package, {})
    return PackageResult(
        package,
        PACKAGE_TITLES.get(package, package),
        "requires_data",
        item.get("why", "не выпускается: нужны данные (см. требования)"),
        tuple(item.get("needs", ())),
        (),
        {"code": f"{package.upper()}_REQUIRES_DATA", "why": item.get("why", "")},
    )


# ---------------------------------------------------------------------------
# Пакеты задания 3: зажигание / топливо / ВАНос
#
# Ворота и пороги — единые с MS43: машиночитаемая спецификация
# ``docs/pack_log_requirements.json`` и человекочитаемый документ
# ``docs/PACKS_LOG_REQUIREMENTS_RU.md``. Отличия семейства:
#   * аналога MBT-карты с доказанной семантикой в MS45 нет (кандидат
#     ``ip_iga_ref__n__maf`` отклонён — см. ``docs/STAGE1_REPORT_RU.md``):
#     раскладка по запасу до MBT — REQUIRES_DATA, добавка ограничена
#     абсолютными воротами стадии из общей спецификации (Stage 1 до +3.0° для
#     RON98 и +1.5° для RON95 без лога, Stage 2 с логом до +5.0°) и формой по
#     нагрузке;
#   * малой топливной карты (аналога ``ip_tib``) без лога не двигаем: на
#     прогрев и переходные режимы она влияет сильнее;
#   * обогащение полной нагрузки разрешено только там, где доказаны ворота
#     диагностики богатой смеси (``c_lamb_min_ls_act_test_afr``, 4570LO00S).
# ---------------------------------------------------------------------------

PACK_REQUIREMENTS_PATH = Path(__file__).resolve().parents[2] / "docs" / "pack_log_requirements.json"
_PACK_REQUIREMENTS: dict | None = None


def load_pack_requirements() -> dict:
    """Машиночитаемые требования к логам (единый источник порогов)."""
    global _PACK_REQUIREMENTS
    if _PACK_REQUIREMENTS is None:
        _PACK_REQUIREMENTS = json.loads(PACK_REQUIREMENTS_PATH.read_text(encoding="utf-8"))
    return _PACK_REQUIREMENTS


def _load_evidence(intent: dict, findings: list[dict]) -> dict | None:
    """Доказательство из заявки: словарь или путь к JSON-файлу."""
    raw = intent.get("evidence")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(Path(str(raw)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _finding(
            findings,
            "EVIDENCE_UNREADABLE",
            "reject",
            f"{intent.get('id')}: файл доказательства {raw!r} не читается: {exc}",
        )
        return None


def evidence_gate(evidence: dict | None, package: str) -> tuple[bool, tuple[str, ...]]:
    """Задание 1 ревью: доказательство принимается, только если оно выдано
    анализатором ``tools/pack_log_analyzer.py``.

    Ручной ``pack_log``-JSON отклоняется отдельным кодом
    ``EVIDENCE_MANUAL_JSON``; результат анализатора сверяется с исходным
    логом (поиск файла, sha256, пересчёт метрик). Пороговые проверки —
    отдельно в ``validate_pack_evidence``.
    """
    return pack_log_analyzer.verify_evidence(
        evidence, package, search_dirs=(str(_REPO_ROOT),)
    )


def validate_pack_evidence(
    evidence: dict | None, package: str, spec: dict | None = None
) -> tuple[bool, tuple[str, ...]]:
    """Проверка лога-доказательства по порогам из спецификации.

    Структурные измерения из клиентского лога сверяются с порогами; булево
    «всё хорошо» доказательством не считается.
    """
    spec = spec or load_pack_requirements()
    problems: list[str] = []
    if evidence is None:
        return False, ("доказательство не приложено",)
    if evidence.get("kind") != "pack_log":
        return False, (f"kind должен быть 'pack_log', получено {evidence.get('kind')!r}",)
    if int(evidence.get("schema_version") or 0) != 1:
        problems.append(f"schema_version должна быть 1, получено {evidence.get('schema_version')!r}")
    if evidence.get("package") != package:
        problems.append(
            f"доказательство для пакета {evidence.get('package')!r}, нужен {package!r}"
        )
    if not str(evidence.get("source") or "").strip():
        problems.append("не указан источник лога (поле source)")
    results = evidence.get("results") or {}
    conditions = spec.get("conditions", {})
    if package == "ignition":
        no_knock = spec["packages"]["ignition"]["no_knock"]
        ron = evidence.get("fuel_ron")
        if ron is None or float(ron) < float(conditions["min_ron_for_ignition_evidence"]):
            problems.append(
                f"топливо должно быть не ниже RON {conditions['min_ron_for_ignition_evidence']}"
            )
        fraction = results.get("no_knock_fraction")
        if fraction is None or float(fraction) < float(no_knock["min_fraction_above_load"]):
            problems.append(
                f"доля выбросов без детонации (коррекция ≤ {no_knock['max_retard_deg']}°) "
                f"должна быть ≥ {no_knock['min_fraction_above_load']}"
            )
        sustained = results.get("max_sustained_retard_seconds")
        if sustained is None or float(sustained) >= float(no_knock["max_sustained_seconds"]):
            problems.append(
                f"длительность удержания коррекции ≥ {no_knock['max_sustained_retard_deg']}° "
                f"должна быть < {no_knock['max_sustained_seconds']} с"
            )
        spikes = results.get("knock_spike_events")
        if spikes is None or int(spikes) != 0:
            problems.append(
                f"одиночных пиков напряжения детонации выше {no_knock['knock_spike_ratio_vs_median']}× "
                "медианы быть не должно"
            )
        iat = results.get("max_iat_c")
        if iat is None or float(iat) > float(conditions["intake_temp_max_c"]):
            problems.append(
                f"температура воздуха на впуске в заезде ≤ {conditions['intake_temp_max_c']} °C"
            )
    elif package == "fuel":
        crit = spec["packages"]["fuel"]["criteria"]
        stdev = results.get("lambda_stdev_wot")
        if stdev is None or float(stdev) > float(crit["lambda_stdev_max_wot"]):
            problems.append(
                f"стандартное отклонение лямбды на полном ходу ≤ {crit['lambda_stdev_max_wot']}"
            )
        trims = results.get("partial_load_trim_max_percent")
        if trims is None or float(trims) > float(crit["partial_load_trim_max_percent"]):
            problems.append(
                "тримы частичной нагрузки в установившемся фрагменте "
                f"≤ ±{crit['partial_load_trim_max_percent']} %"
            )
        floor = results.get("lambda_floor")
        if floor is None or float(floor) < float(crit["lambda_target_min"]):
            problems.append(
                f"измеренный минимум лямбды ≥ {crit['lambda_target_min']} (ниже — переобогащение)"
            )
    elif package == "vanos":
        crit = spec["packages"]["vanos"]["criteria"]
        track = results.get("max_track_error_deg")
        if track is None or float(track) > float(crit["max_track_error_deg"]):
            problems.append(
                f"максимальное рассогласование факт/цель по фазам ≤ {crit['max_track_error_deg']}° "
                "(иначе механика не держит цели и править их бессмысленно)"
            )
        hold = spec["packages"]["vanos"]["hold_points"]
        hold_seconds = results.get("min_hold_seconds")
        if hold_seconds is None or float(hold_seconds) < float(hold["min_hold_seconds"]):
            problems.append(
                f"удержание каждой точки установившегося режима ≥ {hold['min_hold_seconds']} с"
            )
    else:
        problems.append(f"неизвестный пакет {package!r}")
    return not problems, tuple(problems)


#: Что нужно измерить, когда пакет ждёт данных (соответствует
#: ``docs/PACKS_LOG_REQUIREMENTS_RU.md``, разделы 3–5).
PACK_EVIDENCE_NEEDS = {
    "ignition": (
        "лог детонации по цилиндрам: ≥3 полных разгона 2000→ограничитель, ≥10 Гц",
        "коррекция по детонации ≤0.75° на ≥99% выбросов, без удержаний ≥1.5° дольше 0.5 с",
        "топливо не ниже RON 98, температура воздуха на впуске ≤45 °C",
    ),
    "fuel": (
        "широкополосная лямбда: ≥2 полных разгона + установившаяся частичная нагрузка ≥10 с",
        "стандартное отклонение лямбды на полном ходу ≤0.03, тримы частичной нагрузки ≤±8%",
    ),
    "vanos": (
        "лог фактических и целевых фаз валов (впуск/выпуск), ≥10 Гц",
        "сетка установившихся точек 2000..6000 об/мин × 150..600 мг/такт, удержание ≥3 с",
        "рассогласование факт/цель ≤3° (иначе механика не держит цели)",
    ),
}

#: Пределы добавок пакета зажигания (°): без лога / с логом детонации.
#: Это общие потолки «полного права» (профиль RON98); для RON95 потолок без
#: лога ниже — его считает ``ignition_caps.cap_no_log_deg`` по профилю
#: клиента. Числа живут в docs/pack_log_requirements.json, здесь только
#: алиасы, чтобы тексты и тесты могли сослаться на модуль.
IGNITION_DELTA_NO_LOG_DEG = ignition_caps.delta_without_log_deg()
IGNITION_DELTA_MAX_DEG = ignition_caps.delta_with_log_deg()
#: Квант сетки карт угла и фаз (0.375°) — все правки прижимаются к сетке.
IGNITION_QUANTUM_DEG = ignition_caps.layout()["quantum_deg"]
#: Обогащение полной нагрузки: максимум сдвига λ и пол (ниже — не выпускаем).
FUEL_ENRICH_MAX_LAMBDA = 0.03
FUEL_LAMBDA_FLOOR = 0.82
#: Сдвиг фаз от стока (°), прижат к сетке: 13 шагов по 0.375°.
VANOS_SHIFT_MAX_DEG = 5.0
VANOS_SHIFT_GRID_DEG = 4.875

# Форма добавки зажигания по нагрузке (задание 3 ревью, решение и источники —
# в ``docs/PACKS_WEB_ANALYSIS_RU.md`` §7): плоская добавка во все 320 ячеек
# неверна — на малых нагрузках (ХХ, ПХХ, лёгкий круиз) она даёт рост NOx и
# риск нестабильного холостого хода без прироста момента, поэтому добавка
# начинается только со средней нагрузки и выходит на полную к зоне полной
# нагрузки. Пороги — по оси нагрузки самой карты:
#   * ниже 200 мг/такт добавки нет (сообщество начинает добавку угла примерно
#     с 250 мг/такт — e46fanatics, «M54 E-85 Tuning (N.A) MS43»; ниже
#     100 мг/такт живут режимы ПХХ/хлопков — ms4x.net, Siemens MS45);
#   * от 200 до 600 мг/такт — линейный рост;
#   * 600 мг/такт и выше (полная нагрузка; окно «газ в пол» в спецификации
#     логов начинается уже с 400 мг/такт) — полная добавка.
# Пороги — в docs/pack_log_requirements.json (packages.ignition.layout), это
# только алиасы для текстов наряда и тестов: форма считается тем же общим
# кодом, что и у MS43 (tools/ignition_layout.py).
IGNITION_SHAPE_LOAD_LOW_MG = ignition_caps.layout()["shape_low_mg_stroke"]
IGNITION_SHAPE_LOAD_HIGH_MG = ignition_caps.layout()["shape_high_mg_stroke"]
#: Масштаб оси нагрузки (мг/такт) из формулы оси ``0.021195 * X`` определений
#: (одинаков у всех карт ``__n__maf`` семейства).
IGN_MAF_AXIS_SCALE = 0.021195


#: Оси карт, которые политика семейства отдаёт как отдельные объекты (только
#: чтение). Адреса — из ``<XDFAXIS mmedaddress=...>`` определения семейства
#: ``_116K`` (у каждой версии свои), поэтому берутся они у политики, а не
#: переносятся между версиями (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.3).
FUEL_RPM_AXIS = "axis_ip_lamb_fl__n_x"
VANOS_RPM_AXIS = "axis_ip_cam_sp_2_in_pl__n__maf_ivvt_y"
VANOS_LOAD_AXIS = "axis_ip_cam_sp_2_in_pl__n__maf_ivvt_x"


def _policy_axis(
    image: bytes, family_policy: pl.FamilyPolicy, cal_offset: int, symbol: str
) -> list[float] | None:
    """Значения оси карты из политики семейства (только чтение) или ``None``."""
    entry = family_policy.objects.get(symbol)
    if entry is None or entry.rows * entry.columns < 2:
        return None
    return _read_all_ms45(image, entry, cal_offset)


def _fuel_zone_limits(
    evidence: dict | None, image: bytes, family_policy: pl.FamilyPolicy, cal_offset: int,
    entry: pl.PolicyEntryData, floor: float, requested: float, findings: list[dict],
) -> tuple[list[float] | None, str | None]:
    """Пределы обогащения по столбцам оборотов карты ``ip_lamb_fl__n``.

    Ряд карты обогащения индексирован оборотами: предел столбца — минимум
    запаса λ (измеренная λ min зоны минус пол) по зонам лога с этими оборотами.
    Обороты, которых лог не покрыл, не обогащаются вовсе: у топлива «нет
    данных» — это «нельзя», а не «консервативно можно» (§7.3).
    """
    recommendation, why = pack_recommendation.trusted_recommendation(
        evidence, "fuel", pack_log_analyzer.ANALYZER_VERSION
    )
    if recommendation is None:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            f"fuel: раскладка обогащения по оборотам не применена — {why}; "
            f"обогащение плоское ≤ −{requested:g} λ",
        )
        return None, None
    axis = _policy_axis(image, family_policy, cal_offset, FUEL_RPM_AXIS)
    if axis is None or len(axis) != entry.rows * entry.columns:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            "fuel: ось оборотов карты обогащения не найдена или не совпала с картой "
            f"({0 if axis is None else len(axis)} против {entry.rows * entry.columns}); "
            f"обогащение плоское ≤ −{requested:g} λ",
        )
        return None, None
    limits = pack_recommendation.full_load_delta_by_rpm(
        recommendation, axis, floor, requested
    )
    untouched = sum(1 for value in limits if value <= 0)
    digest = str(recommendation.get("source_sha256") or "")
    note = (
        f"раскладка обогащения по оборотам (confidence {recommendation.get('confidence')}, "
        f"sha256 {digest[:12]}…): запас до пола λ {floor:g} по зонам лога; "
        f"{untouched} столбцов без данных не обогащены, максимум −{max(limits):g} λ"
    )
    return limits, note


def _vanos_zone_limits(
    evidence: dict | None, image: bytes, family_policy: pl.FamilyPolicy, cal_offset: int,
    entry: pl.PolicyEntryData, requested: float, findings: list[dict],
) -> tuple[list[float] | None, str | None]:
    """Пределы сдвига фаз по зонам (обороты × нагрузка) карты ВАНос.

    Сдвиг разрешён только в зонах, где лог подтвердил сторону сдвига; зона без
    данных, зона с рассогласованием выше предела и противоположное направление
    получают 0 — ячейка не двигается (§7.3).
    """
    recommendation, why = pack_recommendation.trusted_recommendation(
        evidence, "vanos", pack_log_analyzer.ANALYZER_VERSION
    )
    if recommendation is None:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            f"vanos: раскладка сдвига по зонам лога не применена — {why}; "
            f"сдвиг плоский {requested:+g}°",
        )
        return None, None
    rpm_axis = _policy_axis(image, family_policy, cal_offset, VANOS_RPM_AXIS)
    load_axis = _policy_axis(image, family_policy, cal_offset, VANOS_LOAD_AXIS)
    if (rpm_axis is None or load_axis is None
            or len(rpm_axis) != entry.rows or len(load_axis) != entry.columns):
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            "vanos: оси карты фаз не найдены или не совпали с картой "
            f"({0 if rpm_axis is None else len(rpm_axis)}×"
            f"{0 if load_axis is None else len(load_axis)} против "
            f"{entry.rows}×{entry.columns}); сдвиг плоский {requested:+g}°",
        )
        return None, None
    limits = pack_recommendation.shift_limits_for_map(
        recommendation, rpm_axis, load_axis, requested
    )
    allowed = sum(1 for value in limits if value > 0)
    digest = str(recommendation.get("source_sha256") or "")
    note = (
        f"раскладка сдвига по зонам лога (confidence {recommendation.get('confidence')}, "
        f"sha256 {digest[:12]}…): сдвиг разрешён в {allowed} ячейках из {len(limits)} "
        f"(остальные — без данных, с рассогласованием или в другую сторону), "
        f"максимум {max(limits, default=0.0):.3g}°"
    )
    return limits, note


def _ignition_zone_limits(
    evidence: dict | None, load_axis: list[float], delta: float, fallback_deg: float,
    findings: list[dict],
) -> tuple[list[float] | None, str | None]:
    """Пределы добавки по столбцам нагрузки из рекомендации анализатора.

    Ось оборотов карт ``__n__maf`` в конвертированных определениях не доказана
    (``find_maf_axis_ms45`` находит только ось нагрузки), поэтому берётся
    **минимум** по зонам каждой полосы нагрузки — осторожно: добавка не выше
    самой слабой зоны полосы, полоса без данных получает консервативный
    потолок ``fallback_deg``. Заявка задаёт потолок ``delta``.
    """
    recommendation, why = pack_recommendation.trusted_recommendation(
        evidence, "ignition", pack_log_analyzer.ANALYZER_VERSION
    )
    if recommendation is None:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            f"ignition: раскладка по полосам нагрузки не применена — {why}; "
            f"добавка общая ≤ {delta:g}°",
        )
        return None, None
    limits = [
        min(value, delta)
        for value in pack_recommendation.deltas_for_load_axis(
            recommendation, load_axis, fallback_deg=min(fallback_deg, delta)
        )
    ]
    zones = recommendation.get("zones") or []
    no_data = 0
    outside = 0
    for load in load_axis:
        band = pack_recommendation.load_band_index(float(load))
        if band is None:
            outside += 1  # столбец вне сетки зон: лог такие нагрузки не покрывает
            continue
        if not any((zone.get("index") or {}).get("load") == band and "delta_deg" in zone
                   for zone in zones):
            no_data += 1
    blocks = sum(1 for value in limits if value <= 0)
    digest = str(recommendation.get("source_sha256") or "")
    note = (
        f"раскладка по полосам нагрузки (confidence {recommendation.get('confidence')}, "
        f"sha256 {digest[:12]}…): предел полосы — минимум по зонам; "
        f"{blocks} столбцов без добавки (детонация/перегрев/бедная смесь в зоне), "
        f"{no_data} столбцов в полосах без данных и {outside} вне сетки зон — "
        f"консервативно {min(fallback_deg, delta):g}°; "
        "ось оборотов карты не доказана, поэтому по оборотам раскладки нет"
    )
    return limits, note


def find_maf_axis_ms45(image: bytes, columns: int) -> list[float] | None:
    """Общая ось нагрузки карт ``__n__maf`` (``columns`` точек, мг/такт).

    Адреса осей в конвертированных определениях указывают в программную
    область, которой нет в частичных чтениях 116 КБ, поэтому ось находится
    в самой калибровке: единственный изолированный возрастающий участок из
    ``columns`` u16, который после масштаба даёт осмысленный диапазон
    мг/такт (у всех пяти карт зажигания 20×16 ось общая — в определениях все
    их оси ссылаются на одну таблицу). Ось найдена и сверена на стоках
    4560BO00S, 4570LO00S и 4560BN00S.
    """
    step = 2
    size = columns * step
    for offset in range(0, len(image) - size - step, step):
        raw = struct.unpack_from(f"<{columns}H", image, offset)
        if not all(a < b for a, b in zip(raw, raw[1:])):
            continue
        # «изолированность»: если сосед продолжает монотонность, это окно
        # более длинной оси, а не отдельная ось карты зажигания.
        if offset >= step:
            before = struct.unpack_from("<H", image, offset - step)[0]
            if before < raw[0]:
                continue
        after = struct.unpack_from("<H", image, offset + size)[0]
        if after > raw[-1]:
            continue
        axis = [value * IGN_MAF_AXIS_SCALE for value in raw]
        if 5.0 <= axis[0] <= 150.0 and 900.0 <= axis[-1] <= 1500.0:
            return axis
    return None


#: Ось нагрузки карты RON98 как объект политики (только чтение).
IGN_LOAD_AXIS = "axis_ip_iga_bas_ron_98__n__maf_x"


def _ignition_load_axis(
    image: bytes, family_policy: pl.FamilyPolicy, cal_offset: int, entry: pl.PolicyEntryData,
) -> list[float] | None:
    """Ось нагрузки (мг/такт) для формы добавки зажигания.

    * Семейства без XDF (``policy.IGNITION_STOCK_DERIVED_FAMILIES``): ось
      читается по доказанному адресу из политики семьи (отпечаток цепочки
      записей осей, ``tests/test_family_ignition_proof.py``) — big-endian u16,
      формула ``0.021195*X``. Ось обязана иметь столько точек, сколько столбцов у
      карты, строго возрастать и лежать в физичных пределах, иначе ``None``
      (пакет не выпускается).
    * BO00S/LO00S — та же ось из политики (адрес из XDF): прежний поиск
      ``find_maf_axis_ms45`` находил посторонний ряд 32,6…1351 мг/такт и сдвигал
      форму добавки (полная добавка приходилась на ~325 мг/такт вместо 600).
      Исправлено: выход на этих семействах изменился осознанно, см.
      ``docs/FAMILY_CALIBRATION_EVIDENCE_RU.md`` §4.6.
    """
    axis = _policy_axis(image, family_policy, cal_offset, IGN_LOAD_AXIS)
    if axis is None or len(axis) != entry.columns:
        return None
    if not all(a < b for a, b in zip(axis, axis[1:])):
        return None
    if not (5.0 <= axis[0] <= 150.0 and 300.0 <= axis[-1] <= 1500.0):
        return None
    return axis


def ignition_shape_factor(load_mg_stroke: float) -> float:
    """Доля запрошенной добавки для данной нагрузки: 0 ниже порога,
    линейный рост в середине, 1 в зоне полной нагрузки."""
    if load_mg_stroke <= IGNITION_SHAPE_LOAD_LOW_MG:
        return 0.0
    if load_mg_stroke >= IGNITION_SHAPE_LOAD_HIGH_MG:
        return 1.0
    return (load_mg_stroke - IGNITION_SHAPE_LOAD_LOW_MG) / (
        IGNITION_SHAPE_LOAD_HIGH_MG - IGNITION_SHAPE_LOAD_LOW_MG
    )


def _read_all_ms45(image: bytes, entry: pl.PolicyEntryData, cal_offset: int) -> list[float]:
    return [
        _current(image, entry, cal_offset, index)
        for index in range(entry.rows * entry.columns)
    ]


def _snap(value: float, entry: pl.PolicyEntryData) -> float:
    """Значение, прижатое к сетке символа (половина вверх, детерминированно)."""
    raw, _ = entry.quantize(value)
    return entry.to_engineering(raw)


def _pack_requires_data(package: str, why: str, needs: tuple[str, ...]) -> PackageResult:
    return PackageResult(
        package,
        PACKAGE_TITLES.get(package, package),
        "requires_data",
        why,
        needs,
        (),
        {"code": f"{package.upper()}_REQUIRES_DATA", "why": why},
    )


def ignition_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    findings: list[dict],
    octane_profile: str | None = None,
) -> PackageResult:
    """Зажигание: добавка к карте ``ip_iga_bas_ron_98__n__maf``.

    Карта RON91 — защитный пол октановой адаптации и не пишется никогда;
    защита по детонации (``ip_iga_bas_max_knk__n__maf``) — только чтение.
    Угол никогда не убавляется.

    Потолки стадии — из единой спецификации (``tools/ignition_caps.py``):
    Stage 1 без логов ≤ +3,0° для профиля RON98 (и ≤ +1,5° для RON95, 0 для
    RON91), Stage 2 с проверенным логом детонации ≤ +5,0°. Потолка по MBT
    ``ip_iga_optm_tco_2`` в MS45 нет, и аналога с доказанной семантикой найти
    не удалось (кандидат ``ip_iga_ref__n__maf`` отклонён — находка
    ``MBT_LAYOUT_REQUIRES_DATA``), поэтому раскладка «по запасу до MBT» здесь
    честно не применяется, а форма по нагрузке и сглаживание — общие с MS43
    (``tools/ignition_layout.py``): 0° ниже 200 мг/такт, линейный рост, полная
    добавка от 600 мг/такт, соседние столбцы отличаются не больше шага из
    спецификации.
    """
    package = "ignition"
    mine = [i for i in intents if i.get("kind") == "ignition"]
    if not mine:
        return PackageResult(package, PACKAGE_TITLES[package], "not_requested")
    if "ignition" not in family_policy.roles:
        reason = pl.IGNITION_REQUIRES_DATA_REASON.get(family_policy.family)
        if reason:
            return _pack_requires_data(
                package,
                f"зажигание: {reason}",
                ("второй (и следующие) стоковый образ калибровки семейства "
                 f"{family_policy.family} — проверить, что адрес карты RON98 не меняется",
                 "или определения семейства (XDF/A2L) с адресом карты RON98 и её осей"),
            )
        return _pack_requires_data(
            package,
            f"у семейства {family_policy.family} адреса карт зажигания не доказаны "
            "(по умолчанию запрещены; нужны определения с подтверждением на стоках)",
            ("определения семейства с доказанными адресами (как у 4560BO00S/4570LO00S)",
             "стоковые образцы семейства для проверки адресов и границ"),
        )
    symbol = family_policy.roles["ignition"][0]
    entry = _entry(family_policy, symbol, findings)
    if entry is None:
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "символ недоступен")
    intent = mine[0]
    ceiling_profile = ignition_caps.cap_with_log_deg(octane_profile)
    delta = intent.get("delta_deg")
    if delta is None or float(delta) <= 0 or float(delta) > ceiling_profile + 1e-9:
        _finding(
            findings,
            "REQUEST_INVALID",
            "reject",
            f"ignition: добавка угла должна быть в 0..{ceiling_profile:g}° "
            f"(профиль {(octane_profile or 'не выбран').upper()}, с логом; "
            f"получено {delta!r})",
        )
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "добавка вне предела")
    delta = float(delta)
    limit_no_log = ignition_caps.cap_no_log_deg(octane_profile)
    log_verified = False
    per_column: list[float] | None = None
    zone_note: str | None = None
    axis = _ignition_load_axis(image, family_policy, cal_offset, entry)
    if axis is None:
        _finding(
            findings,
            "IGNITION_SHAPE_AXIS_NOT_FOUND",
            "reject",
            "ignition: ось нагрузки карты не найдена в калибровке — форма "
            "добавки по нагрузке не может быть применена, пакет не выпускается",
        )
        return PackageResult(package, PACKAGE_TITLES[package], "rejected",
                             "ось нагрузки не найдена")
    _finding(
        findings,
        "MBT_LAYOUT_REQUIRES_DATA",
        "info",
        "ignition: раскладка по запасу до MBT в MS45 не применяется — аналога "
        "ip_iga_optm_tco_2 с доказанной семантикой нет (кандидат "
        "ip_iga_ref__n__maf «reference ignition angle at max. spread» не "
        "подтверждён: описание не расшифровывает его как потолок момента, а "
        "ip_iga_bas_max_knk — предел ЗАДЕРЖКИ, не угол). Нужны DAMOS/XDF-"
        "описание назначения ip_iga_ref и сверка на стоках семьи — см. "
        "docs/STAGE1_REPORT_RU.md §4; до этого форма по нагрузке из "
        "docs/PACKS_WEB_ANALYSIS_RU.md §7 остаётся",
    )
    if delta > limit_no_log + 1e-9:
        evidence = _load_evidence(intent, findings)
        ok_gate, gate_problems = evidence_gate(evidence, package)
        ok, problems = validate_pack_evidence(evidence, package)
        problems = gate_problems + problems
        ok = ok and ok_gate
        if not ok:
            return _pack_requires_data(
                package,
                f"добавка {delta:g}° требует лога детонации: " + "; ".join(problems),
                PACK_EVIDENCE_NEEDS[package],
            )
        log_verified = True
        # Лог детонации проверен: предел добавки по полосам нагрузки —
        # минимум по зонам полосы из рекомендации анализатора (форма по
        # нагрузке остаётся, но каждая полоса не превышает свой предел).
        per_column, zone_note = _ignition_zone_limits(
            evidence, axis, delta, limit_no_log, findings
        )
    if per_column is None:
        per_column = [delta] * len(axis)
    # Форма по нагрузке → прижатие к сетке вниз → сглаживание по столбцам
    # (только понижение: запретные полосы не «подтягиваются» вверх).
    column_adds = ignition_layout.smooth_1d(
        ignition_layout.quantize_down([
            min(delta, float(per_column[column])) * ignition_shape_factor(axis[column])
            for column in range(len(axis))
        ])
    )
    stock = _read_all_ms45(image, entry, cal_offset)
    values: list[float] = []
    changed = 0
    blocked = 0
    for index, value in enumerate(stock):
        add = column_adds[index % entry.columns]
        if add <= 0:
            values.append(value)
            continue
        snapped = _snap(value + add, entry)
        # Прижатие к сетке может увести выше плана или ниже стока — ни того,
        # ни другого не допускаем.
        if snapped > value + add + 1e-9 or snapped <= value:
            snapped = value + add if value + add > value else value
        if snapped != value:
            changed += 1
        values.append(snapped)
    requests = [{"symbol": symbol, "values": values, "evidence": intent.get("evidence")}]
    written_max = max(column_adds, default=0.0)
    detail = (
        f"{symbol}: запрошено +{delta:g}°, записано по форме нагрузки: 0° при "
        f"нагрузке до {IGNITION_SHAPE_LOAD_LOW_MG:g} мг/такт, линейный рост, "
        f"до +{written_max:g}° от {IGNITION_SHAPE_LOAD_HIGH_MG:g} мг/такт "
        f"(квант сетки {IGNITION_QUANTUM_DEG:g}°, соседи столбцов различаются не "
        f"больше {ignition_caps.layout()['neighbour_max_step_deg']:g}°) — изменено "
        f"{changed} ячеек из {len(stock)}; раскладка по запасу до MBT — "
        f"REQUIRES_DATA (аналог карты не доказан); карта RON91 (защитный пол) и "
        "защита по детонации не изменялись"
    )
    if zone_note:
        detail += "; " + zone_note
    if blocked:
        detail += f"; {blocked} ячеек не сдвинулись: предел полосы или квант сетки"
    return PackageResult(package, PACKAGE_TITLES[package], "written", detail, requests=tuple(requests))


def fuel_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    findings: list[dict],
) -> PackageResult:
    """Топливо: обогащение полной нагрузки ``ip_lamb_fl__n``.

    Выпускается только с логом широкой лямбды и только там, где доказаны
    ворота диагностики богатой смеси (``c_lamb_min_ls_act_test_afr``). Ячейка,
    которая в стоке уже богаче пола или ворот, не трогается: обеднять и
    опускаться под ворота диагностики запрещено.
    """
    package = "fuel"
    mine = [i for i in intents if i.get("kind") == "fuel"]
    if not mine:
        return PackageResult(package, PACKAGE_TITLES[package], "not_requested")
    if "fuel" not in family_policy.roles:
        return _pack_requires_data(
            package,
            f"у семейства {family_policy.family} топливо полной нагрузки не выпускается: "
            "нет доказанных ворот диагностики богатой смеси (без них обогащение "
            "риск зажечь лампу/аварийный режим)",
            ("лог широкой лямбды (≥2 разгона + частичная нагрузка ≥10 с)",
             "доказанный символ ворот диагностики богатой смеси для семейства"),
        )
    intent = mine[0]
    enrich = intent.get("ti_fl_enrich_lambda")
    if enrich is None or float(enrich) <= 0:
        return _pack_requires_data(
            package,
            "без лога широкой лямбды в этом семействе нечего писать безопасно: "
            "прогревочные и переходные карты не трогаем, а обогащение полной "
            "нагрузки требует замера (укажите ti_fl_enrich_lambda ≤ 0.03 и evidence)",
            PACK_EVIDENCE_NEEDS[package],
        )
    enrich = float(enrich)
    if enrich > FUEL_ENRICH_MAX_LAMBDA:
        _finding(
            findings,
            "REQUEST_INVALID",
            "reject",
            f"fuel: сдвиг лямбды {enrich:g} больше предела {FUEL_ENRICH_MAX_LAMBDA:g}",
        )
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "сдвиг вне предела")
    evidence = _load_evidence(intent, findings)
    ok_gate, gate_problems = evidence_gate(evidence, package)
    ok, problems = validate_pack_evidence(evidence, package)
    problems = gate_problems + problems
    ok = ok and ok_gate
    if not ok:
        return _pack_requires_data(
            package,
            "обогащение полной нагрузки требует лога широкой лямбды: " + "; ".join(problems),
            PACK_EVIDENCE_NEEDS[package],
        )
    symbol = family_policy.roles["fuel"][0]
    entry = _entry(family_policy, symbol, findings)
    if entry is None:
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "символ недоступен")
    # Ворота диагностики: цель обязана остаться не беднее этого порога
    # (символ только для чтения, читаем его как данные).
    guard = None
    guard_symbol = "c_lamb_min_ls_act_test_afr"
    guard_entry = family_policy.objects.get(guard_symbol)
    if guard_entry is not None:
        guard = _current(image, guard_entry, cal_offset)
    floor = max(FUEL_LAMBDA_FLOOR, guard if guard is not None else 0.0)
    # Ряд карты индексирован оборотами: правка берётся по замеру λ в зонах с
    # этими оборотами (столбец без данных не обогащается — §7.3).
    zone_limits, zone_note = _fuel_zone_limits(
        evidence, image, family_policy, cal_offset, entry, floor, enrich, findings
    )
    # В MS45 ячейки ``ip_lamb_fl__n`` — сама лямбда (уменьшить = обогатить),
    # а не «коэффициент обогащения», как в некоторых других семействах.
    stock = _read_all_ms45(image, entry, cal_offset)
    values: list[float] = []
    changed = 0
    for index, value in enumerate(stock):
        if value <= floor:
            values.append(value)  # уже богаче пола/ворот — не трогаем
            continue
        add = enrich if zone_limits is None else zone_limits[index]
        if add <= 0:  # в этой зоне лог запаса не даёт
            values.append(value)
            continue
        new_lambda = max(value - add, floor)
        snapped = _snap(new_lambda, entry)
        if snapped >= value:  # квантование не должно обеднить
            snapped = value
        if snapped != value:
            changed += 1
        values.append(snapped)
    requests = [{"symbol": symbol, "values": values, "evidence": intent.get("evidence")}]
    detail = (
        f"{symbol}: обогащение до −{enrich:g} лямбды в {changed} ячейках из {len(stock)}; "
        f"пол {floor:.4g}, сток богаче пола не тронут"
    )
    if zone_note:
        detail += "; " + zone_note
    return PackageResult(package, PACKAGE_TITLES[package], "written", detail, requests=tuple(requests))


def vanos_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    findings: list[dict],
) -> PackageResult:
    """ВАНос: тёплые карты фаз частичной нагрузки, всегда по логу фаз.

    Двигаются только прогретые карты частичной нагрузки; холодные карты и
    карты полной нагрузки — только чтение. Сдвиг от стока ≤ 5°, прижат к
    сетке (13 шагов по 0.375° = 4.875°).
    """
    package = "vanos"
    mine = [i for i in intents if i.get("kind") == "vanos"]
    if not mine:
        return PackageResult(package, PACKAGE_TITLES[package], "not_requested")
    if "vanos" not in family_policy.roles:
        return _pack_requires_data(
            package,
            f"у семейства {family_policy.family} карты фаз частичной нагрузки не доказаны "
            "(по умолчанию запрещены; нужны определения с подтверждением на стоках)",
            ("определения семейства с доказанными адресами карт фаз",
             "стоковые образцы семейства для проверки адресов и границ"),
        )
    intent = mine[0]
    shift_in = intent.get("shift_in_deg")
    shift_ex = intent.get("shift_ex_deg")
    if shift_in is None and shift_ex is None:
        _finding(
            findings,
            "REQUEST_INVALID",
            "reject",
            f"vanos: укажите сдвиг целей (shift_in_deg и/или shift_ex_deg, ±{VANOS_SHIFT_MAX_DEG:g}° от стока)",
        )
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "нет сдвига")
    for label, shift in (("впуск", shift_in), ("выпуск", shift_ex)):
        if shift is not None and abs(float(shift)) > VANOS_SHIFT_MAX_DEG:
            _finding(
                findings,
                "REQUEST_INVALID",
                "reject",
                f"vanos: сдвиг {label}а {float(shift):g}° больше предела "
                f"±{VANOS_SHIFT_MAX_DEG:g}° от стока",
            )
            return PackageResult(package, PACKAGE_TITLES[package], "rejected", "сдвиг вне предела")
    evidence = _load_evidence(intent, findings)
    ok_gate, gate_problems = evidence_gate(evidence, package)
    ok, problems = validate_pack_evidence(evidence, package)
    problems = gate_problems + problems
    ok = ok and ok_gate
    if not ok:
        return _pack_requires_data(
            package,
            "VANOS без измеренного лога фаз не выпускается: " + "; ".join(problems),
            PACK_EVIDENCE_NEEDS[package],
        )
    symbols = family_policy.roles["vanos"]
    requests: list[dict] = []
    details: list[str] = []
    for symbol, shift in zip(symbols, (shift_in, shift_ex)):
        if shift is None:
            continue
        entry = _entry(family_policy, symbol, findings)
        if entry is None:
            continue
        snapped_shift = round(float(shift) / IGNITION_QUANTUM_DEG) * IGNITION_QUANTUM_DEG
        # Сдвиг раскладывается по зонам лога: ячейка без подтверждённой стороны
        # (нет данных, рассогласование выше предела, лог против) не двигается.
        zone_limits, zone_note = _vanos_zone_limits(
            evidence, image, family_policy, cal_offset, entry, float(shift), findings
        )
        sign = 1.0 if float(shift) >= 0 else -1.0
        stock = _read_all_ms45(image, entry, cal_offset)
        values: list[float] = []
        changed = 0
        blocked = 0
        for index, value in enumerate(stock):
            allowed = abs(float(shift)) if zone_limits is None else zone_limits[index]
            if allowed <= 0:
                values.append(value)
                blocked += 1
                continue
            target = value + sign * allowed
            snapped = _snap(target, entry)
            if snapped == value:
                values.append(value)
                blocked += 1
                continue
            changed += 1
            values.append(snapped)
        requests.append({"symbol": symbol, "values": values, "evidence": intent.get("evidence")})
        details.append(
            f"{symbol}: сдвиг {snapped_shift:+g}° в {changed} ячейках"
            + (f"; {blocked} ячеек без сдвига" if blocked else "")
            + (f"; {zone_note}" if zone_note else "")
        )
    if not requests:
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "символы недоступны")
    return PackageResult(
        package, PACKAGE_TITLES[package], "written", "; ".join(details), requests=tuple(requests)
    )


# ---------------------------------------------------------------------------
# Слой «без катализатора» (вариант 3): удаление диагностики катализатора и
# продувки (СВП). Только проверенные флаги с однозначной семантикой
# (мс4х-вики, «Cat Delete»); c_obd_state_2 (битовая маска) и lc_ad_clr_var
# (управляется диагностикой/владельцем) намеренно не трогается. Рецепты:
#   катализатор: c_conf_cat=0, подогрев/диагностика заднего зонда выключены;
#   СВП: задержки продувки в 0, диагностика СВП выключена.
# ---------------------------------------------------------------------------

#: Целевые значения слоя: символ -> список значений (по порядку ячеек).
CAT_DELETE_TARGETS: dict[str, tuple[float, ...]] = {
    "c_conf_cat": (0.0,),
    "lc_lsh_down_1": (0.0,),
    "lc_lsh_down_2": (0.0,),
    "lc_inh_ls_down_man_deac": (1.0,),
}
SAP_DELETE_TARGETS: dict[str, tuple[float, ...]] = {
    "ip_t_sa_off_0": (0.0,) * 8,
    "lc_inh_sa_man_deac": (1.0,),
    "lc_sa_swi_acq": (0.0,),
}


def _delete_layer_package(
    package: str,
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    request: dict,
    findings: list[dict],
    targets: dict[str, tuple[float, ...]],
    why_absent: str,
) -> PackageResult:
    """Общий механизм слоёв «без катализатора»: флаги пишутся в целевые
    значения только при подтверждении ``no_catalyst`` (плюс обязательное
    подтверждение «no-catalyst» у каждого символа в политике)."""
    mine = [i for i in intents if i.get("kind") == package]
    if not mine:
        return PackageResult(package, PACKAGE_TITLES[package], "not_requested")
    if package not in family_policy.roles:
        return _pack_requires_data(
            package,
            f"у семейства {family_policy.family} {why_absent}",
            ("определения семейства с доказанными адресами флагов",
             "стоковые образцы семейства для проверки адресов и значений"),
        )
    if not request.get("no_catalyst"):
        _finding(
            findings,
            f"{package.upper()}_NO_CATALYST_REQUIRED",
            "reject",
            f"{PACKAGE_TITLES[package]}: нужно подтверждение no_catalyst: true в заявке "
            "(слой только для машины без катализатора)",
        )
        return PackageResult(package, PACKAGE_TITLES[package], "rejected", "нет подтверждения")
    requests: list[dict] = []
    details: list[str] = []
    already = 0
    for symbol, values in targets.items():
        entry = _entry(family_policy, symbol, findings)
        if entry is None:
            return PackageResult(package, PACKAGE_TITLES[package], "rejected",
                                 f"{symbol} недоступен")
        current = [_current(image, entry, cal_offset, i) for i in range(entry.element_count)]
        if current == list(values):
            already += 1
            continue
        requests.append({"symbol": symbol, "values": list(values)})
        if len(current) > 1:
            details.append(f"{symbol}: {len(current)} ячеек -> {values[0]:g}")
        else:
            details.append(f"{symbol}: {current[0]:g} -> {values[0]:g}")
    if not requests:
        return PackageResult(
            package, PACKAGE_TITLES[package], "written",
            f"изменений нет: все флаги слоя уже в целевых значениях ({already})",
        )
    _finding(
        findings,
        f"{package.upper()}_LEGALITY_WARNING",
        "info",
        f"{PACKAGE_TITLES[package]}: машина без катализатора/СВП — эксплуатация на "
        "дорогах общего пользования может нарушать технические и экологические "
        "требования; после записи — диагностический скан и контроль смеси",
    )
    detail = "; ".join(details)
    if already:
        detail += f"; уже в целевых значениях: {already}"
    return PackageResult(package, PACKAGE_TITLES[package], "written", detail,
                         requests=tuple(requests))


#: Группы слоя диагностики привода: какие счётчики гасим и почему это безопасно.
#: Приращения проверены по стокам семейств (BO00S — 42 файла, LO00S — 33):
#: приращение 1, максимум счётчика 10 (VIM) и 6 (ISA) — то есть приращение
#: меньше максимума; 0 останавливает рост счётчика, а сама функция (положение
#: заслонки, регулирование ХХ) остаётся в силе.
DTC_DRIVER_GROUPS: dict[str, tuple[str, ...]] = {
    "vim": ("c_det_inc_vim",),
    "isa": ("c_det_inc_isa_1", "c_det_inc_isa_2"),
}


def dtc_driver_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    request: dict,
    findings: list[dict],
) -> PackageResult:
    """Слой диагностики привода DISA (VIM) и клапана ХХ (ISA).

    Пишется только при двух подтверждениях: правовая справка
    (``acknowledge_legality``) и механическое состояние (``acknowledge_mechanics``
    в самом намерении) — иначе скрытие кода ошибки при живом приводе было бы
    обманом. Пишем только 0 (подавить); вернуть приращение обратно продукт не
    умеет. Механику это не отключает: положение заслонки и холостой ход
    остаются штатными, поэтому «DISA off» и «ISA off» — по-прежнему отдельные
    намерения со своими доказательствами.
    """
    package = "dtc_driver"
    title = PACKAGE_TITLES[package]
    mine = [item for item in intents if item.get("kind") == "dtc_driver"]
    if not mine:
        return PackageResult(package, title, "not_requested")
    if not request.get("acknowledge_legality"):
        _finding(
            findings,
            "ACKNOWLEDGE_LEGALITY_REQUIRED",
            "reject",
            "слой диагностики привода: подавление кодов ошибок выпускается только "
            "с подтверждённой правовой справкой (acknowledge_legality: true)",
        )
        return PackageResult(package, title, "rejected", "нет правовой справки")
    if not all(item.get("acknowledge_mechanics") for item in mine):
        _finding(
            findings,
            "REQUEST_INVALID",
            "reject",
            "слой диагностики привода: нужно подтверждение механики "
            "(acknowledge_mechanics: true) — что именно сделано с приводом/клапаном",
        )
        return PackageResult(package, title, "rejected", "нет подтверждения механики")
    groups: list[str] = []
    for item in mine:
        for group in item.get("groups") or ():
            groups.append(str(group).lower())
    unknown = sorted(set(groups) - set(DTC_DRIVER_GROUPS))
    if unknown:
        _finding(
            findings,
            "DTC_GROUP_UNKNOWN",
            "reject",
            f"слой диагностики привода: неизвестные группы {', '.join(unknown)}; "
            f"допустимо {', '.join(sorted(DTC_DRIVER_GROUPS))}",
        )
        return PackageResult(package, title, "rejected", "группы неизвестны")
    if not groups:
        _finding(
            findings,
            "REQUEST_INVALID",
            "reject",
            'слой диагностики привода: укажите группы (например, {"kind": '
            '"dtc_driver", "groups": ["vim","isa"], "acknowledge_mechanics": true})',
        )
        return PackageResult(package, title, "rejected", "группы не указаны")
    requests: list[dict] = []
    details: list[str] = []
    for group in dict.fromkeys(groups):
        for symbol in DTC_DRIVER_GROUPS[group]:
            entry = family_policy.objects.get(symbol)
            if entry is None or entry.read_only:
                _finding(
                    findings,
                    "SYMBOL_NOT_ALLOWED",
                    "reject",
                    f"слой диагностики привода: {symbol} не разрешён политикой "
                    f"семейства {family_policy.family}",
                )
                return PackageResult(package, title, "rejected", f"{symbol} вне политики")
            current = _current(image, entry, cal_offset)
            if current == 0:
                details.append(f"{symbol}: уже 0")
                continue
            requests.append({"symbol": symbol, "values": [0.0]})
            details.append(f"{symbol}: {current:g} -> 0")
    if not requests:
        return PackageResult(package, title, "written",
                             "изменений нет: счётчики уже подавлены в стоке (" +
                             ", ".join(details) + ")")
    _finding(
        findings,
        "DTC_DRIVER_WARNING",
        "info",
        "слой диагностики привода: коды по VIM/ISA выставляться не будут; сама "
        "функция сохраняется, но состояние привода больше не контролируется — "
        "после записи обязательны скан диагностикой и проверка холостого хода",
    )
    return PackageResult(package, title, "written", "; ".join(details),
                         requests=tuple(requests))


def cat_delete_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    request: dict,
    findings: list[dict],
) -> PackageResult:
    """Удаление диагностики катализатора (флаги + задний зонд)."""
    return _delete_layer_package(
        "cat_delete", image, family_policy, cal_offset, intents, request, findings,
        CAT_DELETE_TARGETS,
        "адреса флагов удаления катализатора не доказаны (по умолчанию запрещены)",
    )


def sap_delete_package(
    image: bytes,
    family_policy: pl.FamilyPolicy,
    cal_offset: int,
    intents: list[dict],
    request: dict,
    findings: list[dict],
) -> PackageResult:
    """Удаление системы продувки (СВП): задержки в 0, диагностика выключена."""
    return _delete_layer_package(
        "sap_delete", image, family_policy, cal_offset, intents, request, findings,
        SAP_DELETE_TARGETS,
        "адреса флагов удаления продувки не доказаны (по умолчанию запрещены)",
    )


# ---------------------------------------------------------------------------
# Три варианта поставки (задание 3, уточнения — в веб-анализе §6).
# Вариант задаётся полем заявки ``variant``: «v1», «v2» или «v3». Без поля —
# свободная сборка. Состав вариантов у семейства свой: на 4560BN00S адреса
# карт зажигания/фаз не доказаны, поэтому там варианты собирают только то, что
# разрешает политика (default-deny).
# ---------------------------------------------------------------------------

VARIANTS: dict[str, dict] = {
    # Вариант 1 («Stage 1 базовый», задание docs/TASK_VARIANT1_RU.md §1):
    # базовое зажигание (топливо на этом варианте не пишется) + одобренные
    # функции отдельными намерениями: октан, лимитеры/vmax, хлопки и
    # диагностический слой (по воротам «катализатора нет» + правовой справке),
    # DISA off, ISA off, холостой ход, вентилятор/термостат. Launch control для
    # MS45 остаётся «недоступно»: калибровочного пути нет. Варианты v2/v3 не
    # пересобирались (решение владельца: пока расширения).
    "v1": {
        "title": "базовый (вариант 1): зажигание до +3.0° (профиль RON98; RON95 "
                 "— до +1.5°) без логов + одобренные функции отдельными "
                 "намерениями (топливо по логу — с варианта 2)",
        "kinds": {
            "ignition", "octane", "vmax", "rev_limit", "pops",
            "cat_delete", "sap_delete", "idle_speed", "disa", "launch_control",
            "disa_off", "isa_off", "fan_thermostat", "dtc_driver",
        },
        # Потолки — из единой спецификации (docs/pack_log_requirements.json
        # через tools/ignition_caps.py), «на глаз» здесь чисел нет.
        "ignition_without_log_deg": ignition_caps.delta_without_log_deg(),
        "ignition_with_log_deg": ignition_caps.delta_with_log_deg(),
    },
    "v2": {
        "title": "расширенный: вариант 1 + лямбда полной нагрузки по логу, ВАНос, обороты",
        "kinds": {"ignition", "fuel", "vanos", "vmax", "rev_limit"},
        "ignition_without_log_deg": ignition_caps.delta_without_log_deg(),
        "ignition_with_log_deg": ignition_caps.delta_with_log_deg(),
        "rev_limit_oil_pump_ack": True,
    },
    "v3": {
        "title": "без катализатора: вариант 2 + диагностический слой и хлопки по существующим воротам",
        "kinds": {"ignition", "fuel", "vanos", "vmax", "rev_limit", "pops",
                  "cat_delete", "sap_delete"},
        "ignition_without_log_deg": ignition_caps.delta_without_log_deg(),
        "ignition_with_log_deg": ignition_caps.delta_with_log_deg(),
        "rev_limit_oil_pump_ack": True,
        "requires_no_catalyst": True,
    },
}


def apply_variant_rules(
    variant: str | None, request: dict, intents: list[dict], findings: list[dict],
    octane_profile: str | None = None,
) -> None:
    """Ворота варианта поставки: состав, пределы и подтверждения."""
    if not variant:
        return
    variant = str(variant).lower()
    spec = VARIANTS.get(variant)
    if spec is None:
        _finding(
            findings,
            "VARIANT_UNKNOWN",
            "reject",
            f"вариант {variant!r} неизвестен: поддерживаются "
            + ", ".join(sorted(VARIANTS)),
        )
        return
    for intent in intents:
        kind = intent.get("kind")
        if kind not in spec["kinds"]:
            _finding(
                findings,
                "VARIANT_SCOPE_VIOLATION",
                "reject",
                f"вариант {variant} ({spec['title']}): намерение {kind!r} вне состава "
                "варианта — соберите заявку другого варианта",
            )
    if variant == "v1":
        # Stage 1 («без логов») — потолок профиля клиента из единой
        # спецификации: RON98 → 3.0°, RON95 → 1.5°, не выбран → 1.5°.
        v1_cap = ignition_caps.cap_no_log_deg(octane_profile)
        for intent in intents:
            if intent.get("kind") == "ignition":
                delta = intent.get("delta_deg")
                if delta is not None and float(delta) > v1_cap + 1e-9:
                    _finding(
                        findings,
                        "VARIANT_SCOPE_VIOLATION",
                        "reject",
                        f"вариант v1: добавка зажигания {float(delta):g}° больше "
                        f"потолка Stage 1 для профиля "
                        f"{(octane_profile or 'не выбран').upper()} "
                        f"(+{v1_cap:g}°) — добавки по логам разрешены с варианта v2",
                    )
    if spec.get("rev_limit_oil_pump_ack"):
        for intent in intents:
            if intent.get("kind") == "rev_limit" and not intent.get("acknowledge_oil_pump"):
                _finding(
                    findings,
                    "ACKNOWLEDGE_REQUIRED",
                    "reject",
                    "повышение оборотов в варианте "
                    f"{variant}: нужно подтверждение acknowledge_oil_pump: true "
                    "(ресурс масляного насоса при высоких оборотах)",
                )
    if spec.get("requires_no_catalyst") and not request.get("no_catalyst"):
        _finding(
            findings,
            "NO_CATALYST_REQUIRED",
            "reject",
            f"вариант {variant}: нужно подтверждение no_catalyst: true в заявке "
            "(вариант для машины без катализатора)",
        )
    if spec.get("requires_no_catalyst"):
        _finding(
            findings,
            "V3_LEGALITY_WARNING",
            "info",
            "вариант v3: машина без катализатора — эксплуатация на дорогах общего "
            "пользования может нарушать технические и экологические требования; "
            "план проверки: герметичность выпуска, лямбда-логи, температуры, "
            "диагностический скан до и после записи",
        )


def build(
    stage: str,
    image: bytes,
    request: dict,
    *,
    policy: pl.Policy | None = None,
    source_name: str = "",
) -> StageResult:
    """Собрать стадию: пакеты -> план ядра -> файл -> наряд."""
    stage = str(stage or request.get("stage", "")).lower()
    source_name = pathfmt.to_posix(source_name)  # артефакты не зависят от ОС
    if stage not in STAGE_PACKAGES:
        raise cal.PatchError(pl.CODE_POLICY_INVALID, f"стадия {stage!r} неизвестна: stage1 или stage2")
    policy = policy or pl.load_policy()
    intents = list(request.get("intents") or [])
    findings: list[dict] = []
    cal_offset = 0
    family: str | None = None
    family_policy: pl.FamilyPolicy | None = None
    image_cal = b""

    try:
        image_cal, cal_offset = ck.extract_cal(image)
        family = ck.read_family(image_cal)
        family_policy = policy.for_family(family)
    except (ck.MS45Error, pl.PolicyError) as error:
        code = getattr(error, "code", pl.CODE_POLICY_INVALID)
        _finding(findings, code, "reject", str(error))

    if not intents:
        _finding(findings, "NOTHING_REQUESTED", "reject", "в заявке нет ни одного намерения")

    # Вариант 1 (задание docs/TASK_VARIANT1_RU.md): профиль топлива читаем
    # раньше ворот варианта — потолок Stage 1 зависит от профиля (раунд 4),
    # чтобы «95-й бензин + добавка угла выше права» и «хлопки без
    # подтверждения» не дошли до записи.
    octane_profile = variant1.octane_choice(request, intents, findings)
    # Вариант поставки (задание 3): состав и ворота проверяются до пакетов.
    apply_variant_rules(request.get("variant"), request, intents, findings,
                        octane_profile)
    variant1.enforce_octane(octane_profile, intents, findings)
    variant1.legality_gate(request, intents, findings,
                           ("pops", "cat_delete", "sap_delete", "dtc_driver"))

    if stage == "stage2" and not request.get("hardware"):
        _finding(
            findings,
            "HARDWARE_REQUIRED",
            "reject",
            "Stage 2 без блока hardware не собирается: Stage 2 привязан к конкретному железу",
        )

    packages: list[PackageResult] = []
    requests: list[dict] = []
    if family_policy is not None and not any(f["severity"] == "reject" for f in findings):
        requested_kinds = {intent.get("kind") for intent in intents}
        unknown = sorted(set(requested_kinds) - set(INTENT_PACKAGES) - PROFILE_KINDS)
        for kind in unknown:
            _finding(findings, "INTENT_UNKNOWN", "reject", f"намерение {kind!r} неизвестно")
        for package in STAGE_PACKAGES[stage]:
            if package == "limits" and {"vmax", "rev_limit"} & requested_kinds:
                writable_limit_intents = []
                for intent in intents:
                    kind = intent.get("kind")
                    missing_package = None
                    if kind == "vmax":
                        symbols = family_policy.roles.get("vmax", ())
                        if not symbols or any(
                            symbol not in family_policy.objects or family_policy.objects[symbol].read_only
                            for symbol in symbols
                        ):
                            missing_package = "vmax"
                    elif kind == "rev_limit":
                        transmission = str(intent.get("transmission", "mt")).lower()
                        if transmission in ("mt", "at", "amt"):
                            symbols = family_policy.roles.get(f"rev_limit_{transmission}", ())
                            if len(symbols) != 2 or any(
                                symbol not in family_policy.objects or family_policy.objects[symbol].read_only
                                for symbol in symbols
                            ):
                                missing_package = "rev_limit"
                    if missing_package:
                        pending_item = requires_data_package(missing_package)
                        packages.append(pending_item)
                        _finding(
                            findings,
                            str(pending_item.manifest["code"]),
                            "info",
                            f"{pending_item.title}: не выпускается — {pending_item.detail}",
                        )
                    else:
                        writable_limit_intents.append(intent)
                if writable_limit_intents:
                    item = limits_package(
                        image, family_policy, cal_offset, writable_limit_intents, findings
                    )
                else:
                    item = PackageResult("limits", PACKAGE_TITLES["limits"], "not_requested")
            elif package == "pops" and "pops" in requested_kinds:
                item = pops_package(image, family_policy, cal_offset, intents, request, findings)
            elif package == "ignition" and "ignition" in requested_kinds:
                item = ignition_package(image, family_policy, cal_offset, intents,
                                       findings, octane_profile=octane_profile)
            elif package == "fuel" and "fuel" in requested_kinds:
                item = fuel_package(image, family_policy, cal_offset, intents, findings)
            elif package == "vanos" and "vanos" in requested_kinds:
                item = vanos_package(image, family_policy, cal_offset, intents, findings)
            elif package == "dtc_driver" and "dtc_driver" in requested_kinds:
                item = dtc_driver_package(image, family_policy, cal_offset, intents,
                                          request, findings)
            elif package == "cat_delete" and "cat_delete" in requested_kinds:
                item = cat_delete_package(image, family_policy, cal_offset, intents,
                                          request, findings)
            elif package == "sap_delete" and "sap_delete" in requested_kinds:
                item = sap_delete_package(image, family_policy, cal_offset, intents,
                                          request, findings)
            elif package in REQUIRES_DATA_CATALOG and any(
                INTENT_PACKAGES.get(kind) == package for kind in requested_kinds
            ):
                # Это не отказ заявке: остальные пакеты собираются, а в наряде честно
                # написано, почему функция не выпускается и что нужно, чтобы её выпустить.
                item = requires_data_package(package)
                _finding(
                    findings,
                    str(item.manifest["code"]),
                    "info",
                    f"{PACKAGE_TITLES.get(package, package)}: не выпускается — {item.detail}",
                )
            else:
                item = PackageResult(package, PACKAGE_TITLES.get(package, package), "not_requested")
            packages.append(item)
            if item.status == "written":
                requests.extend(item.requests)
    else:
        packages = [
            PackageResult(package, PACKAGE_TITLES.get(package, package), "rejected")
            for package in STAGE_PACKAGES[stage]
        ]

    result = StageResult(
        stage=stage,
        status="REJECTED",
        source_sha256=hashlib.sha256(image).hexdigest(),
        policy_version=policy.version,
        customer=str(request.get("customer", "")),
        notes=str(request.get("notes", "")),
        packages=tuple(packages),
        family=family,
        octane_profile=octane_profile,
    )

    rejected = any(f["severity"] == "reject" for f in findings)
    if not rejected and requests:
        acknowledgements = {"no-catalyst"} if request.get("no_catalyst") else set()
        plan = cal.plan_changes(image, requests, policy=policy, acknowledgements=acknowledgements)
        result.plan = plan
        for finding in plan.findings:
            _finding(findings, finding.code, finding.severity, finding.message)
        if plan.status == "PLANNED":
            result.result = cal.apply_plan(image, plan)
            result.status = "BUILT"
        else:
            result.status = "REJECTED"
    elif not rejected:
        # Ничего не пишем, но есть пакеты, ждущие данных.
        result.status = "REQUIRES_DATA" if result.pending else "REJECTED"

    result.findings = findings
    result.manifest = _manifest(result, policy, request, source_name)
    if result.status == "REJECTED" and not any(f["severity"] == "reject" for f in findings):
        _finding(findings, "NOTHING_TO_BUILD", "reject", "нечего собирать: все пакеты пусты")
        result.findings = findings
    return result


#: Версия схемы манифеста: меняем при изменении состава полей/смысла.
MANIFEST_VERSION = 1


def _manifest(result: StageResult, policy: pl.Policy, request: dict, source_name: str) -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "trust_level": variant1.TRUST_LEVEL_STATEMENT,
        "product": "MS45 Stage Generator",
        "stage": result.stage,
        "status": result.status,
        "customer": result.customer,
        "date": date.today().isoformat(),
        "source_name": source_name,
        "source_sha256": result.source_sha256,
        #: Имя выпущенного файла (заполняет run(), когда известен --out).
        "result_name": None,
        "family": result.family,
        "supported_families": list(policy.software),
        "policy_version": policy.version,
        "hardware": request.get("hardware"),
        "octane": variant1.octane_report(result.octane_profile),
        "legal_notice": (
            variant1.legal_notice_payload()
            if request.get("acknowledge_legality") is True
            or any(
                isinstance(item, dict) and item.get("kind") in variant1.LEGAL_GATE_KINDS
                for item in (request.get("intents") or ())
            )
            else None
        ),
        "rollback": variant1.rollback_block(
            result.source_sha256,
            verify_command=(
                "python3 -m MS45.tools.stages --verify-manifest <манифест>.json "
                "--image <файл>"
            ),
            image_kind="область калибровки 0x40000..0x5CFFF",
        ),
        "packages": [item.as_dict() for item in result.packages],
        "requires_data": [
            {
                "id": item.id,
                "title": item.title,
                "code": item.manifest.get("code"),
                "why": item.manifest.get("why"),
                "needs": list(item.needs),
            }
            for item in result.packages
            if item.status == "requires_data"
        ],
        "findings": result.findings,
        "plan": result.plan.as_dict() if result.plan else None,
        "result": {
            "sha256": result.result.sha256,
            "image_size": len(result.result.image),
            "kind": result.plan.kind if result.plan else None,
            "changed_cells": result.result.changed_cells,
            "changed_bytes": result.result.changed_bytes,
            "checksum_words": [
                {"name": name, "offset": f"0x{offset:05X}", "before": before, "after": after}
                for name, offset, before, after in result.result.checksum_words
            ],
        }
        if result.result
        else None,
    }


def build_note(result: StageResult, source_name: str = "") -> str:
    """Наряд по-русски: что записано, чем подтверждено и что осталось ждать."""
    source_name = pathfmt.to_posix(source_name)  # артефакт не зависит от ОС
    lines = [
        f"# Наряд MS45 — {result.stage.upper()}",
        "",
        f"* клиент: {result.customer or '—'}",
        f"* дата: {date.today().isoformat()}",
        f"* статус: {result.status}",
        f"* исходный образ: {source_name or '—'} (sha256 {result.source_sha256[:16]}…)",
        f"* семейство: {result.family or 'не прочитано'}",
        f"* политика: v{result.policy_version}, поддерживаются "
        f"{', '.join(result.manifest.get('supported_families') or [])}",
    ]
    lines.append(f"* {variant1.trust_note_ru()}")
    octane = result.manifest.get("octane") or variant1.octane_report(result.octane_profile)
    lines.append(
        f"* топливо: {octane.get('fuel') or 'профиль не выбран'} — {octane['note_ru']}"
    )
    if result.notes:
        lines.append(f"* примечание заявки: {result.notes}")
    if result.manifest.get("hardware"):
        lines.append(
            "* железо: "
            + ", ".join(f"{key}={value}" for key, value in result.manifest["hardware"].items())
        )
    if result.result:
        lines.append(f"* результат: sha256 {result.result.sha256[:16]}…")
    lines.append("")

    lines.append("## Что записано")
    lines.append("")
    if not result.result or not result.result.changed_cells:
        lines.append("Ничего не записано: пакеты либо не запрошены, либо ждут данных.")
        lines.append("")
    else:
        for item in result.written_packages:
            lines.append(f"### {item.title} (`{item.id}`)")
            lines.append("")
            if item.detail:
                lines.append(f"{item.detail}")
                lines.append("")
        lines.append("Ячейки (адрес — смещение внутри калибровки 116 КиБ):")
        lines.append("")
        lines.append("| символ | ячейка | адрес | было | стало | ед. |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for planned in result.plan.symbols if result.plan else []:
            for cell in planned.cells:
                if not cell.changed:
                    continue
                lines.append(
                    f"| {planned.symbol} | {cell.index} | 0x{cell.cal_offset:05X} | "
                    f"{cell.value_before:g} | {cell.value_after:g} | {planned.entry.units or '-'} |"
                )
        lines.append("")
        lines.append("Контрольные суммы пересчитаны нашим ядром:")
        lines.append("")
        for name, offset, before, after in result.result.checksum_words:
            lines.append(f"* {name} @0x{offset:05X}: 0x{before:08X} → 0x{after:08X}")
        lines.append("")

    if result.findings:
        lines.append("## Замечания ядра и продукта")
        lines.append("")
        lines.append("| важность | код | что именно |")
        lines.append("| --- | --- | --- |")
        for finding in result.findings:
            severity = {"reject": "отказ", "warn": "внимание", "info": "к сведению"}.get(
                finding.get("severity", ""), finding.get("severity", "")
            )
            message = str(finding.get("message", "")).replace("|", "/")
            lines.append(f"| {severity} | `{finding.get('code', '')}` | {message} |")
        lines.append("")

    lines.append("## Что НЕ менялось")
    lines.append("")
    lines.append("* области вне калибровки `0x40000..0x5CFFF`: загрузчик/системная часть, ")
    lines.append("  программа `0x60000..0xFFFFF`, внутренняя флеш MPC и запись программатора;")
    lines.append("* RSA-подпись калибровки (`0x174..0x1B3`) — не пересчитывается и не восстанавливается;")
    lines.append("* штатные защиты: `c_n_lim_min` (порог «overrevving event», 7200 rpm), ")
    lines.append("  детонация, температурные ограничения, `c_t_min_pu_msr`.")
    lines.append("")

    lines.append("## Что требует данных")
    lines.append("")
    if not result.pending:
        lines.append("Запрошенные функции реализованы полностью.")
        lines.append("")
    else:
        for item in result.pending:
            lines.append(f"### {item.title} (`{item.id}`)")
            lines.append("")
            lines.append(f"{item.detail}")
            lines.append("")
            for need in item.needs:
                lines.append(f"* {need}")
            lines.append("")

    lines.append("## Предупреждения")
    lines.append("")
    lines.append("* **Мощность не обещается.** В наряде нет ни одной цифры прироста: без стенда ")
    lines.append("  это было бы выдумыванием.")
    lines.append("* **Подпись.** Любая правка калибровки делает RSA-подпись недействительной — ")
    lines.append("  она не входит в наши суммы и не восстанавливается (нет ключа BMW). Файл ")
    lines.append("  предназначен для прошивки тюнинг-флешером области `0x40000..0x5CFFF`; путь ")
    lines.append("  штатного WinKFP/ISTA требует переподписи на стороне, которая её умеет.")
    lines.append("* **Только эта калибровка.** Адреса и границы сняты со стока именно этого ")
    lines.append("  семейства; в другой версии прошивки записанные значения означают другое.")
    if any(item.id == "pops" for item in result.written_packages):
        lines.append("* **Хлопки.** Разрешены только без катализатора и требуют контроля температуры ")
        lines.append("  выпуска. Агрессивные пресеты не выпускаются.")
    if result.plan and any(
        cell.raw_after != cell.raw_before
        for planned in result.plan.symbols
        if planned.group == "rev_limit_mt"
        for cell in planned.cells
    ):
        lines.append("* **Отсечка.** Выше 6900 rpm на M54B30 требуется демпфер крутильных ")
        lines.append("  колебаний (ms4x.net); штатный порог 7200 rpm остался на месте.")
    lines.append("")

    if result.manifest.get("legal_notice"):
        lines.append(variant1.legal_notice_markdown())
    rollback = result.manifest.get("rollback") or variant1.rollback_block(
        result.source_sha256, image_kind="область калибровки 0x40000..0x5CFFF"
    )
    lines.append(variant1.rollback_markdown(rollback))

    lines.append("## Порядок прошивки")
    lines.append("")
    lines.append("1. Сохранить исходное чтение ЭБУ (полный образ 1 МиБ или калибровку 116 КиБ).")
    lines.append("2. Прошить файл области калибровки `0x40000..0x5CFFF` (тюнинг-флешер, ")
    lines.append("   «tune flash»); программа и нижняя область не записываются.")
    lines.append("3. Убедиться, что ЭБУ не сообщает ошибок по контрольным суммам ")
    lines.append("   (обе суммы в файле уже пересчитаны).")
    lines.append("4. Первый запуск — на месте, с контролем: холостой ход, отсечка, ")
    lines.append("   ограничитель скорости, ошибки в памяти неисправностей.")
    lines.append("")
    lines.append("## Проверка файла")
    lines.append("")
    lines.append("```")
    lines.append("python3 -m MS45.tools.checksums <файл> --explain")
    lines.append("python3 -m MS45.tools.stages --verify-manifest <манифест>.json --image <файл>")
    lines.append("```")
    lines.append("")
    lines.append("Вторая команда сверяет файл с манифестом: суммы, семейство, каждая ячейка ")
    lines.append("плана и отсутствие изменений вне плана и вне калибровки.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Проверка выпущенного файла по манифесту
# ---------------------------------------------------------------------------

#: Где искать стоковый исходник, чтобы доказать «изменено только запланированное».
STOCK_SEARCH_DIRS: tuple[Path, ...] = (
    FIRMWARE_DIR,
    SPDATEN_DIR,
    ROOT / "firmware_dumps",
)


@dataclass
class ManifestCheck:
    """Одна проверка файла по манифесту."""

    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ManifestVerification:
    """Итог проверки: что сошлось, что нет."""

    manifest_path: str
    image_path: str | None
    status: str  # VERIFIED | FAILED | NOT_BUILT
    checks: list[ManifestCheck] = field(default_factory=list)
    source_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "VERIFIED"

    def as_dict(self) -> dict:
        # Пути в отчёте — с прямыми слэшами: отчёт читают и на Windows.
        return {
            "manifest": pathfmt.to_posix(self.manifest_path),
            "image": None if self.image_path is None else pathfmt.to_posix(self.image_path),
            "status": self.status,
            "ok": self.ok,
            "source_found": None if self.source_path is None else pathfmt.to_posix(self.source_path),
            "checks": [item.as_dict() for item in self.checks],
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_stock_source(sha256: str, source_dir: str | Path | None = None) -> Path | None:
    """Найти в репозитории стоковый файл с таким sha256 (для сверки «до/после»)."""
    dirs = ([Path(source_dir)] if source_dir else []) + list(STOCK_SEARCH_DIRS)
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.bin")):
            try:
                if _sha256_file(path) == sha256:
                    return path
            except OSError:  # pragma: no cover - файл исчез во время обхода
                continue
    return None


def verify_manifest(
    manifest_path: str | Path,
    image_path: str | Path | None = None,
    *,
    source_dir: str | Path | None = None,
) -> ManifestVerification:
    """Проверить выпущенный файл по манифесту: суммы, семейство, ячейки, «ничего лишнего».

    Манифест — это то, что генератор отдал клиенту. Проверка отвечает на вопрос
    «тот ли это файл и записано ли в нём ровно то, что в наряде», не требуя
    доверия к тому, кто файл пересылал.
    """
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    verification = ManifestVerification(str(manifest_file), None, "FAILED")
    checks = verification.checks

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(ManifestCheck(name, bool(ok), detail))

    version = manifest.get("manifest_version")
    check(
        "manifest_version",
        isinstance(version, int) and 1 <= version <= MANIFEST_VERSION,
        f"версия схемы {version}, поддерживается до {MANIFEST_VERSION}",
    )
    status = manifest.get("status")
    result_block = manifest.get("result") or {}
    if status != "BUILT" or not result_block:
        check("status", status == "BUILT", f"статус {status}: проверять нечего, файл не выпущен")
        verification.status = "NOT_BUILT"
        return verification

    check("status", True, "BUILT")

    # Файл: сначала явный путь, иначе — имя из манифеста рядом с ним.
    if image_path is None:
        result_name = manifest.get("result_name")
        if result_name:
            candidate = manifest_file.parent / str(result_name)
            if candidate.exists():
                image_path = candidate
    if image_path is None:
        check("result_name", False, "файл не указан и в манифесте нет result_name")
        return verification
    image_file = Path(image_path)
    if not image_file.exists():
        check("image", False, f"нет файла {image_file}")
        return verification
    verification.image_path = str(image_file)
    image = image_file.read_bytes()

    expected_size = result_block.get("image_size")
    check("image_size", len(image) == expected_size, f"{len(image)} байт, в манифесте {expected_size}")
    actual_sha = hashlib.sha256(image).hexdigest()
    check("sha256", actual_sha == result_block.get("sha256"),
          f"sha256 {actual_sha[:16]}…" + ("" if actual_sha == result_block.get("sha256") else " не совпал"))

    try:
        cal, _ = ck.extract_cal(image)
        report = ck.verify_bytes(image, str(image_file))
    except ck.MS45Error as error:
        check("image", False, f"{getattr(error, 'code', 'ERROR')}: {error}")
        return verification

    check("checksums", report.ok,
          f"CRC-32 {'OK' if report.crc32_ok else 'FAIL'}"
          f" (0x{(report.crc32_stored or 0):08X}), MO3 {'OK' if report.mo3_ok else 'FAIL'}")
    check("family", report.family == manifest.get("family"),
          f"в файле {report.family}, в манифесте {manifest.get('family')}")
    if report.switch_value is not None:
        check(
            "switch",
            report.switch_value != ck.MO3_SWITCH_DISABLE,
            f"переключатель 0x{(report.switch_offset or 0):05X} = 0x{report.switch_value:02X}"
            + (" (выключен — так выпускать нельзя)" if report.switch_value == ck.MO3_SWITCH_DISABLE else ""),
        )

    # Слова сумм из манифеста должны совпасть с тем, что реально лежит в файле.
    # Смещения в манифесте — от начала файла (не от начала калибровки).
    word_problems: list[str] = []
    for word in result_block.get("checksum_words") or []:
        offset = int(str(word["offset"]), 16)
        stored = int.from_bytes(image[offset : offset + 4], "big")
        if offset + 4 > len(image) or stored != int(word["after"]):
            word_problems.append(f"{word['name']}@0x{offset:05X}")
    check("checksum_words", bool(result_block.get("checksum_words")) and not word_problems,
          f"{len(result_block.get('checksum_words') or [])} слова сумм совпадают с манифестом"
          + (f", расхождения: {', '.join(word_problems)}" if word_problems else ""))

    # Ячейки: в файле должно лежать ровно то, что записано в плане.
    plan = manifest.get("plan") or {}
    cells = [cell for item in plan.get("symbols", []) for cell in item.get("cells", [])]
    mismatched: list[str] = []
    for cell in cells:
        offset = int(str(cell["cal_offset"]), 16)
        size = int(cell["size_bytes"])
        raw = cal[offset : offset + size]
        if len(raw) != size or int.from_bytes(raw, "big") != int(cell["raw_after"]):
            mismatched.append(f"[{cell['index']}]@0x{offset:05X}")
    check("planned_cells", not mismatched and bool(cells),
          f"{len(cells) - len(mismatched)}/{len(cells)} ячеек совпадают с планом"
          + (f", расхождения: {', '.join(mismatched[:5])}" if mismatched else ""))

    # «Ничего лишнего»: ищем стоковый исходник и сверяем байтовую разницу.
    source_sha = manifest.get("source_sha256")
    source_path = find_stock_source(str(source_sha), source_dir) if source_sha else None
    if source_path is None:
        check("outside_plan_changes", True,
              "стоковый исходник не найден локально — проверка «ничего лишнего» пропущена, "
              "суммы и ячейки проверены")
    else:
        verification.source_path = str(source_path)
        source = source_path.read_bytes()
        if len(source) != len(image):
            check("outside_plan_changes", False,
                  f"размер исходника {len(source)} ≠ размера файла {len(image)}")
        else:
            # Разрешены только запланированные ячейки и байты контрольных сумм:
            # ровно то же множество, что проверяет пост-проверка ядра при выпуске.
            allowed: set[int] = set()
            for cell in cells:
                start = int(str(cell["image_offset"]), 16)
                allowed.update(range(start, start + int(cell["size_bytes"])))
            if not allowed:
                allowed = {int(str(item), 16) for item in plan.get("checksum_cells", [])}
            else:
                allowed.update(int(str(item), 16) for item in plan.get("checksum_cells", []))
            changed = [i for i, (a, b) in enumerate(zip(source, image)) if a != b]
            outside = [i for i in changed if i not in allowed]
            expected_bytes = result_block.get("changed_bytes")
            check("changed_bytes", expected_bytes is None or len(changed) >= expected_bytes,
                  f"изменено {len(changed)} байт (в манифесте {expected_bytes})")
            check("outside_plan_changes", not outside,
                  "изменённых байт вне плана нет" if not outside
                  else "байты вне плана: " + ", ".join(f"0x{i:05X}" for i in outside[:8]))
            # Калибровочная область: сверяем по границам файла.
            if len(image) > ck.CAL_SIZE:
                low, high = ck.EXT_CAL_OFFSET, ck.EXT_CAL_OFFSET + ck.CAL_SIZE
            else:
                low, high = 0, min(len(image), ck.CAL_SIZE)
            outside_cal = [i for i in changed if not (low <= i < high)]
            check("cal_region_only", not outside_cal,
                  "все изменения внутри калибровки" if not outside_cal
                  else "вне калибровки: " + ", ".join(f"0x{i:05X}" for i in outside_cal[:8]))

    verification.status = "VERIFIED" if all(item.ok for item in checks) else "FAILED"
    return verification


def verification_as_markdown(verification: ManifestVerification) -> str:
    """Отчёт проверки по-русски: для акта сдачи клиенту."""
    lines = [
        "# Проверка файла по манифесту",
        "",
        f"* манифест: {pathfmt.to_posix(verification.manifest_path)}",
        f"* файл: {pathfmt.to_posix(verification.image_path) if verification.image_path else '—'}",
        "* стоковый исходник: "
        + (pathfmt.to_posix(verification.source_path) if verification.source_path
           else 'не найден локально'),
        f"* итог: {verification.status}",
        "",
        "| проверка | итог | подробности |",
        "| --- | --- | --- |",
    ]
    for item in verification.checks:
        lines.append(f"| {item.name} | {'OK' if item.ok else 'НЕТ'} | {item.detail} |")
    lines.append("")
    if verification.status == "VERIFIED":
        lines.append("Файл соответствует манифесту: суммы сходятся, записано ровно запланированное, "
                     "изменений вне калибровки нет.")
    elif verification.status == "NOT_BUILT":
        lines.append("Манифест описывает стадию без выпущенного файла — проверять нечего.")
    else:
        lines.append("Файл НЕ соответствует манифесту. Не прошивать: сначала выяснить расхождение.")
    lines.append("")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    if getattr(args, "verify_manifest", None):
        return _run_verify(args)

    if not args.image:
        print("нужен --image (или --verify-manifest для проверки выпущенного файла)",
              file=sys.stderr)
        return 2
    image_path = Path(args.image)
    if not image_path.exists():
        print(f"нет файла {image_path}", file=sys.stderr)
        return 2
    request = json.loads(Path(args.request).read_text(encoding="utf-8")) if args.request else {}
    image = image_path.read_bytes()
    try:
        result = build(args.stage or request.get("stage", "stage1"), image, request,
                       source_name=image_path.name)
    except (cal.PatchError, pl.PolicyError, ck.MS45Error) as error:
        print(f"ошибка: {getattr(error, 'code', 'ERROR')}: {error}", file=sys.stderr)
        return 2

    if args.out:
        result.manifest["result_name"] = Path(args.out).name

    print(f"стадия {result.stage}: {result.status}, семейство {result.family}")
    for item in result.packages:
        print(f"  пакет {item.id:14s} {item.status:14s} {item.detail}")
    for finding in result.findings:
        print(f"  [{finding['severity'].upper()}] {finding['code']}: {finding['message']}")
    if args.manifest_out:
        Path(args.manifest_out).write_text(
            json.dumps(result.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if args.note_out:
        Path(args.note_out).write_text(
            build_note(result, image_path.name), encoding="utf-8", newline="\n"
        )
        print(f"наряд: {args.note_out}")
    if args.dry_run:
        print("(dry-run: файл не записан)")
        return 2 if result.status == "REJECTED" else 0
    if result.status != "BUILT" or result.result is None:
        print(f"файл не выпущен: статус {result.status}", file=sys.stderr)
        return 2
    if not args.out:
        print("нужен --out, чтобы записать файл", file=sys.stderr)
        return 2
    Path(args.out).write_bytes(result.result.image)
    print(f"записано: {args.out} (sha256 {result.result.sha256[:16]}…)")
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    """Режим ``--verify-manifest``: проверка выпущенного файла по манифесту."""
    try:
        verification = verify_manifest(args.verify_manifest, args.image, source_dir=args.source_dir)
    except (OSError, ValueError, KeyError) as error:
        print(f"ошибка чтения манифеста: {error}", file=sys.stderr)
        return 2
    print(f"проверка по манифесту: {verification.status}")
    for item in verification.checks:
        print(f"  [{'OK' if item.ok else 'НЕТ'}] {item.name}: {item.detail}")
    if verification.status == "VERIFIED":
        print(f"файл {verification.image_path} соответствует манифесту")
        code = 0
    elif verification.status == "NOT_BUILT":
        print("стадия не выпускала файл — проверять нечего")
        code = 0
    else:
        print("файл НЕ соответствует манифесту: прошивать нельзя", file=sys.stderr)
        code = 2
    if args.note_out:
        Path(args.note_out).write_text(
            verification_as_markdown(verification), encoding="utf-8", newline="\n"
        )
        print(f"отчёт проверки: {args.note_out}")
    return code


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()
    parser = argparse.ArgumentParser(
        description="Генератор MS45: Stage 1 / Stage 2 из заявки клиента"
    )
    parser.add_argument("--image", help="стоковая калибровка или полный образ")
    parser.add_argument("--stage", choices=sorted(STAGE_PACKAGES), help="stage1 или stage2")
    parser.add_argument("--request", help="JSON заявки клиента")
    parser.add_argument("--out", help="куда записать готовый файл")
    parser.add_argument("--note-out", help="куда записать наряд (Markdown)")
    parser.add_argument("--manifest-out", help="куда записать манифест (JSON)")
    parser.add_argument("--dry-run", action="store_true", help="только отчёт, без записи")
    parser.add_argument(
        "--verify-manifest",
        help="режим проверки: манифест выпущенного файла (файл — --image или result_name из манифеста)",
    )
    parser.add_argument(
        "--source-dir",
        help="где искать стоковый исходник для проверки «ничего лишнего» (по умолчанию — папки репозитория)",
    )
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
