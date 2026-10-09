"""Разбор A2L MS45 (экспорт ETA S/DDS) — исследовательский инструмент.

Не входит в продукт: нужен, чтобы установить, какой семье соответствуют
первоисточные определения, и получить независимую проверку карты калибровки.
"""

from __future__ import annotations

import sys

import re
from dataclasses import dataclass, field
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MS45.tools.console import prepare_stdout  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

_DTYPES = {
    "UBYTE": (1, False), "SBYTE": (1, True),
    "UWORD": (2, False), "SWORD": (2, True),
    "ULONG": (4, False), "SLONG": (4, True),
    "FLOAT32_IEEE": (4, False), "A_UINT64": (8, False),
}


@dataclass
class CompuMethod:
    name: str
    linear: tuple[float, float] | None  # (a, b): phys = a*raw + b
    unit: str = ""


@dataclass
class RecordLayout:
    name: str
    size: int
    signed: bool


@dataclass
class Characteristic:
    name: str
    kind: str                 # VALUE | CURVE | MAP | VAL_BLK | ASCII
    address: int
    layout: str
    compu: str
    minimum: float | None = None
    maximum: float | None = None
    axes: list[tuple[str, int, str, str | None]] = field(default_factory=list)


@dataclass
class Measurement:
    name: str
    address: int
    layout: str
    compu: str


@dataclass
class A2L:
    path: Path
    compu: dict[str, CompuMethod]
    layouts: dict[str, RecordLayout]
    characteristics: dict[str, Characteristic]
    measurements: dict[str, Measurement]

    def value(self, cal: bytes, ch: Characteristic, shift: int = 0) -> float | None:
        lay = self.layouts.get(ch.layout)
        cm = self.compu.get(ch.compu)
        if lay is None or cm is None or cm.linear is None:
            return None
        off = ch.address + shift
        if off < 0 or off + lay.size > len(cal):
            return None
        raw = int.from_bytes(cal[off:off + lay.size], "big", signed=lay.signed)
        a, b = cm.linear
        return a * raw + b


def _blocks(text: str, kind: str) -> list[str]:
    out = []
    for m in re.finditer(rf"/begin {kind}\b(.*?)/end {kind}\b", text, re.S):
        out.append(m.group(1))
    return out


def _tok_lines(body: str) -> list[str]:
    lines = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s.startswith("/*"):
            continue
        if s.startswith('"'):
            continue
        lines.append(s)
    return lines


def parse_a2l(path: Path | str) -> A2L:
    path = Path(path)
    text = path.read_text(encoding="latin-1")

    compu: dict[str, CompuMethod] = {}
    for body in _blocks(text, "COMPU_METHOD"):
        name = body.split(None, 1)[0]
        unit = ""
        mu = re.findall(r'"([^"]*)"', body)
        if len(mu) >= 2:
            unit = mu[1]
        lin = re.search(r"COEFFS_LINEAR\s+([-\d.eE+]+)\s+([-\d.eE+]+)", body)
        if lin:
            compu[name] = CompuMethod(name, (float(lin.group(1)), float(lin.group(2))), unit)
        else:
            rat = re.search(
                r"RAT_FUNC\s+\"([^\"]*)\"\s+\"([^\"]*)\"\s+COEFFS\s+"
                r"([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+"
                r"([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", body)
            if rat:
                a, b, c, d, e, f = (float(rat.group(i)) for i in range(3, 9))
                if d == 0.0 and e == 0.0 and f != 0.0:
                    compu[name] = CompuMethod(name, (b / f, c / f), rat.group(2) or unit)
                else:
                    compu[name] = CompuMethod(name, None, rat.group(2) or unit)
            else:
                compu[name] = CompuMethod(name, None, unit)

    layouts: dict[str, RecordLayout] = {}
    for body in _blocks(text, "RECORD_LAYOUT"):
        name = body.split(None, 1)[0]
        m = re.search(r"FNC_VALUES\s+\d+\s+(\w+)", body)
        if not m:
            continue
        dtype = m.group(1)
        if dtype not in _DTYPES:
            continue
        size, signed = _DTYPES[dtype]
        layouts[name] = RecordLayout(name, size, signed)

    chars: dict[str, Characteristic] = {}
    for body in _blocks(text, "CHARACTERISTIC"):
        lines = _tok_lines(body)
        if len(lines) < 5:
            continue
        name, kind, addr = lines[0], lines[1], lines[2]
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", addr):
            continue
        ch = Characteristic(name, kind, int(addr, 16), lines[3], lines[5])
        tail = []
        for line in lines[6:]:
            if line.startswith(("DISPLAY_IDENTIFIER", "FORMAT", "/begin", "ECU_ADDRESS", "BIT_MASK",
                                "EXTENDED_LIMITS", "MONOTONY", "BYTE_ORDER", "NUMBER", "PHYS_UNIT",
                                "STEP_SIZE", "READ_WRITE", "ADDR_TYPE", "LAYOUT")):
                break
            tail.append(line)
        rest = " ".join(tail)
        nums = re.findall(r"(?<![\w.])(-?\d+(?:\.\d*)?)(?![\w.])", rest)
        if len(nums) >= 2:
            ch.minimum, ch.maximum = float(nums[0]), float(nums[1])
        for ax in re.finditer(
            r"/begin AXIS_DESCR\s+(\w+)\s+(\w+)\s+(\S+)\s*(.*?)/end AXIS_DESCR", body, re.S
        ):
            axis_type, attribute, ref, inner = ax.groups()
            ax_addr = None
            m = re.search(r"0[xX][0-9a-fA-F]+", inner)
            if m:
                ax_addr = int(m.group(0), 16)
            ch.axes.append((axis_type, -1 if ax_addr is None else ax_addr, attribute, ref))
        chars[name] = ch

    meas: dict[str, Measurement] = {}
    for body in _blocks(text, "MEASUREMENT"):
        lines = _tok_lines(body)
        if len(lines) < 5:
            continue
        m = re.search(r"ECU_ADDRESS\s+(0[xX][0-9a-fA-F]+)", body)
        if not m:
            continue
        meas[lines[0]] = Measurement(lines[0], int(m.group(1), 16), lines[1], lines[2])

    return A2L(path, compu, layouts, chars, meas)


def main() -> int:
    prepare_stdout()

    import argparse

    ap = argparse.ArgumentParser(description="разбор A2L MS45")
    ap.add_argument("path", nargs="?", default=str(ROOT / "a2l/456BG23P.a2l"))
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    a2l = parse_a2l(args.path)
    print(f"{a2l.path.name}: COMPU {len(a2l.compu)}, RECORD_LAYOUT {len(a2l.layouts)}, "
          f"CHARACTERISTIC {len(a2l.characteristics)}, MEASUREMENT {len(a2l.measurements)}")
    linear = sum(1 for c in a2l.characteristics.values()
                 if a2l.compu.get(c.compu) and a2l.compu[c.compu].linear)
    with_range = sum(1 for c in a2l.characteristics.values() if c.minimum is not None)
    print(f"  линейных методов: {linear}, с границами min/max: {with_range}")
    for ch in list(a2l.characteristics.values())[: args.limit]:
        cm = a2l.compu.get(ch.compu)
        lay = a2l.layouts.get(ch.layout)
        print(f"  {ch.name:34s} {ch.kind:8s} 0x{ch.address:05X} "
              f"{lay.size if lay else '?'}Б лин={cm.linear if cm else None} "
              f"[{ch.minimum},{ch.maximum}] {cm.unit[:12] if cm else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
