#!/usr/bin/env python3
"""Политика MS45: allowlist символов, границы, роли и защита версий.

Принципы (из ``MS45/TASK_RU.md``):

* **default-deny**: писать можно только то, что есть в allowlist; всё остальное —
  ``SYMBOL_NOT_ALLOWED``. Это не «список запрещённого», а «список разрешённого».
* **Адреса между семействами не переносятся**: у ``4560BO00S``/``4570LO00S``
  свои XDF, у ``4560BN00S`` — проверенная XML-карта, а у ``4560BM00S``,
  ``4560BL00S``, ``4560BL30S``, ``4570LL00S``, ``4570LL30S``, ``4570LM00S`` и
  ``4570LN00S`` — своя stock-derived карта (``definitions/stock_derived_maps.json``):
  адреса ролей доказаны структурными отпечатками по всем стокам семьи;
  недоказанные роли (карты, диагностика привода) остаются закрыты.
* Для ``4560BO00S``/``4570LO00S`` источник адресов — XDF (MS4X). Для
  ``4560BN00S`` XDF нет: адреса берутся из XML-определений сообщества
  (``definitions/MS45 ECU Definitions.xml``), которые мы проверили на BO/LO
  (совпадение всех ролей до байта), а разрядность, число элементов и формула —
  из упаковки таблиц и поведения стоковых значений по 39 файлам семейства.
  Подробности и доказательства: ``MS45/docs/BN00S_DEFINITIONS_RU.md``.
* Границы берутся из данных: минимальные/максимальные *стоковые* значения
  считаются по всем файлам семейства, а не вписываются руками. Ручными остаются
  только продуктовые решения (пределы, комментарии, обоснования) — они лежат
  в :data:`POLICY_SPEC`.
* Символы, покрывающие штатные защиты (``c_n_lim_min`` — порог «overrevving
  event», ``c_t_min_pu_msr`` — задержка ПХХ после вмешательства MSR), помечены
  ``read_only``: их нельзя менять ни одним запросом.

Файл политики — ``MS45/policies/stage_policy.json``; он генерируется командой
``python3 -m MS45.tools.policy --build`` и проверяется ``--validate``.
"""

from __future__ import annotations

import argparse
import json
import math
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
from MS45.tools import pathfmt
from MS45.tools import xdf as xd
from MS45.tools import xml_defs as xm

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = ROOT / "firmware"
SPDATEN_DIR = ROOT / "firmware_spdaten" / "cal"
POLICIES_DIR = ROOT / "policies"
DEFAULT_POLICY = POLICIES_DIR / "stage_policy.json"
POLICY_VERSION = 1

#: Поддерживаемые семейства: только те, для которых разрешённые объекты
#: подтверждены стоками соответствующего SW-ID. Все семейства покрыты по ролям
#: ``vmax``/``rev_limit_*``/``pops_delay``; недоказанные роли (карты зажигания,
#: топлива, ВАНос, диагностика привода) остаются REQUIRES_DATA. Карта RON98
#: доказана у BO00S/LO00S (XDF) и у :data:`IGNITION_STOCK_DERIVED_FAMILIES`.
SUPPORTED_FAMILIES = (
    "4560BO00S", "4570LO00S", "4560BN00S", "4560BM00S",
    "4560BL00S", "4560BL30S", "4570LL00S", "4570LL30S", "4570LM00S", "4570LN00S",
)

#: Семейство -> имя ROM в XML-определениях сообщества. Используется там, где
#: своего XDF нет. Файл проверяем: его адреса для 4560BO00 совпадают с XDF
#: (12 ролей), а таблицы ролей 4560BN00 упакованы без дыр и пересечений.
XML_FAMILIES: dict[str, str] = {"4560BN00S": "4560BN00"}

#: XML-определения сообщества (несколько заводских вариантов в одном файле).
XML_DEFINITIONS = ROOT / "definitions" / "MS45 ECU Definitions.xml"
STOCK_DERIVED_DEFINITIONS = ROOT / "definitions" / "stock_derived_maps.json"
#: Семейства без XDF и без разметки в XML: адреса доказываются по стокам
#: семейства структурными отпечатками (см. docs/FAMILY_CALIBRATION_EVIDENCE_RU.md).
STOCK_DERIVED_FAMILIES = (
    "4560BM00S", "4560BL00S", "4560BL30S", "4570LL00S", "4570LL30S",
    "4570LM00S", "4570LN00S",
)
DUMPS_DIR = ROOT / "firmware_dumps"
#: Реальное чтение E60 (4560BN00S, 1 МиБ): единственный настоящий дамп в репозитории.
BN_REAL_DUMP = DUMPS_DIR / "MS45.0_0044560BN_fullread_ORI.bin"

#: Базовые роли полностью покрытых семейств (иначе политика неполная).
REQUIRED_ROLES = ("vmax", "rev_limit_mt", "rev_limit_at", "rev_limit_amt", "pops_delay")
#: Минимум, доказанный для семейства с частичной поддержкой.
REQUIRED_ROLES_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "4560BO00S": REQUIRED_ROLES,
    "4570LO00S": REQUIRED_ROLES,
    "4560BN00S": REQUIRED_ROLES,
    # BM00S: роли vmax/rev_limit добавлены после доказательства адресов
    # структурными отпечатками по 32 стокам (см. шаг 3, FAMILY_CALIBRATION_EVIDENCE_RU.md).
    "4560BM00S": REQUIRED_ROLES,
    "4560BL00S": REQUIRED_ROLES,
    "4560BL30S": REQUIRED_ROLES,
    "4570LL00S": REQUIRED_ROLES,
    "4570LL30S": REQUIRED_ROLES,
    "4570LM00S": REQUIRED_ROLES,
    "4570LN00S": REQUIRED_ROLES,
}

def _data_family_roles() -> dict[str, tuple[str, ...]]:
    """Роли семейства, доказанного только по стокам (шаг 3).

    Ни XDF, ни размеченных XML-таблиц у этих семейств нет: адреса ``c_vs_max``,
    шести лимитеров оборотов и группы задержек ПХХ доказаны структурными
    отпечатками по всем стокам семьи (см.
    ``docs/FAMILY_CALIBRATION_EVIDENCE_RU.md`` и
    ``tests/test_family_stock_proof.py``). Карты (зажигание, топливо, ВАНос) и
    диагностика привода остаются REQUIRES_DATA.
    """
    return {
        "vmax": ("c_vs_max",),
        "rev_limit_mt": ("id_n_max_mt", "id_n_max_h_mt"),
        "rev_limit_at": ("id_n_max_at", "id_n_max_h_at"),
        "rev_limit_amt": ("id_n_max_amt", "id_n_max_h_amt"),
        "pops_delay": ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l"),
    }


def _data_family_limits(stocks: int) -> dict[str, dict]:
    """Продуктовые границы записи для семейства без XDF (номера стоков — в note)."""
    return {
        "c_vs_max": dict(min=100.0, max=255.0, max_delta_percent=None,
                         note=f"км/ч; 255 = без ограничения; адрес доказан структурным отпечатком по {stocks} стокам"),
        "id_n_max_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
        "id_n_max_h_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0,
                              note="rpm; «Increased» — держать равным id_n_max_mt"),
        "id_n_max_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
        "id_n_max_h_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
        "id_n_max_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
        "id_n_max_h_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
        "c_t_min_pu": dict(min=0.0, max=1.5, max_delta_percent=None,
                           note="с; задержка активации ПХХ (сцепление выжато / круиз активен); только без катализатора"),
        "c_t_min_pu_vs": dict(min=0.0, max=1.5, max_delta_percent=None,
                              note="с; задержка при неподвижном автомобиле"),
        "c_t_min_pu_rng_l": dict(min=0.0, max=1.5, max_delta_percent=None,
                                 note="с; задержка в пониженном диапазоне"),
    }


def _data_family_read_only(stocks: int) -> dict[str, str]:
    """Защищённые объекты семейства без XDF (число стоков — в пояснении)."""
    return {
        "c_n_lim_min": f"штатный порог «overrevving event» (7200 rpm во всех {stocks} стоках семейства); "
                       "новая отсечка <= 7000 rpm, поднимать нельзя",
        "c_t_min_pu_msr": f"задержка ПХХ после вмешательства MSR: 1.00 с (raw 100) во всех {stocks} стоках — "
                          "инвариант всех семейств MS45",
        "val_mo3_cal_cks_high": "место хранения суммы MO3 — пишет только ядро",
        "val_mo3_cal_cks_low": "место хранения суммы MO3 — пишет только ядро",
    }


def _data_family_packs() -> dict[str, str]:
    """Пакеты стадий для семейства без XDF: только доказанные роли."""
    return {
        "vmax": "c_vs_max",
        "rev_limit": "id_n_max_mt,id_n_max_h_mt",
        "pops": "c_t_min_pu,c_t_min_pu_vs,c_t_min_pu_rng_l",
    }


def _data_family_ack() -> dict[str, str]:
    """Подтверждения клиента для семейства без XDF (только слой ПХХ)."""
    return {
        "c_t_min_pu": "no-catalyst",
        "c_t_min_pu_vs": "no-catalyst",
        "c_t_min_pu_rng_l": "no-catalyst",
    }


def _data_family_spec(stocks: int) -> dict:
    """Спецификация семейства, доказанного только по стокам: адреса — в карте стоков."""
    return {
        "roles": _data_family_roles(),
        "limits": _data_family_limits(stocks),
        "read_only": _data_family_read_only(stocks),
        "packs": _data_family_packs(),
        "ack": _data_family_ack(),
    }


#: Семейства без XDF, у которых карта RON98 (зажигание Stage 1) доказана по
#: стокам: кластер пяти карт 20x16 и оси найдены отпечатками, которые на
#: BO00S/LO00S/BN00S воспроизводят адреса XDF/XML, уникальны во всей калибровке
#: и одинаковы во всех стоках семьи (``tests/test_family_ignition_proof.py``,
#: ``docs/FAMILY_CALIBRATION_EVIDENCE_RU.md`` §4). Семейства с одним стоком сюда
#: не входят: инвариантность «по всем стокам» на одном образе не проверяется.
IGNITION_STOCK_DERIVED_FAMILIES: tuple[str, ...] = (
    "4560BL00S", "4560BM00S", "4570LL30S", "4570LM00S", "4570LN00S",
)

#: Почему зажигание у семейства без XDF остаётся REQUIRES_DATA (текст наряда).
IGNITION_REQUIRES_DATA_REASON: dict[str, str] = {
    "4560BL30S": "в репозитории один сток семейства 4560BL30S — нужен второй образ: "
                 "адрес карты RON98 найден отпечатком, но его неизменность по стокам "
                 "семьи на одном образе не доказать",
    "4570LL00S": "в репозитории один сток семейства 4570LL00S — нужен второй образ: "
                 "адрес карты RON98 найден отпечатком, но его неизменность по стокам "
                 "семьи на одном образе не доказать",
}


def _data_family_ignition_spec(stocks: int) -> dict:
    """Семейство без XDF с доказанной картой RON98: базовые роли + зажигание.

    Повторяет BO00S/LO00S: пишется только ``ip_iga_bas_ron_98__n__maf``, границы
    — токены от стока; карта RON91 и fallback-карта — только чтение; оси карты —
    только чтение (форма добавки по нагрузке). Топливо и ВАНос — REQUIRES_DATA.
    Потолка по детонации (``ip_iga_bas_max_knk__n__maf``) в карте стоков нет —
    его адрес у этих семейств не доказан, поэтому он не перечислен: писать его
    генератор всё равно не может (default-deny + пост-проверка «вне плана
    байт-в-байт»).
    """
    spec = _data_family_spec(stocks)
    spec["roles"]["ignition"] = ("ip_iga_bas_ron_98__n__maf",)
    spec["limits"]["ip_iga_bas_ron_98__n__maf"] = dict(
        min="@stock_min", max="@stock_max+3.0", max_delta_percent=None,
        note=f"°CRK; базовый угол RON98, 20x16; адрес доказан отпечатком кластера "
             f"зажигания по {stocks} стокам. Добавка — по ignition_caps (Stage 1 без "
             "лога <= +3.0° для RON98, <= +1.5° для RON95, RON91 — 0); карта не убавляется")
    spec["read_only"]["ip_iga_bas_ron_91__n__maf"] = (
        "карта RON91 — защитный пол октановой адаптации; не пишется никогда")
    spec["read_only"]["ip_iga_bas__n__maf"] = (
        "fallback-карта зажигания (режим без VANOS); не трогаем")
    spec["packs"]["ignition"] = "ip_iga_bas_ron_98__n__maf"
    spec["axes"] = {
        "axis_ip_iga_bas_ron_98__n__maf_x": {
            "table": "ip_iga_bas_ron_98__n__maf", "axis": "x",
            "note": "ось нагрузки карты RON98 — только чтение (форма добавки по нагрузке)"},
        "axis_ip_iga_bas_ron_98__n__maf_y": {
            "table": "ip_iga_bas_ron_98__n__maf", "axis": "y",
            "note": "ось оборотов карты RON98 — только чтение"},
    }
    return spec


#: Роли -> символы внутри семейства. Карта своя у каждого семейства, потому что
#: адреса и состав символов у MS45.0 и MS45.1 разные.
POLICY_SPEC: dict[str, dict] = {
    "4560BO00S": {
        "roles": {
            "vmax": ("c_vs_max",),
            "rev_limit_mt": ("id_n_max_mt", "id_n_max_h_mt"),
            "rev_limit_at": ("id_n_max_at", "id_n_max_h_at"),
            "rev_limit_amt": ("id_n_max_amt", "id_n_max_h_amt"),
            "pops_delay": ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l"),
            # Пакеты задания 3 (зажигание/топливо/ВАНос). Границы — «токены от
            # стока» (@stock_min/@stock_max/@floor:), разрешаются при сборке
            # политики из данных семейства. В 4560BO00S нет символа
            # c_lamb_min_ls_act_test_afr (нет в XDF), поэтому роль ``fuel``
            # здесь не разрешается: обогащение без проверяемых ворот
            # диагностики — честное REQUIRES_DATA у генератора.
            "ignition": ("ip_iga_bas_ron_98__n__maf",),
            "vanos": ("ip_cam_sp_2_in_pl__n__maf_ivvt", "ip_cam_sp_2_ex_pl__n__maf_ivvt"),
            # Слой диагностики привода DISA (VIM) и клапана холостого хода (ISA):
            # подавляется приращение счётчика дребезга, а не функция. Значения
            # проверены по стокам семейства: приращение 1 во всех файлах,
            # максимум 10 (VIM) и 6 (ISA) — то есть приращение меньше максимума,
            # как и должно быть; 0 останавливает рост счётчика.
            "dtc_driver": ("c_det_inc_vim", "c_det_inc_isa_1", "c_det_inc_isa_2"),
            # Слой «без катализатора» (вариант 3): только проверенные флаги с
            # однозначной семантикой (мс4х-вики, «Cat Delete»). c_obd_state_2 и
            # lc_ad_clr_var намеренно не входят: битовые/диагностические роли
            # не доказаны на стоках.
            "cat_delete": ("c_conf_cat", "lc_lsh_down_1", "lc_lsh_down_2",
                           "lc_inh_ls_down_man_deac"),
            "sap_delete": ("ip_t_sa_off_0", "lc_inh_sa_man_deac", "lc_sa_swi_acq"),
        },
        "limits": {
            "c_vs_max": dict(min=100.0, max=255.0, max_delta_percent=None,
                             note="км/ч; 255 = без ограничения (ms4x.net, «Vehicle Speed Limiter»)"),
            "id_n_max_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0,
                                note="rpm; выше 7000 rpm для M54B30 нужен демпфер (ms4x.net)"),
            "id_n_max_h_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0,
                                  note="rpm; «Increased» — держать равным id_n_max_mt"),
            "id_n_max_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "c_t_min_pu": dict(min=0.0, max=1.5, max_delta_percent=None,
                               note="с; задержка активации ПХХ (pops, режим «timered»); только без катализатора"),
            "c_t_min_pu_vs": dict(min=0.0, max=1.5, max_delta_percent=None,
                                  note="с; задержка при неподвижном автомобиле"),
            "c_t_min_pu_rng_l": dict(min=0.0, max=1.5, max_delta_percent=None,
                                     note="с; задержка в пониженном диапазоне"),
            # Пакеты задания 3: границы — от стока семейства (токены), фактические
            # ворота — абсолютные, в генераторе (см. стадии).
            "ip_iga_bas_ron_98__n__maf": dict(
                min="@stock_min", max="@stock_max+3.0", max_delta_percent=None,
                note="°CRK; базовый угол RON98, 20x16. Добавка <= +1.5° без лога, "
                     "<= +3.0° только по логу детонации; карта не убавляется"),
            "ip_cam_sp_2_in_pl__n__maf_ivvt": dict(
                min="@stock_min", max="@stock_max+4.875", max_delta_percent=None,
                note="°CRK; фазы впускного распредвала, прогретый, частичная нагрузка; "
                     "сдвиг только по логу фаз, <= +5° от стока (по сетке 4.875°)"),
            "ip_cam_sp_2_ex_pl__n__maf_ivvt": dict(
                min="@stock_min-4.875", max="@stock_max", max_delta_percent=None,
                note="°CRK; фазы выпускного распредвала, прогретый, частичная нагрузка; "
                     "сдвиг только по логу фаз, <= 5° от стока (по сетке 4.875°)"),
            "c_det_inc_vim": dict(min=0, max="@stock_max", max_delta_percent=None,
                                 note="приращение счётчика ошибки привода DISA (VIM); сток 1 во всех файлах семейства, максимум счётчика 10; 0 — ошибка привода не выставляется (функцию заслонки не отключает)"),
            "c_det_inc_isa_1": dict(min=0, max="@stock_max", max_delta_percent=None,
                                    note="приращение счётчика ошибки клапана холостого хода, канал 1; сток 1, максимум счётчика 6; 0 — код не выставляется (регулирование ХХ остаётся штатным)"),
            "c_det_inc_isa_2": dict(min=0, max="@stock_max", max_delta_percent=None,
                                    note="приращение счётчика ошибки клапана холостого хода, канал 2; см. c_det_inc_isa_1"),
            "c_conf_cat": dict(min=0, max=2, max_delta_percent=None,
                               note="конфигурация «катализатор есть» (сток 1 или 2); 0 — катализатора нет; только слой «без катализатора»"),
            "lc_lsh_down_1": dict(min=0, max=1, max_delta_percent=None,
                                  note="подогрев заднего зонда, линия 1 (сток 1); 0 — мониторинг выключен; только слой «без катализатора»"),
            "lc_lsh_down_2": dict(min=0, max=1, max_delta_percent=None,
                                  note="подогрев заднего зонда, линия 2 (сток 1); 0 — мониторинг выключен; только слой «без катализатора»"),
            "lc_inh_ls_down_man_deac": dict(min=0, max=1, max_delta_percent=None,
                                            note="ручное отключение диагностики заднего зонда (сток 0); 1 — диагностика выключена; только слой «без катализатора»"),
            "ip_t_sa_off_0": dict(min=0, max="@stock_max", max_delta_percent=None,
                                  note="задержки завершения продувки (СВП), 8 значений; 0 — продувка не удерживается; только слой «без катализатора»"),
            "lc_inh_sa_man_deac": dict(min=0, max=1, max_delta_percent=None,
                                       note="ручное отключение диагностики СВП (сток 0); 1 — диагностика выключена; только слой «без катализатора»"),
            "lc_sa_swi_acq": dict(min=0, max=1, max_delta_percent=None,
                                  note="режим принятия решения СВП по модели (сток 0 у 4560BO00S, 1 у 4570LO00S); 0 — только слой «без катализатора»"),
        },
        "read_only": {
            "c_n_lim_min": "штатный порог «overrevving event» (7200 rpm во всех 81 файле); новая отсечка <= 7000 rpm, поднимать нельзя",
            "c_t_min_pu_msr": "задержка ПХХ после вмешательства MSR; к pops отношения не имеет",
            "val_mo3_cal_cks_high": "место хранения суммы MO3 — пишет только ядро",
            "val_mo3_cal_cks_low": "место хранения суммы MO3 — пишет только ядро",
            "val_mo3_swi_cal_cks": "переключатель проверки суммы MO3 (0xA5 отключает) — менять запрещено",
            "lc_ad_clr_var": "сброс обученных вариантов — управляется диагностикой/владельцем, не калибровкой Stage",
            # Защита задания 3: октановый пол, защита по детонации, холодные и
            # полнонагрузочные карты фаз. Адреса — из XDF семейства.
            "ip_iga_bas_ron_91__n__maf": "карта RON91 — защитный пол октановой адаптации; не пишется никогда",
            "ip_iga_bas__n__maf": "fallback-карта зажигания (режим без VANOS); не трогаем",
            "ip_iga_bas_max_knk__n__maf": "потолок угла по детонации — штатная защита; только чтение",
            "ip_cam_sp_1_in_pl__n__maf_ivvt": "холодная карта фаз впуска: продукт двигает только прогретые карты",
            "ip_cam_sp_1_ex_pl__n__maf_ivvt": "холодная карта фаз выпуска: продукт двигает только прогретые карты",
            "ip_cam_sp_2_in_fl__n": "фазы впуска полной нагрузки: двигается только частичная нагрузка",
            "ip_cam_sp_2_ex_fl__n": "фазы выпуска полной нагрузки: двигается только частичная нагрузка",
        },
        "packs": {
            "vmax": "c_vs_max",
            "rev_limit": "id_n_max_mt,id_n_max_h_mt",
            "pops": "c_t_min_pu,c_t_min_pu_vs,c_t_min_pu_rng_l",
            "ignition": "ip_iga_bas_ron_98__n__maf",
            "vanos": "ip_cam_sp_2_in_pl__n__maf_ivvt,ip_cam_sp_2_ex_pl__n__maf_ivvt",
            "dtc_driver": "c_det_inc_vim,c_det_inc_isa_1,c_det_inc_isa_2",
            "cat_delete": "c_conf_cat,lc_lsh_down_1,lc_lsh_down_2,lc_inh_ls_down_man_deac",
            "sap_delete": "ip_t_sa_off_0,lc_inh_sa_man_deac,lc_sa_swi_acq",
        },
        # Подтверждения клиента, обязательные для отдельных символов.
        # Оси карт, снятые из определения семейства (``mmedaddress`` внутри
        # ``<XDFAXIS>``; для политики берётся ``_116K``-вариант XDF). Символы
        # осей — производные от XDF: у самих осей нет имени в определении, поэтому
        # имя строится как ``axis_<таблица>_<ось>``, а адрес, разрядность и формула
        # берутся из определения, число значений — ``indexcount``. Только чтение:
        # раскладка правок по зонам лога (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.3).
        "axes": {
            "axis_ip_iga_bas_ron_98__n__maf_x": {
                "table": "ip_iga_bas_ron_98__n__maf", "axis": "x",
                "note": "ось нагрузки карты RON98 — только чтение (раскладка по зонам лога)"},
            "axis_ip_iga_bas_ron_98__n__maf_y": {
                "table": "ip_iga_bas_ron_98__n__maf", "axis": "y",
                "note": "ось оборотов карты RON98 — только чтение (раскладка по зонам лога)"},
            "axis_ip_lamb_fl__n_x": {
                "table": "ip_lamb_fl__n", "axis": "x",
                "note": "ось оборотов карты обогащения полной нагрузки — только чтение"},
            "axis_ip_cam_sp_2_in_pl__n__maf_ivvt_x": {
                "table": "ip_cam_sp_2_in_pl__n__maf_ivvt", "axis": "x",
                "note": "ось нагрузки карты фаз впуска — только чтение"},
            "axis_ip_cam_sp_2_in_pl__n__maf_ivvt_y": {
                "table": "ip_cam_sp_2_in_pl__n__maf_ivvt", "axis": "y",
                "note": "ось оборотов карты фаз впуска — только чтение"},
            "axis_ip_cam_sp_2_ex_pl__n__maf_ivvt_x": {
                "table": "ip_cam_sp_2_ex_pl__n__maf_ivvt", "axis": "x",
                "note": "ось нагрузки карты фаз выпуска — только чтение"},
            "axis_ip_cam_sp_2_ex_pl__n__maf_ivvt_y": {
                "table": "ip_cam_sp_2_ex_pl__n__maf_ivvt", "axis": "y",
                "note": "ось оборотов карты фаз выпуска — только чтение"},
        },

        "ack": {
            "c_t_min_pu": "no-catalyst",
            "c_t_min_pu_vs": "no-catalyst",
            "c_t_min_pu_rng_l": "no-catalyst",
            "c_conf_cat": "no-catalyst",
            "lc_lsh_down_1": "no-catalyst",
            "lc_lsh_down_2": "no-catalyst",
            "lc_inh_ls_down_man_deac": "no-catalyst",
            "ip_t_sa_off_0": "no-catalyst",
            "lc_inh_sa_man_deac": "no-catalyst",
            "lc_sa_swi_acq": "no-catalyst",
        },
    },
    "4570LO00S": {
        "roles": {
            "vmax": ("c_vs_max",),
            "rev_limit_mt": ("id_n_max_mt", "id_n_max_h_mt"),
            "rev_limit_at": ("id_n_max_at", "id_n_max_h_at"),
            "rev_limit_amt": ("id_n_max_amt", "id_n_max_h_amt"),
            "pops_delay": ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l"),
            # Пакеты задания 3: у семейства доказаны ворота обогащения
            # (c_lamb_min_ls_act_test_afr = 0.8207 λ во всех стоках), поэтому
            # здесь разрешается и роль ``fuel``.
            "ignition": ("ip_iga_bas_ron_98__n__maf",),
            "fuel": ("ip_lamb_fl__n",),
            "vanos": ("ip_cam_sp_2_in_pl__n__maf_ivvt", "ip_cam_sp_2_ex_pl__n__maf_ivvt"),
            # Слой диагностики привода DISA (VIM) и клапана холостого хода (ISA):
            # подавляется приращение счётчика дребезга, а не функция. Значения
            # проверены по стокам семейства: приращение 1 во всех файлах,
            # максимум 10 (VIM) и 6 (ISA) — то есть приращение меньше максимума,
            # как и должно быть; 0 останавливает рост счётчика.
            "dtc_driver": ("c_det_inc_vim", "c_det_inc_isa_1", "c_det_inc_isa_2"),
            # Слой «без катализатора» (вариант 3) — см. комментарий у 4560BO00S.
            "cat_delete": ("c_conf_cat", "lc_lsh_down_1", "lc_lsh_down_2",
                           "lc_inh_ls_down_man_deac"),
            "sap_delete": ("ip_t_sa_off_0", "lc_inh_sa_man_deac", "lc_sa_swi_acq"),
        },
        "limits": {
            "c_vs_max": dict(min=100.0, max=255.0, max_delta_percent=None,
                             note="км/ч; 255 = без ограничения"),
            "id_n_max_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "c_t_min_pu": dict(min=0.0, max=1.5, max_delta_percent=None,
                               note="с; pops «timered»; только без катализатора"),
            "c_t_min_pu_vs": dict(min=0.0, max=1.5, max_delta_percent=None, note="с"),
            "c_t_min_pu_rng_l": dict(min=0.0, max=1.5, max_delta_percent=None, note="с"),
            # Пакеты задания 3: границы — от стока семейства (токены).
            "ip_iga_bas_ron_98__n__maf": dict(
                min="@stock_min", max="@stock_max+3.0", max_delta_percent=None,
                note="°CRK; базовый угол RON98, 20x16. Добавка <= +1.5° без лога, "
                     "<= +3.0° только по логу детонации; карта не убавляется"),
            "ip_lamb_fl__n": dict(
                min="@floor:0.82", max="@stock_max", max_delta_percent=None,
                note="лямбда полной нагрузки; только обогащение (в меньшую сторону), "
                     "сдвиг <= 0.03 лямбды и только по логу широкой лямбды; пол 0.82; "
                     "не обедняем относительно стока"),
            "ip_cam_sp_2_in_pl__n__maf_ivvt": dict(
                min="@stock_min", max="@stock_max+4.875", max_delta_percent=None,
                note="°CRK; фазы впускного распредвала, прогретый, частичная нагрузка; "
                     "сдвиг только по логу фаз, <= +5° от стока (по сетке 4.875°)"),
            "ip_cam_sp_2_ex_pl__n__maf_ivvt": dict(
                min="@stock_min-4.875", max="@stock_max", max_delta_percent=None,
                note="°CRK; фазы выпускного распредвала, прогретый, частичная нагрузка; "
                     "сдвиг только по логу фаз, <= 5° от стока (по сетке 4.875°)"),
            "c_det_inc_vim": dict(min=0, max="@stock_max", max_delta_percent=None,
                                 note="приращение счётчика ошибки привода DISA (VIM); сток 1 во всех файлах семейства, максимум счётчика 10; 0 — ошибка привода не выставляется (функцию заслонки не отключает)"),
            "c_det_inc_isa_1": dict(min=0, max="@stock_max", max_delta_percent=None,
                                    note="приращение счётчика ошибки клапана холостого хода, канал 1; сток 1, максимум счётчика 6; 0 — код не выставляется (регулирование ХХ остаётся штатным)"),
            "c_det_inc_isa_2": dict(min=0, max="@stock_max", max_delta_percent=None,
                                    note="приращение счётчика ошибки клапана холостого хода, канал 2; см. c_det_inc_isa_1"),
            "c_conf_cat": dict(min=0, max=2, max_delta_percent=None,
                               note="конфигурация «катализатор есть» (сток 2); 0 — катализатора нет; только слой «без катализатора»"),
            "lc_lsh_down_1": dict(min=0, max=1, max_delta_percent=None,
                                  note="подогрев заднего зонда, линия 1 (сток 1); 0 — мониторинг выключен; только слой «без катализатора»"),
            "lc_lsh_down_2": dict(min=0, max=1, max_delta_percent=None,
                                  note="подогрев заднего зонда, линия 2 (сток 1); 0 — мониторинг выключен; только слой «без катализатора»"),
            "lc_inh_ls_down_man_deac": dict(min=0, max=1, max_delta_percent=None,
                                            note="ручное отключение диагностики заднего зонда (сток 0); 1 — диагностика выключена; только слой «без катализатора»"),
            "ip_t_sa_off_0": dict(min=0, max="@stock_max", max_delta_percent=None,
                                  note="задержки завершения продувки (СВП), 8 значений; 0 — продувка не удерживается; только слой «без катализатора»"),
            "lc_inh_sa_man_deac": dict(min=0, max=1, max_delta_percent=None,
                                       note="ручное отключение диагностики СВП (сток 0); 1 — диагностика выключена; только слой «без катализатора»"),
            "lc_sa_swi_acq": dict(min=0, max=1, max_delta_percent=None,
                                  note="режим принятия решения СВП по модели (сток 0 у 4560BO00S, 1 у 4570LO00S); 0 — только слой «без катализатора»"),
        },
        "read_only": {
            "c_n_lim_min": "штатный порог «overrevving event» (7200 rpm во всех 33 файлах)",
            "c_t_min_pu_msr": "задержка ПХХ после вмешательства MSR",
            "val_mo3_cal_cks_high": "место хранения суммы MO3 — пишет только ядро",
            "val_mo3_cal_cks_low": "место хранения суммы MO3 — пишет только ядро",
            "val_mo3_swi_cal_cks": "переключатель проверки суммы MO3",
            "lc_ad_clr_var": "сброс обученных вариантов",
            # Защита задания 3 (адреса из XDF семейства).
            "ip_iga_bas_ron_91__n__maf": "карта RON91 — защитный пол октановой адаптации; не пишется никогда",
            "ip_iga_bas__n__maf": "fallback-карта зажигания (режим без VANOS); не трогаем",
            "ip_iga_bas_max_knk__n__maf": "потолок угла по детонации — штатная защита; только чтение",
            "ip_cam_sp_1_in_pl__n__maf_ivvt": "холодная карта фаз впуска: продукт двигает только прогретые карты",
            "ip_cam_sp_1_ex_pl__n__maf_ivvt": "холодная карта фаз выпуска: продукт двигает только прогретые карты",
            "ip_cam_sp_2_in_fl__n": "фазы впуска полной нагрузки: двигается только частичная нагрузка",
            "ip_cam_sp_2_ex_fl__n": "фазы выпуска полной нагрузки: двигается только частичная нагрузка",
            "c_lamb_min_ls_act_test_afr": "ворота диагностики «смесь слишком богатая» (0.8207 лямбды во всех стоках): "
                                          "пакет топлива обязан держать цель выше этого порога; символ не пишется",
        },
        "packs": {
            "vmax": "c_vs_max",
            "rev_limit": "id_n_max_mt,id_n_max_h_mt",
            "pops": "c_t_min_pu,c_t_min_pu_vs,c_t_min_pu_rng_l",
            "ignition": "ip_iga_bas_ron_98__n__maf",
            "fuel": "ip_lamb_fl__n",
            "vanos": "ip_cam_sp_2_in_pl__n__maf_ivvt,ip_cam_sp_2_ex_pl__n__maf_ivvt",
            "dtc_driver": "c_det_inc_vim,c_det_inc_isa_1,c_det_inc_isa_2",
            "cat_delete": "c_conf_cat,lc_lsh_down_1,lc_lsh_down_2,lc_inh_ls_down_man_deac",
            "sap_delete": "ip_t_sa_off_0,lc_inh_sa_man_deac,lc_sa_swi_acq",
        },
        # Оси карт, снятые из определения семейства (``mmedaddress`` внутри
        # ``<XDFAXIS>``; для политики берётся ``_116K``-вариант XDF). Символы
        # осей — производные от XDF: у самих осей нет имени в определении, поэтому
        # имя строится как ``axis_<таблица>_<ось>``, а адрес, разрядность и формула
        # берутся из определения, число значений — ``indexcount``. Только чтение:
        # раскладка правок по зонам лога (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.3).
        "axes": {
            "axis_ip_iga_bas_ron_98__n__maf_x": {
                "table": "ip_iga_bas_ron_98__n__maf", "axis": "x",
                "note": "ось нагрузки карты RON98 — только чтение (раскладка по зонам лога)"},
            "axis_ip_iga_bas_ron_98__n__maf_y": {
                "table": "ip_iga_bas_ron_98__n__maf", "axis": "y",
                "note": "ось оборотов карты RON98 — только чтение (раскладка по зонам лога)"},
            "axis_ip_lamb_fl__n_x": {
                "table": "ip_lamb_fl__n", "axis": "x",
                "note": "ось оборотов карты обогащения полной нагрузки — только чтение"},
            "axis_ip_cam_sp_2_in_pl__n__maf_ivvt_x": {
                "table": "ip_cam_sp_2_in_pl__n__maf_ivvt", "axis": "x",
                "note": "ось нагрузки карты фаз впуска — только чтение"},
            "axis_ip_cam_sp_2_in_pl__n__maf_ivvt_y": {
                "table": "ip_cam_sp_2_in_pl__n__maf_ivvt", "axis": "y",
                "note": "ось оборотов карты фаз впуска — только чтение"},
            "axis_ip_cam_sp_2_ex_pl__n__maf_ivvt_x": {
                "table": "ip_cam_sp_2_ex_pl__n__maf_ivvt", "axis": "x",
                "note": "ось нагрузки карты фаз выпуска — только чтение"},
            "axis_ip_cam_sp_2_ex_pl__n__maf_ivvt_y": {
                "table": "ip_cam_sp_2_ex_pl__n__maf_ivvt", "axis": "y",
                "note": "ось оборотов карты фаз выпуска — только чтение"},
        },

        "ack": {
            "c_t_min_pu": "no-catalyst",
            "c_t_min_pu_vs": "no-catalyst",
            "c_t_min_pu_rng_l": "no-catalyst",
            "c_conf_cat": "no-catalyst",
            "lc_lsh_down_1": "no-catalyst",
            "lc_lsh_down_2": "no-catalyst",
            "lc_inh_ls_down_man_deac": "no-catalyst",
            "ip_t_sa_off_0": "no-catalyst",
            "lc_inh_sa_man_deac": "no-catalyst",
            "lc_sa_swi_acq": "no-catalyst",
        },
    },
    "4560BN00S": {
        "roles": {
            "vmax": ("c_vs_max",),
            "rev_limit_mt": ("id_n_max_mt", "id_n_max_h_mt"),
            "rev_limit_at": ("id_n_max_at", "id_n_max_h_at"),
            "rev_limit_amt": ("id_n_max_amt", "id_n_max_h_amt"),
            "pops_delay": ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l"),
        },
        "limits": {
            "c_vs_max": dict(min=100.0, max=255.0, max_delta_percent=None,
                             note="км/ч; 255 = без ограничения"),
            "id_n_max_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_mt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0,
                                  note="rpm; «Increased» — держать равным id_n_max_mt"),
            "id_n_max_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_at": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "id_n_max_h_amt": dict(min=6000.0, max=7000.0, max_delta_percent=10.0, note="rpm"),
            "c_t_min_pu": dict(min=0.0, max=1.5, max_delta_percent=None,
                               note="с; задержка активации ПХХ (pops, режим «timered»); только без катализатора"),
            "c_t_min_pu_vs": dict(min=0.0, max=1.5, max_delta_percent=None, note="с"),
            "c_t_min_pu_rng_l": dict(min=0.0, max=1.5, max_delta_percent=None, note="с"),
        },
        "read_only": {
            "c_n_lim_min": "штатный порог «overrevving event» (7200 rpm во всех 39 файлах семейства)",
            "c_t_min_pu_msr": "задержка ПХХ после вмешательства MSR; 1.00 с во всех 39 файлах",
            "val_mo3_cal_cks_high": "место хранения суммы MO3 — пишет только ядро",
            "val_mo3_cal_cks_low": "место хранения суммы MO3 — пишет только ядро",
            "lc_ad_clr_var": "сброс обученных вариантов — управляется диагностикой/владельцем, не калибровкой Stage",
        },
        "packs": {
            "vmax": "c_vs_max",
            "rev_limit": "id_n_max_mt,id_n_max_h_mt",
            "pops": "c_t_min_pu,c_t_min_pu_vs,c_t_min_pu_rng_l",
        },
        "ack": {
            "c_t_min_pu": "no-catalyst",
            "c_t_min_pu_vs": "no-catalyst",
            "c_t_min_pu_rng_l": "no-catalyst",
        },
    },
    # 4560BM00S (E46/E39 MS45.0, 32 стока). До шага 3 была доказана только группа
    # задержек ПХХ; роли vmax и лимитеров оборотов добавлены после того, как
    # структурные отпечатки воспроизвели адреса, доказанные первоисточниками у
    # BO00S/LO00S/BN00S (см. docs/FAMILY_CALIBRATION_EVIDENCE_RU.md §1, §3).
    # Зажигание Stage 1 (карта RON98) добавлено после доказательства кластера
    # карт и осей по всем стокам (tests/test_family_ignition_proof.py).
    "4560BM00S": _data_family_ignition_spec(32),
    # --- Шаг 3: семейства, доказанные по стокам (без XDF и без XML-разметки). ---
    # Роли/границы/паки одинаковы по составу, различаются только пояснения с числом
    # стоков; адреса — в definitions/stock_derived_maps.json (своя карта на семью).
    # BL30S/LL00S — один сток: зажигание остаётся REQUIRES_DATA.
    "4560BL00S": _data_family_ignition_spec(7),
    "4560BL30S": _data_family_spec(1),
    "4570LL00S": _data_family_spec(1),
    "4570LL30S": _data_family_ignition_spec(6),
    "4570LM00S": _data_family_ignition_spec(7),
    "4570LN00S": _data_family_ignition_spec(8),
}

#: Символы, которые обязаны быть только для чтения (проверка политики).
#: Переключатель ``val_mo3_swi_cal_cks`` известен только для семейств с XDF:
#: у 4560BN00S его адрес не доказан, а записать его ядро всё равно не может
#: (default-deny + пост-проверка «изменилось только плановое»).
#: Защита задания 3, обязательная для семейств с доказанными адресами.
TASK3_READ_ONLY: tuple[str, ...] = (
    "ip_iga_bas_ron_91__n__maf",
    "ip_iga_bas__n__maf",
    "ip_iga_bas_max_knk__n__maf",
    "ip_cam_sp_1_in_pl__n__maf_ivvt",
    "ip_cam_sp_1_ex_pl__n__maf_ivvt",
    "ip_cam_sp_2_in_fl__n",
    "ip_cam_sp_2_ex_fl__n",
)

REQUIRED_READ_ONLY: dict[str, tuple[str, ...]] = {
    "4560BO00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_swi_cal_cks") + TASK3_READ_ONLY,
    "4570LO00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_swi_cal_cks")
    + TASK3_READ_ONLY
    + ("c_lamb_min_ls_act_test_afr",),
    "4560BN00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low"),
    "4560BM00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low")
    + ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"),
    # Шаг 3: у семейств без XDF обязательны те же четыре защиты. Переключатель
    # суммы (val_mo3_swi_cal_cks) в карту не входит: адрес не доказан, а записать
    # его ядро всё равно не может (default-deny + пост-проверка «изменилось только плановое»).
    # У семейств с доказанной картой RON98 добавлены защиты зажигания (RON91 и
    # fallback-карта) — их адреса доказаны тем же отпечатком кластера.
    "4560BL00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low")
    + ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"),
    "4560BL30S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low"),
    "4570LL00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low"),
    "4570LL30S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low")
    + ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"),
    "4570LM00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low")
    + ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"),
    "4570LN00S": ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low")
    + ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas__n__maf"),
}


#: Символы, обязанные быть только для чтения, у семейств с XDF (прежнее имя).
MUST_BE_READ_ONLY = REQUIRED_READ_ONLY["4560BO00S"]


@dataclass(frozen=True)
class SymbolSource:
    """Символ семейства без XDF: проверенный адрес, разрядность и формула.

    ``source``:

    * ``xml`` — адрес взят из XML-определений (проверяются при сборке политики);
    * ``data`` — адрес доказан данными (место пары MO3 из ``checksums``);
    * ``structural`` — адрес определён по структуре (упаковка группы), данные
      подтверждают поведение (константа во всех файлах).
    """

    symbol: str
    size_bits: int
    title: str = ""
    address: int | None = None
    rows: int = 1
    columns: int = 1
    scale_a: float = 1.0
    scale_b: float = 0.0
    units: str = ""
    source: str = "xml"
    require_constant: bool = False
    mo3_half: str = ""
    note: str = ""


#: Проверенная карта 4560BN00S (E60, SW 456). Все адреса — из XML-определений,
#: кроме отмеченных ``data``/``structural``; см. docs/BN00S_DEFINITIONS_RU.md.
BN00S_SYMBOLS: tuple[SymbolSource, ...] = (
    SymbolSource("c_vs_max", 8, title="Vehicle speed limiter", units="km/h",
                 note="E60: 235 и 254 км/ч в стоке"),
    SymbolSource("id_n_max_mt", 16, title="Engine speed limiter (MT)", columns=9, units="rpm"),
    SymbolSource("id_n_max_h_mt", 16, title="Increased engine speed limiter (MT)", columns=9, units="rpm"),
    SymbolSource("id_n_max_at", 16, title="Engine speed limiter (AT)", columns=9, units="rpm"),
    SymbolSource("id_n_max_h_at", 16, title="Increased engine speed limiter (AT)", columns=9, units="rpm"),
    SymbolSource("id_n_max_amt", 16, title="Engine speed limiter (AMT)", columns=9, units="rpm"),
    SymbolSource("id_n_max_h_amt", 16, title="Increased engine speed limiter (AMT)", columns=9, units="rpm"),
    SymbolSource("c_t_min_pu", 16, scale_a=0.01, units="s",
                 title="Delay before activating trailing throttle fuel cut when clutch is pressed or cruise active"),
    SymbolSource("c_t_min_pu_rng_l", 16, scale_a=0.01, units="s",
                 title="Delay before activating trailing throttle fuel cut when clutch is pressed or cruise active (low range)"),
    SymbolSource("c_t_min_pu_vs", 16, scale_a=0.01, units="s",
                 title="Delay before activating trailing throttle fuel cut when car is stationary"),
    SymbolSource("c_t_min_pu_msr", 16, address=0x5C44, scale_a=0.01, units="s",
                 source="structural", require_constant=True,
                 note="четвёртое слово группы задержек между c_t_min_pu (0x5C42) и c_t_min_pu_rng_l (0x5C46)"),
    SymbolSource("c_n_lim_min", 16, title="Engine speed threshold for overrevving event",
                 units="rpm", require_constant=True),
    SymbolSource("lc_ad_clr_var", 8, title="Reset learned variants",
                 require_constant=True, note="0 во всех файлах, как у BO/LO"),
    SymbolSource("val_mo3_cal_cks_high", 32, source="data", mo3_half="high",
                 note="место пары MO3 доказано пересчётом суммы во всех 39 файлах"),
    SymbolSource("val_mo3_cal_cks_low", 32, source="data", mo3_half="low",
                 note="место пары MO3 доказано пересчётом суммы во всех 39 файлах"),
)

#: Коды ошибок — как в MS42/MS43, чтобы поведение ядра было одинаковым.
CODE_SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
CODE_READ_ONLY = "READ_ONLY"
CODE_POLICY_RANGE_VIOLATION = "POLICY_RANGE_VIOLATION"
CODE_CAL_VERSION_UNSUPPORTED = "CAL_VERSION_UNSUPPORTED"
CODE_VALUE_NOT_REPRESENTABLE = "VALUE_NOT_REPRESENTABLE"
CODE_QUANTIZATION_TOO_COARSE = "QUANTIZATION_TOO_COARSE"
CODE_DELTA_TOO_LARGE = "DELTA_TOO_LARGE"
CODE_TOO_MANY_CELLS = "TOO_MANY_CELLS"
CODE_POLICY_INVALID = "POLICY_INVALID"


class PolicyError(ValueError):
    """Ошибка политики (или отказ по политике)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class StockStats:
    """Стоковые значения символа по всем файлам семейства (из данных)."""

    samples: int = 0
    variants: int = 0
    minimum: int | None = None
    maximum: int | None = None
    uniform: bool = False
    note: str = ""


@dataclass
class PolicyEntryData:
    """Разрешение на запись одного символа в одном семействе."""

    symbol: str
    family: str
    group: str
    address: int
    element_size_bits: int
    rows: int
    columns: int
    units: str
    scale_a: float
    scale_b: float
    equation: str
    minimum: float | None
    maximum: float | None
    max_delta_percent: float | None
    read_only: bool
    note: str
    stock: StockStats = field(default_factory=StockStats)

    @property
    def element_size_bytes(self) -> int:
        return self.element_size_bits // 8

    @property
    def element_count(self) -> int:
        return self.rows * self.columns

    @property
    def byte_range(self) -> range:
        return range(self.address, self.address + self.element_count * self.element_size_bytes)

    def to_raw(self, value: float) -> float:
        return (value - self.scale_b) / self.scale_a

    def to_engineering(self, raw: float) -> float:
        return self.scale_a * raw + self.scale_b

    @property
    def raw_limits(self) -> tuple[int, int]:
        return 0, (1 << self.element_size_bits) - 1

    def check_value(self, value: float) -> list[str]:
        """Проверить значение по границам политики и по разрядности."""
        problems: list[str] = []
        if value != value:  # NaN
            return [f"{CODE_VALUE_NOT_REPRESENTABLE}: {self.symbol}: значение не число"]
        if self.minimum is not None and value < self.minimum:
            problems.append(
                f"{CODE_POLICY_RANGE_VIOLATION}: {self.symbol}={value:g} меньше минимума {self.minimum:g}"
            )
        if self.maximum is not None and value > self.maximum:
            problems.append(
                f"{CODE_POLICY_RANGE_VIOLATION}: {self.symbol}={value:g} больше максимума {self.maximum:g}"
            )
        raw = self.to_raw(value)
        low, high = self.raw_limits
        if raw < low - 1e-9 or raw > high + 1e-9:
            problems.append(
                f"{CODE_VALUE_NOT_REPRESENTABLE}: {self.symbol}={value:g} не выражается "
                f"в {self.element_size_bits} битах (сырое {raw:g}, допустимо {low}..{high})"
            )
        return problems

    def quantize(self, value: float) -> tuple[int, float]:
        """Округлить значение к сырому коду и вернуть (код, ошибка квантования в %)."""
        raw = self.to_raw(value)
        low, high = self.raw_limits
        if raw < low - 1e-9 or raw > high + 1e-9:
            raise PolicyError(
                CODE_VALUE_NOT_REPRESENTABLE,
                f"{self.symbol}={value:g}: сырое {raw:g} вне {low}..{high}",
            )
        # Округление «половина вверх»: детерминировано и совпадает с ожиданием
        # пользователя (python round() использует банковское округление).
        code = min(max(int(math.floor(raw + 0.5)), low), high)
        achieved = self.to_engineering(code)
        error = abs(achieved - value) / abs(value) * 100 if value else abs(achieved)
        return code, error

    def as_dict(self) -> dict:
        return {
            "address": f"0x{self.address:05X}",
            "size_bits": self.element_size_bits,
            "shape": f"{self.rows}x{self.columns}",
            "units": self.units,
            "equation": self.equation,
            "scale_a": self.scale_a,
            "scale_b": self.scale_b,
            "min": self.minimum,
            "max": self.maximum,
            "max_delta_percent": self.max_delta_percent,
            "read_only": self.read_only,
            "group": self.group,
            "note": self.note,
            "stock": {
                "samples": self.stock.samples,
                "variants": self.stock.variants,
                "min": self.stock.minimum,
                "max": self.stock.maximum,
                "uniform": self.stock.uniform,
            },
        }


@dataclass
class FamilyPolicy:
    """Политика одного семейства: объекты, роли, запреты."""

    family: str
    definition_path: str
    roles: dict[str, tuple[str, ...]]
    packs: dict[str, tuple[str, ...]]
    objects: dict[str, PolicyEntryData] = field(default_factory=dict)
    read_only_reasons: dict[str, str] = field(default_factory=dict)
    #: Символ -> требуемое подтверждение клиента (например ``no-catalyst``).
    ack: dict[str, str] = field(default_factory=dict)

    def entry(self, symbol: str) -> PolicyEntryData:
        entry = self.objects.get(symbol)
        if entry is None:
            if symbol in self.read_only_reasons:
                raise PolicyError(
                    CODE_READ_ONLY,
                    f"{symbol}: только чтение — {self.read_only_reasons[symbol]}",
                )
            raise PolicyError(
                CODE_SYMBOL_NOT_ALLOWED,
                f"{symbol}: символа нет в allowlist семейства {self.family} "
                "(default-deny: разрешено только перечисленное)",
            )
        if entry.read_only:
            raise PolicyError(CODE_READ_ONLY, f"{symbol}: только чтение — {entry.note}")
        return entry

    def role(self, role: str) -> tuple[str, ...]:
        try:
            symbols = self.roles[role]
        except KeyError as exc:
            raise PolicyError(
                CODE_SYMBOL_NOT_ALLOWED, f"роль {role!r} не определена для {self.family}"
            ) from exc
        return symbols

    def acknowledgement(self, symbol: str) -> str | None:
        """Подтверждение, обязательное для символа (или ``None``)."""
        return self.ack.get(symbol)

    def pack(self, name: str) -> tuple[str, ...]:
        try:
            return self.packs[name]
        except KeyError as exc:
            raise PolicyError(
                CODE_SYMBOL_NOT_ALLOWED, f"пакет {name!r} не определён для {self.family}"
            ) from exc

    def as_dict(self) -> dict:
        return {
            "definition": pathfmt.to_posix(self.definition_path),
            "roles": {k: list(v) for k, v in sorted(self.roles.items())},
            "packs": {k: list(v) for k, v in sorted(self.packs.items())},
            "ack": dict(sorted(self.ack.items())),
            "read_only": dict(sorted(self.read_only_reasons.items())),
            "objects": {s: e.as_dict() for s, e in sorted(self.objects.items())},
        }


@dataclass
class Policy:
    """Политика MS45 по всем поддерживаемым семействам."""

    version: int
    family: str
    families: dict[str, FamilyPolicy]
    description: str
    max_quantization_error_percent: float = 1.0

    @property
    def software(self) -> tuple[str, ...]:
        return tuple(sorted(self.families))

    def for_family(self, family: str | None) -> FamilyPolicy:
        if not family:
            raise PolicyError(
                CODE_CAL_VERSION_UNSUPPORTED,
                "семейство не определено (нет ASCII-идентификатора в начале образа)",
            )
        try:
            return self.families[family]
        except KeyError as exc:
            raise PolicyError(
                CODE_CAL_VERSION_UNSUPPORTED,
                f"семейство {family!r} не поддерживается; поддерживаются "
                f"{', '.join(sorted(self.families))} (адреса между версиями не переносятся)",
            ) from exc

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "family": self.family,
            "software": list(self.software),
            "max_quantization_error_percent": self.max_quantization_error_percent,
            "description": self.description,
            "families": {name: fp.as_dict() for name, fp in sorted(self.families.items())},
        }


# ---------------------------------------------------------------------------
# Построение политики из данных
# ---------------------------------------------------------------------------


def stock_images(family: str) -> list[tuple[str, bytes]]:
    """Стоковые калибровки семейства (имя, байты) — границы считаются по ним.

    Для ``4560BN00S`` дополнительно берётся область калибровки реального чтения
    ЭБУ (``firmware_dumps/MS45.0_0044560BN_fullread_ORI.bin``) — это единственный
    настоящий дамп в репозитории и самый сильный свидетель стока. Для BM00S
    stock-derived карта дополнительно требует точного числа семейных стоков.
    """
    patterns = {
        "4560BO00S": ("MS45.0_0044560BO00S*.bin",),
        "4570LO00S": ("MS45.1_0044570LO00S*.bin",),
        "4560BN00S": ("MS45.0_0044560BN00S*.bin",),
        "4560BM00S": ("MS45.0_0044560BM00S*.bin",),
        "4560BL00S": ("MS45.0_0044560BL00S*.bin",),
        "4560BL30S": ("MS45.0_0044560BL30S*.bin",),
        "4570LL00S": ("MS45.1_0044570LL00S*.bin",),
        "4570LL30S": ("MS45.1_0044570LL30S*.bin",),
        "4570LM00S": ("MS45.1_0044570LM00S*.bin",),
        "4570LN00S": ("MS45.1_0044570LN00S*.bin",),
    }
    paths: list[Path] = []
    for pattern in patterns.get(family, ()):
        paths += sorted(SPDATEN_DIR.glob(pattern))
    if family == "4560BO00S":
        paths = sorted((FIRMWARE_DIR / "MS45.0_456BO00S_E83").glob("*.bin")) + paths
    images = [(path.name, path.read_bytes()) for path in paths if path.stat().st_size == ck.CAL_SIZE]
    if family == "4560BN00S" and BN_REAL_DUMP.exists():
        dump = BN_REAL_DUMP.read_bytes()
        start = ck.EXT_CAL_OFFSET
        images.append((BN_REAL_DUMP.name, dump[start : start + ck.CAL_SIZE]))
    return images


def _read_raw(image: bytes, symbol: "SymbolDef") -> tuple[int, ...]:
    """Прочитать сырые значения символа (big-endian, как в стоке MS45)."""
    size = symbol.element_size_bytes
    values = []
    for index in range(symbol.element_count):
        start = symbol.address + index * size
        chunk = image[start : start + size]
        if len(chunk) != size:
            raise PolicyError(
                CODE_POLICY_INVALID, f"{symbol.symbol}: данные обрезаны по {start:#x}"
            )
        values.append(int.from_bytes(chunk, "big"))
    return tuple(values)


def _stock_stats(symbol: "SymbolDef", images: list[tuple[str, bytes]]) -> StockStats:
    stats = StockStats()
    values: set[tuple[int, ...]] = set()
    for name, image in images:
        raw = _read_raw(image, symbol)
        values.add(raw)
        stats.samples += 1
        low = min(raw)
        high = max(raw)
        stats.minimum = low if stats.minimum is None else min(stats.minimum, low)
        stats.maximum = high if stats.maximum is None else max(stats.maximum, high)
    stats.variants = len(values)
    stats.uniform = len(values) == 1
    if not stats.uniform:
        stats.note = f"вариантов {stats.variants} из {stats.samples}"
    return stats


@dataclass(frozen=True)
class SymbolDef:
    """Объект калибровки, приведённый к одному виду независимо от источника."""

    symbol: str
    address: int
    element_size_bits: int
    rows: int
    columns: int
    scale_a: float
    scale_b: float
    units: str
    equation: str
    description: str
    source: str

    @property
    def element_size_bytes(self) -> int:
        return self.element_size_bits // 8

    @property
    def element_count(self) -> int:
        return self.rows * self.columns

    @classmethod
    def from_xdf(cls, obj: xd.XdfObject, source: str) -> "SymbolDef":
        if obj.scaler is None:
            raise PolicyError(
                CODE_POLICY_INVALID,
                f"{obj.title}: формула {obj.equation!r} непригодна для записи",
            )
        return cls(
            symbol=obj.title,
            address=obj.address,
            element_size_bits=obj.element_size_bits,
            rows=obj.rows,
            columns=obj.columns,
            scale_a=obj.scaler.a,
            scale_b=obj.scaler.b,
            units=obj.units,
            equation=obj.equation,
            description=obj.description,
            source=source,
        )


_DEFINITION_CACHE: dict[str, xd.Definition] = {}
_XML_SYMBOL_CACHE: dict[str, dict[str, SymbolDef]] = {}


def _xml_symbols(family: str) -> dict[str, SymbolDef]:
    """Символы семейства из XML-определений: адрес сверяется с проверенной картой."""
    if family in _XML_SYMBOL_CACHE:
        return _XML_SYMBOL_CACHE[family]
    rom_id = XML_FAMILIES[family]
    rom = xm.load_rom(XML_DEFINITIONS, rom_id, filesize="116kb")
    titles = {t.name: t.address for t in rom.tables}
    symbols: dict[str, SymbolDef] = {}
    for spec in BN00S_SYMBOLS:
        if spec.source == "xml":
            if spec.title not in titles:
                raise PolicyError(
                    CODE_POLICY_INVALID,
                    f"{family}:{spec.symbol}: в XML нет таблицы {spec.title!r}",
                )
            address = titles[spec.title]
            description = spec.title
        elif spec.source == "data":
            base = ck.MO3_STORE_OFFSET.get(family)
            if base is None:
                raise PolicyError(
                    CODE_POLICY_INVALID, f"{family}:{spec.symbol}: неизвестно место пары MO3"
                )
            address = base if spec.mo3_half == "high" else base + 4
            description = spec.note
        else:  # structural
            if spec.address is None:
                raise PolicyError(
                    CODE_POLICY_INVALID, f"{family}:{spec.symbol}: нет адреса"
                )
            address = spec.address
            description = spec.note
        equation = "X" if (spec.scale_a, spec.scale_b) == (1.0, 0.0) else f"{spec.scale_a:g}*X"
        symbols[spec.symbol] = SymbolDef(
            symbol=spec.symbol,
            address=address,
            element_size_bits=spec.size_bits,
            rows=spec.rows,
            columns=spec.columns,
            scale_a=spec.scale_a,
            scale_b=spec.scale_b,
            units=spec.units,
            equation=equation,
            description=description,
            source=f"xml:{rom_id}" if spec.source == "xml" else spec.source,
        )
    _XML_SYMBOL_CACHE[family] = symbols
    return symbols

_STOCK_DERIVED_SYMBOL_CACHE: dict[str, dict[str, SymbolDef]] = {}


def _stock_derived_symbols(family: str) -> dict[str, SymbolDef]:
    """Загрузить адреса, независимо выведенные из стоков только этой семьи."""
    if family in _STOCK_DERIVED_SYMBOL_CACHE:
        return _STOCK_DERIVED_SYMBOL_CACHE[family]
    try:
        definition = json.loads(STOCK_DERIVED_DEFINITIONS.read_text(encoding="utf-8"))
        family_block = definition["families"][family]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise PolicyError(
            CODE_POLICY_INVALID,
            f"{family}: нет корректной stock-derived карты в {STOCK_DERIVED_DEFINITIONS.name}",
        ) from exc
    samples = stock_images(family)
    expected_count = int(family_block.get("stock_count", 0))
    if expected_count and len(samples) != expected_count:
        raise PolicyError(
            CODE_POLICY_INVALID,
            f"{family}: карта требует {expected_count} стоков, найдено {len(samples)}",
        )
    symbols: dict[str, SymbolDef] = {}
    for name, spec in family_block.get("symbols", {}).items():
        source_kind = str(spec.get("source", "stock"))
        if source_kind == "data":
            base = ck.MO3_STORE_OFFSET.get(family)
            if base is None or spec.get("mo3_half") not in ("high", "low"):
                raise PolicyError(CODE_POLICY_INVALID, f"{family}:{name}: неверная MO3-привязка")
            address = base + (4 if spec["mo3_half"] == "low" else 0)
        else:
            try:
                address = int(str(spec["address"]), 0)
            except (KeyError, ValueError) as exc:
                raise PolicyError(CODE_POLICY_INVALID, f"{family}:{name}: неверный адрес") from exc
        bits = int(spec["size_bits"])
        rows, columns = int(spec.get("rows", 1)), int(spec.get("columns", 1))
        if bits not in (8, 16, 32) or rows < 1 or columns < 1:
            raise PolicyError(CODE_POLICY_INVALID, f"{family}:{name}: неверная геометрия объекта")
        if address < 0 or address + rows * columns * (bits // 8) > ck.CAL_SIZE:
            raise PolicyError(CODE_POLICY_INVALID, f"{family}:{name}: объект выходит за калибровку")
        scale_a = float(spec.get("scale_a", 1.0))
        scale_b = float(spec.get("scale_b", 0.0))
        if not math.isfinite(scale_a) or scale_a == 0 or not math.isfinite(scale_b):
            raise PolicyError(CODE_POLICY_INVALID, f"{family}:{name}: неверный масштаб")
        if scale_a == 1.0 and scale_b == 0.0:
            equation = "X"
        elif scale_b == 0.0:
            equation = f"{scale_a:g}*X"
        else:
            equation = f"{scale_a:g}*X+{scale_b:g}"
        symbol = SymbolDef(
            symbol=name,
            address=address,
            element_size_bits=bits,
            rows=rows,
            columns=columns,
            scale_a=scale_a,
            scale_b=scale_b,
            units=str(spec.get("units", "")),
            equation=equation,
            description=str(spec.get("note", "")),
            source=f"{source_kind}:{family}",
        )
        symbols[name] = symbol
        if source_kind == "stock" and spec.get("require_constant"):
            sample_values = {
                _read_raw(image, symbol)
                for _, image in samples
            }
            if len(sample_values) != 1:
                raise PolicyError(
                    CODE_POLICY_INVALID,
                    f"{family}:{name}: stock-derived definition requires one constant "
                    f"value across all {len(samples)} stocks; found {len(sample_values)} variants",
                )
    _STOCK_DERIVED_SYMBOL_CACHE[family] = symbols
    return symbols


_AXIS_SYMBOL_CACHE: dict[str, dict[str, SymbolDef]] = {}


def _axis_symbols(family: str) -> dict[str, SymbolDef]:
    """Оси карт семейства как символы политики (производные от XDF).

    У осей в определении нет собственного имени: есть адрес, разрядность,
    ``indexcount`` и формула внутри ``<XDFAXIS>``. Политика даёт им имя
    ``axis_<таблица>_<ось>``, чтобы генератор мог прочитать ось как обычный
    объект (только чтение). Адреса берутся из определения семейства, поэтому
    между семьями не переносятся.
    """
    if family in _AXIS_SYMBOL_CACHE:
        return _AXIS_SYMBOL_CACHE[family]
    spec = POLICY_SPEC.get(family, {}).get("axes", {})
    symbols: dict[str, SymbolDef] = {}
    if spec and family in STOCK_DERIVED_FAMILIES:
        # Семейство без XDF: оси — в карте стоков семьи (адрес доказан
        # отпечатком цепочки записей осей, см. tests/test_family_ignition_proof.py).
        derived = _stock_derived_symbols(family)
        for name in spec:
            if name not in derived:
                raise PolicyError(
                    CODE_POLICY_INVALID, f"{family}:{name}: оси нет в stock-derived карте"
                )
            symbols[name] = derived[name]
    elif spec:
        definition = _DEFINITION_CACHE.get(family)
        if definition is None:
            definition = xd.load_definition(family=family, variant="116K")
            _DEFINITION_CACHE[family] = definition
        for name, item in spec.items():
            try:
                obj = definition.find(item["table"])
            except xd.XdfError as exc:
                raise PolicyError(
                    CODE_POLICY_INVALID, f"{family}:{name}: нет таблицы {item['table']!r} ({exc})"
                ) from exc
            axis = obj.axis(item["axis"])
            if axis is None:
                raise PolicyError(
                    CODE_POLICY_INVALID,
                    f"{family}:{name}: у таблицы {item['table']!r} ось {item['axis']!r} без адреса",
                )
            if axis.scaler is None:
                raise PolicyError(
                    CODE_POLICY_INVALID, f"{family}:{name}: формула оси не линейна — читать нельзя"
                )
            if axis.address < 0 or axis.address + axis.count * axis.element_size_bytes > ck.CAL_SIZE:
                raise PolicyError(
                    CODE_POLICY_INVALID, f"{family}:{name}: ось выходит за калибровку (0x{axis.address:X})"
                )
            symbols[name] = SymbolDef(
                symbol=name,
                address=axis.address,
                element_size_bits=axis.element_size_bits,
                rows=1,
                columns=axis.count,
                scale_a=axis.scaler.a,
                scale_b=axis.scaler.b,
                units=axis.units,
                equation=axis.equation,
                description=str(item.get("note", "")),
                source=f"xdf-axis:{definition.path.name}",
            )
    _AXIS_SYMBOL_CACHE[family] = symbols
    return symbols


def family_symbol(family: str, symbol: str) -> SymbolDef:
    """Описание символа: из XDF, XML, оси XDF или карты, полученной из стоков семьи."""
    axis = _axis_symbols(family).get(symbol)
    if axis is not None:
        return axis
    if family in XML_FAMILIES:
        symbols = _xml_symbols(family)
        try:
            return symbols[symbol]
        except KeyError as exc:
            raise PolicyError(
                CODE_POLICY_INVALID, f"{family}:{symbol} отсутствует в проверенной карте"
            ) from exc
    if family in STOCK_DERIVED_FAMILIES:
        symbols = _stock_derived_symbols(family)
        try:
            return symbols[symbol]
        except KeyError as exc:
            raise PolicyError(
                CODE_POLICY_INVALID, f"{family}:{symbol} отсутствует в stock-derived карте"
            ) from exc
    definition = _DEFINITION_CACHE.get(family)
    if definition is None:
        definition = xd.load_definition(family=family, variant="116K")
        _DEFINITION_CACHE[family] = definition
    source = f"xdf:{definition.path.name}"
    try:
        return SymbolDef.from_xdf(definition.find(symbol), source)
    except xd.XdfError as exc:
        raise PolicyError(CODE_POLICY_INVALID, f"{family}:{symbol} отсутствует в XDF ({exc})") from exc


def definition_path(family: str) -> str:
    """Путь к первоисточнику определений семейства (для отчёта и политики).

    Всегда в POSIX-нотации: политика — артефакт, и её JSON обязан совпадать
    побайтово на Windows и Linux (иначе ``--validate`` падает на обратном
    слэше в путях).
    """
    if family in XML_FAMILIES:
        return pathfmt.relative_posix(XML_DEFINITIONS, ROOT)
    if family in STOCK_DERIVED_FAMILIES:
        return pathfmt.relative_posix(STOCK_DERIVED_DEFINITIONS, ROOT)
    definition = _DEFINITION_CACHE.get(family) or xd.load_definition(family=family, variant="116K")
    _DEFINITION_CACHE[family] = definition
    return pathfmt.relative_posix(definition.path, ROOT)


def _resolve_bound(
    token: float | str | None, obj: SymbolDef, stock: StockStats
) -> float | None:
    """Разрешить границу допуска: число — как есть, токен — от стока семейства.

    Границы «от стока» — принципиальная позиция: числа не вписываются руками, а
    считаются по всем стоковым файлам семейства при сборке политики (см.
    ``docs/PACKS_WEB_ANALYSIS_RU.md``). Поддерживаемые токены:

    * ``@stock_min`` / ``@stock_max`` — экстремумы стока (±``число`` — сдвиг);
    * ``@floor:<λ>`` — ближайшее снизу значение сетки не больше ``λ``
      (пол лямбды: порог обязан выражаться целым сырым кодом).
    """
    if token is None or isinstance(token, (int, float)):
        return token
    text = str(token).strip()
    if text.startswith("@floor:"):
        value = float(text.split(":", 1)[1])
        raw = math.floor((value - obj.scale_b) / obj.scale_a)
        return obj.scale_a * raw + obj.scale_b
    if text.startswith("@stock_min") or text.startswith("@stock_max"):
        if stock.minimum is None or stock.maximum is None:
            raise PolicyError(
                CODE_POLICY_INVALID,
                f"{obj.symbol}: токен {text!r} без стоковых значений",
            )
        # Экстремумы — инженерные: у карт с отрицательной формулой (фазы
        # выпуска) меньший сырой код означает БОЛЬШЕЕ значение.
        engineering = (
            obj.scale_a * stock.minimum + obj.scale_b,
            obj.scale_a * stock.maximum + obj.scale_b,
        )
        base = min(engineering) if text.startswith("@stock_min") else max(engineering)
        rest = text[len("@stock_min"):] if text.startswith("@stock_min") else text[len("@stock_max"):]
        return base + float(rest) if rest else base
    raise PolicyError(CODE_POLICY_INVALID, f"{obj.symbol}: неизвестный токен границы {text!r}")


def build_family_policy(family: str) -> FamilyPolicy:
    """Собрать политику семейства: адреса из определений, границы и сток — из данных."""
    if family not in SUPPORTED_FAMILIES:
        raise PolicyError(CODE_CAL_VERSION_UNSUPPORTED, f"семейство {family!r} не поддерживается")
    spec = POLICY_SPEC[family]
    images = stock_images(family)
    if not images:
        raise PolicyError(CODE_POLICY_INVALID, f"для {family} не найдено стоковых калибровок")

    objects: dict[str, PolicyEntryData] = {}
    for role, role_symbols in spec["roles"].items():
        for symbol in role_symbols:
            if symbol in objects:
                continue
            obj = family_symbol(family, symbol)
            limit = spec["limits"].get(symbol, {})
            stock = _stock_stats(obj, images)
            objects[symbol] = PolicyEntryData(
                symbol=symbol,
                family=family,
                group=role,
                address=obj.address,
                element_size_bits=obj.element_size_bits,
                rows=obj.rows,
                columns=obj.columns,
                units=obj.units,
                scale_a=obj.scale_a,
                scale_b=obj.scale_b,
                equation=obj.equation,
                minimum=_resolve_bound(limit.get("min"), obj, stock),
                maximum=_resolve_bound(limit.get("max"), obj, stock),
                max_delta_percent=limit.get("max_delta_percent"),
                read_only=False,
                note=limit.get("note", obj.description),
                stock=stock,
            )

    for symbol, reason in spec["read_only"].items():
        obj = family_symbol(family, symbol)  # символ обязан существовать в определениях
        objects[symbol] = PolicyEntryData(
            symbol=symbol,
            family=family,
            group="protected",
            address=obj.address,
            element_size_bits=obj.element_size_bits,
            rows=obj.rows,
            columns=obj.columns,
            units=obj.units,
            scale_a=obj.scale_a,
            scale_b=obj.scale_b,
            equation=obj.equation,
            minimum=None,
            maximum=None,
            max_delta_percent=None,
            read_only=True,
            note=reason,
            stock=_stock_stats(obj, images),
        )

    for symbol, spec_item in spec.get("axes", {}).items():
        obj = _axis_symbols(family)[symbol]
        objects[symbol] = PolicyEntryData(
            symbol=symbol,
            family=family,
            group="axes",
            address=obj.address,
            element_size_bits=obj.element_size_bits,
            rows=obj.rows,
            columns=obj.columns,
            units=obj.units,
            scale_a=obj.scale_a,
            scale_b=obj.scale_b,
            equation=obj.equation,
            minimum=None,
            maximum=None,
            max_delta_percent=None,
            read_only=True,
            note=str(spec_item.get("note", obj.description)),
            stock=_stock_stats(obj, images),
        )

    _check_source_expectations(family, objects)

    return FamilyPolicy(
        family=family,
        definition_path=definition_path(family),
        roles={role: tuple(role_symbols) for role, role_symbols in spec["roles"].items()},
        packs={name: tuple(s.strip() for s in value.split(",")) for name, value in spec["packs"].items()},
        objects=objects,
        read_only_reasons=dict(spec["read_only"]),
        ack=dict(spec.get("ack", {})),
    )


def _check_source_expectations(family: str, objects: dict[str, PolicyEntryData]) -> None:
    """Проверить ожидания, записанные в проверенной карте XML-семейства."""
    for spec in BN00S_SYMBOLS if family in XML_FAMILIES else ():
        entry = objects.get(spec.symbol)
        if entry is None:
            continue
        if spec.require_constant and not entry.stock.uniform:
            raise PolicyError(
                CODE_POLICY_INVALID,
                f"{family}:{spec.symbol}: ожидалась константа во всех стоковых файлах, "
                f"а значений {entry.stock.variants} из {entry.stock.samples}",
            )


def build_policy() -> Policy:
    """Собрать политику целиком (по данным стока)."""
    families = {family: build_family_policy(family) for family in SUPPORTED_FAMILIES}
    return Policy(
        version=POLICY_VERSION,
        family="MS45",
        families=families,
        description=(
            "MS45 (MS45.0 / MS45.1). Разрешены только символы, подтверждённые "
            "стоковыми образами; адреса ролей vmax / rev_limit_* / pops_delay "
            "доказаны для всех десяти семейств: 4560BO00S (XDF, SW 456, E83), "
            "4570LO00S (XDF, SW 457, E46 ZHP), 4560BN00S (XML, SW 456, E60) и "
            "структурными отпечатками по стокам — 4560BM00S, 4560BL00S, 4560BL30S, "
            "4570LL00S, 4570LL30S, 4570LM00S, 4570LN00S. Проверка отпечатков "
            "воспроизводится в tests/test_family_stock_proof.py. Launch control, "
            "таблицы интенсивности ПХХ и прочие недоказанные карты — REQUIRES_DATA. "
            "Задание 3: зажигание (карта RON98) и ВАНос (прогретые карты частичной "
            "нагрузки) выпускаются по логам из docs/pack_log_requirements.json на "
            "семействах с доказанными адресами (4560BO00S, 4570LO00S); обогащение "
            "полной нагрузки — только на 4570LO00S, где доказаны ворота диагностики "
            "богатой смеси. Зажигание (только карта RON98) доказано по стокам и у "
            "4560BL00S, 4560BM00S, 4570LL30S, 4570LM00S, 4570LN00S "
            "(tests/test_family_ignition_proof.py); у 4560BL30S и 4570LL00S один "
            "сток — зажигание REQUIRES_DATA."
        ),
    )


# ---------------------------------------------------------------------------
# Загрузка и проверка
# ---------------------------------------------------------------------------


def load_policy(path: Path | str = DEFAULT_POLICY) -> Policy:
    """Загрузить политику из JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    families: dict[str, FamilyPolicy] = {}
    for name, block in data["families"].items():
        objects: dict[str, PolicyEntryData] = {}
        for symbol, item in block["objects"].items():
            rows, columns = (int(v) for v in item["shape"].split("x"))
            stock = item.get("stock", {})
            for required in ("address", "size_bits", "shape", "scale_a", "scale_b"):
                if required not in item:
                    raise PolicyError(
                        CODE_POLICY_INVALID,
                        f"{name}:{symbol}: в политике нет поля {required!r} — пересоберите --build",
                    )
            objects[symbol] = PolicyEntryData(
                symbol=symbol,
                family=name,
                group=item.get("group", ""),
                address=int(item["address"], 0),
                element_size_bits=int(item["size_bits"]),
                rows=rows,
                columns=columns,
                units=item.get("units", ""),
                scale_a=float(item["scale_a"]),
                scale_b=float(item["scale_b"]),
                equation=item.get("equation", "X"),
                minimum=item.get("min"),
                maximum=item.get("max"),
                max_delta_percent=item.get("max_delta_percent"),
                read_only=bool(item.get("read_only")),
                note=item.get("note", ""),
                stock=StockStats(
                    samples=stock.get("samples", 0),
                    variants=stock.get("variants", 0),
                    minimum=stock.get("min"),
                    maximum=stock.get("max"),
                    uniform=stock.get("uniform", False),
                ),
            )
        families[name] = FamilyPolicy(
            family=name,
            definition_path=block.get("definition", ""),
            roles={role: tuple(symbols) for role, symbols in block.get("roles", {}).items()},
            packs={p: tuple(s) for p, s in block.get("packs", {}).items()},
            objects=objects,
            read_only_reasons=dict(block.get("read_only", {})),
            ack=dict(block.get("ack", {})),
        )
    return Policy(
        version=int(data["version"]),
        family=data["family"],
        families=families,
        description=data.get("description", ""),
        max_quantization_error_percent=float(data.get("max_quantization_error_percent", 1.0)),
    )


def write_policy(policy: Policy, path: Path | str = DEFAULT_POLICY) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(policy.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",  # артефакт одинаков на Windows и Linux
    )
    return target


def validate_policy(policy: Policy) -> list[str]:
    """Проверить политику: соответствие XDF, границы, обязательные запреты."""
    problems: list[str] = []
    if policy.version != POLICY_VERSION:
        problems.append(f"версия политики {policy.version} != {POLICY_VERSION}")
    for family in SUPPORTED_FAMILIES:
        if family not in policy.families:
            problems.append(f"нет политики для семейства {family}")
    for name, fp in policy.families.items():
        if name not in SUPPORTED_FAMILIES:
            problems.append(f"{name}: политика для неподдерживаемого семейства")
            continue
        for role in REQUIRED_ROLES_BY_FAMILY.get(name, REQUIRED_ROLES):
            if role not in fp.roles:
                problems.append(f"{name}: нет роли {role}")
        for symbol, entry in fp.objects.items():
            try:
                obj = family_symbol(name, symbol)
            except PolicyError as error:
                problems.append(f"{name}:{symbol}: нет определения ({error})")
                continue
            if entry.address != obj.address:
                problems.append(
                    f"{name}:{symbol}: адрес политики {entry.address:#x} != определения "
                    f"{obj.address:#x} ({obj.source})"
                )
            if entry.element_size_bits != obj.element_size_bits:
                problems.append(f"{name}:{symbol}: разрядность политики != определения")
            if abs(obj.scale_a - entry.scale_a) > 1e-12 or abs(obj.scale_b - entry.scale_b) > 1e-12:
                problems.append(f"{name}:{symbol}: формула политики != определения")
            if entry.rows != obj.rows or entry.columns != obj.columns:
                problems.append(f"{name}:{symbol}: форма политики != определения")
            if entry.read_only:
                continue
            if entry.minimum is None or entry.maximum is None:
                problems.append(f"{name}:{symbol}: нет границ допуска")
                continue
            if entry.minimum > entry.maximum:
                problems.append(f"{name}:{symbol}: минимум больше максимума")
            upper = (1 << entry.element_size_bits) - 1
            for label, value in (("min", entry.minimum), ("max", entry.maximum)):
                raw = entry.to_raw(value)
                if raw < 0 or raw > upper or abs(raw - round(raw)) > 1e-9:
                    problems.append(
                        f"{name}:{symbol}: {label}={value:g} не выражается сырым значением "
                        f"в {entry.element_size_bits} битах"
                    )
            if entry.stock.minimum is None or entry.stock.maximum is None:
                problems.append(f"{name}:{symbol}: нет стоковых значений")
                continue
            stock_range = (
                entry.to_engineering(entry.stock.minimum),
                entry.to_engineering(entry.stock.maximum),
            )
            if entry.maximum < stock_range[1]:
                problems.append(
                    f"{name}:{symbol}: предел {entry.maximum:g} ниже стокового максимума "
                    f"{stock_range[1]:g} — разрешение бессмысленно"
                )
        for symbol in REQUIRED_READ_ONLY.get(name, MUST_BE_READ_ONLY):
            entry = fp.objects.get(symbol)
            if entry is None:
                problems.append(f"{name}: защищённый символ {symbol} отсутствует в политике")
            elif not entry.read_only:
                problems.append(f"{name}:{symbol} обязан быть read_only")
        # запрет на запись сумм и подписи: их в allowlist быть не должно
        for symbol, entry in fp.objects.items():
            if not entry.read_only and entry.address in (0x100,):
                problems.append(f"{name}:{symbol}: адрес 0x100 — это ячейка CRC-32, писать нельзя")
            if not entry.read_only and 0x170 <= entry.address < 0x1B4:
                problems.append(f"{name}:{symbol}: адрес попадает в блок RSA-подписи")
    return problems


def _report(policy: Policy) -> int:
    print(f"Политика MS45 v{policy.version}, семейства: {', '.join(policy.software)}")
    for name, fp in sorted(policy.families.items()):
        writable = [e for e in fp.objects.values() if not e.read_only]
        protected = [e for e in fp.objects.values() if e.read_only]
        print(f"\n{name}: {fp.definition_path}")
        print(f"  разрешено к записи: {len(writable)}, только чтение: {len(protected)}")
        for entry in sorted(writable, key=lambda e: e.group):
            stock = (
                f"{entry.to_engineering(entry.stock.minimum):g}.."
                f"{entry.to_engineering(entry.stock.maximum):g}"
                if entry.stock.minimum is not None
                else "?"
            )
            print(
                f"    {entry.group:14s} {entry.symbol:20s} {entry.address:#07x} "
                f"{entry.element_size_bits:2d}бит {entry.rows}x{entry.columns} "
                f"допуск {entry.minimum:g}..{entry.maximum:g} {entry.units or '-'} "
                f"(сток {stock})"
            )
        for entry in sorted(protected, key=lambda e: e.symbol):
            print(f"    ЗАПРЕТ         {entry.symbol:20s} {entry.address:#07x} — {entry.note[:60]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()

    parser = argparse.ArgumentParser(description="Политика MS45: построение, проверка, отчёт")
    parser.add_argument("--build", action="store_true", help="пересобрать JSON из данных")
    parser.add_argument("--validate", action="store_true", help="проверить политику")
    parser.add_argument("--show", action="store_true", help="показать политику")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="путь к JSON политики")
    args = parser.parse_args(argv)

    if args.build:
        policy = build_policy()
        path = write_policy(policy, Path(args.policy))
        print(f"политика записана: {path}")
    else:
        policy = load_policy(Path(args.policy))

    problems = validate_policy(policy)
    if problems:
        print(f"ПРОБЛЕМЫ ({len(problems)}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    if args.validate or not args.show:
        print(f"политика корректна: {policy.family}, {len(policy.families)} семейства")
    if args.show:
        return _report(policy)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
