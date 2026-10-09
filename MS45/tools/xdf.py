#!/usr/bin/env python3
"""Разбор определений MS45 (TunerPro XDF) и проверка их по стоковым образам.

Определения MS4X для MS45 сконвертированы из A2L и содержат **все** объекты
(MS45.0: 3 661 константа + 1 795 таблиц; MS45.1: 3 885 + 1 820), поэтому этот
модуль — только чтение структуры: адрес, размер элемента, разметка таблицы,
формула ``MATH``, единицы, описание, категория. Разрешения на запись он не даёт:
это делает allowlist (`MS45/policies/stage_policy.json`).

Что важно и проверено по данным
-------------------------------

* ``BASEOFFSET``: у определений на **116 КиБ** он равен ``0`` (адреса — смещения
  внутри файла калибровки), у определений на **1024 КиБ** — ``0x40000`` (адреса —
  смещения внутри полного образа 1 МиБ). Область объявлена тегом ``REGION``.
* **Порядок байт: big-endian**, несмотря на ``lsbfirst="1"`` в ``DEFAULTS`` и
  ``mmedtypeflags="0x01"`` у констант. Это проверено значениями: ``c_n_lim_min``
  = 7200 rpm (совпадает с «set to 7200RPM by default» у ms4x) и ``id_n_max_mt`` =
  6480…6900 rpm читаются только как BE; чтение LE даёт бессмыслицу (61.4 s вместо
  0.24 s и т. п.). Определение в этом месте врёт, данные — нет.
* Переменные ``X0``/``X00``/… в уравнениях — тот же дефект, что и у MS43
  (``<VAR id="X00" />`` без атрибутов означает сырое значение), поэтому они
  приводятся к ``X`` и запоминаются в ``aliased_variables``.
* Оси таблиц в MS45 **связаны с бинарём** (у них есть свой ``mmedaddress``), то
  есть значения осей читаются из образа, а не из ``<LABEL>``.
"""

from __future__ import annotations

import argparse
import ast
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Прямой запуск скриптом (``python MS45/tools/<инструмент>.py``): в этом режиме
# пакета MS45 в ``sys.path`` нет, поэтому добавляем корень репозитория. Импорты
# ниже — всегда пакетные, чтобы модуль был один, а не две его копии.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MS45.tools.console import prepare_stdout  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = ROOT / "definitions"

#: Размер области калибровки (116 КиБ) и полного образа внешней флеш (1 МиБ).
CAL_SIZE = 0x1D000
EXT_CAL_OFFSET = 0x40000
FULL_SIZE = 0x100000

#: Область калибровки в координатах определения: (начало, размер).
CAL_REGION_BY_VARIANT: dict[str, tuple[int, int]] = {
    "116K": (0x0, CAL_SIZE),
    "1024K": (EXT_CAL_OFFSET, CAL_SIZE),
}

#: Определения по семействам: ``variant -> путь``.
XDF_116K: dict[str, Path] = {
    "4560BO00S": DEFINITIONS / "Siemens_MS450_456BO02S_116K.xdf",
    "4570LO00S": DEFINITIONS / "Siemens_MS451_457LO02S_116K.xdf",
}
XDF_1024K: dict[str, Path] = {
    "4560BO00S": DEFINITIONS / "Siemens_MS450_456BO02S_1024K.xdf",
    "4570LO00S": DEFINITIONS / "Siemens_MS451_457LO02S_1024K.xdf",
}

OBJECT_PATTERN = re.compile(r"<XDFCONSTANT\b.*?</XDFCONSTANT>|<XDFTABLE\b.*?</XDFTABLE>", re.S)
ALIASED_VARIABLE_RE = re.compile(r"^X0*$")
DEFAULT_VARIABLE = "X"


class XdfError(ValueError):
    """Определение не соответствует поддерживаемому диалекту."""


def _clean(equation: str) -> str:
    return (
        equation.replace("&#013;&#010;", "")
        .replace("&#13;&#10;", "")
        .replace("\r", "")
        .replace("\n", "")
        .strip()
        .replace("x", DEFAULT_VARIABLE)
    )


def _eval_node(node: ast.AST, x: float) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, x)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise XdfError("в уравнении недопустимая константа")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id == DEFAULT_VARIABLE:
            return x
        raise XdfError(f"неизвестная переменная в уравнении: {node.id!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand, x)
        return value if isinstance(node.op, ast.UAdd) else -value
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
                raise XdfError("деление на ноль в уравнении")
            return left / right
        raise XdfError("неподдерживаемый оператор в уравнении")
    raise XdfError(f"неподдерживаемый узел выражения: {type(node).__name__}")


def _substitute_aliases(equation: str) -> tuple[str, tuple[str, ...]]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", equation)
    aliases = tuple(
        sorted({t for t in tokens if t != DEFAULT_VARIABLE and ALIASED_VARIABLE_RE.match(t)})
    )
    if not aliases:
        return equation, ()
    fixed = equation
    for alias in aliases:
        fixed = re.sub(rf"\b{re.escape(alias)}\b", DEFAULT_VARIABLE, fixed)
    return fixed, aliases


def _to_linear(equation: str) -> tuple[float, float]:
    """Привести линейное выражение к паре (a, b): engineering = a * raw + b."""
    text = _clean(equation)
    if DEFAULT_VARIABLE not in text:
        raise XdfError(f"в уравнении нет переменной: {equation!r}")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise XdfError(f"уравнение не разбирается: {equation!r}") from exc
    at_zero = _eval_node(tree, 0.0)
    at_one = _eval_node(tree, 1.0)
    at_two = _eval_node(tree, 2.0)
    factor = at_one - at_zero
    if abs((2 * factor + at_zero) - at_two) > 1e-6 * max(1.0, abs(at_two)):
        raise XdfError(f"уравнение не линейно: {equation!r}")
    if not math.isfinite(factor) or not math.isfinite(at_zero):
        raise XdfError(f"уравнение дало не конечный коэффициент: {equation!r}")
    if factor == 0:
        raise XdfError(f"уравнение с нулевым множителем: {equation!r}")
    return factor, at_zero


@dataclass(frozen=True)
class Scaler:
    """Линейный переход сырое <-> инженерное: значение = a * raw + b."""

    a: float
    b: float

    @classmethod
    def from_equation(cls, equation: str) -> "Scaler | None":
        """``None`` — формула не линейна или вырождена (запись запрещена)."""
        try:
            factor, offset = _to_linear(equation)
        except XdfError:
            return None
        return cls(factor, offset)

    def to_engineering(self, raw: float) -> float:
        return self.a * raw + self.b

    def to_raw(self, value: float) -> float:
        if self.a == 0:
            raise XdfError("вырожденный масштаб с нулевым множителем")
        return (value - self.b) / self.a


@dataclass(frozen=True)
class XdfAxis:
    """Ось таблицы со своим адресом в бинаре (``mmedaddress`` в ``XDFAXIS``).

    В MS45 оси связаны с калибровкой: у них те же атрибуты, что у таблиц
    (адрес, разрядность, формула, число значений), поэтому они читаются так же.
    """

    axis_id: str
    address: int
    element_size_bits: int
    count: int
    units: str
    scaler: Scaler | None
    equation: str

    @property
    def element_size_bytes(self) -> int:
        if self.element_size_bits % 8:
            raise XdfError(f"ось {self.axis_id}: {self.element_size_bits} бит — не целые байты")
        return self.element_size_bits // 8

    @property
    def byte_range(self) -> range:
        return range(self.address, self.address + self.count * self.element_size_bytes)

    def to_dict(self) -> dict:
        return {
            "axis": self.axis_id,
            "address": self.address,
            "element_size_bits": self.element_size_bits,
            "count": self.count,
            "units": self.units,
            "scaler": (
                {"a": self.scaler.a, "b": self.scaler.b} if self.scaler else None
            ),
            "equation": self.equation,
        }


@dataclass
class XdfObject:
    """Один объект определения (константа или таблица)."""

    title: str
    kind: str  # "constant" | "table"
    address: int
    element_size_bits: int
    scaler: Scaler | None
    rows: int = 1
    columns: int = 1
    units: str = ""
    description: str = ""
    category: str = ""
    range_low: float | None = None
    range_high: float | None = None
    type_flags: int = 0
    labels: dict[str, tuple[float, ...]] = field(default_factory=dict)
    linked_axes: tuple[str, ...] = ()
    text_axes: tuple[str, ...] = ()
    aliased_variables: tuple[str, ...] = ()
    equation: str = ""
    axes: tuple[XdfAxis, ...] = ()

    def axis(self, axis_id: str) -> XdfAxis | None:
        """Ось по имени (``x``/``y``) или ``None`` — если она без адреса."""
        for item in self.axes:
            if item.axis_id == axis_id:
                return item
        return None

    @property
    def element_size_bytes(self) -> int:
        if self.element_size_bits % 8:
            raise XdfError(f"{self.title}: {self.element_size_bits} бит — не целые байты")
        return self.element_size_bits // 8

    @property
    def element_count(self) -> int:
        return self.rows * self.columns

    @property
    def byte_length(self) -> int:
        return self.element_count * self.element_size_bytes

    @property
    def byte_range(self) -> range:
        return range(self.address, self.address + self.byte_length)

    @property
    def scaling_usable(self) -> bool:
        """Пригодна ли формула для пересчёта (вырожденные объекты — метаданные)."""
        return self.scaler is not None

    def _require_scaler(self) -> "Scaler":
        if self.scaler is None:
            raise XdfError(
                f"{self.title}: формула {self.equation!r} не линейна или вырождена — "
                "объект нельзя ни читать в инженерных единицах, ни записывать"
            )
        return self.scaler

    def to_engineering(self, raw: float) -> float:
        return self._require_scaler().to_engineering(raw)

    def to_raw(self, value: float) -> float:
        return self._require_scaler().to_raw(value)

    def byte_offsets(self, index: int = 0) -> range:
        if not 0 <= index < self.element_count:
            raise XdfError(f"{self.title}: индекс {index} вне 0..{self.element_count - 1}")
        step = self.element_size_bytes
        start = self.address + index * step
        return range(start, start + step)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "kind": self.kind,
            "address": f"{self.address:#x}",
            "bits": self.element_size_bits,
            "rows": self.rows,
            "columns": self.columns,
            "units": self.units,
            "scaler": (
                {"a": self.scaler.a, "b": self.scaler.b} if self.scaler else None
            ),
            "equation": self.equation,
            "category": self.category,
            "description": self.description,
        }


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


def _axis_metadata(
    block: str,
) -> tuple[dict[str, tuple[float, ...]], tuple[str, ...], tuple[str, ...]]:
    labels: dict[str, tuple[float, ...]] = {}
    linked: list[str] = []
    text_axes: list[str] = []
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
    return labels, tuple(linked), tuple(text_axes)


def _axis_objects(block: str) -> tuple[XdfAxis, ...]:
    """Оси таблицы с адресом в бинаре (``<EMBEDDEDDATA mmedaddress=...>``)."""
    axes: list[XdfAxis] = []
    for axis_id in ("x", "y"):
        match = re.search(rf'<XDFAXIS id="{axis_id}"(.*?)</XDFAXIS>', block, re.S)
        if not match:
            continue
        body = match.group(1)
        embedded = _embedded(body)
        address = _int(embedded.get("mmedaddress"), -1)
        if address < 0:
            continue
        count = _int(_field(body, "indexcount"), 0) or 1
        math_groups = re.findall(r'<MATH equation="([^"]*)"', body)
        equation = math_groups[-1] if math_groups else DEFAULT_VARIABLE
        fixed, _aliases = _substitute_aliases(equation)
        axes.append(
            XdfAxis(
                axis_id=axis_id,
                address=address,
                element_size_bits=_int(embedded.get("mmedelementsizebits"), 8) or 8,
                count=count,
                units=_field(body, "units") or "",
                scaler=Scaler.from_equation(fixed),
                equation=equation,
            )
        )
    return tuple(axes)


def parse_object(block: str) -> XdfObject | None:
    """Собрать объект из блока XDF; ``None`` — если это метаданные без адреса."""
    title = _field(block, "title")
    if not title:
        return None
    kind = "table" if block.startswith("<XDFTABLE") else "constant"

    if kind == "table":
        z_match = re.search(r'<XDFAXIS id="z"(.*?)</XDFAXIS>', block, re.S)
        if not z_match:
            return None
        z_body = z_match.group(1)
        embedded = _embedded(z_body)
        rows = _int(embedded.get("mmedrowcount"), 1) or 1
        columns = _int(embedded.get("mmedcolcount"), 1) or 1
        math_groups = re.findall(r'<MATH equation="([^"]*)"', z_body)
        units = _field(z_body, "units") or ""
        low = _float(_field(z_body, "min"))
        high = _float(_field(z_body, "max"))
    else:
        embedded = _embedded(block)
        rows = columns = 1
        math_groups = re.findall(r'<MATH equation="([^"]*)"', block)
        # Единицы и подписи константы берём вне блоков осей.
        without_axes = re.sub(r"<XDFAXIS\b.*?</XDFAXIS>", "", block, flags=re.S)
        units = _field(without_axes, "units") or ""
        low = _float(_field(without_axes, "min"))
        high = _float(_field(without_axes, "max"))

    address = _int(embedded.get("mmedaddress"), -1)
    if address < 0:
        return None
    equation = math_groups[-1] if math_groups else DEFAULT_VARIABLE
    fixed, aliases = _substitute_aliases(equation)
    labels, linked, text_axes = _axis_metadata(block)
    category = _field(block, "CATEGORYMEM index=\"0\" category=\"0\"") or ""
    category_match = re.search(r'<CATEGORYMEM[^>]*category="([^"]*)"', block)

    return XdfObject(
        title=title,
        kind=kind,
        address=address,
        element_size_bits=_int(embedded.get("mmedelementsizebits"), 8) or 8,
        scaler=Scaler.from_equation(fixed),
        rows=rows,
        columns=columns,
        units=units,
        description=_field(block, "description") or "",
        category=category_match.group(1) if category_match else category,
        range_low=low,
        range_high=high,
        type_flags=_int(embedded.get("mmedtypeflags"), 0),
        labels=labels,
        linked_axes=linked,
        text_axes=text_axes,
        aliased_variables=aliases,
        equation=equation,
        axes=_axis_objects(block) if kind == "table" else (),
    )


@dataclass
class Definition:
    """Разобранное определение XDF."""

    path: Path
    title: str
    base_offset: int
    region_start: int
    region_size: int
    objects: dict[str, XdfObject]
    variant: str = "116K"
    aliased: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()

    @property
    def degenerate_count(self) -> int:
        """Сколько объектов имеют нелинейную/вырожденную формулу."""
        return sum(1 for obj in self.objects.values() if not obj.scaling_usable)

    @property
    def file_size(self) -> int:
        """Размер образа, к которому применимы адреса определения."""
        return self.region_start + self.region_size

    def find(self, title: str) -> XdfObject:
        try:
            return self.objects[title]
        except KeyError as exc:  # pragma: no cover - сообщение важнее
            raise XdfError(f"в определении нет символа {title!r}") from exc

    def absolute_address(self, obj: XdfObject) -> int:
        """Адрес объекта в том образе, для которого определение сделано."""
        return self.base_offset + obj.address

    @property
    def cal_region(self) -> tuple[int, int]:
        """Область калибровки в координатах ЭТОГО определения."""
        return CAL_REGION_BY_VARIANT[self.variant]

    def objects_in_cal(self) -> list[XdfObject]:
        """Объекты, целиком лежащие внутри области калибровки (кандидаты на запись)."""
        start, size = self.cal_region
        return [
            obj
            for obj in self.objects.values()
            if self.base_offset + obj.address >= start
            and self.base_offset + obj.address + obj.byte_length <= start + size
        ]

    def objects_outside_cal(self) -> list[XdfObject]:
        """Объекты вне области калибровки — записывать их нельзя никогда."""
        inside = {obj.title for obj in self.objects_in_cal()}
        return [obj for obj in self.objects.values() if obj.title not in inside]

    def validate_addresses(self) -> list[str]:
        """Проверка, что адреса объектов вообще попадают в объявленную REGION."""
        problems: list[str] = []
        for obj in self.objects.values():
            start = self.base_offset + obj.address
            end = start + obj.byte_length
            if start < self.base_offset or end > self.base_offset + self.region_size:
                problems.append(
                    f"{obj.title}: {start:#x}..{end:#x} вне области "
                    f"{self.base_offset:#x}..{self.base_offset + self.region_size:#x}"
                )
        return problems

    def report(self) -> str:
        kinds: dict[str, int] = {}
        for obj in self.objects.values():
            kinds[obj.kind] = kinds.get(obj.kind, 0) + 1
        total_bytes = sum(obj.byte_length for obj in self.objects.values())
        inside = self.objects_in_cal()
        outside = self.objects_outside_cal()
        cal_start, cal_size = self.cal_region
        lines = [
            f"{self.path.name}",
            f"  заголовок:        {self.title}",
            f"  BASEOFFSET:       {self.base_offset:#x}",
            f"  область (REGION): {self.region_start:#x} + {self.region_size:#x} = {self.file_size:#x}",
            f"  объектов:         {len(self.objects)} (таблиц {kinds.get('table', 0)}, "
            f"констант {kinds.get('constant', 0)})",
            f"  суммарно байт:    {total_bytes:#x}",
            f"  подстановок X0…:  {len(self.aliased)}",
            f"  вырожденных формул: {self.degenerate_count}",
            f"  область калибровки: {cal_start:#x}..{cal_start + cal_size:#x} "
            f"({len(inside)} объектов внутри, {len(outside)} вне — писать нельзя)",
        ]
        if self.duplicates:
            lines.append(f"  повторов имён:    {len(self.duplicates)}")
        return "\n".join(lines)


def load_definition(path: Path | str | None = None, *, family: str | None = None, variant: str = "116K") -> Definition:
    """Загрузить определение: либо по пути, либо по семейству и варианту."""
    if path is None:
        if not family:
            raise XdfError("нужно указать path или family")
        table = XDF_116K if variant == "116K" else XDF_1024K
        if family not in table:
            raise XdfError(f"для семейства {family!r} нет определения {variant}")
        path = table[family]
    path = Path(path)
    text = path.read_text(encoding="latin1")
    header = re.search(r"<XDFHEADER>(.*?)</XDFHEADER>", text, re.S)
    header_body = header.group(1) if header else text[:4096]
    base_offset = _int(
        (re.search(r'<BASEOFFSET offset="([^"]+)"', header_body) or [None, "0"])[1], 0
    )
    region = re.search(
        r'<REGION[^>]*startaddress="([^"]+)"[^>]*size="([^"]+)"', header_body
    )
    region_start = _int(region.group(1), 0) if region else 0
    region_size = _int(region.group(2), 0) if region else 0
    title = (re.search(r"<deftitle>([^<]*)</deftitle>", header_body) or [None, path.stem])[1]

    objects: dict[str, XdfObject] = {}
    duplicates: list[str] = []
    aliased: set[str] = set()
    for block in OBJECT_PATTERN.findall(text):
        obj = parse_object(block)
        if obj is None:
            continue
        if obj.title in objects:
            duplicates.append(obj.title)
            continue
        objects[obj.title] = obj
        aliased.update(obj.aliased_variables)

    return Definition(
        path=path,
        title=title,
        base_offset=base_offset,
        region_start=region_start,
        region_size=region_size,
        objects=objects,
        variant=variant,
        aliased=tuple(sorted(aliased)),
        duplicates=tuple(duplicates),
    )


def read_raw(image: bytes, obj: XdfObject, *, base_offset: int = 0) -> list[int]:
    """Прочитать сырые значения объекта (big-endian, как в стоке MS45)."""
    size = obj.element_size_bytes
    values: list[int] = []
    for index in range(obj.element_count):
        start = base_offset + obj.address + index * size
        chunk = image[start : start + size]
        if len(chunk) != size:
            raise XdfError(f"{obj.title}: данные обрезаны по {start:#x}")
        values.append(int.from_bytes(chunk, "big"))
    return values


def read_engineering(image: bytes, obj: XdfObject, *, base_offset: int = 0) -> list[float]:
    """Прочитать объект в инженерных единицах по формуле ``MATH``."""
    return [obj.to_engineering(raw) for raw in read_raw(image, obj, base_offset=base_offset)]


def writable_objects(definition: Definition) -> list[XdfObject]:
    """Объекты, которые вообще можно записывать (формула пригодна)."""
    return [obj for obj in definition.objects.values() if obj.scaling_usable]


def _stock_summary(definition: Definition, files: list[Path], titles: list[str]) -> list[str]:
    """Свод по стоковым образам: минимум/максимум сырых значений по каждому символу."""
    lines = [
        "| символ | адрес | бит | элементов | ед. | сырой мин..макс | инж. мин..макс | формула |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for title in titles:
        obj = definition.find(title)
        raw_min: int | None = None
        raw_max: int | None = None
        for path in files:
            image = path.read_bytes()
            for raw in read_raw(image, obj):
                raw_min = raw if raw_min is None else min(raw_min, raw)
                raw_max = raw if raw_max is None else max(raw_max, raw)
        if not obj.scaling_usable:
            lines.append(
                f"| {title} | {obj.address:#x} | {obj.element_size_bits} | {obj.element_count} "
                f"| {obj.units or '-'} | нет данных | — | {obj.equation} (нелинейна) |"
            )
            continue
        if raw_min is None:
            lines.append(f"| {title} | {obj.address:#x} | — | — | — | нет данных | — | {obj.equation} |")
            continue
        lines.append(
            "| {t} | {a:#x} | {bits} | {n} | {u} | {lo}..{hi} | {elo:.3f}..{ehi:.3f} | {eq} |".format(
                t=title,
                a=obj.address,
                bits=obj.element_size_bits,
                n=obj.element_count,
                u=obj.units or "-",
                lo=raw_min,
                hi=raw_max,
                elo=obj.to_engineering(raw_min),
                ehi=obj.to_engineering(raw_max),
                eq=obj.equation,
            )
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()

    parser = argparse.ArgumentParser(description="Разбор и проверка определений MS45 (XDF)")
    parser.add_argument("--family", choices=sorted(XDF_116K), help="семейство прошивки")
    parser.add_argument("--variant", choices=("116K", "1024K"), default="116K")
    parser.add_argument("--xdf", help="путь к определению (вместо --family)")
    parser.add_argument("--symbol", help="показать объект по имени")
    parser.add_argument("--stock", nargs="*", help="каталоги/файлы стока для свода")
    parser.add_argument("--symbols", nargs="*", help="символы для свода по стоку")
    parser.add_argument("--check", action="store_true", help="проверить адреса всех объектов")
    args = parser.parse_args(argv)

    definition = load_definition(args.xdf, family=args.family, variant=args.variant)
    print(definition.report())

    if args.symbol:
        obj = definition.find(args.symbol)
        for key, value in obj.as_dict().items():
            print(f"  {key}: {value}")
        return 0

    if args.check:
        problems = definition.validate_addresses()
        print(f"  проверка адресов: {'ok' if not problems else f'{len(problems)} проблем'}")
        for problem in problems[:10]:
            print(f"    {problem}")
        return 1 if problems else 0

    if args.stock and args.symbols:
        files: list[Path] = []
        for item in args.stock:
            path = Path(item)
            files.extend(sorted(path.rglob("*.bin")) if path.is_dir() else [path])
        files = [f for f in files if f.stat().st_size in (CAL_SIZE, FULL_SIZE)]
        print()
        print("\n".join(_stock_summary(definition, files, args.symbols)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
