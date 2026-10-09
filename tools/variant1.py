# -*- coding: utf-8 -*-
"""Вариант 1 («Stage 1 базовый»): общие ворота и тексты для генераторов MS43/MS45.

Задание `docs/TASK_VARIANT1_RU.md` §1 (состав), §2 (правовая справка) и §5.3
(генератор); веб-анализ `docs/VARIANT1_WEB_ANALYSIS_RU.md` (§4 — правовая
справка, §5/§6/§8 — опции, §7 — октан). Модуль **не пишет байты** и ничего не
знает про образы ЭБУ: здесь только выбор клиента, ворота подтверждений и тексты
для манифеста и наряда. Числа и адреса — в политиках версий.

Что здесь есть:

* профиль топлива RON91/RON95/RON98 задаёт потолок добавки в карту RON98
  (единый источник — ``tools/ignition_caps.py`` и
  ``docs/pack_log_requirements.json``): RON98 — до +3,0° без лога и до +5,0°
  с логом детонации, RON95 — половинный потолок без лога, RON91 — карта не
  трогается и остаётся защитным полом;
* правовая справка (RU+EN) и ворота ``acknowledge_legality`` — для хлопков и
  диагностического слоя «без катализатора»;
* блок отката на сток для манифеста и наряда: sha256 стока, порядок возврата,
  чем проверить выпущенный файл;
* список опций варианта 1 (``disa_off``, ``isa_off``, ``idle``,
  ``fan_thermostat``): почему не выпускаются сегодня и что нужно, чтобы
  выпустить. Вердикты — из веб-анализа, не выдуманные.
"""

from __future__ import annotations

try:  # импортованный из репозитория (tests, MS4x/tools) и прямой запуск из tools/
    from tools import ignition_caps
except ImportError:  # pragma: no cover
    import ignition_caps  # type: ignore

#: Уровень доверия к выпущенным файлам (задание §6). Пока ни один файл не
#: проверен на живой машине, публикуется только первая строка; вторая появится
#: после отчётов по `docs/FIELD_TEST_PROTOCOL_RU.md`.
TRUST_LEVEL_BYTES_CRC = "bytes+crc verified"
TRUST_LEVEL_FIELD = "field verified"
#: Пометка «заявлено/проверено» для README, манифеста и наряда.
TRUST_LEVEL_STATEMENT = (
    f"{TRUST_LEVEL_BYTES_CRC}: байты и контрольные суммы сверены; "
    f"{TRUST_LEVEL_FIELD}: на реальном ЭБУ не проверялось"
)


def trust_note_ru() -> str:
    """Одна строка для наряда: что проверено, а что нет."""
    return (
        f"уровень доверия: {TRUST_LEVEL_BYTES_CRC} (байты вне плана не менялись, "
        f"суммы пересчитаны и проверены). {TRUST_LEVEL_FIELD} — нет: на живой "
        "машине этот файл не проверялся, первый выезд — по "
        "docs/FIELD_TEST_PROTOCOL_RU.md"
    )


#: Дата проверки правовых ссылок (совпадает с веб-анализом §4).
LEGAL_NOTICE_DATE = "2026-10-07"

#: Профили топлива. ``add_allowed`` = «в этом профиле генератор имеет право
#: писать добавку в карту RON98» (в пределах потолка профиля из
#: ``tools/ignition_caps.py``); у RON91 добавки нет вовсе — карта остаётся
#: защитным полом. Числа в текстах не хардкодятся: их подставляет
#: ``profile_note_ru`` из единой спецификации, чтобы текст не мог разойтись
#: с воротами.
OCTANE_PROFILES: dict[str, dict] = {
    "ron98": {
        "title": "RON98",
        "add_allowed": True,
    },
    "ron95": {
        "title": "RON95",
        # Раунд 4 (docs/TASK_STAGE1_RU.md §1): RON95 получает малый потолок
        # без лога — запись идёт только в карту RON98, а на рабочей точке
        # 95-го топлива ЭБУ сам интерполирует между RON91/RON98 по выученному
        # октану, так что фактическая добавка строго меньше записанной.
        "add_allowed": True,
    },
    "ron91": {
        "title": "RON91",
        "add_allowed": False,
    },
}


def profile_note_ru(profile: str | None) -> str:
    """Строка профиля для наряда и манифеста — с числами из спецификации."""
    no_log = ignition_caps.cap_no_log_deg(profile)
    with_log = ignition_caps.cap_with_log_deg(profile)
    if profile == "ron91":
        return (
            "профиль RON91: консервативный режим — карты зажигания стоковые, "
            "работает защита по детонации; карта RON91 как пол не трогается; "
            f"добавка не пишется вовсе (потолок {no_log:g}°)"
        )
    if profile is None:
        full = ignition_caps.delta_without_log_deg()
        return (
            "профиль топлива не выбран: добавка возможна только в карту RON98 "
            f"и без лога ограничена консервативным потолком +{no_log:g}°; для "
            f"полного потолка Stage 1 (+{full:g}°) выберите RON98, карта RON91 "
            "не изменяется"
        )
    title = OCTANE_PROFILES[profile]["title"]
    return (
        f"профиль {title}: добавка угла пишется только в карту RON98 — "
        f"без лога детонации до +{no_log:g}°, с проверенным логом (Stage 2) "
        f"до +{with_log:g}°; карта RON91 не изменяется и остаётся защитным "
        "полом, ЭБУ между картами интерполирует по выученному октану"
    )


#: Как клиент может назвать топливо в заявке.
OCTANE_ALIASES: dict[str, str] = {
    "98": "ron98", "ron98": "ron98", "ron_98": "ron98", "98ron": "ron98",
    "95": "ron95", "ron95": "ron95", "ron_95": "ron95", "95ron": "ron95",
    "91": "ron91", "ron91": "ron91", "ron_91": "ron91", "91ron": "ron91",
}

#: Пакеты-опции варианта 1, которые сегодня честно ждут данных.
OPTION_TITLES: dict[str, str] = {
    "disa_off": "DISA off (фиксация заслонок + диагностика)",
    "isa_off": "ISA off / обход клапана холостого хода",
    "idle": "Холостой ход (обороты, прогрев)",
    "fan_thermostat": "Вентилятор и термостат (трек-режим)",
}

#: Почему опция не выпускается и что нужно, чтобы её выпустить.
#: Формулировки — из веб-анализа §5 (DISA), §6 (ISA), §8 (вентилятор/термостат).
OPTION_NEEDS: dict[str, dict] = {
    "disa_off": {
        "why": (
            "«DISA off» — не одна константа: карты задают положение заслонки, а "
            "диагностика привода живёт отдельными счётчиками; запись требует "
            "согласования с механикой конкретной машины (заслонка снята или "
            "зафиксирована) и проверки адресов на стоке этой версии; подавление "
            "счётчика ошибку убирает, но функцию не отключает"
        ),
        "needs": (
            "состояние механики: заслонка снята физически или зафиксирована, каким "
            "способом и в каком положении (подтверждение клиента)",
            "причина обращения (треск/износ/подсос/ошибка привода) и артикул коллектора",
            "лог DISA/наполнения до и после (у MS42/MS43 положение не измеряется — "
            "проверка по отсутствию ошибок, расходу и моменту в зоне переключения)",
            "проверка адресов объектов версии на стоке этой версии (процедура проекта) "
            "и подтверждение, что побочный эффект (потеря момента внизу) принят клиентом",
        ),
    },
    "isa_off": {
        "why": (
            "удаление клапана холостого хода без заглушки канала ХХ глохнет и даёт "
            "P0505; подавление диагностики (c_abc_inc_isa_*) убирает код, но не "
            "регулирование ХХ — функция остаётся"
        ),
        "needs": (
            "подтверждение клиента: клапан снят/заглушен, канал ХХ заглушен, ХХ "
            "держится дросселем, риск неустойчивого ХХ принят",
            "лог устойчивости ХХ (разброс оборотов, регулятор ХХ) до и после",
            "правка карт холостого хода вместе с подавлением диагностики "
            "(отдельно друг от друга не выпускается)",
        ),
    },
    "idle": {
        "why": (
            "карты холостого хода правятся только вместе с проверкой устойчивости "
            "ХХ и адаптаций (холодный и прогретый мотор, климат/нагрузка)"
        ),
        "needs": (
            "замер холостого хода на прогретом и холодном двигателе (до/после)",
            "проверка адаптаций и отсутствия «плавания» оборотов",
            "подтверждение, что климат/кондиционер и нагрузка не выводят обороты "
            "за пределы",
        ),
    },
    "fan_thermostat": {
        "why": (
            "«трек-режим» (раннее охлаждение) правит цели термостата и карты "
            "вентилятора; слишком низкая целевая температура на M54B30 ощущается "
            "как потеря мощности, а по MS41 объектов вентилятора/термостата в "
            "определениях нет вовсе — вердикт по каждому семейству свой"
        ),
        "needs": (
            "сценарий эксплуатации (трек/город) и подтверждение клиента, что "
            "раннее охлаждение ему нужно именно для трека",
            "лог температур ОЖ/впуска и включения вентилятора до/после",
            "проверка, что цель не уходит ниже порога входа ЭБУ в диагностику прогрева, "
            "и что зимой прогрев остаётся в допуске",
        ),
    },
}

#: Как называть функции в сообщении ворот (для правовой справки).
LEGAL_GATE_KINDS = {
    "popcorn": "хлопки (pops & bangs)",
    "pops": "хлопки (pops & bangs)",
    "dtc": "диагностический слой «без катализатора»",
    "cat_delete": "удаление катализатора (слой диагностики)",
    "sap_delete": "удаление СВП (слой диагностики)",
}

LEGAL_NOTICE_RU: tuple[str, ...] = (
    "Правовая справка (не является юридической консультацией; проверено "
    f"{LEGAL_NOTICE_DATE}). Работа с системами контроля выбросов ограничена законом:",
    "ЕС — Регламент (ЕС) № 715/2007: определение «defeat device» — ст. 3 п. 10, "
    "запрет использования устройств, снижающих эффективность систем контроля "
    "выбросов — ст. 5 п. 2 (исключения: защита двигателя от повреждения или аварии; "
    "пуск двигателя). Дополнительно — Правила ЕЭК ООН № 83 (одобрение типа "
    "M1/M2/N1/N2 по выбросам, бортовая диагностика OBD, соответствие в эксплуатации).",
    "США — Clean Air Act § 203(a)(3): запрещено удалять или выводить из строя "
    "устройства контроля выбросов (п. A) и изготавливать, продавать или "
    "устанавливать снижающие их эффективность компоненты (п. B); 40 CFR § 1068.101(b). "
    "EPA прямо относит к таким компонентам программное обеспечение (тюны).",
    "Страна клиента — требования могут отличаться (Грузия, Россия, Германия и др.); "
    "клиент проверяет их самостоятельно и подтверждает acknowledge_legality: true.",
    "Эксплуатация на дорогах общего пользования без катализатора может нарушать "
    "технические и экологические требования. Хлопки допускаются только на машине "
    "без катализатора (иначе — перегрев и разрушение катализатора); удаление "
    "катализатора — только для использования вне дорог общего пользования.",
)

LEGAL_NOTICE_EN: tuple[str, ...] = (
    "Legal notice (not legal advice; verified " + LEGAL_NOTICE_DATE + "). Modifying "
    "emission control systems is restricted by law:",
    "EU — Regulation (EC) No 715/2007: definition of a \"defeat device\" in Art. 3(10), "
    "prohibition of devices that reduce the effectiveness of emission control systems "
    "in Art. 5(2) (exceptions: protecting the engine against damage or accident; "
    "engine starting). UNECE Regulation No. 83 sets type approval of M1/M2/N1/N2 "
    "vehicles for emissions, OBD and in-service conformity.",
    "USA — Clean Air Act § 203(a)(3)(A) (tampering) and § 203(a)(3)(B) "
    "(manufacturing/selling/installing defeat devices), 40 CFR § 1068.101(b); the EPA "
    "explicitly treats software (tunes) as covered.",
    "The customer's country may have different rules (Georgia, Russia, Germany, etc.) — "
    "the customer verifies them and confirms acknowledge_legality: true.",
    "Driving on public roads without a catalytic converter may violate technical and "
    "environmental requirements. Pops & bangs are allowed only on a car without a "
    "catalytic converter (otherwise: overheating and destruction of the catalyst); "
    "catalyst removal is for off-road use only.",
)


def _finding(findings: list[dict], code: str, severity: str, message: str) -> None:
    findings.append({"code": code, "severity": severity, "message": message})


# ---------------------------------------------------------------------------
# Октан: выбор клиента и ворота добавки
# ---------------------------------------------------------------------------


def _octane_intents(intents: list[dict]) -> list[dict]:
    return [item for item in intents if item.get("kind") == "octane"]


def octane_choice(request: dict, intents: list[dict], findings: list[dict]) -> str | None:
    """Профиль топлива RON91/RON95/RON98 (или ``None``, если не выбран).

    Профиль можно задать полем заявки ``octane`` или намерением
    ``{"kind": "octane", "fuel": "ron98"}``. Противоречие «поле и намерение не
    совпадают» — отказ (`OCTANE_PROFILE_CONFLICT`), а не молчаливый выбор.
    """
    raw = request.get("octane")
    profiles: set[str] = set()
    if raw is not None:
        profile = OCTANE_ALIASES.get(str(raw).strip().lower())
        if profile is None:
            _finding(
                findings, "OCTANE_PROFILE_UNKNOWN", "reject",
                f"octane: топливо {raw!r} неизвестно — выберите RON91, RON95 или RON98",
            )
            return None
        profiles.add(profile)
    for intent in _octane_intents(intents):
        value = intent.get("fuel", intent.get("value"))
        if value is None:
            _finding(
                findings, "OCTANE_PROFILE_UNKNOWN", "reject",
                f"{intent.get('id')}: в намерении octane укажите fuel = RON91/RON95/RON98",
            )
            return None
        profile = OCTANE_ALIASES.get(str(value).strip().lower())
        if profile is None:
            _finding(
                findings, "OCTANE_PROFILE_UNKNOWN", "reject",
                f"{intent.get('id')}: топливо {value!r} неизвестно — выберите RON91, "
                "RON95 или RON98",
            )
            return None
        profiles.add(profile)
    if len(profiles) > 1:
        _finding(
            findings, "OCTANE_PROFILE_CONFLICT", "reject",
            "профиль топлива задан противоречиво: " + ", ".join(sorted(profiles)),
        )
        return None
    return profiles.pop() if profiles else None


def enforce_octane(profile: str | None, intents: list[dict], findings: list[dict]) -> None:
    """Ворота профиля: потолок добавки зажигания задаёт топливо клиента.

    Смысл — §7 веб-анализа и §1 задания раунда 4: у MS42/MS43/MS45 база
    считается по двум картам (RON98 и RON91) с интерполяцией по выученному
    октану, поэтому добавка пишется только в карту RON98, а профиль клиента
    ограничивает запись:

    * RON98 — потолок Stage 1 из спецификации (без лога), с проверенным логом
      детонации — потолок Stage 2;
    * RON95 — малый потолок без лога (на рабочей точке 95-го топлива выходит
      ещё меньше из-за интерполяции), с логом — как RON98;
    * RON91 — добавка не пишется никогда (`OCTANE_PROFILE_NO_ADDITION`).

    Различие «без лога / с логом» здесь не проверяется: пакет сам потребует
    доказательство (requires_data), если добавка выше потолка без лога. Здесь
    — только максимальные права профиля, иначе «приложи лог» превратилось бы в
    отказ. Числа — из ``tools/ignition_caps.py``, не отсюда.
    """
    if profile is None:
        return
    title = OCTANE_PROFILES[profile]["title"]
    if ignition_caps.is_profile_rejected(profile):
        for intent in intents:
            if intent.get("kind") not in ("ignition", "ignition_stage1"):
                continue
            delta = intent.get("delta_deg")
            if delta is None:
                continue
            try:
                value = float(delta)
            except (TypeError, ValueError):
                continue
            if value > 0:
                label = intent.get("id") or intent.get("kind") or "ignition"
                _finding(
                    findings, "OCTANE_PROFILE_NO_ADDITION", "reject",
                    f"{label}: профиль {title} — добавка зажигания {value:g}° "
                    "не пишется (карты остаются стоковыми, ЭБУ интерполирует между "
                    "RON91/RON98 по выученному октану)",
                )
        return
    cap = ignition_caps.cap_with_log_deg(profile)
    for intent in intents:
        if intent.get("kind") not in ("ignition", "ignition_stage1"):
            continue
        delta = intent.get("delta_deg")
        if delta is None:
            continue
        try:
            value = float(delta)
        except (TypeError, ValueError):
            continue
        if value > cap + 1e-9:
            label = intent.get("id") or intent.get("kind") or "ignition"
            _finding(
                findings, "OCTANE_PROFILE_LIMIT", "reject",
                f"{label}: профиль {title} — добавка {value:g}° выше максимального "
                f"права профиля (+{cap:g}° даже с логом детонации)",
            )



def octane_report(profile: str | None) -> dict:
    """Профиль топлива для манифеста и наряда (``None`` — не заявлен).

    Потолки в отчёте — из единой спецификации (``tools/ignition_caps.py``),
    поэтому наряд не может пообещать то, что ворота не пропустят.
    """
    def caps_of(name: str | None) -> dict:
        return {
            "no_log_deg": ignition_caps.cap_no_log_deg(name),
            "with_log_deg": ignition_caps.cap_with_log_deg(name),
        }

    def profile_row(name: str) -> dict:
        spec = OCTANE_PROFILES[name]
        return {
            "title": spec["title"],
            "add_allowed": spec["add_allowed"],
            "note_ru": profile_note_ru(name),
            "caps": caps_of(name),
        }

    if profile is None:
        return {
            "profile": None,
            "fuel": None,
            "add_allowed": False,
            "note_ru": profile_note_ru(None),
            "caps": caps_of(None),
            "profiles": {name: profile_row(name) for name in OCTANE_PROFILES},
        }
    spec = OCTANE_PROFILES[profile]
    return {
        "profile": profile,
        "fuel": spec["title"],
        "add_allowed": spec["add_allowed"],
        "note_ru": profile_note_ru(profile),
        "caps": caps_of(profile),
        "profiles": {name: profile_row(name) for name in OCTANE_PROFILES},
    }


# ---------------------------------------------------------------------------
# Правовая справка: ворота подтверждения и тексты
# ---------------------------------------------------------------------------


def legality_gate(request: dict, intents: list[dict], findings: list[dict],
                  kinds: tuple[str, ...]) -> bool:
    """Хлопки/диагностика катализатора — только с ``acknowledge_legality``.

    Подтверждение принимается полем заявки или самим намерением. Без него
    заявка отклоняется (`ACKNOWLEDGE_LEGALITY_REQUIRED`) до расчёта пакетов:
    правовая справка выдаётся вместе с файлом, а не «когда-нибудь потом».
    """
    relevant = [item for item in intents if item.get("kind") in kinds]
    if not relevant:
        return True
    if request.get("acknowledge_legality") is True:
        return True
    missing = [
        str(item.get("id") or item.get("kind")) for item in relevant
        if item.get("acknowledge_legality") is not True
    ]
    if not missing:
        return True
    labels = ", ".join(LEGAL_GATE_KINDS.get(kind, str(kind)) for kind in kinds)
    _finding(
        findings, "ACKNOWLEDGE_LEGALITY_REQUIRED", "reject",
        f"{labels}: нужно подтверждение acknowledge_legality: true "
        "(правовая справка в наряде: Регламент (ЕС) 715/2007 ст. 3 п. 10 / ст. 5 п. 2, "
        "Правила ЕЭК ООН № 83, Clean Air Act § 203(a)(3), 40 CFR § 1068.101(b)); "
        "не подтверждено в: " + ", ".join(missing),
    )
    return False


def legal_notice_payload() -> dict:
    """Правовая справка для манифеста (RU + EN, с датой проверки)."""
    return {
        "date": LEGAL_NOTICE_DATE,
        "disclaimer_ru": "не является юридической консультацией",
        "text_ru": list(LEGAL_NOTICE_RU),
        "text_en": list(LEGAL_NOTICE_EN),
    }


def legal_notice_markdown() -> str:
    """Правовая справка для наряда: русский текст, затем английский."""
    lines = ["## Правовая справка", ""]
    for paragraph in LEGAL_NOTICE_RU:
        lines.append(f"> {paragraph}")
        lines.append(">")
    if lines and lines[-1] == ">":
        lines.pop()
    lines.append("")
    lines.append("Legal notice (EN):")
    lines.append("")
    for paragraph in LEGAL_NOTICE_EN:
        lines.append(f"> {paragraph}")
        lines.append(">")
    if lines and lines[-1] == ">":
        lines.pop()
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Откат на сток
# ---------------------------------------------------------------------------


def rollback_block(source_sha256: str, *, verify_command: str | None = None,
                   image_kind: str = "полный образ") -> dict:
    """Блок отката для манифеста: sha256 стока, порядок возврата, проверка.

    Сток — тот образ, из которого собран файл (``source_sha256``); это и есть
    «sha256 стока» из задания. Порядок возврата одинаков для всех семейств:
    сохранить сток до записи, хранить его побайтово, вернуть записью полного
    образа и сверить хэш.
    """
    instructions = [
        "исходный сток сохраняется копией до записи: ровно тот файл, чей sha256 "
        f"указан в поле source_sha256 ({source_sha256[:16]}…)",
        "возврат — записью этого файла тем же способом, что и тюн (полная запись "
        f"{image_kind}); ЭБУ после отката принимается штатной диагностикой",
        "после возврата сверить sha256 поступившего файла с source_sha256 манифеста; "
        "несовпадение — файл повреждён или это другой сток, записывать нельзя",
    ]
    block = {
        "stock_sha256": source_sha256,
        "image_kind": image_kind,
        "instructions_ru": instructions,
    }
    if verify_command:
        block["verify_command"] = verify_command
        block["instructions_ru"].append(
            f"проверка выпущенного файла перед записью и после отката: {verify_command}"
        )
    return block


def rollback_markdown(block: dict) -> str:
    """Раздел наряда «Откат на сток»."""
    lines = ["## Откат на сток", ""]
    lines.append(f"* sha256 стока (исходный образ): `{block['stock_sha256']}`")
    for item in block.get("instructions_ru") or ():
        lines.append(f"* {item}")
    if block.get("verify_command"):
        lines.append("")
        lines.append("```")
        lines.append(str(block["verify_command"]))
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Опции варианта 1, которые честно ждут данных
# ---------------------------------------------------------------------------


def option_needs(kind: str) -> tuple[str, ...]:
    """Что нужно, чтобы выпустить опцию (пусто — опция не из этого списка)."""
    spec = OPTION_NEEDS.get(kind)
    return tuple(spec["needs"]) if spec else ()


def option_why(kind: str) -> str:
    spec = OPTION_NEEDS.get(kind)
    return str(spec["why"]) if spec else ""


def options_requested(intents: list[dict]) -> dict[str, list[str]]:
    """Какие опции запрошены и какими намерениями (kind → id-ы)."""
    requested: dict[str, list[str]] = {}
    for intent in intents:
        kind = intent.get("kind")
        if kind in OPTION_TITLES:
            requested.setdefault(kind, []).append(str(intent.get("id") or kind))
    return requested
