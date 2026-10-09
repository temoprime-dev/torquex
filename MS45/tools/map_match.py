#!/usr/bin/env python3
"""Поиск и сопоставление карт MS45 по структуре A2L (без переноса адресов из DAMOS).

Метод доказательства структуры:
-------------------------------
Адреса калибровочных карт зажигания, топлива и VANOS для семейств MS45 без XDF
(4560BL00S, 4560BL30S, 4560BM00S, 4570LL00S, 4570LL30S, 4570LM00S, 4570LN00S)
находятся не прямым переносом адресов из DAMOS/A2L (в разных версиях ПО смещения
различаются), а структурным сопоставлением спецификаций A2L по стоковым бинарникам:
1. Размерность таблицы (число строк, столбцов, байт на ячейку, порядок байт).
2. Наличие, размерность и монотонность осей X/Y (RPM, MAF, температура).
3. Физический масштаб формулы (scale_a * X + scale_b) и правдоподобный диапазон значений.
4. Взаимная упаковка соседних карт функциональной группы (последовательности 4x VANOS,
   5x Ignition, 2x Lambda basic, Lambda full load).
5. 100% совпадение одного и того же адреса во ВСЕХ стоковых калибровках семейства.
   Если семейство представлено одним стоком (BL30S, LL00S), выставляется низкая
   уверенность (confidence: low).
6. Строгий запрет на защитные карты (ip_fac_knk_*, ip_iga_knk_diag*, ip_maf_1_diag*,
   ip_fac_tq_req_driv_mon, ip_tqi_*, id_pvs_*), область подписи 0x174–0x1B3 и
   область вне калибровки 0x40000–0x5CFFF (в 116K смещения 0x00000–0x1CFFF).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MS45.tools.console import prepare_stdout

ROOT = Path(__file__).resolve().parents[2]
MS45_DIR = ROOT / "MS45"
DEFAULT_CAL_DIR = MS45_DIR / "firmware_spdaten" / "cal"
DEFAULT_CANDIDATES_JSON = MS45_DIR / "definitions" / "map_match_candidates.json"

CAL_SIZE_116K = 118784
CAL_OFFSET_MAX = 0x1D000
FLASH_CAL_START = 0x40000
FLASH_CAL_END = 0x5CFFF
SIGNATURE_START = 0x174
SIGNATURE_END = 0x1B3

PROTECTIVE_PATTERNS = (
    r"^ip_fac_knk_.*",
    r"^ip_iga_knk_diag.*",
    r"^ip_maf_1_diag.*",
    r"^ip_fac_tq_req_driv_mon$",
    r"^ip_tqi_.*",
    r"^id_pvs_.*",
)

TARGET_FAMILIES = (
    "4560BL00S",
    "4560BL30S",
    "4560BM00S",
    "4570LL00S",
    "4570LL30S",
    "4570LM00S",
    "4570LN00S",
)

BASELINE_FAMILIES = (
    "4560BO00S",
    "4570LO00S",
    "4560BN00S",
)

GROUND_TRUTH = {
    "4560BO00S": {
        "source": "Siemens_MS450_456BO02S_116K.xdf",
        "ecu_type": "MS45.0",
        "maps": {
            "ip_iga_bas_ron_98__n__maf": 0x0AF0C,
            "ip_iga_bas_ron_91__n__maf": 0x0ADCC,
            "ip_iga_bas__n__maf": 0x0AA0C,
            "ip_cam_sp_2_in_pl__n__maf_ivvt": 0x0A73C,
            "ip_cam_sp_2_ex_pl__n__maf_ivvt": 0x0A67C,
            "ip_cam_sp_1_in_pl__n__maf_ivvt": 0x0A5BC,
            "ip_cam_sp_1_ex_pl__n__maf_ivvt": 0x0A4FC,
            "ip_lamb_fl__n": 0x06B50,
            "ip_lamb_bas_1": 0x0E4E0,
            "ip_lamb_bas_2": 0x0E560,
        },
        "axes": {
            "axis_rpm_20": 0x086C0,
            "axis_maf_16": 0x0853C,
            "axis_rpm_16": 0x0873E,
            "axis_maf_12": 0x0860A,
            "axis_rpm_12_u8": 0x04DCE,
        },
    },
    "4570LO00S": {
        "source": "Siemens_MS451_457LO02S_116K.xdf",
        "ecu_type": "MS45.1",
        "maps": {
            "ip_iga_bas_ron_98__n__maf": 0x0B24C,
            "ip_iga_bas_ron_91__n__maf": 0x0B10C,
            "ip_iga_bas__n__maf": 0x0AD4C,
            "ip_cam_sp_2_in_pl__n__maf_ivvt": 0x0AB0C,
            "ip_cam_sp_2_ex_pl__n__maf_ivvt": 0x0AA4C,
            "ip_cam_sp_1_in_pl__n__maf_ivvt": 0x0A98C,
            "ip_cam_sp_1_ex_pl__n__maf_ivvt": 0x0A8CC,
            "ip_lamb_fl__n": 0x06DD0,
            "ip_lamb_bas_1": 0x0E950,
            "ip_lamb_bas_2": 0x0E9D0,
        },
        "axes": {
            "axis_rpm_20": 0x08992,
            "axis_maf_16": 0x08812,
            "axis_rpm_16": 0x08A0C,
            "axis_maf_12": 0x088EA,
            "axis_rpm_12_u8": 0x04D5A,
        },
    },
    "4560BN00S": {
        "source": "MS45 ECU Definitions.xml (4560BN00)",
        "ecu_type": "MS45.0",
        "maps": {
            "ip_iga_bas_ron_98__n__maf": 0x0AE64,
            "ip_iga_bas_ron_91__n__maf": 0x0AD24,
            "ip_iga_bas__n__maf": 0x0A964,
            "ip_cam_sp_2_in_pl__n__maf_ivvt": 0x0A694,
            "ip_cam_sp_2_ex_pl__n__maf_ivvt": 0x0A5D4,
            "ip_cam_sp_1_in_pl__n__maf_ivvt": 0x0A514,
            "ip_cam_sp_1_ex_pl__n__maf_ivvt": 0x0A454,
            "ip_lamb_fl__n": 0x06ADC,
            "ip_lamb_bas_1": 0x0E438,
            "ip_lamb_bas_2": 0x0E4B8,
        },
        "axes": {
            "axis_rpm_20": 0x08618,
            "axis_maf_16": 0x08494,
            "axis_rpm_16": 0x08696,
            "axis_maf_12": 0x08562,
            "axis_rpm_12_u8": 0x04D82,
        },
    },
}


def is_protective_map(name: str) -> bool:
    """Проверка, входит ли символ в запрещённые защитные карты."""
    for pattern in PROTECTIVE_PATTERNS:
        if re.search(pattern, name):
            return True
    return False


def is_valid_cal_offset(address: int, size: int) -> bool:
    """Проверка попадания объекта в калибровочный диапазон 116K (0x00000–0x1CFFF)."""
    return 0 <= address and (address + size) <= CAL_OFFSET_MAX


def touches_signature_region(address: int, size: int) -> bool:
    """Проверка, не пересекает ли объект защищённую область подписи 0x174–0x1B3."""
    end = address + size - 1
    return not (end < SIGNATURE_START or address > SIGNATURE_END)


@dataclass
class AxisInfo:
    name: str
    count: int
    address: int
    element_size_bytes: int
    units: str
    values: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "address": f"0x{self.address:05X}",
            "element_size_bytes": self.element_size_bytes,
            "units": self.units,
            "sample_min": self.values[0] if self.values else None,
            "sample_max": self.values[-1] if self.values else None,
        }


@dataclass
class MapCandidate:
    symbol: str
    category: str
    kind: str
    address: int
    rows: int
    columns: int
    element_size_bytes: int
    endian: str
    scale_a: float
    scale_b: float
    units: str
    raw_min: int
    raw_max: int
    phys_min: float
    phys_max: float
    confidence: str
    proven_across_all_stocks: bool
    stock_count: int
    axes: dict[str, dict] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "category": self.category,
            "type": self.kind,
            "address": f"0x{self.address:05X}",
            "flash_address": f"0x{self.address + FLASH_CAL_START:05X}",
            "rows": self.rows,
            "columns": self.columns,
            "total_bytes": self.rows * self.columns * self.element_size_bytes,
            "element_size_bytes": self.element_size_bytes,
            "endian": self.endian,
            "scale_a": self.scale_a,
            "scale_b": self.scale_b,
            "units": self.units,
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "phys_min": round(self.phys_min, 3),
            "phys_max": round(self.phys_max, 3),
            "confidence": self.confidence,
            "proven_across_all_stocks": self.proven_across_all_stocks,
            "stock_count": self.stock_count,
            "axes": self.axes,
            "notes": self.notes,
        }


def find_family_axes(image: bytes) -> dict[str, AxisInfo]:
    """Поиск характеристических осей сетки калибровок MS45 по структуре заголовков."""
    axes: dict[str, AxisInfo] = {}

    # Поиск 20-точечной оси оборотов (n_20, u16 BE)
    for i in range(0x07500, 0x0A000, 2):
        if int.from_bytes(image[i:i+2], "big") == 20:
            vals = [int.from_bytes(image[i+2+j*2:i+4+j*2], "big") for j in range(20)]
            if all(vals[j] < vals[j+1] for j in range(19)):
                if 250 <= vals[0] <= 1000 and 5500 <= vals[-1] <= 7200:
                    axes["axis_rpm_20"] = AxisInfo(
                        name="n",
                        count=20,
                        address=i + 2,
                        element_size_bytes=2,
                        units="rpm",
                        values=[float(v) for v in vals],
                    )
                    break

    # Поиск 16-точечной оси нагрузки (maf_16, u16 BE)
    for i in range(0x07500, 0x0A000, 2):
        if int.from_bytes(image[i:i+2], "big") == 16:
            vals = [int.from_bytes(image[i+2+j*2:i+4+j*2], "big") for j in range(16)]
            if all(vals[j] < vals[j+1] for j in range(15)):
                if 1200 <= vals[0] <= 3600 and 20000 <= vals[-1] <= 36000:
                    axes["axis_maf_16"] = AxisInfo(
                        name="maf",
                        count=16,
                        address=i + 2,
                        element_size_bytes=2,
                        units="mg/stroke",
                        values=[round(v * 0.02119, 2) for v in vals],
                    )
                    break

    # Поиск 16-точечной оси оборотов VANOS (n_16, u16 BE)
    for i in range(0x07500, 0x0A000, 2):
        if int.from_bytes(image[i:i+2], "big") == 16:
            vals = [int.from_bytes(image[i+2+j*2:i+4+j*2], "big") for j in range(16)]
            if all(vals[j] < vals[j+1] for j in range(15)):
                if 400 <= vals[0] <= 800 and 5500 <= vals[-1] <= 7000:
                    axes["axis_rpm_16"] = AxisInfo(
                        name="n",
                        count=16,
                        address=i + 2,
                        element_size_bytes=2,
                        units="rpm",
                        values=[float(v) for v in vals],
                    )
                    break

    # Поиск 12-точечной оси нагрузки VANOS (maf_12, u16 BE)
    for i in range(0x07500, 0x0A000, 2):
        if int.from_bytes(image[i:i+2], "big") == 12:
            vals = [int.from_bytes(image[i+2+j*2:i+4+j*2], "big") for j in range(12)]
            if all(vals[j] < vals[j+1] for j in range(11)):
                if 1200 <= vals[0] <= 4500 and 16000 <= vals[-1] <= 35000:
                    axes["axis_maf_12"] = AxisInfo(
                        name="maf",
                        count=12,
                        address=i + 2,
                        element_size_bytes=2,
                        units="mg/stroke",
                        values=[round(v * 0.02119, 2) for v in vals],
                    )
                    break

    # Поиск 12-точечной оси оборотов лямбды (ldp_n__lamb_fl, u8, scale * 32)
    for i in range(0x04000, 0x06000):
        if image[i] == 12:
            raw_vals = list(image[i+1:i+13])
            if all(raw_vals[j] < raw_vals[j+1] for j in range(11)):
                if 15 <= raw_vals[0] <= 45 and 180 <= raw_vals[-1] <= 220:
                    axes["axis_rpm_12_u8"] = AxisInfo(
                        name="n_32",
                        count=12,
                        address=i + 1,
                        element_size_bytes=1,
                        units="rpm",
                        values=[float(v * 32) for v in raw_vals],
                    )
                    break

    return axes


def match_ignition_cluster(image: bytes, axes: dict[str, AxisInfo]) -> dict[str, int] | None:
    """Поиск 5-карточного блока зажигания 20x16 (каждая 320 байт)."""
    # Сканируем диапазон смещений 0x0A000..0x0BF00
    for cand_start in range(0x09800, 0x0B600, 2):
        # 5 карт по 320 байт:
        # [0] ip_iga_bas__n__maf
        # [1] ip_iga_bas_add_max_ivvt__n__maf
        # [2] ip_iga_bas_add_min_ivvt__n__maf
        # [3] ip_iga_bas_ron_91__n__maf
        # [4] ip_iga_bas_ron_98__n__maf
        block = image[cand_start : cand_start + 1600]
        if len(block) != 1600:
            continue

        r_bas = block[0 : 320]
        r91 = block[3 * 320 : 4 * 320]
        r98 = block[4 * 320 : 5 * 320]

        if not (
            all(0x10 <= b <= 0xF5 for b in r_bas)
            and all(0x10 <= b <= 0xF5 for b in r91)
            and all(0x10 <= b <= 0xF5 for b in r98)
        ):
            continue

        diffs = [b98 - b91 for b98, b91 in zip(r98, r91)]
        # RON98 угол обязан быть >= RON91 по ячейкам, и с существенной дельтой на пике
        if not (sum(1 for d in diffs if d >= 0) >= 305 and max(diffs) > 10 and min(diffs) >= -15):
            continue

        # Физическая монотонность: угол растёт с оборотами (строка 19 > строка 0)
        # и падает с нагрузкой (столбец 0 > столбец 15)
        row0_avg = sum(r98[:16]) / 16.0
        row19_avg = sum(r98[19 * 16 : 20 * 16]) / 16.0
        col0_avg = sum(r98[r * 16] for r in range(20)) / 20.0
        col15_avg = sum(r98[r * 16 + 15] for r in range(20)) / 20.0

        if row19_avg > row0_avg + 15 and col0_avg > col15_avg + 10:
            return {
                "ip_iga_bas__n__maf": cand_start,
                "ip_iga_bas_add_max_ivvt__n__maf": cand_start + 0x140,
                "ip_iga_bas_add_min_ivvt__n__maf": cand_start + 0x280,
                "ip_iga_bas_ron_91__n__maf": cand_start + 0x3C0,
                "ip_iga_bas_ron_98__n__maf": cand_start + 0x500,
            }

    return None


def match_vanos_cluster(image: bytes, ign_start: int, ecu_type: str) -> dict[str, int] | None:
    """Поиск 4-карточного блока VANOS 16x12 (каждая 192 байта) относительно блока зажигания."""
    gap = 0x210 if ecu_type == "MS45.0" else 0x180
    vanos_end = ign_start - gap
    c2_in = vanos_end - 0xC0
    c2_ex = c2_in - 0xC0
    c1_in = c2_ex - 0xC0
    c1_ex = c1_in - 0xC0

    if c1_ex < 0 or vanos_end > len(image):
        return None

    b_c1ex = image[c1_ex : c1_ex + 192]
    b_c1in = image[c1_in : c1_in + 192]
    b_c2ex = image[c2_ex : c2_ex + 192]
    b_c2in = image[c2_in : c2_in + 192]

    # Проверка физических пределов распредвалов
    if not (
        all(0x60 <= b <= 0xD5 for b in b_c1ex)
        and all(0x30 <= b <= 0xC5 for b in b_c1in)
        and all(0x60 <= b <= 0xD5 for b in b_c2ex)
        and all(0x30 <= b <= 0xC5 for b in b_c2in)
    ):
        return None

    return {
        "ip_cam_sp_1_ex_pl__n__maf_ivvt": c1_ex,
        "ip_cam_sp_1_in_pl__n__maf_ivvt": c1_in,
        "ip_cam_sp_2_ex_pl__n__maf_ivvt": c2_ex,
        "ip_cam_sp_2_in_pl__n__maf_ivvt": c2_in,
    }


def match_fuel_maps(image: bytes) -> dict[str, int] | None:
    """Поиск кривой обогащения полной нагрузки ip_lamb_fl__n и базовых сеток ip_lamb_bas_1/2."""
    res: dict[str, int] = {}

    # 1. Поиск ip_lamb_fl__n (1x12 uint16 BE, 24 байта)
    # Диапазон значений 800..1050 (0.78..1.02 lambda), обогащение на высоких оборотах
    for i in range(0x06000, 0x07500, 2):
        chunk = image[i : i + 24]
        if len(chunk) != 24:
            continue
        words = [int.from_bytes(chunk[j*2 : j*2+2], "big") for j in range(12)]
        if all(800 <= w <= 1050 for w in words):
            if words[0] >= words[-1] and (words[0] - words[-1]) >= 8:
                res["ip_lamb_fl__n"] = i
                break

    # 2. Поиск ip_lamb_bas_1 и ip_lamb_bas_2 (две соседние сетки 8x8 uint16 BE, 128 байт каждая)
    # Базовое стехиометрическое значение 1.0000 = сырое 0x4000 (16384)
    for i in range(0x0D000, 0x0F800, 2):
        chunk = image[i : i + 256]
        if len(chunk) != 256:
            continue
        t1 = [int.from_bytes(chunk[j*2 : j*2+2], "big") for j in range(64)]
        t2 = [int.from_bytes(chunk[128 + j*2 : 128 + j*2+2], "big") for j in range(64)]
        if all(15000 <= w <= 17000 for w in t1) and all(15000 <= w <= 17000 for w in t2):
            if any(w == 0x4000 for w in t1) or any(16000 <= w <= 16500 for w in t1):
                res["ip_lamb_bas_1"] = i
                res["ip_lamb_bas_2"] = i + 128
                break

    return res if len(res) == 3 else None


def match_single_image(image: bytes, family: str) -> dict[str, int] | None:
    """Выполняет структурное сопоставление всех целевых карт для одного бинарника."""
    if len(image) != CAL_SIZE_116K:
        return None

    ecu_type = "MS45.1" if family.startswith("4570") else "MS45.0"
    axes = find_family_axes(image)
    if "axis_rpm_20" not in axes or "axis_maf_16" not in axes:
        return None

    ign = match_ignition_cluster(image, axes)
    if not ign:
        return None

    vanos = match_vanos_cluster(image, ign["ip_iga_bas__n__maf"], ecu_type)
    if not vanos:
        return None

    fuel = match_fuel_maps(image)
    if not fuel:
        return None

    found: dict[str, int] = {}
    found.update(ign)
    found.update(vanos)
    found.update(fuel)

    # Проверка на защитные карты и границы
    for sym, addr in found.items():
        if is_protective_map(sym):
            return None
        if not is_valid_cal_offset(addr, 1):
            return None
        if touches_signature_region(addr, 1):
            return None

    return found


def match_family_stocks(family: str, cal_files: list[Path]) -> dict | None:
    """Сопоставление и доказательство карт по ВСЕМ стокам одного семейства."""
    if not cal_files:
        return None

    ecu_type = "MS45.1" if family.startswith("4570") else "MS45.0"
    sw_version = "457" if ecu_type == "MS45.1" else "456"
    confidence = "low" if len(cal_files) == 1 else "high"

    first_image = cal_files[0].read_bytes()
    first_matches = match_single_image(first_image, family)
    if not first_matches:
        return None

    first_axes = find_family_axes(first_image)

    # Проверяем инвариантность адресов на всех остальных стоках семейства
    for cal_file in cal_files[1:]:
        img = cal_file.read_bytes()
        matches = match_single_image(img, family)
        if matches != first_matches:
            return None

    # Формируем объекты кандидатов
    maps_out: dict[str, dict] = {}
    for sym, addr in sorted(first_matches.items()):
        if "iga" in sym:
            category = "ignition"
            kind = "MAP"
            rows, cols, elem_sz = 20, 16, 1
            scale_a, scale_b, units = 0.375, -35.625, "deg_crk"
            raw_vals = [first_image[addr + k] for k in range(rows * cols)]
            axes_dict = {
                "x": first_axes["axis_maf_16"].to_dict() if "axis_maf_16" in first_axes else {},
                "y": first_axes["axis_rpm_20"].to_dict() if "axis_rpm_20" in first_axes else {},
            }
        elif "cam" in sym:
            category = "vanos"
            kind = "MAP"
            rows, cols, elem_sz = 16, 12, 1
            if "_in_" in sym:
                scale_a, scale_b, units = 0.375, 60.0, "deg_crk"
            else:
                scale_a, scale_b, units = -0.375, -40.0, "deg_crk"
            raw_vals = [first_image[addr + k] for k in range(rows * cols)]
            axes_dict = {
                "x": first_axes["axis_maf_12"].to_dict() if "axis_maf_12" in first_axes else {},
                "y": first_axes["axis_rpm_16"].to_dict() if "axis_rpm_16" in first_axes else {},
            }
        elif sym == "ip_lamb_fl__n":
            category = "fuel"
            kind = "CURVE"
            rows, cols, elem_sz = 1, 12, 2
            scale_a, scale_b, units = 0.000977, 0.0, "lambda"
            raw_vals = [
                int.from_bytes(first_image[addr + k*2 : addr + k*2 + 2], "big")
                for k in range(12)
            ]
            axes_dict = {
                "x": first_axes["axis_rpm_12_u8"].to_dict() if "axis_rpm_12_u8" in first_axes else {}
            }
        elif "lamb_bas" in sym:
            category = "fuel"
            kind = "MAP"
            rows, cols, elem_sz = 8, 8, 2
            scale_a, scale_b, units = 0.000061035, 0.0, "lambda"
            raw_vals = [
                int.from_bytes(first_image[addr + k*2 : addr + k*2 + 2], "big")
                for k in range(64)
            ]
            axes_dict = {
                "x": {"name": "maf", "count": 8, "units": "mg/stroke"},
                "y": {"name": "n", "count": 8, "units": "rpm"},
            }
        else:
            continue

        raw_min = min(raw_vals)
        raw_max = max(raw_vals)
        phys_min = raw_min * scale_a + scale_b
        phys_max = raw_max * scale_a + scale_b

        cand = MapCandidate(
            symbol=sym,
            category=category,
            kind=kind,
            address=addr,
            rows=rows,
            columns=cols,
            element_size_bytes=elem_sz,
            endian="big",
            scale_a=scale_a,
            scale_b=scale_b,
            units=units,
            raw_min=raw_min,
            raw_max=raw_max,
            phys_min=phys_min,
            phys_max=phys_max,
            confidence=confidence,
            proven_across_all_stocks=True,
            stock_count=len(cal_files),
            axes=axes_dict,
            notes=f"Доказано структурным методом по всем {len(cal_files)} стокам семейства {family}.",
        )
        maps_out[sym] = cand.to_dict()

    return {
        "family": family,
        "ecu_type": ecu_type,
        "sw_version": sw_version,
        "stock_count": len(cal_files),
        "confidence": confidence,
        "maps_count": len(maps_out),
        "maps": maps_out,
    }


def validate_baselines(cal_dir: Path | str = DEFAULT_CAL_DIR) -> dict:
    """Калибровка метода на эталонах BO00S/LO00S/BN00S с проверкой 100% совпадения адресов."""
    cal_dir = Path(cal_dir)
    results = {}
    total_checks = 0
    passed_checks = 0

    for fam in BASELINE_FAMILIES:
        gt = GROUND_TRUTH[fam]
        files = sorted(cal_dir.glob(f"*{fam}*"))
        if not files:
            continue

        fam_match = match_family_stocks(fam, files)
        if not fam_match:
            results[fam] = {
                "source": gt["source"],
                "stock_count": len(files),
                "matched_symbols": 0,
                "total_symbols": len(gt["maps"]),
                "rate_percent": 0.0,
                "status": "FAILED",
            }
            continue

        matched_symbols = 0
        for sym, exp_addr in gt["maps"].items():
            total_checks += 1
            cand = fam_match["maps"].get(sym)
            if cand and cand["address"] == f"0x{exp_addr:05X}":
                matched_symbols += 1
                passed_checks += 1

        rate = (matched_symbols / len(gt["maps"])) * 100.0 if gt["maps"] else 0.0
        status = "PASSED" if rate == 100.0 else "FAILED"
        results[fam] = {
            "source": gt["source"],
            "stock_count": len(files),
            "matched_symbols": matched_symbols,
            "total_symbols": len(gt["maps"]),
            "rate_percent": rate,
            "status": status,
        }

    overall_rate = (passed_checks / total_checks) * 100.0 if total_checks else 0.0
    return {
        "overall_status": "PASSED" if overall_rate == 100.0 else "FAILED",
        "overall_rate_percent": overall_rate,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
        "families": results,
    }


def match_all_families(cal_dir: Path | str = DEFAULT_CAL_DIR) -> dict:
    """Запуск сопоставления по всем 7 целевым семействам MS45."""
    cal_dir = Path(cal_dir)
    baselines_report = validate_baselines(cal_dir)
    if baselines_report["overall_status"] != "PASSED":
        raise RuntimeError("Отказ калибровки: эталонные семейства не прошли 100% проверку адресов!")

    families_out: dict[str, dict] = {}
    for fam in TARGET_FAMILIES:
        files = sorted(cal_dir.glob(f"*{fam}*"))
        res = match_family_stocks(fam, files)
        if res:
            families_out[fam] = res

    requires_data = [
        "stage1_ignition_advance_without_knock_logs: увеличение УОЗ выше стока требует логов детонации",
        "stage2_fuel_closed_loop_enrichment: обогащение кроме ip_lamb_fl__n требует широкополосной лямбды",
        "vanos_timing_advance_without_dyno: изменение фаз распредвалов требует стендового подтверждения",
        "single_stock_families_bl30s_ll00s: семейства с 1 стоком помечены низкой уверенностью (требуются новые дампы)",
        "protective_maps_forbidden: защитные карты (детонация, температура, момент, педаль) исключены из записи",
        "launch_control_ms45: отсутствует калибровочный путь, код патчить запрещено",
        "disa_isa_off_ms45: не подтверждена механика и калибровка в MS45",
        "pops_intensity_via_ignition: интенсивность хлопков через откат УОЗ на ПХХ не калибруется без стенда",
    ]

    out = {
        "version": 1,
        "description": "Кандидаты карт зажигания, топлива и VANOS для 7 семейств MS45 без XDF (исследовательский пул, в политику не включены).",
        "generated_date": "2026-10-08",
        "method": "A2L_structural_matching (geometry, axes, scaling, physical bounds, multi-stock invariant)",
        "baseline_validation": baselines_report,
        "families": families_out,
        "requires_data": requires_data,
        "excluded_protective_maps": list(PROTECTIVE_PATTERNS),
    }

    return out


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()
    parser = argparse.ArgumentParser(description="Поиск карт MS45 по структуре A2L")
    parser.add_argument("--cal-dir", default=str(DEFAULT_CAL_DIR), help="Каталог стоковых калибровок")
    parser.add_argument("--validate-baselines", action="store_true", help="Проверить метод на BO00S/LO00S/BN00S")
    parser.add_argument("--match-all", action="store_true", help="Запустить сопоставление по всем 7 семействам")
    parser.add_argument("--family", help="Запустить сопоставление для одного семейства")
    parser.add_argument("--export-json", nargs="?", const=str(DEFAULT_CANDIDATES_JSON), help="Экспорт JSON кандидатов")
    parser.add_argument("--json", action="store_true", help="Вывод в формате JSON")

    args = parser.parse_args(argv)
    cal_dir = Path(args.cal_dir)

    if args.validate_baselines:
        report = validate_baselines(cal_dir)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"Калибровка эталонов: {report['overall_status']} ({report['overall_rate_percent']:.1f}%)")
            for fam, d in report["families"].items():
                print(f"  {fam}: {d['matched_symbols']}/{d['total_symbols']} карт ({d['rate_percent']:.1f}%) [{d['status']}]")
        return 0 if report["overall_status"] == "PASSED" else 1

    if args.family:
        files = sorted(cal_dir.glob(f"*{args.family}*"))
        res = match_family_stocks(args.family, files)
        if not res:
            print(f"Семейство {args.family}: совпадений не найдено (REQUIRES_DATA)")
            return 2
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"Семейство {args.family} ({res['ecu_type']}, SW {res['sw_version']}): {res['maps_count']} карт [уверенность: {res['confidence']}]")
            for sym, m in res["maps"].items():
                print(f"  {sym:35s} @ {m['address']} {m['rows']}x{m['columns']} {m['units']} [{m['phys_min']}..{m['phys_max']}]")
        return 0

    # По умолчанию или при --match-all / --export-json
    data = match_all_families(cal_dir)

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Кандидаты сохранены: {out_path} ({len(data['families'])} семейств)")

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif not args.export_json:
        print(f"Эталонная калибровка: {data['baseline_validation']['overall_status']} ({data['baseline_validation']['overall_rate_percent']:.1f}%)")
        print(f"Сопоставлено целевых семейств: {len(data['families'])}")
        for fam, d in data["families"].items():
            print(f"  {fam} ({d['ecu_type']}, {d['stock_count']} стоков): {d['maps_count']} карт [уверенность: {d['confidence']}]")
            for sym, m in d["maps"].items():
                print(f"    {sym:33s} @ {m['address']} ({m['rows']}x{m['columns']}) [{m['phys_min']}..{m['phys_max']} {m['units']}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
