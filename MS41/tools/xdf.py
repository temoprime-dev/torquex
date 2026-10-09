#!/usr/bin/env python3
"""Read Siemens MS41 TunerPro XDF definitions into exact, editable objects.

Two facts about this XDF family drive the whole design:

1. Word order is little-endian for 16-bit elements. The global ``DEFAULTS``
   block claims big-endian, but every 16-bit object carries
   ``mmedtypeflags="0x02"``, and little-endian is the only reading that produces
   physically sensible values, for example a 1.0 multiplier stored as 32768
   with a 1/32768 scale.
2. Axis addresses are display metadata. The ``x``/``y`` axis breakpoints point
   into shared or linked storage and do not read back as their own labels, so
   only the ``z`` values are treated as addressable data.

Only the ``z`` block is exposed for reading and writing. Axis blocks are never
written.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEFAULT_XDF = ROOT / "definitions" / "Siemens_MS41_1429861_ENG_24K_v1.0.xdf"

# mmedtypeflags bit that marks little-endian element storage.
TYPE_FLAG_LSB_FIRST = 0x02

OBJECT_PATTERN = re.compile(
    r"<XDFCONSTANT\b.*?</XDFCONSTANT>|<XDFTABLE\b.*?</XDFTABLE>", re.S
)


def _clean(equation: str) -> str:
    return (
        equation.replace("&#013;&#010;", "")
        .replace("&#13;&#10;", "")
        .replace("\r", "")
        .replace("\n", "")
        .replace(" ", "")
    )


class XdfError(ValueError):
    """Raised when a definition cannot be represented exactly."""


def _eval_node(node, x: float) -> float:
    """Evaluate a restricted arithmetic AST with the single variable X."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, x)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise XdfError(f"unsupported constant {node.value!r}")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id != "X":
            raise XdfError(f"unsupported variable {node.id!r}")
        return x
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, x)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise XdfError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, x)
        right = _eval_node(node.right, x)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise XdfError("division by zero in equation")
            return left / right
        raise XdfError("unsupported binary operator")
    raise XdfError(f"unsupported expression node {type(node).__name__}")


def _to_linear(equation: str) -> tuple[float, float]:
    """Reduce an arbitrary linear expression in X to (a, b) with value = a*X + b."""
    text = _clean(equation).replace("x", "X")
    if "X" not in text:
        raise XdfError(f"equation has no variable: {equation!r}")
    if not re.fullmatch(r"[0-9X+\-*/().eE]+", text):
        raise XdfError(f"equation uses unsupported characters: {equation!r}")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise XdfError(f"cannot parse equation {equation!r}") from exc
    at_zero = _eval_node(tree, 0.0)          # b
    at_one = _eval_node(tree, 1.0)           # a + b
    at_two = _eval_node(tree, 2.0)           # 2a + b, proof of linearity
    factor = at_one - at_zero
    if abs((2 * factor + at_zero) - at_two) > 1e-6 * max(1.0, abs(at_two)):
        raise XdfError(f"equation is not linear: {equation!r}")
    if not math.isfinite(factor) or not math.isfinite(at_zero):
        raise XdfError(f"equation produced a non-finite scale: {equation!r}")
    if factor == 0:
        raise XdfError(f"equation has a zero factor: {equation!r}")
    return factor, at_zero


@dataclass(frozen=True)
class Scaler:
    """Linear raw <-> engineering transform: value = a * raw + b."""

    a: float
    b: float

    @classmethod
    def from_equation(cls, equation: str) -> "Scaler":
        factor, offset = _to_linear(equation)
        return cls(factor, offset)

    def to_engineering(self, raw: float) -> float:
        return self.a * raw + self.b

    def to_raw(self, value: float) -> float:
        if self.a == 0:
            raise XdfError("degenerate scale with zero factor")
        return (value - self.b) / self.a


@dataclass
class XdfObject:
    title: str
    kind: str  # "constant" | "table"
    address: int
    element_size_bits: int
    lsb_first: bool
    scaler: Scaler
    rows: int = 1
    columns: int = 1
    header_address: int | None = None
    units: str = ""
    description: str = ""
    range_low: float | None = None
    range_high: float | None = None
    comments: str = ""
    #: Static axis labels declared in the XDF, per axis id ("x"/"y"/"z").
    #: These are *grids*, not calibration data: an axis that declares labels and
    #: no embedded address is a fixed lookup grid (the MAF voltage axis is one).
    #: An axis with an embedded address is linkage into the binary instead, and
    #: its bytes must never be read as values.
    labels: dict[str, tuple[float, ...]] = field(default_factory=dict)
    #: Axis ids that carry an embedded address, i.e. read their data from the BIN.
    linked_axes: tuple[str, ...] = ()
    #: Axis ids whose label list mixes in non-numeric markers ("INC" ramps).
    text_axes: tuple[str, ...] = ()

    def axis_labels(self, axis: str) -> tuple[float, ...]:
        """Static grid for an axis, empty when the axis has none."""
        return self.labels.get(axis, ())

    def has_static_axis(self, axis: str) -> bool:
        return bool(self.labels.get(axis))

    @property
    def element_size_bytes(self) -> int:
        if self.element_size_bits % 8:
            raise XdfError(f"{self.title}: {self.element_size_bits} is not a whole byte")
        return self.element_size_bits // 8

    @property
    def element_count(self) -> int:
        return self.rows * self.columns

    @property
    def byte_length(self) -> int:
        return self.element_count * self.element_size_bytes

    def byte_offsets(self, index: int = 0) -> range:
        if not 0 <= index < self.element_count:
            raise XdfError(
                f"{self.title}: index {index} outside 0..{self.element_count - 1}"
            )
        step = self.element_size_bytes
        start = self.address + index * step
        return range(start, start + step)

    def to_engineering(self, raw: float) -> float:
        return self.scaler.to_engineering(raw)

    def to_raw(self, value: float) -> float:
        return self.scaler.to_raw(value)


def _field(block: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    return match.group(1).strip() if match else None


def _embedded(body: str) -> dict[str, str]:
    match = re.search(r"<EMBEDDEDDATA([^/>]*)/>", body)
    if not match:
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', match.group(1)))


def _int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value, 0)
    except ValueError:
        return default


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _axis_metadata(block: str) -> tuple[dict[str, tuple[float, ...]], tuple[str, ...],
                                        tuple[str, ...]]:
    """Read the static axis grids of a table.

    XDF marks a fixed lookup grid with a list of ``<LABEL>`` entries; it marks an
    axis that points into the binary with an ``mmedaddress`` instead. The two are
    not interchangeable: the MAF table's voltage axis is a static grid, while
    ``id_maf_hys_ivvt__n``'s axes are linkage whose bytes decode to garbage.
    """
    labels: dict[str, tuple[float, ...]] = {}
    text_axes: list[str] = []
    linked: list[str] = []
    for axis_id in ("x", "y", "z"):
        match = re.search(rf'<XDFAXIS id="{axis_id}"(.*?)</XDFAXIS>', block, re.S)
        if not match:
            continue
        body = match.group(1)
        if axis_id != "z" and _embedded(body).get("mmedaddress"):
            linked.append(axis_id)
            continue
        raw = re.findall(r'<LABEL index="\d+" value="([^"]+)"', body)
        if not raw:
            continue
        numeric: list[float] = []
        for value in raw:
            try:
                numeric.append(float(value))
            except ValueError:
                text_axes.append(axis_id)
                break
        else:
            labels[axis_id] = tuple(numeric)
    return labels, tuple(text_axes), tuple(linked)


def parse_object(block: str) -> XdfObject | None:
    title = _field(block, "title")
    if not title:
        return None
    kind = "table" if block.startswith("<XDFTABLE") else "constant"
    head = _embedded(block)
    if kind == "constant" and not head.get("mmedaddress"):
        # Label-only helpers carry no direct address: they are display metadata,
        # not editable calibration data.
        return None
    address = _int(head.get("mmedaddress"), -1)
    bits = _int(head.get("mmedelementsizebits"), 8)
    flags = _int(head.get("mmedtypeflags"), 0)
    lsb_first = bool(flags & TYPE_FLAG_LSB_FIRST) or bits == 16

    rows = _int(head.get("mmedrowcount"), 1) or 1
    columns = _int(head.get("mmedcolcount"), 1) or 1
    data_address = address
    z_embedded: dict[str, str] = {}
    if kind == "table":
        match = re.search(
            r'<XDFAXIS id="z">(.*?)</XDFAXIS>', block, re.S
        )
        if not match:
            raise XdfError(f"{title}: table has no z axis")
        z_body = match.group(1)
        z_embedded = _embedded(z_body)
        if not z_embedded.get("mmedaddress"):
            raise XdfError(f"{title}: z axis has no address")
        data_address = _int(z_embedded.get("mmedaddress"), -1)
        if data_address < 0:
            raise XdfError(f"{title}: z axis has no address")
        rows = _int(z_embedded.get("mmedrowcount"), rows) or 1
        columns = _int(z_embedded.get("mmedcolcount"), columns) or 1
        bits = _int(z_embedded.get("mmedelementsizebits"), bits)
        z_flags = _int(z_embedded.get("mmedtypeflags"), flags)
        lsb_first = bool(z_flags & TYPE_FLAG_LSB_FIRST) or bits == 16
        stride = _int(z_embedded.get("mmedmajorstridebits"), 0)
        if stride != 0:
            raise XdfError(
                f"{title}: z rows use a non-contiguous stride of {stride} bits"
            )
        equation = _field(z_body, "MATH") or ""
        equation = re.search(r'<MATH equation="([^"]*)"', z_body)
        equation = equation.group(1) if equation else "X"
    else:
        match = re.search(r'<MATH equation="([^"]*)"', block)
        equation = match.group(1) if match else "X"

    scaler = Scaler.from_equation(equation)
    labels, text_axes, linked = _axis_metadata(block)
    return XdfObject(
        title=title,
        kind=kind,
        address=data_address,
        element_size_bits=bits,
        lsb_first=lsb_first,
        scaler=scaler,
        rows=rows,
        columns=columns,
        header_address=address if kind == "table" else None,
        units=_field(block, "units") or "",
        description=(_field(block, "description") or "").replace("&#013;&#010;", " | "),
        range_low=_float(_field(block, "rangelow")),
        range_high=_float(_field(block, "rangehigh")),
        labels=labels,
        linked_axes=linked,
        text_axes=text_axes,
    )


def load_definition(path: Path = DEFAULT_XDF) -> tuple[dict[str, list[XdfObject]], list[str]]:
    """Load every representable object plus the titles that had to be skipped.

    Skipped objects are reported, never silently dropped: a title that cannot be
    represented exactly must not later look like a valid, patchable symbol.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    objects: dict[str, list[XdfObject]] = {}
    problems: list[str] = []
    for block in OBJECT_PATTERN.findall(text):
        try:
            obj = parse_object(block)
        except XdfError as exc:
            problems.append(str(exc))
            continue
        if obj:
            objects.setdefault(obj.title, []).append(obj)
    return objects, problems


def load_objects(path: Path = DEFAULT_XDF) -> dict[str, list[XdfObject]]:
    """All representable objects, keyed by title.

    Raises XdfError when any object cannot be represented; this is the strict
    behaviour the rest of the toolchain relies on.
    """
    objects, problems = load_definition(path)
    if problems:
        raise XdfError("; ".join(problems[:5]))
    return objects


def get_object(
    objects: dict[str, list[XdfObject]], title: str, index: int = 0
) -> XdfObject:
    matches = objects.get(title)
    if not matches:
        raise XdfError(f"{title!r} is not present in the XDF")
    if index >= len(matches):
        raise XdfError(f"{title!r} has only {len(matches)} definition(s)")
    return matches[index]


def read_element(raw: bytes, offset: int, obj: XdfObject) -> int:
    size = obj.element_size_bytes
    chunk = raw[offset:offset + size]
    if len(chunk) != size:
        raise XdfError(f"{obj.title}: read past end of file at 0x{offset:X}")
    return int.from_bytes(chunk, "little" if obj.lsb_first else "big")


def write_element(raw: bytearray, offset: int, obj: XdfObject, value: int) -> None:
    size = obj.element_size_bytes
    limit = 1 << obj.element_size_bits
    if not 0 <= value < limit:
        raise XdfError(
            f"{obj.title}: raw {value} does not fit in {obj.element_size_bits} bits"
        )
    raw[offset:offset + size] = value.to_bytes(size, "little" if obj.lsb_first else "big")


def read_values(raw: bytes, obj: XdfObject) -> list[int]:
    step = obj.element_size_bytes
    return [
        read_element(raw, obj.address + i * step, obj)
        for i in range(obj.element_count)
    ]


def read_engineering(raw: bytes, obj: XdfObject) -> list[float]:
    return [obj.to_engineering(v) for v in read_values(raw, obj)]
