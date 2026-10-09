#!/usr/bin/env python3
"""Политика записи MS43 (allowlist): какие символы можно писать и в каких границах.

Задание (`MS43/TASK_RU.md`, пункт 3) требует **свои** имена MS43 (имена MS42
не искать), адреса и границы — из стоковых образов. Здесь три вещи:

* ``CATALOG`` — каталог символов MS43 с группой, источником и заметкой;
* ``build_policy`` — сборка политики: адреса читаются из определения XDF,
  границы берутся из объявленного диапазона объекта, а стоковые значения —
  измеряются по 18 образам ``MS43/firmware``;
* ``validate_policy`` — проверки, без которых политика не выпускается:
  каждый символ обязан существовать в XDF, любое стоковое значение обязано
  лежать в границах, у дублирующихся имён обязан быть выбран ``xdf_index``.

Правило проекта: новый символ сначала попадает в политику с границами из
стоков, и только потом его может писать ядро. Символ без источника в базе
знаний в политику не попадает — у каждой записи есть поле ``source``.

Границы — это *пределы данных*, а не продуктовая политика. Продуктовые ворота
(«выше стока — acknowledge_risk», «выше 6750 об/мин — acknowledge_oil_pump»,
запрет хлопков на машине с катализатором) реализует генератор: он сравнивает
запрос со ``stock`` из этой политики. Так же устроен MS42.

Что такое ``stock``: значения, измеренные на стоковых образах, — не то же
самое, что сток конкретной машины клиента. Все скаляры здесь одинаковы во всех
18 образах, кроме перечисленных ниже (см. ``notes``); таблицы, как правило,
различаются, поэтому для них печатается диапазон.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from MS43.tools.xdf import (
        Definition,
        XDF_512K,
        XdfError,
        XdfObject,
        Scaler,
        load_definition,
    )
    from MS43.tools.patches import DEFAULT_CATALOG as DEFAULT_PATCH_CATALOG
    from MS43.tools.patches import PatchCatalog, bin_files, load_catalog as load_patch_catalog
    from MS43.tools.console import prepare_stdout
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    from xdf import (  # type: ignore[no-redef]
        Definition,
        XDF_512K,
        XdfError,
        XdfObject,
        Scaler,
        load_definition,
    )
    from patches import DEFAULT_CATALOG as DEFAULT_PATCH_CATALOG  # type: ignore[no-redef]
    from patches import (  # type: ignore[no-redef]
        PatchCatalog,
        bin_files,
        load_catalog as load_patch_catalog,
    )
    from console import prepare_stdout  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = ROOT / "firmware"
ARCHIVE_DIR = ROOT / "firmware_archive"
DEFINITIONS_DIR = ROOT / "definitions"
POLICIES_DIR = ROOT / "policies"
DEFAULT_POLICY = POLICIES_DIR / "stage_policy.json"
POLICY_VERSION = 1

#: Версия калибровки, на которой собрана и проверена политика по умолчанию
#: (``stage_policy.json``): адреса объектов, границы и стоковые значения сняты с
#: 18 образов MS430069 из ``MS43/firmware``, патчлист сверен с basedata этих же
#: образов.
DEFAULT_SOFTWARE = "430069"

#: Реестр «версия калибровки → файл политики».  Ядро выбирает политику по версии,
#: прочитанной из образа (``calibration.check_cal_version``): версии нет в реестре —
#: отказ ``CAL_VERSION_UNSUPPORTED``.  Порядок ключей — порядок работы по заданию.
VERSION_POLICY_FILES: dict[str, Path] = {
    DEFAULT_SOFTWARE: DEFAULT_POLICY,
    "430056": POLICIES_DIR / "stage_policy_430056.json",
    "430037": POLICIES_DIR / "stage_policy_430037.json",
    "430055": POLICIES_DIR / "stage_policy_430055.json",
    "430066": POLICIES_DIR / "stage_policy_430066.json",
}

#: Определения MS4X (официальные, ``ms4x-net/ms43``) на каждую покрытую версию:
#: ``(512 КиБ, 64 КиБ)``.  Адреса политики версии берутся только отсюда — перенос
#: адресов 430069 запрещён (``MS43/docs/VERSION_COVERAGE_RU.md``).
VERSION_DEFINITIONS: dict[str, tuple[Path, Path]] = {
    DEFAULT_SOFTWARE: (
        DEFINITIONS_DIR / "Siemens_MS43_430069_Extended_Load_512K.xdf",
        DEFINITIONS_DIR / "Siemens_MS43_430069_Extended_Load_64K.xdf",
    ),
    "430037": (
        DEFINITIONS_DIR / "MS430037_Extended_Load_512K.xdf",
        DEFINITIONS_DIR / "MS430037_Extended_Load_64K.xdf",
    ),
    "430055": (
        DEFINITIONS_DIR / "MS430055_Extended_Load_512K.xdf",
        DEFINITIONS_DIR / "MS430055_Extended_Load_64K.xdf",
    ),
    "430056": (
        DEFINITIONS_DIR / "MS430056_Extended_Load_512K.xdf",
        DEFINITIONS_DIR / "MS430056_Extended_Load_64K.xdf",
    ),
    "430066": (
        DEFINITIONS_DIR / "MS430066_Extended_Load_512K.xdf",
        DEFINITIONS_DIR / "MS430066_Extended_Load_64K.xdf",
    ),
}

#: Версии калибровки, для которых политика проверена целиком (есть файл политики).
SUPPORTED_SOFTWARE: tuple[str, ...] = tuple(VERSION_POLICY_FILES)

#: Версии MS43, встречающиеся в стоках и в ``MS43/firmware_archive``.
#: Версии без политики служат только для понятного текста отказа, права на запись
#: они не дают: у 430064 в архиве только 64 КиБ чтения, полного образа нет, поэтому
#: доказать адреса и границы по стокам этой версии нельзя.
KNOWN_SOFTWARE = ("430037", "430055", "430056", "430064", "430066", "430069")

#: Допустимая ошибка квантования при подборе сырого значения (проценты).
DEFAULT_MAX_QUANTIZATION_PERCENT = 1.0

#: Пороги для документации ворот (реализуются генератором, не политикой).
OIL_PUMP_ACK_RPM = 6750


class PolicyError(ValueError):
    """Политика не выпускается: данные не сходятся."""


@dataclass(frozen=True)
class CatalogEntry:
    """Символ MS43 в каталоге политики."""

    symbol: str
    group: str
    source: str
    note: str
    #: Переопределения границ. ``None`` — брать объявленный диапазон из XDF.
    minimum: float | None = None
    maximum: float | None = None
    max_delta_percent: float | None = None
    max_cells_per_patch: int | None = None
    require_monotonic: bool = False
    xdf_index: int | None = None
    #: Только чтение: символ пишется не инструментом, а проверяется (например,
    #: конфигурация катализатора нужна как признак для ворот хлопков).
    read_only: bool = False
    #: Явный контекст, в котором символ может быть записан.  Это не делает
    #: ``read_only`` обычным writable: ядро требует и этот контекст, и (для
    #: связанной MBT-карты) имя родительского символа.
    write_context: str | None = None


# --------------------------------------------------------------------------
# Каталог MS43
# --------------------------------------------------------------------------

#: Группы: limits (отсечка), vmax (ограничитель скорости), puc (ПХХ и хлопки),
#: full_load, disa, vanos, fuel, ignition, maf, torque, config.
CATALOG: tuple[CatalogEntry, ...] = (
    # --- ограничитель оборотов -------------------------------------------------
    CatalogEntry(
        symbol="id_n_max_mt__gear",
        group="limits",
        source="XDF 430069 EDT; MS4X wiki Siemens_MS43",
        note=(
            "Мягкий ограничитель оборотов, механика: одна уставка на передачу "
            "(6 ячеек), шаг 32 об/мин. Сток: 6464..6656 (4 варианта по 18 образам). "
            "Подъём выше стока требует acknowledge_risk, выше 6750 — "
            "acknowledge_oil_pump (ворота генератора)."
        ),
        minimum=2000,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="id_n_max_at__gear",
        group="limits",
        source="XDF 430069 EDT; MS4X wiki Siemens_MS43",
        note=(
            "Мягкий ограничитель оборотов, автомат, 6 передач по 32 об/мин. "
            "Сток: 6464..6624. Генератор правит только ту коробку, которая "
            "указана в запросе."
        ),
        minimum=2000,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="id_n_max_max_mt__gear",
        group="limits",
        source="XDF 430069 EDT; MS4X wiki Siemens_MS43",
        note=(
            "Жёсткий ограничитель оборотов (полный cut), механика. Сток: "
            "6592..6688 — всегда выше мягкого. Порядок ступеней проверяет ядро."
        ),
        minimum=2000,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="id_n_max_max_at__gear",
        group="limits",
        source="XDF 430069 EDT; MS4X wiki Siemens_MS43",
        note="Жёсткий ограничитель, автомат. Сток: 6592..6720.",
        minimum=2000,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="id_n_max_vs_max_mt__gear",
        group="limits",
        source="XDF 430069 EDT",
        note=(
            "Ограничитель оборотов при ограничении скорости, механика: 16 бит, "
            "1 об/мин на единицу. Сток: 6500..8000 (у части образов стоит 8000 — "
            "потолок в данных, а не в железе)."
        ),
        minimum=0,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="id_n_max_vs_max_at__gear",
        group="limits",
        source="XDF 430069 EDT",
        note="То же для автомата. Сток: 6500..8000.",
        minimum=0,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="c_n_max_hys",
        group="limits",
        source="XDF 430069 EDT",
        note=(
            "Гистерезис возврата впрыска после отсечки, 8 бит по 32 об/мин. "
            "Сток: 0 или 32 (два варианта)."
        ),
        minimum=0,
        maximum=512,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_n_max_hys_max",
        group="limits",
        source="XDF 430069 EDT",
        note=(
            "Гистерезис возврата всех форсунок, 8 бит по 32 об/мин. "
            "Сток: 320 или 800."
        ),
        minimum=0,
        maximum=1600,
        max_cells_per_patch=1,
    ),
    # --- ограничитель скорости -------------------------------------------------
    CatalogEntry(
        symbol="c_vs_max_mt_1",
        group="vmax",
        source="XDF 430069 EDT; MS4X wiki Siemens_MS43",
        note=(
            "Порог мягкого ограничителя скорости (гашение цилиндров), механика, "
            "1 км/ч на единицу. Сток: 206/240/250/255. 255 — «выключить». "
            "Для M54 заводского предела выше 250 км/ч нет."
        ),
        minimum=0,
        maximum=255,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_vs_max_at_1",
        group="vmax",
        source="XDF 430069 EDT",
        note="То же для автомата. Сток: 206/238/240/245/255.",
        minimum=0,
        maximum=255,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_vs_max_add",
        group="vmax",
        source="XDF 430069 EDT",
        note=(
            "Добавка к порогу для ЖЁСТКОГО ограничителя скорости (отсечка "
            "подачи): c_vs_max_mt_1 + c_vs_max_add, шаг 0.125 км/ч. "
            "Сток: 5.0 во всех 18 образах."
        ),
        minimum=0,
        maximum=31.875,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_vs_max_hys",
        group="vmax",
        source="XDF 430069 EDT",
        note="Гистерезис ограничителя скорости, км/ч. Сток: 3.",
        minimum=0,
        maximum=20,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_vs_max_hys_1",
        group="vmax",
        source="XDF 430069 EDT",
        note="Второй гистерезис, шаг 0.125 км/ч. Сток: 3.0 или 4.0.",
        minimum=0,
        maximum=31.875,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_vs_max_cycnr",
        group="vmax",
        source="XDF 430069 EDT",
        note=(
            "Задержка между шагами гашения цилиндров при ограничении скорости. "
            "Сток: 1 во всех 18 образах."
        ),
        minimum=0,
        maximum=255,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_vs_max_fl",
        group="vmax",
        source="XDF 430069 EDT",
        note=(
            "Порог скорости, при котором ограничитель скорости работает как "
            "full load (полная нагрузка), км/ч. Сток: 250 или 255."
        ),
        minimum=0,
        maximum=255,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_n_min_vs_max",
        group="vmax",
        source="XDF 430069 EDT",
        note=(
            "Минимальные обороты, с которых действует ограничитель скорости, "
            "8 бит по 32 об/мин. Сток: 800 во всех 18 образах. 8160 — снять "
            "ограничение по оборотам (это отдельное изменение в наряде)."
        ),
        minimum=0,
        maximum=8160,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_n_max_vs_max_hys",
        group="vmax",
        source="XDF 430069 EDT",
        note="Гистерезис по оборотам для ограничителя скорости. Сток: 160 или 300.",
        minimum=0,
        maximum=8160,
        max_cells_per_patch=1,
    ),
    # --- ПХХ и хлопки ----------------------------------------------------------
    CatalogEntry(
        symbol="ip_iga_puc__n__tco",
        group="puc",
        source="XDF 430069 EDT; MS4X wiki Siemens_MS43",
        note=(
            "Угол зажигания при отключении подачи (ПХХ), 6x4. Сток: от -15.0 "
            "до +10.875 °CRK. Именно эта карта формирует хлопок при позднем угле; "
            "правка допустима только на машине без катализатора (ворота в наряде)."
        ),
        minimum=-23.625,
        maximum=10.875,
        max_cells_per_patch=24,
    ),
    CatalogEntry(
        symbol="ip_n_min_puc__tco",
        group="puc",
        source="XDF 430069 EDT",
        note=(
            "Минимальные обороты активации ПХХ, 6 значений. Сток: "
            "(1600, 1344, 1152, 1056, 1024, 1024) или "
            "(1600, 1344, 1216, 1152, 1088, 1056)."
        ),
        minimum=0,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="c_n_hys_max_puc",
        group="puc",
        source="XDF 430069 EDT",
        note="Верхний гистерезис ПХХ по оборотам, 32 об/мин на единицу. Сток: 480 или 640.",
        minimum=0,
        maximum=1600,
        max_cells_per_patch=1,
    ),
    # --- хлопки: семейство «A/C on» и задержка ПХХ -----------------------------
    # Источник имён и смысла: MS4X wiki, Siemens_MS43, раздел «Exhaust Pop
    # Modifications» (toggleable pops: ip_n_min_accin_puc__tco,
    # ip_iga_accin_puc__n__tco; timered pops: c_t_puc_deacc_vs,
    # ip_t_puc_deacc_1..5__n__maf_mmv). Адреса, формы и масштабы — из определения
    # версии; стоки проверены на образах всех покрытых версий.
    CatalogEntry(
        symbol="ip_iga_accin_puc__n__tco",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note=(
            "Угол зажигания при ПХХ с включённым кондиционером, 6x4. Парная карта "
            "к ip_iga_puc__n__tco: хлопки должны быть одинаковыми при любом "
            "состоянии A/C, поэтому генератор пишет обе. Сток: -16.875..+11.25 °CRK."
        ),
        minimum=-23.625,
        maximum=11.25,
        max_cells_per_patch=24,
    ),
    CatalogEntry(
        symbol="ip_n_min_accin_puc__tco",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note=(
            "Минимальные обороты активации ПХХ с кондиционером, 6 значений. "
            "Парный объект к ip_n_min_puc__tco. Сток: 1056..1600 об/мин."
        ),
        minimum=0,
        maximum=8160,
        max_cells_per_patch=6,
    ),
    CatalogEntry(
        symbol="c_t_puc_deacc_vs",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note=(
            "Задержка активации ПХХ на месте (timered pops), секунды, 16 бит. "
            "Сток: 0..0.325 с. В XDF у объекта нет объявленного диапазона, "
            "границы выведены из шкалы (0.00017066667*X, 0..65535 сырых)."
        ),
        minimum=0,
        maximum=11.184560,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="ip_t_puc_deacc_1__n__maf_mmv",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note=(
            "Задержка активации ПХХ, 1-я передача (timered pops), 6x4, секунды. "
            "В XDF единицы поля подписаны «mg/stk» (это единицы осей n/maf_mmv), "
            "а шкала 0.00017066667*X даёт секунды: сток 0.6..2.45 с согласуется "
            "с описанием вики. Границы выведены из шкалы."
        ),
        minimum=0,
        maximum=11.184560,
        max_cells_per_patch=24,
    ),
    CatalogEntry(
        symbol="ip_t_puc_deacc_2__n__maf_mmv",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note="Задержка активации ПХХ, 2-я передача (timered pops), 6x4, секунды.",
        minimum=0,
        maximum=11.184560,
        max_cells_per_patch=24,
    ),
    CatalogEntry(
        symbol="ip_t_puc_deacc_3__n__maf_mmv",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note="Задержка активации ПХХ, 3-я передача (timered pops), 6x4, секунды.",
        minimum=0,
        maximum=11.184560,
        max_cells_per_patch=24,
    ),
    CatalogEntry(
        symbol="ip_t_puc_deacc_4__n__maf_mmv",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note="Задержка активации ПХХ, 4-я передача (timered pops), 6x4, секунды.",
        minimum=0,
        maximum=11.184560,
        max_cells_per_patch=24,
    ),
    CatalogEntry(
        symbol="ip_t_puc_deacc_5__n__maf_mmv",
        group="puc",
        source="MS4X wiki Siemens_MS43 «Exhaust Pop Modifications»; XDF",
        note="Задержка активации ПХХ, 5-я передача (timered pops), 6x4, секунды.",
        minimum=0,
        maximum=11.184560,
        max_cells_per_patch=24,
    ),

    CatalogEntry(
        symbol="c_n_hys_puc_st",
        group="puc",
        source="XDF 430069 EDT",
        note="Порог статического гистерезиса ПХХ. Сток: 5504 или 5600.",
        minimum=0,
        maximum=8160,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_conf_n_hys_puc",
        group="puc",
        source="XDF 430069 EDT",
        note="Переключатель режима гистерезиса ПХХ (0/1). Сток: 0 или 1 — менять только с обоснованием.",
        minimum=0,
        maximum=1,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_conf_n_hys_puc_dri",
        group="puc",
        source="XDF 430069 EDT",
        note="Второй переключатель режима гистерезиса ПХХ (0/1). Сток: 0 или 1.",
        minimum=0,
        maximum=1,
        max_cells_per_patch=1,
    ),
    # --- full load -------------------------------------------------------------
    CatalogEntry(
        symbol="id_pvs_fl__n",
        group="full_load",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P0)",
        note=(
            "Порог педали для full load впрыска, 16 точек по оборотам. "
            "Сток различается: 44.988..66.545 %PVS. Символ Stage 1: пороги "
            "опускаются или выравниваются согласованно с ivvt/vim."
        ),
        minimum=0,
        maximum=100,
        max_cells_per_patch=16,
        write_context="pedal_response",
    ),
    CatalogEntry(
        symbol="id_pvs_fl_ivvt__n",
        group="full_load",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P0)",
        note=(
            "Порог педали для full load VANOS, 16 точек. Сток: 50.143..69.825 "
            "или 76.855..59.984. Согласуется с id_pvs_fl__n."
        ),
        minimum=0,
        maximum=100,
        max_cells_per_patch=16,
        write_context="pedal_response",
    ),
    CatalogEntry(
        symbol="id_pvs_fl_vim__n_vim",
        group="full_load",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P0)",
        note=(
            "Порог педали для full load DISA, 8 точек. Сток: "
            "(37.49..62.327) или (50.143..67.014)."
        ),
        minimum=0,
        maximum=100,
        max_cells_per_patch=8,
        write_context="pedal_response",
    ),
    # --- DISA ------------------------------------------------------------------
    CatalogEntry(
        symbol="id_vim_fl__n_vim",
        group="disa",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1/P2)",
        note=(
            "Положение заслонки DISA на full load по оборотам (0/1), 8 точек. "
            "Сток: 1 на низких оборотах, 0 на высоких (точка переключения "
            "различается: 5-я, 6-я или 7-я ячейка)."
        ),
        minimum=0,
        maximum=1,
        max_cells_per_patch=8,
    ),
    CatalogEntry(
        symbol="id_vim_pl__n_vim__maf",
        group="disa",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1/P2)",
        note=(
            "Положение DISA на частичной нагрузке, 8x6 (обороты x нагрузка), "
            "только 0/1. Сток: 4 варианта, включая полностью открытую и "
            "переключающуюся по нагрузке."
        ),
        minimum=0,
        maximum=1,
        max_cells_per_patch=48,
    ),
    CatalogEntry(
        symbol="c_maf_hys_vim",
        group="disa",
        source="XDF 430069 EDT (донор MS42→MS43: MS42/docs/DONOR_MS43_RU.md)",
        note=(
            "Гистерезис нагрузки для переключения DISA, шаг 10.8941 мг/стк "
            "(у MS42 другая шкала). Сток: 43.576 или 54.471."
        ),
        minimum=0,
        maximum=100,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_n_hys_vim",
        group="disa",
        source="XDF 430069 EDT (донор MS42→MS43)",
        note="Гистерезис по оборотам для DISA, 1 об/мин на единицу. Сток: 25, 30 или 32.",
        minimum=0,
        maximum=500,
        max_cells_per_patch=1,
    ),
    # --- VANOS -----------------------------------------------------------------
    CatalogEntry(
        symbol="ip_cam_sp_tco_2_in_fl__n",
        group="vanos",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1)",
        note=(
            "Задание впускного вала на full load, прогретый мотор, 16 точек. "
            "Сток: 79.875..126.0 °CRK, 5 вариантов. Основной источник прироста "
            "Stage 1 по веб-анализу — но только по логам (иначе REQUIRES_DATA)."
        ),
        minimum=60.0,
        maximum=155.625,
        max_cells_per_patch=16,
    ),
    CatalogEntry(
        symbol="ip_cam_sp_tco_2_ex_fl__n",
        group="vanos",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1)",
        note=(
            "Задание выпускного вала на full load, прогретый мотор, 16 точек. "
            "Шкала отрицательная (-0.375 на единицу): сырое 0..255 даёт -60..-155.625, "
            "сток -106.125..-79.875."
        ),
        minimum=-155.625,
        maximum=-60.0,
        max_cells_per_patch=16,
    ),
    CatalogEntry(
        symbol="ip_cam_sp_tco_1_in_fl__n",
        group="vanos",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1)",
        note="Задание впускного вала на full load, холодный мотор, 16 точек. Сток: 79.875..126.0.",
        minimum=60.0,
        maximum=155.625,
        max_cells_per_patch=16,
    ),
    CatalogEntry(
        symbol="ip_cam_sp_tco_1_ex_fl__n",
        group="vanos",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1)",
        note="Задание выпускного вала на full load, холодный мотор, 16 точек. Сток: -105.0..-82.5.",
        minimum=-155.625,
        maximum=-60.0,
        max_cells_per_patch=16,
    ),
    CatalogEntry(
        symbol="ip_cam_sp_tco_2_in_pl__n__maf_iv",
        group="vanos",
        source="docs/PACKS_WEB_ANALYSIS_RU.md §3; XDF 430069 EDT",
        note=(
            "Задание впускного вала на частичной нагрузке, прогретый мотор, "
            "16x12 (обороты x нагрузка). Пакет `vanos` пишет только тёплые "
            "карты частичной нагрузки и только по измеренному логу фаз "
            "(сдвиг ≤ ±5° от стока ячейки). Карты полной нагрузки не правятся: "
            "сток на верхних оборотах уже на механическом потолке (126°)."
        ),
        minimum=60.0,
        maximum=155.625,
        max_cells_per_patch=192,
        write_context="vanos_stage1",
    ),
    CatalogEntry(
        symbol="ip_cam_sp_tco_2_ex_pl__n__maf_iv",
        group="vanos",
        source="docs/PACKS_WEB_ANALYSIS_RU.md §3; XDF 430069 EDT",
        note=(
            "Задание выпускного вала на частичной нагрузке, прогретый мотор, "
            "16x12. Шкала отрицательная (-0.375 на единицу). Те же ворота, "
            "что у впускной карты частичной нагрузки."
        ),
        minimum=-155.625,
        maximum=-60.0,
        max_cells_per_patch=192,
        write_context="vanos_stage1",
    ),
    # --- топливо и зажигание ---------------------------------------------------
    CatalogEntry(
        symbol="ip_ti_fl__n",
        group="fuel",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1)",
        note=(
            "Коэффициент обогащения на full load, 16 точек. Сток: 5 вариантов, "
            "например 0.078..0.102 или 0.223..0.172. Менять только с широкой "
            "лямбдой: без логов смесь не подтверждается."
        ),
        minimum=0.0,
        maximum=0.4959999865,
        # предохранитель — ворота пакета (сдвиг λ ≤ 0.05 и не ниже λ 0.82,
        # только с логом широкой лямбды): проценты на бедных ячейках не работают
        max_delta_percent=None,
        max_cells_per_patch=16,
    ),
    CatalogEntry(
        symbol="ip_tib__n__maf",
        group="fuel",
        source="docs/PACKS_WEB_ANALYSIS_RU.md §2; MS4X wiki Siemens_MS43 (VANOS failsafe)",
        note=(
            "Резервная карта времени впрыска при отказе VANOS, 16x12, мс "
            "(0.0053333 на единицу, 16 бит). Пакет `fuel` (вариант 1) двигает "
            "её относительной добавкой ≤ +0.25 мс и ≤ +4 % на ячейку: это "
            "единственная топливная карта, которую безопасно трогать без "
            "лямбда-лога. Карты частичной нагрузки без лога не правятся."
        ),
        minimum=0.0,
        maximum=60.0,
        # предохранитель — абсолютные ворота пакета (≤0.25 мс и ≤4%): на
        # малых ячейках 0.6 мс добавка всегда «больше 10%»
        max_delta_percent=None,
        max_cells_per_patch=192,
    ),
    CatalogEntry(
        symbol="ip_iga_ron_98_pl_ivvt__n__maf",
        group="ignition",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1); MS4X wiki",
        note=(
            "Целевой угол зажигания RON98, 20x16 (обороты x нагрузка). Сток: "
            "5 вариантов, например 0.0..22.125 °CRK. Октановая адаптация и "
            "защита по детонации сохраняются: карту сдвигаем осторожно и "
            "проверяем логом детонации (иначе REQUIRES_DATA)."
        ),
        minimum=-23.625,
        maximum=72.0,
        # процентный предохранитель отключён: настоящие ворота пакета —
        # абсолютные (см. stages.ignition_package), на ячейках с малым углом
        # проценты всегда «превышены»
        max_delta_percent=None,
        max_cells_per_patch=320,
    ),
    CatalogEntry(
        symbol="ip_iga_ron_91_pl_ivvt__n__maf",
        group="ignition",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P1); docs/PACKS_WEB_ANALYSIS_RU.md §1.3",
        note=(
            "То же для RON91 (320 ячеек). Это защитный пол октановой адаптации: "
            "при длительной детонации ЭБУ сам выцветает в эту карту. Генератор "
            "её НЕ пишет (решение веб-анализа §1.3, п. 4): сдвиг пола уменьшает "
            "защиту. Запись возможна только ручным патчем осознанно."
        ),
        minimum=-23.625,
        maximum=72.0,
        max_delta_percent=None,
        max_cells_per_patch=320,
    ),
    # Оси карт зажигания — только чтение: нужны генератору, чтобы интерполировать
    # потолок ip_iga_optm_tco_2 на сетку карты RON98 (docs/PACKS_WEB_ANALYSIS_RU.md §1.2).
    CatalogEntry(
        symbol="ldpm_n_14",
        group="axes",
        source="XDF 430069 EDT (ось оборотов карт зажигания 20 точек)",
        note="Ось оборотов карт ip_iga_ron_91/98_pl_ivvt__n__maf; только чтение.",
        minimum=0,
        maximum=8160,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ldpm_maf_iga_2",
        group="axes",
        source="XDF 430069 EDT (ось нагрузки карт зажигания 16 точек)",
        note="Ось нагрузки карт ip_iga_ron_91/98_pl_ivvt__n__maf; только чтение.",
        minimum=0,
        maximum=2000,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ldpm_n_4",
        group="axes",
        source="XDF 430069 EDT (ось оборотов карт optm 16 точек)",
        note="Ось оборотов карт ip_iga_optm_tco_1/2__n__maf; только чтение.",
        minimum=0,
        maximum=8160,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ldpm_maf_iga_1",
        group="axes",
        source="XDF 430069 EDT (ось нагрузки карт optm 12 точек)",
        note="Ось нагрузки карт ip_iga_optm_tco_1/2__n__maf; только чтение.",
        minimum=0,
        maximum=2000,
        read_only=True,
    ),
    # Оси карт топлива и фаз (VANOS) — только чтение: по ним генератор
    # раскладывает обогащение и сдвиг фаз по зонам лога
    # (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.3). Адреса каждой версии — из её
    # определения (ссылки x/y осей этих карт), значения сверены на всех стоках
    # версии: монотонны и лежат в физических пределах.
    CatalogEntry(
        symbol="ldpm_n_5",
        group="axes",
        source="XDF (ось оборотов карт VANOS частичной нагрузки, 16 точек)",
        note="Ось оборотов ip_cam_sp_tco_2_in/ex_pl__n__maf_iv; только чтение.",
        minimum=0,
        maximum=8160,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ldpm_maf_ivvt_2",
        group="axes",
        source="XDF (ось нагрузки карт VANOS частичной нагрузки, 12 точек)",
        note="Ось нагрузки ip_cam_sp_tco_2_in/ex_pl__n__maf_iv, мг/такт; только чтение.",
        minimum=0,
        maximum=2000,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ldpm_n_11",
        group="axes",
        source="XDF (ось оборотов карт топлива, 16 точек)",
        note="Ось оборотов ip_ti_fl__n и ip_tib__n__maf; только чтение.",
        minimum=0,
        maximum=8160,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ldpm_maf_8",
        group="axes",
        source="XDF (ось нагрузки карты топлива ip_tib__n__maf, 12 точек)",
        note="Ось нагрузки ip_tib__n__maf, мг/такт; только чтение.",
        minimum=0,
        maximum=2000,
        read_only=True,
    ),
    # --- ДМРВ ------------------------------------------------------------------
    CatalogEntry(
        symbol="id_maf_tab__v_maf",
        group="maf",
        source="MS4X wiki Siemens_MS43 (масштабирование форсунок/ДМРВ); XDF 430069 EDT",
        note=(
            "Передаточная функция ДМРВ, 256 точек, 0.015625 кг/ч на единицу. "
            "Сток: 0..1023.984 (у части образов 0..777.906). Кривая обязана "
            "оставаться монотонной. В определении есть второе представление тех "
            "же байтов (16x16), поэтому выбран xdf_index=0."
        ),
        minimum=0.0,
        maximum=1023.984375,
        max_delta_percent=30.0,
        max_cells_per_patch=256,
        require_monotonic=True,
        xdf_index=0,
    ),
    CatalogEntry(
        symbol="c_maf_max_diag",
        group="maf",
        source="MS4X wiki Siemens_MS43 (порог диагностики ДМРВ)",
        note=(
            "Порог диагностики «короткое на +» (кг/ч). Сток: 750 или 890. "
            "Поднимать только вместе с кривой ДМРВ, иначе ложные ошибки."
        ),
        minimum=0,
        maximum=2000,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_maf_min_diag",
        group="maf",
        source="MS4X wiki Siemens_MS43 (порог диагностики ДМРВ)",
        note="Порог диагностики «короткое на массу» (кг/ч). Сток: 4 во всех образах.",
        minimum=0,
        maximum=100,
        max_cells_per_patch=1,
    ),
    # --- момент ---------------------------------------------------------------
    CatalogEntry(
        symbol="c_conf_tq_lim_gear",
        group="torque",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P2)",
        note=(
            "Переключатель ограничения момента на первой передаче (0/1). "
            "Сток: 0 во всех 18 образах. Отключение ограничения — отдельное "
            "решение наряда, не автоматика Stage 1."
        ),
        minimum=0,
        maximum=1,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_tq_max_gear",
        group="torque",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P2)",
        note="Максимальный момент на первой передаче, Нм. Сток: 265 во всех образах.",
        minimum=0,
        maximum=500,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_tq_stnd",
        group="torque",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P2)",
        note=(
            "Стандартный момент двигателя, Нм. Сток: 400 во всех образах. "
            "Меняется только при смене мотора/железа, с провенансом."
        ),
        minimum=0,
        maximum=600,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="ip_tq_max__n__pvs_cor_rpl",
        group="torque",
        source="MS43/docs/STAGE1_WEB_ANALYSIS_RU.md §4 (P2)",
        note=(
            "Ограничение момента по оборотам и педали, 8x8. Сток: 3 варианта, "
            "диапазон 53..400 Нм. Изменение только с логами момента."
        ),
        minimum=0,
        maximum=500,
        max_delta_percent=20.0,
        max_cells_per_patch=64,
    ),
    # --- конфигурация (только чтение) -----------------------------------------
    CatalogEntry(
        symbol="c_conf_cat",
        group="config",
        source="MS4X wiki Siemens_MS43 (c_conf_cat, 0..4, сброс learned variants)",
        note=(
            "Конфигурация лямбда/катализатора (0..4). Сток: 3 или 4. "
            "Только для чтения: нужна как признак «есть катализатор» в воротах "
            "хлопков. Смена значения сбрасывает обученные варианты и меняет "
            "диагностику — в этой политике не пишется."
        ),
        minimum=0,
        maximum=4,
        read_only=True,
    ),
    # --- модель момента (только чтение) ---------------------------------------
    # Источник: MS43/docs/TORQUE_RU.md §2 — MS4X wiki CanTCU_Integration и
    # Siemens_MS43 (Torque Management), e46fanatics «MS43 tuning». Символы нужны
    # генератору, чтобы показать клиенту, что именно управляет моментом и
    # трансляцией момента в CAN; запись в Stage 1/2 не выполняется.
    CatalogEntry(
        symbol="ip_tqi_pvs__n__pvs",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Индицируемый момент (12x12), на него опирается передача момента в "
            "CAN для АКПП/SMG/DSC. Сток: 0..317 Нм. Не пишем: ошибка здесь "
            "ломает трансляцию момента в другие блоки."
        ),
        minimum=0,
        maximum=1023,
        read_only=True,
        write_context="pedal_response",
    ),
    CatalogEntry(
        symbol="ip_pvs_tqi__n__tqi",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Обратная таблица: максимальное открытие дросселя под запрос "
            "вмешательства по моменту (12x12). Сток: 0..60 Нм. Не пишем."
        ),
        minimum=0,
        maximum=60,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ip_tqi_maf__n__maf",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Теоретический момент по нагрузке и зажиганию (16x4), участвует в "
            "CAN-передаче (TQI_MAF_CAN). Не пишем."
        ),
        minimum=0,
        maximum=65535,
        read_only=True,
        write_context="pedal_response",
    ),
    CatalogEntry(
        symbol="ip_tqfr__n__maf",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Потери момента на трение (8x4). Используются при расчёте "
            "индицируемого момента. Не пишем."
        ),
        minimum=0,
        maximum=0.031219482*65535,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ip_iga_tqr__n__maf",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Максимально допустимый поздний угол при вмешательстве по моменту "
            "(9x8). Сток: -23.625..16.875 °CRK (в этой ячейке стока 0..108). "
            "Не пишем: увеличение позднего угла поднимает температуру выпуска "
            "и давление в коллекторе — по логам CanTCU это +70..80 кПа."
        ),
        minimum=-23.625,
        maximum=72.0,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ip_iga_tqr_amt__n__maf",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "То же для SMG/AMT (9x8). Сток: -23.625..9.0 °CRK. Не пишем."
        ),
        minimum=-23.625,
        maximum=72.0,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ip_iga_optm_tco_1__n__maf",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Оптимальный угол холодного мотора (16x12) — эталон для оценки "
            "потерь момента и температуры выпуска. Сток: -4.125..60.0 °CRK. "
            "Не пишем."
        ),
        minimum=-23.625,
        maximum=72.0,
        read_only=True,
    ),
    CatalogEntry(
        symbol="ip_iga_optm_tco_2__n__maf",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Оптимальный угол прогретого мотора (16x12) — эталон модели момента. "
            "Сток: -4.875..60.0 °CRK. Не пишем."
        ),
        minimum=-23.625,
        maximum=72.0,
        read_only=True,
        write_context="ignition_optm_link",
    ),
    CatalogEntry(
        symbol="ip_fac_tqd_cor__tqi_asr_can",
        group="torque",
        source="TORQUE_RU.md §2; MS4X wiki CanTCU_Integration",
        note=(
            "Коэффициент вмешательства DSC/ASR через CAN (4x1). Сток: 0..1. "
            "Не пишем: это защита от потери устойчивости."
        ),
        minimum=0.0,
        maximum=0.000015258789*65535,
        read_only=True,
    ),
    CatalogEntry(
        symbol="c_tq_max_msr",
        group="torque",
        source="TORQUE_RU.md §2; XDF 430069 EDT (Maximum allowed torque increase request)",
        note=(
            "Предел запроса на повышение момента при обнаружении блокировки "
            "задних колёс (MSR). Сток измеряется по 18 образам. Не пишем: "
            "входит в контур устойчивости."
        ),
        minimum=0,
        maximum=1023,
        read_only=True,
    ),
    # --- Подавление диагностических кодов (слой «без катализатора», задание 3) ---
    # Механизм: приращение счётчика неисправности за цикл; 0 — счётчик не
    # растёт и код не выставляется (мс4х-вики, «Suppressing Diagnostic Trouble
    # Codes»). Сток: 0..2 в зависимости от комплектации; у части стоков завод
    # сам держит 0 (например, DMTL на 430037) — механизм штатный. Пишем только
    # 0 и только при подтверждении удаления катализатора (ворота в генераторе).
    # --- Подавление диагностики привода DISA (VIM) и клапана ХХ (ISA) ---------
    # Отдельная задача (шаг 2 задания): адреса и стоковые значения сверены по
    # 18 образам 430069; это приращения счётчиков («Fault counter increment»),
    # 0 — ошибка водителя заслонки/клапана не выставляется. Позиции самих карт
    # (``id_vim_*``) и карты ХХ — отдельные объекты и отдельные правила.
    CatalogEntry(
        symbol="c_abc_inc_vim",
        group="dtc",
        source="XDF 430069 EDT (DISA - Fault counter increment); ms4x-вики, подавление DTC",
        note=(
            "Приращение счётчика ошибки привода DISA (VIM). 0 — код по приводу "
            "заслонки не выставляется; само поведение заслонки задают карты "
            "id_vim_* (подавление счётчика функцию не отключает). Запись только "
            "отдельным слоем диагностики — при подтверждении снятой/зафиксированной "
            "механики и правовой справке."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_isa_1",
        group="dtc",
        source="XDF 430069 EDT (Idlespeed actuator 1 - Fault counter increment)",
        note=(
            "Приращение счётчика ошибки клапана холостого хода (ISA, канал 1). "
            "0 — код не выставляется; регулирование ХХ и карты холостого хода "
            "остаются штатными (отдельный пакет «холостой ход» требует лога)."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_isa_2",
        group="dtc",
        source="XDF 430069 EDT (Idlespeed actuator 2 - Fault counter increment)",
        note=(
            "Приращение счётчика ошибки клапана холостого хода (ISA, канал 2). "
            "0 — код не выставляется; см. c_abc_inc_isa_1."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_conv_mon",
        group="dtc",
        source="XDF 430069 EDT (AD converter monitoring); мс4х-вики, подавление DTC",
        note=(
            "Приращение счётчика ошибки эффективности катализатора. Сток: 2 во "
            "всех 18 образах 430069. 0 — ошибка по катализатору не выставляется; "
            "запись только в слое «без катализатора»."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_dmtl",
        group="dtc",
        source="XDF 430069 EDT (Diagnostic module tank leakage); мс4х-вики, подавление DTC",
        note=(
            "Приращение счётчика утечки в баке (DMTL). Сток: 0 или 2 в зависимости "
            "от комплектации. 0 — код не выставляется."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_vdmtl",
        group="dtc",
        source="XDF 430069 EDT (Voltage supply for DMTL); мс4х-вики, подавление DTC",
        note=(
            "Приращение счётчика питания модуля DMTL. Сток: 0 или 2. 0 — код не "
            "выставляется; идёт вместе с c_abc_inc_dmtl."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_lsh_down",
        group="dtc",
        source="XDF 430069 EDT (Lambda sensor heater - Post cat); мс4х-вики, подавление DTC",
        note=(
            "Приращение счётчика подогрева лямбды ПОСЛЕ катализатора. Сток: 2. "
            "0 — код не выставляется (задний зонд удалён вместе с катализатором)."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_vls_down",
        group="dtc",
        source="XDF 430069 EDT (Lambda sensor voltage post cat); мс4х-вики, подавление DTC",
        note=(
            "Приращение счётчика напряжения задней лямбды. Сток: 1 (430069) или "
            "1..2 (430037). 0 — код не выставляется."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    # c_abc_inc_vls_down_post_puc в политику не входит: во всех образах всех
    # версий сток уже 0 — подавлять нечего (запись была бы бессмысленной).
    CatalogEntry(
        symbol="c_abc_inc_vls_down_t",
        group="dtc",
        source="XDF 430069 EDT (Lambda sensor voltage response time post cat); мс4х-вики",
        note="Приращение счётчика времени отклика задней лямбды. Сток: 2. 0 — код не выставляется.",
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    CatalogEntry(
        symbol="c_abc_inc_sap",
        group="dtc",
        source="XDF 430069 EDT (Secondary air pump); мс4х-вики, подавление DTC",
        note=(
            "Приращение счётчика насоса вторичного воздуха (СВП). Сток: 0 или 2 "
            "(комплектация). 0 — код не выставляется (насос удалён вместе с "
            "катализатором)."
        ),
        minimum=0,
        maximum=2,
        max_cells_per_patch=1,
    ),
    # Группа «лямбда перед катализатором» намеренно НЕ входит в слой: передний
    # зонд остаётся, его защита (lsh_up/vls_up) не трогается ни в одном варианте.
)


# --------------------------------------------------------------------------
# Сборка и проверка
# --------------------------------------------------------------------------


@dataclass
class StockStats:
    """Стоковые значения символа по всем образам, которые удалось прочитать."""

    samples: int = 0
    variants: int = 0
    minimum: float | None = None
    maximum: float | None = None
    uniform: bool = False
    monotonic: bool = True
    note: str = ""


@dataclass
class PolicyEntryData:
    symbol: str
    group: str
    source: str
    note: str
    address: int
    element_size_bits: int
    rows: int
    columns: int
    units: str
    scale_a: float
    scale_b: float
    minimum: float | None
    maximum: float | None
    max_delta_percent: float | None
    max_cells_per_patch: int | None
    require_monotonic: bool
    xdf_index: int | None
    read_only: bool
    #: Явный контекст записи; для read-only это дополнительное условие, а не
    #: снятие общей защиты.
    write_context: str | None
    #: Патч, без которого параметр не действует (для символов патчлиста).
    requires_patch: str | None
    #: Откуда символ: "xdf" (определение) или "patchlist" (каталог патчей).
    origin: str
    equation: str
    stock: StockStats
    range_low: float | None
    range_high: float | None


@dataclass
class Policy:
    version: int
    family: str
    software: tuple[str, ...]
    generated_note: str
    max_quantization_error_percent: float
    objects: dict[str, PolicyEntryData] = field(default_factory=dict)

    def entry(self, symbol: str) -> PolicyEntryData:
        return self.objects[symbol]

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "family": self.family,
            "software": list(self.software),
            "description": self.generated_note,
            "max_quantization_error_percent": self.max_quantization_error_percent,
            "oil_pump_ack_rpm": OIL_PUMP_ACK_RPM,
            "objects": {
                symbol: {
                    "min": entry.minimum,
                    "max": entry.maximum,
                    "max_delta_percent": entry.max_delta_percent,
                    "max_cells_per_patch": entry.max_cells_per_patch,
                    "require_monotonic": entry.require_monotonic,
                    "xdf_index": entry.xdf_index,
                    "read_only": entry.read_only,
                    "write_context": entry.write_context,
                    "requires_patch": entry.requires_patch,
                    "origin": entry.origin,
                    "group": entry.group,
                    "address": f"0x{entry.address:05X}",
                    "size_bits": entry.element_size_bits,
                    "shape": f"{entry.rows}x{entry.columns}",
                    "units": entry.units,
                    "equation": entry.equation,
                    "stock": {
                        "samples": entry.stock.samples,
                        "variants": entry.stock.variants,
                        "min": entry.stock.minimum,
                        "max": entry.stock.maximum,
                        "uniform": entry.stock.uniform,
                        "note": entry.stock.note,
                    },
                    "source": entry.source,
                    "note": entry.note,
                }
                for symbol, entry in sorted(self.objects.items())
            },
        }


def _stock_stats(
    definition: Definition,
    obj: XdfObject,
    images: list[bytes],
    base_offsets: list[int | None] | None = None,
) -> StockStats:
    """Измерить стоковое значение объекта по образам.

    ``base_offsets`` — база чтения для каждого образа: ``None`` — из определения
    (полный образ 512 КиБ), ``0`` — чтение калибровки 64 КиБ определением 512 КиБ
    (адрес объекта в определении относительный, у чтения база нулевая).
    """
    stats = StockStats()
    seen: list[tuple[float, ...]] = []
    for index, image in enumerate(images):
        base = None if base_offsets is None else base_offsets[index]
        values = tuple(round(v, 6) for v in definition.read_engineering(image, obj, base))
        seen.append(values)
        stats.samples += 1
        if obj.rows * obj.columns > 1:
            stats.monotonic &= all(a <= b for a, b in zip(values, values[1:]))
    unique = sorted(set(seen))
    stats.variants = len(unique)
    stats.minimum = min((min(v) for v in unique), default=None)
    stats.maximum = max((max(v) for v in unique), default=None)
    stats.uniform = len(unique) == 1
    if obj.rows * obj.columns == 1:
        stats.note = "одинаково во всех образах" if stats.uniform else f"вариантов: {len(unique)}"
    else:
        stats.note = (
            f"{len(unique)} вариантов, диапазон {stats.minimum}..{stats.maximum}"
        )
    return stats


#: Группа символов патчлиста по категории фичи.
PATCH_GROUP_BY_CATEGORY: dict[str, str] = {
    "Launch Control Parameters": "patch_lc",
    "LC / NLS / RAL General Settings": "patch_lc",
    "Rolling Anti Lag Parameters": "patch_lc",
    "OBD CVN Fix": "patch_lc",
    "M3/M5 Cluster LEDs": "patch_mcluster",
    "DS2 Logging Extensions": "patch_ds2",
    "MAF Based Full Load Enrichment": "patch_maf_fl",
    "MAF Sensor Hack": "patch_maf_hack",
    "Map Reduction": "patch_map_reduction",
    "Ignition Cut Limiter": "patch_ign_cut",
    "Alpha/N": "patch_alpha_n",
    "MIL Knock Indicator": "patch_mil_knk",
    "Boost Control Over Canister Purge Output": "patch_boost",
    "E3x Vehicle Speed Sensor Accuracy Improvement": "patch_vss",
    "Fuel Cut Table Switch Over Cruise Control": "patch_fuel_cut_sw",
    "CAN Bus Addons": "patch_can",
}

#: Категории патчлиста, символы которых не пишутся (справочные данные).
PATCH_READ_ONLY_CATEGORIES: set[str] = set()

#: Ручные уточнения по символам патчлиста: у части параметров в XDF нет
#: категории, поэтому привязка к фиче определена по имени, адресу и описанию
#: (все — внутри блоков LC/RAL 0x71316..0x71323 и MAF-FL 0x75D7F/0x70C3E).
#: ``exclude`` — символ не попадает в политику (нет уверенной привязки или это
#: идентификационные данные).
SYMBOL_OVERRIDES: dict[str, dict] = {
    "c_conf_lc": {"group": "patch_lc", "requires_patch": "[PATCH] Launch Control & Rolling Anti Lag"},
    "c_conf_lc_iga_rtd": {"group": "patch_lc", "requires_patch": "[PATCH] Launch Control & Rolling Anti Lag"},
    "c_n_max_lc": {"group": "patch_lc", "requires_patch": "[PATCH] Launch Control & Rolling Anti Lag"},
    "c_vs_max_lc": {"group": "patch_lc", "requires_patch": "[PATCH] Launch Control & Rolling Anti Lag"},
    # MIL «knock indicator» — отдельный патч, хотя в патчлисте эти ячейки
    # подписаны категорией LC/RAL.
    # Два флага уровней детонации лежат в ОДНОМ байте 0x707D0 (биты 0 и 1):
    # поштучно их писать нельзя (второй флаг потеряется), байт выставляет
    # [DATA] MIL Knock Indicator значением 0x03. Пишем только время c_t_max_knk_mil.
    "lv_mil_knk_lv_1": {
        "group": "patch_mil_knk", "requires_patch": "[PATCH] MIL Knock Indicator",
        "read_only": True,
    },
    "lv_mil_knk_lv_2": {
        "group": "patch_mil_knk", "requires_patch": "[PATCH] MIL Knock Indicator",
        "read_only": True,
    },
    "c_t_max_knk_mil": {"group": "patch_mil_knk", "requires_patch": "[PATCH] MIL Knock Indicator"},
    "c_conf_ral": {"group": "patch_lc", "requires_patch": "[PATCH] Launch Control & Rolling Anti Lag"},
    "c_pvs_min_ral": {"group": "patch_lc", "requires_patch": "[PATCH] Launch Control & Rolling Anti Lag"},
    "c_maf_fl_accin": {"group": "patch_maf_fl", "requires_patch": "[PATCH] MAF Based Full Load Enrichment"},
    "id_maf_fl_ivvt__n": {"group": "patch_maf_fl", "requires_patch": "[PATCH] MAF Based Full Load Enrichment"},
    "id_maf_fl_vim__n_vim": {"group": "patch_maf_fl", "requires_patch": "[PATCH] MAF Based Full Load Enrichment"},
    "ip_ti_fl__n__maf": {"group": "patch_maf_fl", "requires_patch": "[PATCH] MAF Based Full Load Enrichment"},
    "ip_ti_fl__n__maf___afr": {"group": "patch_maf_fl", "requires_patch": "[PATCH] MAF Based Full Load Enrichment", "read_only": True},
    "ip_ti_fl__n__maf___lambda": {"group": "patch_maf_fl", "requires_patch": "[PATCH] MAF Based Full Load Enrichment", "read_only": True},
    "id_maf_tab__v_maf (2048kg/h)": {"group": "patch_maf_hack", "read_only": True, "requires_patch": "[PATCH] MAF Sensor Hack 2048kg/h"},
    "id_maf_tab__v_maf (4096kg/h)": {"group": "patch_maf_hack", "read_only": True, "requires_patch": "[PATCH] MAF Sensor Hack 4096kg/h"},
    "id_maf_tab__v_maf_1__v_maf_2 (2048kg/h)": {"group": "patch_maf_hack", "read_only": True, "requires_patch": "[PATCH] MAF Sensor Hack 2048kg/h"},
    "id_maf_tab__v_maf_1__v_maf_2 (4096kg/h)": {"group": "patch_maf_hack", "read_only": True, "requires_patch": "[PATCH] MAF Sensor Hack 4096kg/h"},
    "ldp_toil_led": {"group": "patch_ds2", "read_only": True},
    "ldp_n_led": {"group": "patch_ds2", "read_only": True},
    "id_icl_toil_led__n": {"group": "patch_mcluster", "requires_patch": "[PATCH] M Cluster LED Support"},
    "id_icl_n_led__n": {"group": "patch_mcluster", "requires_patch": "[PATCH] M Cluster LED Support"},
    "CAN Bus Initialization Table": {"group": "patch_can", "read_only": True, "requires_patch": "[PATCH] Custom CAN - OLM Boost Gauge"},
    "CAN Bus ARBID Table": {"group": "patch_can", "read_only": True, "requires_patch": "[PATCH] Custom CAN - OLM Boost Gauge"},
    "ldp_n_vim__pvs_fl_vim": {"exclude": "нет категории в патчлисте: привязка к фиче не подтверждена"},
    "ldpm_maf_8": {"exclude": "нет категории в патчлисте: привязка к фиче не подтверждена"},
    "ldpm_n_11": {"exclude": "нет категории в патчлисте: привязка к фиче не подтверждена"},
    "User Information Fields": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "UIF Vehicle Identification Number": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "UIF Flash Date": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "UIF Software Number": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "UIF Service Number": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "UIF Assembly Number": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "ECU Number": {"exclude": "идентификационные данные загрузчика, не калибровка"},
    "ISN Value": {"exclude": "данные иммобилайзера: правятся патчем, а не политикой"},
    "Softwareversion": {"exclude": "строка версии, не калибровка"},
}

#: Читаются, но не пишутся: альтернативные шкалы (MAF hack) и служебные копии.
PATCH_READ_ONLY_SYMBOLS: set[str] = set()


def _representable(scaler: Scaler, size_bits: int) -> tuple[float, float]:
    """Диапазон инженерных значений, представимый сырым целым 0..2^n-1."""
    raw_max = (1 << size_bits) - 1
    first = scaler.to_engineering(0.0)
    last = scaler.to_engineering(raw_max)
    return (min(first, last), max(first, last))


def build_patch_entries(
    policy: Policy,
    patch_catalog: PatchCatalog,
    definition: Definition,
    images: list[bytes],
) -> None:
    """Добавить в политику параметры фич из патчлиста (символы патчей)."""
    titles = set(patch_catalog.titles())
    for symbol in patch_catalog.symbols:
        override = SYMBOL_OVERRIDES.get(symbol.title, {})
        if override.get("exclude"):
            continue
        requires_patch = override.get("requires_patch", symbol.requires_patch)
        if requires_patch is None:
            continue  # категория не привязана к патчу (справочные объекты)
        group = override.get("group") or PATCH_GROUP_BY_CATEGORY.get(symbol.category)
        if group is None:
            continue
        if requires_patch not in titles:
            raise PolicyError(
                f"{symbol.title}: требуется патч {requires_patch!r}, "
                "которого нет в каталоге"
            )
        if symbol.address < 0x10000 and symbol.kind == "table":
            # UIF/ISN в загрузчике: не калибровка, писать нечего
            continue
        scaler = Scaler.from_equation(symbol.equation)
        size = max(1, symbol.size_bits // 8)
        total = symbol.rows * symbol.columns
        minimum, maximum = _representable(scaler, symbol.size_bits)
        read_only = bool(
            override.get("read_only")
            or symbol.category in PATCH_READ_ONLY_CATEGORIES
            or symbol.title in PATCH_READ_ONLY_SYMBOLS
        )
        stock_values: list[float] = []
        for image in images:
            chunk = image[symbol.address : symbol.address + size * total]
            if len(chunk) != size * total:
                raise PolicyError(f"{symbol.title}: адрес 0x{symbol.address:05X} вне образа")
            for index in range(total):
                raw = int.from_bytes(chunk[index * size : (index + 1) * size], "little")
                stock_values.append(scaler.to_engineering(float(raw)))
        policy.objects[symbol.title] = PolicyEntryData(
            symbol=symbol.title,
            group=group,
            source=(
                # категорию источника не пишем: в патчлисте она позиционная и
                # часто не совпадает с фактической привязкой (см. PATCHLIST_RU.md)
                f"patchlist v2.9.2; патч {requires_patch}"
                + ("; привязка уточнена вручную" if override.get("requires_patch") else "")
            ),
            note=(
                f"{symbol.description or 'параметр фичи из community patchlist'}. "
                f"Действует только после патча {requires_patch}. "
                f"Сток в патчлисте: {symbol.stock_note}."
            ),
            # в патчлисте адрес сразу абсолютный (совпадает со смещением в файле)
            address=symbol.address,
            element_size_bits=symbol.size_bits,
            rows=symbol.rows,
            columns=symbol.columns,
            units=symbol.units,
            scale_a=scaler.a,
            scale_b=scaler.b,
            minimum=minimum,
            maximum=maximum,
            max_delta_percent=None,
            max_cells_per_patch=total,
            require_monotonic=False,
            xdf_index=None,
            read_only=read_only,
            write_context=None,
            requires_patch=requires_patch,
            origin="patchlist",
            equation=symbol.equation,
            stock=StockStats(
                samples=len(images),
                variants=len({round(v, 6) for v in stock_values}),
                minimum=min(stock_values),
                maximum=max(stock_values),
                uniform=len({round(v, 6) for v in stock_values}) == 1,
                monotonic=True,
                note=symbol.stock_note,
            ),
            range_low=None,
            range_high=None,
        )


def build_policy(
    definition: Definition | None = None,
    firmware_dir: Path = FIRMWARE_DIR,
    catalog: tuple[CatalogEntry, ...] = CATALOG,
    patch_catalog: PatchCatalog | None = None,
    *,
    software: tuple[str, ...] = (DEFAULT_SOFTWARE,),
    description: str | None = None,
    images: list[bytes] | None = None,
    base_offsets: list[int | None] | None = None,
    tolerance: set[str] = frozenset(),
    include_patchlist: bool = True,
) -> Policy:
    """Собрать политику: адреса из XDF, границы из диапазонов, сток — из образов.

    ``software`` — версия(и), которую покрывает собираемая политика (одна версия
    на файл).  ``images`` / ``base_offsets`` — готовый набор стоков и база чтения
    каждого (см. ``version_stock_images``); без них берутся полные образы из
    ``firmware_dir``.  ``tolerance`` — символы, которых нет в определении версии:
    они не входят в политику версии, и это записывается в описание (объект, не
    доказанный на версии, в политику версии не входит).
    """
    definition = definition or load_definition(XDF_512K)
    if images is None:
        images = [path.read_bytes() for path in sorted(firmware_dir.glob("*.bin"))]
        base_offsets = None
    if not images:
        raise PolicyError(f"нет стоковых образов в {firmware_dir}")
    if description is None:
        description = (
            "Поддерживаемая версия калибровки: 430069 (единственная в software); "
            "другие версии ядро отклоняет кодом CAL_VERSION_UNSUPPORTED. "
            "Allowlist MS43: адреса — абсолютные смещения в образе 512 КиБ "
            "(в XDF они относительные, BASEOFFSET 0x70000 прибавлен при сборке), "
            "масштабы — из определений MS4X XDF, границы — из объявленных диапазонов, "
            "стоковые значения измерены по 18 образам MS430069 в MS43/firmware. "
            "Продуктовые ворота (acknowledge_risk, acknowledge_oil_pump, "
            "запрет хлопков с катализатором) реализует генератор. "
            "Символы с origin=\"patchlist\" действуют только после "
            "соответствующего патча из MS43/patches/ (см. docs/PATCHLIST_RU.md)."
        )
    policy = Policy(
        version=POLICY_VERSION,
        family="MS43",
        software=software,
        generated_note=description,
        max_quantization_error_percent=DEFAULT_MAX_QUANTIZATION_PERCENT,
    )
    for item in catalog:
        matches = definition.objects.get(item.symbol)
        if not matches:
            if item.symbol in tolerance:
                continue
            raise PolicyError(f"{item.symbol}: символа нет в определении {definition.path.name}")
        if len(matches) > 1 and item.xdf_index is None:
            raise PolicyError(
                f"{item.symbol}: в определении {len(matches)} объекта(ов) с этим именем — "
                "нужен xdf_index, иначе непонятно, что писать"
            )
        obj = matches[item.xdf_index or 0]
        minimum = item.minimum if item.minimum is not None else obj.range_low
        maximum = item.maximum if item.maximum is not None else obj.range_high
        stock = _stock_stats(definition, obj, images, base_offsets)
        # Источник адреса — определение версии, а не 430069: в политике версии
        # строка источника обязана называть её собственное определение.
        source = item.source
        if DEFAULT_SOFTWARE not in software:
            source = source.replace(
                f"XDF {DEFAULT_SOFTWARE} EDT", f"XDF {software[0]} EDT"
            )
        policy.objects[item.symbol] = PolicyEntryData(
            symbol=item.symbol,
            group=item.group,
            source=source,
            note=item.note,
            # адрес в образе 512 КиБ: в определении он относительный (BASEOFFSET)
            address=definition.base_offset + obj.address,
            element_size_bits=obj.element_size_bits,
            rows=obj.rows,
            columns=obj.columns,
            units=obj.units,
            scale_a=obj.scaler.a,
            scale_b=obj.scaler.b,
            minimum=minimum,
            maximum=maximum,
            max_delta_percent=item.max_delta_percent,
            max_cells_per_patch=item.max_cells_per_patch,
            require_monotonic=item.require_monotonic,
            xdf_index=item.xdf_index,
            read_only=item.read_only,
            write_context=item.write_context,
            requires_patch=None,
            origin="xdf",
            equation=obj.equation,
            stock=stock,
            range_low=obj.range_low,
            range_high=obj.range_high,
        )
    if not include_patchlist:
        return policy
    if patch_catalog is None and DEFAULT_PATCH_CATALOG.exists():
        patch_catalog = load_patch_catalog(DEFAULT_PATCH_CATALOG)
    if patch_catalog is not None:
        build_patch_entries(policy, patch_catalog, definition, images)
    return policy


# ---------------------------------------------------------------------------
# Политики по версиям калибровки
# ---------------------------------------------------------------------------

#: Тексты-описания политик версий: что именно доказано и что намеренно не вошло.
VERSION_DESCRIPTIONS: dict[str, str] = {
    "430056": (
        "Политика версии 430056 (отдельная от 430069). Адреса объектов взяты из "
        "официального определения MS4X MS430056_Extended_Load_512K.xdf и подтверждены "
        "по определению 64 КиБ той же версии (адрес 512 КиБ минус BASEOFFSET 0x70000). "
        "Границы — объявленные диапазоны XDF, стоковые значения измерены по образам "
        "версии из MS43/firmware_archive (полные 512 КиБ и чтения 64 КиБ). Адреса "
        "430069 на эту версию не переносились. Символы патчлиста сообщества "
        "(origin=patchlist) в политику версии не входят: basedata патчлиста снята "
        "только с 430069, совместимость байтовых масок не доказана. См. "
        "MS43/docs/VERSION_COVERAGE_RU.md."
    ),
    "430037": (
        "Политика версии 430037 (отдельная от 430069). Адреса объектов взяты из "
        "официального определения MS4X MS430037_Extended_Load_512K.xdf и подтверждены "
        "определением 64 КиБ той же версии. Границы — объявленные диапазоны XDF, стоки "
        "измерены по образам версии (5 полных 512 КиБ + 6 чтений 64 КиБ). Объект "
        "ip_iga_tqr_amt__n__maf в определении 430037 отсутствует и в политику версии "
        "не входит. Символы патчлиста в политику не входят (basedata только 430069). "
        "См. MS43/docs/VERSION_COVERAGE_RU.md."
    ),
    "430055": (
        "Политика версии 430055 (отдельная от 430069). Адреса — из официального "
        "определения MS4X MS430055_Extended_Load_512K.xdf, подтверждены определением "
        "64 КиБ той же версии. Стоков у версии мало (1 полный образ + 2 чтения 64 КиБ), "
        "поэтому границы — объявленные диапазоны XDF, а стоковые значения — измеренные "
        "по трём образам. Символы патчлиста не входят. См. "
        "MS43/docs/VERSION_COVERAGE_RU.md."
    ),
    "430066": (
        "Политика версии 430066 (отдельная от 430069). Адреса — из официального "
        "определения MS4X MS430066_Extended_Load_512K.xdf, подтверждены определением "
        "64 КиБ той же версии и поиском таблиц по содержимому в образах версии. Стоки: "
        "1 полный образ + 4 чтения 64 КиБ. Символы патчлиста не входят. См. "
        "MS43/docs/VERSION_COVERAGE_RU.md."
    ),
}


def policy_path_for_version(version: str) -> Path | None:
    """Файл политики для версии калибровки (``None`` — версия не покрыта)."""
    return VERSION_POLICY_FILES.get(version)


def covered_software() -> tuple[str, ...]:
    """Версии, для которых есть политика (в порядке реестра)."""
    return tuple(VERSION_POLICY_FILES)


def load_policy_for_version(version: str) -> Policy | None:
    """Политика для версии калибровки; ``None`` — версия не покрыта или файла нет."""
    path = policy_path_for_version(version)
    if path is None or not path.exists():
        return None
    return load_policy(path)


def resolve_policy(image: bytes) -> Policy | None:
    """Политика по версии, прочитанной из образа.

    Единая точка выбора для ядра и генератора: версия читается из строки
    идентификации (``4300xx``), для неё берётся своя политика.  ``None`` — версия
    не покрыта, вызывающая сторона обязана отказать (``CAL_VERSION_UNSUPPORTED``).
    """
    try:
        from MS43.tools.checksum import read_cal_version
    except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
        from checksum import read_cal_version  # type: ignore[no-redef]
    version = read_cal_version(image)
    if version is None:
        return None
    return load_policy_for_version(version)


def version_stock_images(version: str) -> list[tuple[str, bytes, int | None]]:
    """Стоковые образы версии: ``(имя, байты, база чтения)``.

    Полный образ 512 КиБ читается определением 512 КиБ (``base=None``), чтение
    калибровки 64 КиБ — тем же определением с ``base=0`` (адрес объекта в
    определении относительный, у чтения калибровки база нулевая).  Файлы берутся из
    ``MS43/firmware`` (18 стоков 430069) и ``MS43/firmware_archive`` (остальные
    версии, расширения ``.bin`` и ``.Bin``).

    Файлы перечисляются через ``patches.bin_files``: на регистронезависимой
    файловой системе (Windows) ``glob("*.bin")`` и ``glob("*.Bin")`` возвращают
    одни и те же файлы, и счётчики стоков удваивались (430056 давала 20 образов
    вместо 10, см. ревью 2 — ``MS43/REVIEW2_CLAUDE_RU.md``).
    """
    try:
        from MS43.tools.checksum import read_cal_version
    except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
        from checksum import read_cal_version  # type: ignore[no-redef]
    paths: list[Path] = bin_files(FIRMWARE_DIR) + bin_files(ARCHIVE_DIR)
    images: list[tuple[str, bytes, int | None]] = []
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()
        if read_cal_version(data) != version:
            continue
        images.append((path.name, data, None if len(data) == 0x80000 else 0))
    return images


def definition_missing_symbols(version: str) -> tuple[str, ...]:
    """Символы каталога, которых нет в определении версии (в политику не входят)."""
    xdf_512 = VERSION_DEFINITIONS[version][0]
    if not xdf_512.exists():
        return ()
    definition = load_definition(xdf_512)
    return tuple(
        item.symbol for item in CATALOG if not definition.objects.get(item.symbol)
    )


def build_version_policy(
    version: str,
    *,
    definition: Definition | None = None,
) -> Policy:
    """Собрать политику одной версии: определение и стоки версии, без патчлиста.

    Патчлист сообщества (``origin=patchlist``) в политики других версий не
    попадает сознательно: его basedata снята с 18 стоков 430069, и пока
    совместимость байтовых масок на версии не доказана, фичи патчлиста на ней не
    предлагаются (см. ``MS43/docs/VERSION_COVERAGE_RU.md``).
    """
    if version not in VERSION_DEFINITIONS:
        raise PolicyError(f"версия {version}: нет определения в MS43/definitions")
    xdf_512, _xdf_64 = VERSION_DEFINITIONS[version]
    if not xdf_512.exists():
        raise PolicyError(
            f"версия {version}: нет определения {xdf_512.name}; "
            "оно скачивается MS43/tools/fetch_definitions.py"
        )
    definition = definition or load_definition(xdf_512)
    stocks = version_stock_images(version)
    if not stocks:
        raise PolicyError(f"версия {version}: нет стоковых образов (нужен полный 512 КиБ)")
    if not any(len(data) == 0x80000 for _name, data, _base in stocks):
        raise PolicyError(
            f"версия {version}: есть только чтения калибровки 64 КиБ, полного образа "
            "нет — адреса и границы доказать не по чему, политика не собирается"
        )
    if version == DEFAULT_SOFTWARE:
        # 430069 — существующая политика: 18 полных стоков из MS43/firmware и
        # каталог патчлиста. Ничего не меняем, чтобы влитая политика не поехала.
        return build_policy(definition)
    missing = tuple(
        item.symbol for item in CATALOG if not definition.objects.get(item.symbol)
    )
    policy = build_policy(
        definition,
        catalog=CATALOG,
        # Патчлист сообщества — только 430069: его basedata снята с этих стоков.
        include_patchlist=False,
        software=(version,),
        description=VERSION_DESCRIPTIONS.get(version),
        images=[data for _name, data, _base in stocks],
        base_offsets=[base for _name, _data, base in stocks],
        tolerance=set(missing),
    )
    if missing:
        policy.generated_note += (
            " Символы, отсутствующие в определении этой версии и не вошедшие в "
            f"политику: {', '.join(missing)}."
        )
    return policy


def validate_policy(
    policy: Policy,
    definition: Definition | None = None,
    patch_catalog: PatchCatalog | None = None,
) -> list[str]:
    """Проверки политики; возвращает список предупреждений (не ошибок)."""
    definition = definition or load_definition(XDF_512K)
    if patch_catalog is None and DEFAULT_PATCH_CATALOG.exists():
        patch_catalog = load_patch_catalog(DEFAULT_PATCH_CATALOG)
    warnings: list[str] = []
    if not policy.objects:
        raise PolicyError("политика пуста")
    for symbol, entry in policy.objects.items():
        if entry.origin == "patchlist":
            if patch_catalog is None:
                raise PolicyError(f"{symbol}: символ патчлиста, а каталог патчей не найден")
            if entry.requires_patch not in set(patch_catalog.titles()):
                raise PolicyError(
                    f"{symbol}: патч {entry.requires_patch!r} отсутствует в каталоге"
                )
            if entry.address + entry.rows * entry.columns * max(1, entry.element_size_bits // 8) > 0x80000:
                raise PolicyError(f"{symbol}: адрес выходит за образ")
        elif symbol not in definition.objects:
            raise PolicyError(f"{symbol}: нет в определении")
        if entry.minimum is None or entry.maximum is None:
            raise PolicyError(f"{symbol}: не заданы границы (ни в каталоге, ни в XDF)")
        if entry.minimum >= entry.maximum:
            raise PolicyError(f"{symbol}: min {entry.minimum} >= max {entry.maximum}")
        if entry.stock.minimum is None:
            raise PolicyError(f"{symbol}: нет стоковых измерений")
        # 1. любой сток обязан лежать внутри политики — иначе политика режет сток
        tol = 1e-6 * max(1.0, abs(entry.minimum), abs(entry.maximum))
        if entry.stock.minimum < entry.minimum - tol or entry.stock.maximum > entry.maximum + tol:
            raise PolicyError(
                f"{symbol}: сток {entry.stock.minimum}..{entry.stock.maximum} "
                f"выходит за границы {entry.minimum}..{entry.maximum}"
            )
        # 2. границы обязаны быть представимы сырым значением: считаем сырое
        #    значение и сверяем с разрядностью (XDF datatype=0, без знака).
        if entry.scale_a == 0:
            raise PolicyError(f"{symbol}: нулевой масштаб")
        limit = (1 << entry.element_size_bits) - 1
        for name, value in (("min", entry.minimum), ("max", entry.maximum)):
            raw = (value - entry.scale_b) / entry.scale_a
            # допуск 1e-6 — только на двоичную погрешность деления
            if raw < -1e-6 or raw > limit + 1e-6:
                raise PolicyError(
                    f"{symbol}: {name} {value:g} не представим {entry.element_size_bits} "
                    f"битами со шкалой {entry.scale_a:g}*X+{entry.scale_b:g} "
                    f"(сырое значение {raw:.3f}, допустимо 0..{limit})"
                )
            if raw < 0:
                warnings.append(f"{symbol}: {name} даёт отрицательное сырое значение")
        # 3. таблица с монотонностью: сток обязан быть монотонным
        if entry.require_monotonic and not entry.stock.monotonic:
            warnings.append(
                f"{symbol}: у части стоковых образов кривая не монотонна "
                "(генератор обязан требовать монотонность и не переносить сток как есть)"
            )
        # 4. read_only не должен иметь ворот записи, если он не справочный
        if (
            entry.read_only
            and entry.max_cells_per_patch is not None
            and entry.origin == "xdf"
        ):
            warnings.append(f"{symbol}: read_only, но указан max_cells_per_patch")
        # 5. параметр патча без самого патча в политике бессмысленен
        if entry.requires_patch and entry.origin != "patchlist":
            warnings.append(f"{symbol}: requires_patch без origin=patchlist")
    return warnings


def load_policy(path: Path = DEFAULT_POLICY) -> Policy:
    """Читать политику из JSON. Файл политики — источник правды для ядра."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    objects: dict[str, PolicyEntryData] = {}
    for symbol, item in payload["objects"].items():
        rows, columns = (int(part) for part in str(item["shape"]).split("x"))
        stock = item.get("stock") or {}
        scaler = Scaler.from_equation(str(item.get("equation", "X")))
        objects[symbol] = PolicyEntryData(
            symbol=symbol,
            group=item.get("group", ""),
            source=item.get("source", ""),
            note=item.get("note", ""),
            address=int(str(item["address"]), 16),
            element_size_bits=int(item["size_bits"]),
            rows=rows,
            columns=columns,
            units=item.get("units", ""),
            scale_a=scaler.a,
            scale_b=scaler.b,
            minimum=float(item["min"]),
            maximum=float(item["max"]),
            max_delta_percent=item.get("max_delta_percent"),
            max_cells_per_patch=item.get("max_cells_per_patch"),
            require_monotonic=bool(item.get("require_monotonic", False)),
            xdf_index=item.get("xdf_index"),
            read_only=bool(item.get("read_only", False)),
            write_context=item.get("write_context"),
            requires_patch=item.get("requires_patch"),
            origin=item.get("origin", "xdf"),
            equation=item.get("equation", "X"),
            stock=StockStats(
                samples=int(stock.get("samples", 0)),
                variants=int(stock.get("variants", 1)),
                minimum=stock.get("min"),
                maximum=stock.get("max"),
                uniform=bool(stock.get("uniform", True)),
                monotonic=True,
                note=stock.get("note", ""),
            ),
            range_low=None,
            range_high=None,
        )
    return Policy(
        version=int(payload["version"]),
        family=payload.get("family", "MS43"),
        software=tuple(payload.get("software", ())),
        generated_note=payload.get("description", ""),
        max_quantization_error_percent=float(
            payload.get("max_quantization_error_percent", DEFAULT_MAX_QUANTIZATION_PERCENT)
        ),
        objects=objects,
    )


def write_policy(policy: Policy, path: Path = DEFAULT_POLICY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(policy.as_dict(), ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def report(policy: Policy) -> str:
    lines = [
        f"Политика MS43 v{policy.version}: {len(policy.objects)} символов, "
        f"допуск квантования {policy.max_quantization_error_percent}%",
        "",
        f"{'символ':34s} {'адрес':>8s} {'форм':>7s} {'мин':>9s} {'макс':>9s} "
        f"{'сток':>18s} {'ячеек':>6s}  группа",
    ]
    for symbol, entry in sorted(policy.objects.items(), key=lambda kv: (kv[1].group, kv[0])):
        stock = (
            f"{entry.stock.minimum:g}..{entry.stock.maximum:g}"
            if entry.stock.variants > 1
            else f"{entry.stock.minimum:g}"
        )
        cells = "-" if entry.max_cells_per_patch is None else str(entry.max_cells_per_patch)
        extra = []
        if entry.read_only:
            extra.append("только чтение")
        if entry.require_monotonic:
            extra.append("монотонна")
        if entry.xdf_index is not None:
            extra.append(f"xdf_index={entry.xdf_index}")
        if entry.max_delta_percent is not None:
            # только cp1251-совместимые символы: отчёт печатается в консоль Windows
            extra.append(f"delta<={entry.max_delta_percent:g}%")
        if entry.requires_patch:
            extra.append("патч: " + entry.requires_patch.replace("[PATCH] ", ""))
        suffix = (" — " + ", ".join(extra)) if extra else ""
        lines.append(
            f"{symbol:34s} 0x{entry.address:05X} {entry.rows}x{entry.columns:<5d} "
            f"{entry.minimum:>9g} {entry.maximum:>9g} {stock:>18s} {cells:>6s}  "
            f"{entry.group}{suffix}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()  # Windows: не падать на символах вне cp1251
    parser = argparse.ArgumentParser(
        description="Каталог и политика записи MS43 (allowlist): сборка, проверка, отчёт."
    )
    parser.add_argument("--build", action="store_true", help="записать политику 430069 в JSON")
    parser.add_argument(
        "--build-version",
        action="append",
        metavar="4300XX",
        help="собрать политику версии и записать в её файл (можно повторять)",
    )
    parser.add_argument(
        "--build-all",
        action="store_true",
        help="собрать политики всех покрытых версий из реестра",
    )
    parser.add_argument("--out", default=str(DEFAULT_POLICY), help="путь JSON-файла политики")
    parser.add_argument("--report", action="store_true", help="напечатать таблицу символов")
    parser.add_argument("--strict", action="store_true", help="предупреждения считать ошибкой")
    args = parser.parse_args(argv)

    if args.build_all:
        versions = list(covered_software())
    elif args.build_version:
        versions = list(dict.fromkeys(args.build_version))
    else:
        versions = []
    failures = 0
    for version in versions:
        try:
            definition = load_definition(VERSION_DEFINITIONS[version][0])
            policy = build_version_policy(version, definition=definition)
            warnings = validate_policy(policy, definition)
        except (KeyError, PolicyError, XdfError) as error:
            print(f"{version}: политика не собрана: {error}", file=sys.stderr)
            failures += 1
            continue
        print(report(policy))
        for warning in warnings:
            print(f"{version}: предупреждение: {warning}", file=sys.stderr)
        path = policy_path_for_version(version)
        assert path is not None
        print(f"{version}: политика записана: {write_policy(policy, path)}")
        failures += 1 if (warnings and args.strict) else 0
    if versions:
        return 1 if failures else 0

    definition = load_definition(XDF_512K)
    try:
        policy = build_policy(definition)
        warnings = validate_policy(policy, definition)
    except (PolicyError, XdfError) as error:
        print(f"политика не собрана: {error}", file=sys.stderr)
        return 2

    if args.report or not args.build:
        print(report(policy))
    for warning in warnings:
        print(f"предупреждение: {warning}", file=sys.stderr)
    if args.build:
        print(f"политика записана: {write_policy(policy, Path(args.out))}")
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
