# -*- coding: utf-8 -*-
"""Пресеты логирования Test0 по ЭБУ и функции (задание `docs/TASK_LOGS_RU.md`).

Пресет — машиночитаемое описание того, **какие job-ы и результаты включить
в Test0** для конкретной функции (Stage 0, зажигание, топливо, VANOS,
лимитеры/хлопки, сервис) и **как ехать**, чтобы лог стал доказательством для
соответствующего пакета (`docs/pack_log_requirements.json`).

Официального файлового формата пресетов у Test0 v2 нет (см.
`docs/TEST0_WEB_ANALYSIS_RU.md` §2): пресет собирается в UI. Поэтому файлы
`docs/test0_presets/<ЭБУ>_<функция>.json` — это **наше** описание для клиента
и для проверок; имена job-ов и результатов внутри взяты только из выгрузок
PRG (`docs/sgbd/*_jobs.md`), сведённых в `docs/test0_channels.json`.

Использование::

    python3 tools/test0_presets.py --write    # перезаписать docs/test0_presets/
    python3 tools/test0_presets.py --check    # сверить файлы с генератором
    python3 tools/test0_presets.py --list     # показать, что будет записано
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CHANNELS_FILE = REPO_ROOT / "docs" / "test0_channels.json"
REQUIREMENTS_FILE = REPO_ROOT / "docs" / "pack_log_requirements.json"
PRESETS_DIR = REPO_ROOT / "docs" / "test0_presets"

ECU_ORDER = ("MS41", "MS42", "MS43", "MS45")

#: Соответствие канала Test0 каналу требований пакетов (те имена, что в
#: ``docs/pack_log_requirements.json``); ``None`` — у пакетов такого канала нет.
PACK_CHANNELS = {
    "rpm": "rpm",
    "load": "load",
    "maf_airflow": "maf_airflow",
    "ignition": "ignition",
    "injector_pulse_width": "injector_pulse_width",
    "lambda_wideband": "lambda_wideband",
    "lambda_integrator": "lambda_integrator",
    "coolant_temp": "coolant_temp",
    "intake_temp": "intake_temp",
    "pedal": "pedal",
    "pedal_voltage": None,
    "knock_voltage_bank1": None,
    "knock_voltage_bank2": None,
    "knock_status": None,
    "maf_voltage": None,
    "lambda_bank1_voltage": None,
    "vehicle_speed": "vehicle_speed",
    "vanos_intake_actual": "vanos_intake_actual",
    "vanos_intake_target": "vanos_intake_target",
    "vanos_exhaust_actual": "vanos_exhaust_actual",
    "vanos_exhaust_target": "vanos_exhaust_target",
    "torque_actual": None,
    "battery_voltage": None,
    "fan_duty": None,
    "idle_actuator": None,
    "disa_status": None,
}

#: Функции: набор каналов, пакет требований и инструкция «как ехать».
FUNCTIONS: dict[str, dict] = {
    "stage0": {
        "title_ru": "Stage 0: общий контроль состояния (прогрев, холостой ход, городской круг)",
        "pack": None,
        "channels": [
            "rpm", "coolant_temp", "intake_temp", "maf_airflow", "pedal",
            "vehicle_speed", "battery_voltage", "lambda_bank1_voltage",
            "idle_actuator", "vanos_intake_actual",
        ],
        "drive_ru": [
            "Записать холодный пуск и прогрев до 90 °C (2–3 минуты).",
            "Проехать 10–15 минут в городском режиме (частичные нагрузки, 2–3 остановки).",
            "В конце — один плавный разгон до 4000 об/мин без полного газа.",
        ],
    },
    "ignition": {
        "title_ru": "Зажигание и детонация: запас угла на полном ходу",
        "pack": "ignition",
        "channels": [
            "rpm", "load", "ignition", "maf_airflow", "pedal",
            "knock_voltage_bank1", "knock_voltage_bank2",
            "coolant_temp", "intake_temp",
        ],
        "drive_ru": [
            "Одна передача (обычно 3-я), 3 разгона «газ в пол» с 2000 до 6000+ об/мин.",
            "Между разгонами 30–60 секунд спокойного движения (впуск должен остыть).",
            "Топливо в баке — то, под которое заказан файл (для доказательства по зажиганию — RON 98).",
            "Если слышна детонация или появился провал — прекратить заезд и записать это в карточку.",
        ],
    },
    "fuel": {
        "title_ru": "Топливо: широкая лямбда на полном ходу и топливные коррекции",
        "pack": "fuel",
        "channels": [
            "rpm", "load", "maf_airflow", "maf_voltage", "injector_pulse_width",
            "lambda_wideband", "lambda_integrator", "pedal",
            "coolant_temp", "intake_temp",
        ],
        "drive_ru": [
            "Подключить внешний широкополосный зонд (Innovate/AEM/PLX/14Point7/Techedge) и выбрать его в Test0 внешним источником.",
            "2 разгона «газ в пол» с 2000 до 6000+ об/мин на 3-й передаче.",
            "10 секунд ровного движения на частичной нагрузке (2000–2500 об/мин, педаль неподвижна).",
            "Прогрев: ОЖ не ниже 80 °C до начала заезда.",
        ],
    },
    "vanos": {
        "title_ru": "VANOS: фактические и заданные фазы по зонам",
        "pack": "vanos",
        "channels": [
            "rpm", "load", "vanos_intake_actual", "vanos_intake_target",
            "vanos_exhaust_actual", "vanos_exhaust_target",
            "coolant_temp", "pedal",
        ],
        "drive_ru": [
            "Только прогретый двигатель (ОЖ ≥ 80 °C).",
            "Удерживать по 3–5 секунд точки 2000/3000/4000/5000/6000 об/мин на нагрузках 150/300/450/600 мг/такт (где это безопасно).",
            "Один контрольный разгон «газ в пол» с 2000 до 6000 об/мин.",
        ],
    },
    "limits": {
        "title_ru": "Лимитеры, скорость и хлопки: проверка поведения после прошивки",
        "pack": None,
        "channels": [
            "rpm", "vehicle_speed", "load", "pedal", "ignition",
            "coolant_temp", "intake_temp", "lambda_bank1_voltage",
        ],
        "drive_ru": [
            "Проверка ограничителя оборотов: 2–3 коротких подхода к отсечке на 2-й/3-й передаче.",
            "Проверка ограничителя скорости — только на закрытой площадке.",
            "Хлопки проверяются только на машине без катализатора: 3–5 сбросов газа после прогрева.",
            "Записать в карточку ощущения и сработавшие ограничения.",
        ],
    },
    "octane": {
        "title_ru": "Октан-профиль: поведение зажигания на выбранном топливе",
        "pack": None,
        "channels": [
            "rpm", "load", "ignition", "maf_airflow", "pedal",
            "knock_voltage_bank1", "knock_voltage_bank2",
            "coolant_temp", "intake_temp", "lambda_bank1_voltage",
        ],
        "drive_ru": [
            "Заправиться топливом того октана, под который заказан файл (RON 91/95/98) и указать его в карточке.",
            "Одна передача (обычно 3-я): 3 разгона «газ в пол» с 2000 до 6000+ об/мин, между ними 30–60 секунд спокойного движения.",
            "Температура ОЖ перед заездом — не ниже 80 °C, впуск — как можно холоднее (между разгонами не стоять).",
            "Лог нужен, чтобы видеть, как ведёт себя угол на этом бензине; по нему же принимается решение о добавке зажигания.",
        ],
    },
    "disa_off": {
        "title_ru": "DISA off: поведение впуска на низких и средних оборотах",
        "pack": None,
        "channels": [
            "rpm", "load", "maf_airflow", "pedal", "coolant_temp",
            "intake_temp", "idle_actuator",
        ],
        "drive_ru": [
            "2 подхода: плавные разгоны с 1500 до 3500 об/мин на 40–60 % педали, по 5–8 секунд каждый.",
            "10 секунд ровного движения на 2000 об/мин — посмотреть, нет ли вялости/провала внизу.",
            "Холостой ход 1 минуту: убедиться, что обороты стоят (после отключения DISA это первое, что проверяют).",
            "Если появилась ошибка по DISA/VIM — записать код в карточку, лог всё равно присылать.",
        ],
    },
    "isa_off": {
        "title_ru": "ISA/ICV off: холостой ход и запуск без клапана холостого хода",
        "pack": None,
        "channels": [
            "rpm", "coolant_temp", "intake_temp", "idle_actuator",
            "battery_voltage", "pedal", "lambda_bank1_voltage",
        ],
        "drive_ru": [
            "Записать холодный пуск (первый запуск дня) — как держит обороты первые 2 минуты.",
            "Прогретый холостой ход 2 минуты, в том числе с включённым кондиционером (1 минута).",
            "Короткие перегазовки с возвратом на холостой ход — 3 раза по 3–5 секунд.",
            "Если холостой ход плавает или глохнет — прекратить и написать в карточке; лог всё равно нужен.",
        ],
    },
    "idle": {
        "title_ru": "Обороты холостого хода: проверка целевого значения",
        "pack": None,
        "channels": [
            "rpm", "coolant_temp", "intake_temp", "idle_actuator",
            "battery_voltage", "pedal", "vehicle_speed",
        ],
        "drive_ru": [
            "Прогреть двигатель до 90 °C, затем 2 минуты на холостом ходу.",
            "Включить кондиционер на 1 минуту и выключить: посмотреть, как отрабатываются обороты.",
            "Дать нагрузку на холостом ходу (фары, обогрев) — записать просадку.",
            "Сообщить, на каких оборотах держится холостой ход на прогретом моторе.",
        ],
    },
    "fan_thermostat": {
        "title_ru": "Вентилятор и термостат: пороги включения и температура",
        "pack": None,
        "channels": [
            "coolant_temp", "intake_temp", "fan_duty", "rpm",
            "battery_voltage", "vehicle_speed",
        ],
        "drive_ru": [
            "Дать прогретому двигателю постоять на холостом ходу до включения вентилятора (обычно 2–5 минут).",
            "Записать момент включения и выключения вентилятора и температуру в эти моменты.",
            "Повторить цикл ещё раз (вентилятор включается дважды: сначала первая ступень).",
            "Просьба не проверять термостат на трассе: нужен именно прогретый холостой ход.",
        ],
    },
    "service": {
        "title_ru": "Сервис: холостой ход, вентилятор/термостат, DISA и ISA",
        "pack": None,
        "channels": [
            "rpm", "coolant_temp", "intake_temp", "idle_actuator",
            "fan_duty", "disa_status", "pedal", "battery_voltage",
        ],
        "drive_ru": [
            "2 минуты прогретого холостого хода; в конце — включить кондиционер на 1 минуту.",
            "Дать двигателю нагреться до включения вентилятора и записать момент включения.",
            "Если в логе нет DISA/ISA — проверить, что в Test0 выбраны job-ы из этого пресета.",
        ],
    },
}

#: Какие каналы пресета обязательны для пакета требований, но недоступны
#: через SGBD Test0 (замены и решения — в docs/TEST0_WEB_ANALYSIS_RU.md).
PACK_MISSING_RU = {
    "ignition": {
        "knock_retard": (
            "вердикта детонации (°) в SGBD MS41–MS43 нет (только сырые ADC), "
            "у MS45 имя STATUS_KLOPFEN не подтверждено; замена — расчёт по "
            "поведению угла зажигания (нижняя граница доказательства, "
            "решение — docs/TEST0_WEB_ANALYSIS_RU.md §6)"
        ),
        "knock_cylinders": (
            "поцилиндровых датчиков в PRG нет: у MS41–MS43 — два датчика "
            "(банки), используются как вспомогательный фильтр пиков"
        ),
    },
    "fuel": {
        "lambda_wideband": (
            "внутренняя лямбда MS4x узкополосная (0–1 В); канал даёт только "
            "внешний зонд Test0 ([external_source]/[14point7])"
        ),
    },
    "vanos": {},
}

TEST0_UI_RU = [
    "File → Detect ECU: проверить, что определился правильный ЭБУ и версия (не похожее семейство).",
    "В окне ЭБУ выбрать job из списка и перетащить в график нужные результаты (*_WERT).",
    "Settings → Logging: папка <Desktop>\\Test0_Logs, разделитель CSV и формат времени — по умолчанию.",
    "Внешний широкополосный зонд: подключить и выбрать как внешний источник ДО начала записи.",
    "Сохранить набор как пресет Test0 (имя TorqueX_<ЭБУ>_<функция>) — дальше запуск одной кнопкой.",
    "Перед заездом включить запись и убедиться, что все колонки заполняются.",
]


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def job_entries(channel: dict, ecu: str) -> list[dict]:
    """job-ы и результаты канала, доступные на этом ЭБУ (могут повторяться)."""
    entries = []
    for source in channel.get("ecus", {}).get(ecu, []):
        entry = {
            "job": source["job"],
            "results": list(source["results"]),
        }
        for key in ("note_ru", "ambiguous", "truncated_export"):
            if key in source:
                entry[key] = source[key]
        entries.append(entry)
    return entries


def build_preset(ecu: str, function: str, catalog: dict,
                 pack_spec: dict) -> dict:
    spec = FUNCTIONS[function]
    jobs: list[dict] = []
    unavailable: list[str] = []
    pack_covered: list[str] = []
    for name in spec["channels"]:
        channel = catalog["channels"][name]
        pack_name = PACK_CHANNELS.get(name)
        if pack_name:
            pack_covered.append(pack_name)
        if channel.get("external"):
            # Не SGBD-канал: включается в Test0 как внешний источник.
            jobs.append({
                "channel": name,
                "title_ru": channel["title_ru"],
                "source": "external_ru",
                "external_ru": (
                    "не SGBD: внешний источник Test0 "
                    "(testo.ini [external_source]/[14point7])"
                ),
                "unit_hint": channel.get("unit"),
                "note_ru": channel.get("note_ru"),
            })
            continue
        entries = job_entries(channel, ecu)
        if not entries:
            reason = channel.get("note_ru") or "нет job-а в PRG этого ЭБУ"
            unavailable.append(f"{name} ({channel['title_ru']}): {reason}")
            continue
        # Один канал — один job (лишние опросы снижают частоту); остальные
        # равнозначные источники перечисляем как альтернативы.
        entry = dict(entries[0])
        entry["channel"] = name
        entry["unit_hint"] = channel.get("unit")
        entry["title_ru"] = channel["title_ru"]
        if len(entries) > 1:
            entry["alternatives_ru"] = [
                f"{item['job']}: {', '.join(item['results'])}"
                for item in entries[1:]
            ]
        jobs.append(entry)

    preset: dict = {
        "schema_version": 1,
        "ecu": ecu,
        "prg_variants": list(catalog["ecus"][ecu]["prg_variants"]),
        "function": function,
        "title_ru": spec["title_ru"],
        "how_to_ru": TEST0_UI_RU,
        "test0_ru": (
            "Пресет собирается в UI Test0: выбрать job-ы и результаты из "
            "списка jobs ниже и сохранить набор под именем "
            f"TorqueX_{ecu}_{function}."
        ),
        "jobs": jobs,
        "unavailable_ru": unavailable,
        "drive_ru": list(spec["drive_ru"]),
    }
    if spec["pack"]:
        pack = spec["pack"]
        preset["pack"] = pack
        preset["pack_covered"] = sorted(set(pack_covered))
        missing = PACK_MISSING_RU.get(pack, {})
        if missing:
            preset["pack_missing_ru"] = dict(missing)
    return preset


def build_all() -> dict[Path, str]:
    """Все пресеты: {путь: JSON-текст} (порядок стабилен)."""
    catalog = load_json(CHANNELS_FILE)
    requirements = load_json(REQUIREMENTS_FILE)
    result: dict[Path, str] = {}
    for ecu in ECU_ORDER:
        for function in FUNCTIONS:
            preset = build_preset(ecu, function, catalog, requirements["packages"])
            path = PRESETS_DIR / f"{ecu}_{function}.json"
            result[path] = json.dumps(preset, ensure_ascii=False, indent=2) + "\n"
    return result


def write_all() -> list[Path]:
    written = []
    for path, text in build_all().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        written.append(path)
    return written


def check_all() -> list[str]:
    """Расхождения между файлами на диске и генератором."""
    problems = []
    presets = build_all()
    for path, text in presets.items():
        if not path.is_file():
            problems.append(f"нет файла: {path.relative_to(REPO_ROOT)}")
            continue
        if path.read_text(encoding="utf-8") != text:
            problems.append(
                f"файл устарел: {path.relative_to(REPO_ROOT)} — "
                "пересоздайте: python3 tools/test0_presets.py --write"
            )
    for path in sorted(PRESETS_DIR.glob("*.json")):
        if path not in presets:
            problems.append(
                f"лишний файл: {path.relative_to(REPO_ROOT)} — такого пресета "
                "нет в генераторе"
            )
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Пресеты логирования Test0 (MS41–MS45) из docs/test0_channels.json")
    parser.add_argument("--write", action="store_true",
                        help="перезаписать docs/test0_presets/")
    parser.add_argument("--check", action="store_true",
                        help="сверить файлы на диске с генератором")
    parser.add_argument("--list", action="store_true",
                        help="показать список пресетов")
    args = parser.parse_args(argv)

    if args.write:
        for path in write_all():
            print(f"записан: {path.relative_to(REPO_ROOT)}")
    if args.list:
        for path in build_all():
            print(path.relative_to(REPO_ROOT))
    if args.check:
        problems = check_all()
        for problem in problems:
            print(f"[НЕТ] {problem}")
        print(f"пресетов: {len(build_all())}, расхождений: {len(problems)}")
        return 1 if problems else 0
    if not (args.write or args.check or args.list):
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
