#!/usr/bin/env python3
"""Чтение определений TunerPro XDF для MS43 (430069 и другие версии).

Диалект XDF тот же, что у MS41/MS42 (MS4X генерирует все семейства одним
инструментом), поэтому правила чтения такие же, а файл — свой: изменение в
MS42 не должно молча менять поведение MS43.

Что важно для MS43:

1. ``BASEOFFSET`` 512K-определения — ``0x70000``: адреса объектов отсчитываются
   от начала калибровочной области, файловый адрес = адрес + 0x70000. В 64K-чтении
   калибровки ``BASEOFFSET 0`` (адреса уже файловые, 0x0000..0xFFFF).
2. 16-битные элементы хранятся младшим байтом вперёд (``mmedtypeflags="0x02"``);
   проверено на стоках: только такое чтение даёт монотонные оси и физически
   осмысленные значения (см. `MS43/docs/XDF_RU.md`).
3. Оси с ``mmedaddress`` — это ссылки в бинарь, а не подписи: их нельзя читать
   как значения. Оси с ``<LABEL>`` — статические сетки.
4. **20 констант** в определениях 430069 (и во всех остальных версиях MS43)
   используют в уравнении имена переменных ``X0``/``X00``/``X000``/``X0000``
   вместо ``X``. По формату XDF переменная без атрибутов (``<VAR id="X00" />``)
   означает «сырое значение этого же объекта», то есть уравнение читается как
   ``0.1*X``; это подтверждают объявленные диапазоны (``0.1*raw`` для u8/u16) и
   проверка 360 значений на 18 стоках (ни одного выхода за диапазон). Загрузчик
   подставляет ``X`` и **перечисляет подстановки** в ``aliased_variables`` —
   молча «исправлять» дефект определения нельзя.
"""

from __future__ import annotations

import argparse
import ast
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from MS43.tools.console import prepare_stdout
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    from console import prepare_stdout  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = ROOT / "definitions"
#: Полное определение: адреса калибровки + ``BASEOFFSET 0x70000``.
XDF_512K = DEFINITIONS / "Siemens_MS43_430069_Extended_Load_512K.xdf"
#: Определение для 64 КиБ чтения калибровки: ``BASEOFFSET 0``.
XDF_64K = DEFINITIONS / "Siemens_MS43_430069_Extended_Load_64K.xdf"

#: Смещение калибровочной области в полном 512 КиБ образе MS43.
CALIBRATION_BASE = 0x70000
#: Размер калибровочной области.
CALIBRATION_SIZE = 0x10000

#: Бит ``mmedtypeflags``, отмечающий хранение младшим байтом вперёд.
TYPE_FLAG_LSB_FIRST = 0x02

#: Имя переменной, которой TunerPro по умолчанию связывает с сырым значением.
DEFAULT_VARIABLE = "X"
#: Дефект определений MS4X: ``X`` с «забитыми нулями» хвостами.
ALIASED_VARIABLE_RE = re.compile(r"^X0*$")

OBJECT_PATTERN = re.compile(
    r"<XDFCONSTANT\b.*?</XDFCONSTANT>|<XDFTABLE\b.*?</XDFTABLE>", re.S
)


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
    """Вычислить узел AST при значении переменной ``x``."""
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
    """Заменить ``X0``/``X00``/... на ``X`` и вернуть список подстановок.

    Переменная без атрибутов в ``<VAR>`` — это сырое значение самого объекта,
    поэтому подстановка сохраняет смысл уравнения (проверено диапазонами).
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", equation)
    aliases = tuple(sorted({t for t in tokens if t != DEFAULT_VARIABLE and ALIASED_VARIABLE_RE.match(t)}))
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
    def from_equation(cls, equation: str) -> "Scaler":
        factor, offset = _to_linear(equation)
        return cls(factor, offset)

    def to_engineering(self, raw: float) -> float:
        return self.a * raw + self.b

    def to_raw(self, value: float) -> float:
        if self.a == 0:
            raise XdfError("вырожденный масштаб с нулевым множителем")
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
    units: str = ""
    description: str = ""
    range_low: float | None = None
    range_high: float | None = None
    #: Статические сетки осей из ``<LABEL>`` (это подписи, а не данные).
    labels: dict[str, tuple[float, ...]] = field(default_factory=dict)
    #: Оси со своим ``mmedaddress``: ссылки в бинарь, читать их нельзя.
    linked_axes: tuple[str, ...] = ()
    #: Оси со смешанными (нечисловыми) метками.
    text_axes: tuple[str, ...] = ()
    #: Имена переменных, подставленные как ``X`` (дефект определения MS4X).
    aliased_variables: tuple[str, ...] = ()
    #: Уравнение ровно в том виде, как оно записано в определении.
    equation: str = ""

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

    def axis_labels(self, axis: str) -> tuple[float, ...]:
        return self.labels.get(axis, ())

    def to_engineering(self, raw: float) -> float:
        return self.scaler.to_engineering(raw)

    def to_raw(self, value: float) -> float:
        return self.scaler.to_raw(value)

    def byte_offsets(self, index: int = 0) -> range:
        if not 0 <= index < self.element_count:
            raise XdfError(f"{self.title}: индекс {index} вне 0..{self.element_count - 1}")
        step = self.element_size_bytes
        start = self.address + index * step
        return range(start, start + step)


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
    """Разобрать оси таблицы: статические сетки, ссылки в бинарь, текстовые метки."""
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


def parse_object(block: str) -> XdfObject | None:
    """Собрать объект из блока XDF; ``None`` — если это не данные (нет адреса)."""
    title = _field(block, "title")
    if not title:
        return None
    kind = "table" if block.startswith("<XDFTABLE") else "constant"
    head = _embedded(block)
    if kind == "constant" and not head.get("mmedaddress"):
        # Объекты-подписи без своего адреса: это метаданные, не калибровка.
        return None
    address = _int(head.get("mmedaddress"), -1)
    bits = _int(head.get("mmedelementsizebits"), 8)
    flags = _int(head.get("mmedtypeflags"), 0)
    lsb_first = bool(flags & TYPE_FLAG_LSB_FIRST) or bits == 16

    rows = _int(head.get("mmedrowcount"), 1) or 1
    columns = _int(head.get("mmedcolcount"), 1) or 1
    data_address = address
    if kind == "table":
        match = re.search(r'<XDFAXIS id="z">(.*?)</XDFAXIS>', block, re.S)
        if not match:
            raise XdfError(f"{title}: у таблицы нет оси z")
        z_body = match.group(1)
        z_embedded = _embedded(z_body)
        if not z_embedded.get("mmedaddress"):
            raise XdfError(f"{title}: у оси z нет адреса")
        data_address = _int(z_embedded.get("mmedaddress"), -1)
        if data_address < 0:
            raise XdfError(f"{title}: у оси z нет адреса")
        rows = _int(z_embedded.get("mmedrowcount"), rows) or 1
        columns = _int(z_embedded.get("mmedcolcount"), columns) or 1
        bits = _int(z_embedded.get("mmedelementsizebits"), bits)
        z_flags = _int(z_embedded.get("mmedtypeflags"), flags)
        lsb_first = bool(z_flags & TYPE_FLAG_LSB_FIRST) or bits == 16
        stride = _int(z_embedded.get("mmedmajorstridebits"), 0)
        if stride != 0:
            raise XdfError(f"{title}: строки таблицы идут с шагом {stride} бит — не поддержано")
        equation_match = re.search(r'<MATH equation="([^"]*)"', z_body)
    else:
        equation_match = re.search(r'<MATH equation="([^"]*)"', block)
    equation = equation_match.group(1) if equation_match else DEFAULT_VARIABLE

    fixed_equation, aliases = _substitute_aliases(equation)
    scaler = Scaler.from_equation(fixed_equation)
    labels, linked, text_axes = _axis_metadata(block)
    return XdfObject(
        title=title,
        kind=kind,
        address=data_address,
        element_size_bits=bits,
        lsb_first=lsb_first,
        scaler=scaler,
        rows=rows,
        columns=columns,
        units=_field(block, "units") or "",
        description=(_field(block, "description") or "").replace("&#013;&#010;", " | "),
        range_low=_float(_field(block, "rangelow")),
        range_high=_float(_field(block, "rangehigh")),
        labels=labels,
        linked_axes=linked,
        text_axes=text_axes,
        aliased_variables=aliases,
        equation=_clean(equation),
    )


@dataclass
class Definition:
    """Разобранное определение: объекты, база адресов и найденные дефекты."""

    path: Path
    title: str
    base_offset: int
    region_size: int
    categories: dict[int, str] = field(default_factory=dict)
    objects: dict[str, list[XdfObject]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def object_count(self) -> int:
        return sum(len(items) for items in self.objects.values())

    def symbols(self) -> list[str]:
        return sorted(self.objects)

    def get(self, title: str, index: int = 0) -> XdfObject:
        matches = self.objects.get(title)
        if not matches:
            raise XdfError(f"{title!r} нет в определении {self.path.name}")
        if index >= len(matches):
            raise XdfError(f"{title!r}: есть только {len(matches)} определение(й)")
        return matches[index]

    def aliased(self) -> list[XdfObject]:
        """Объекты с подставленными именами переменных (дефект MS4X)."""
        return [
            obj
            for items in self.objects.values()
            for obj in items
            if obj.aliased_variables
        ]

    def file_offsets(
        self, obj: XdfObject, index: int = 0, base_offset: int | None = None
    ) -> range:
        """Смещения объекта в файле.

        ``base_offset`` переопределяет ``BASEOFFSET`` определения: так читается
        64 КиБ чтение калибровки определением 512 КиБ (адрес объекта в
        определении относительный, у чтения калибровки база — ноль).  ``None`` —
        обычное поведение, базовое смещение берётся из определения.
        """
        base = (
            self.base_offset if base_offset is None else base_offset
        ) + obj.address + index * obj.element_size_bytes
        return range(base, base + obj.element_size_bytes)

    def file_offset(self, obj: XdfObject, index: int = 0, base_offset: int | None = None) -> int:
        return self.file_offsets(obj, index, base_offset).start

    def read_raw(
        self, image: bytes, obj: XdfObject, index: int = 0, base_offset: int | None = None
    ) -> int:
        chunk = b"".join(
            image[offset : offset + 1] for offset in self.file_offsets(obj, index, base_offset)
        )
        if len(chunk) != obj.element_size_bytes:
            raise XdfError(f"{obj.title}: чтение за концом образа (индекс {index})")
        return int.from_bytes(chunk, "little" if obj.lsb_first else "big")

    def read_engineering(
        self, image: bytes, obj: XdfObject, base_offset: int | None = None
    ) -> list[float]:
        return [
            obj.to_engineering(self.read_raw(image, obj, i, base_offset))
            for i in range(obj.element_count)
        ]


def load_definition(path: Path | str = XDF_512K) -> Definition:
    """Прочитать определение MS43.

    Объекты, которые нельзя представить точно, попадают в ``problems``, а не
    молча пропускаются: неизвестное уравнение не должно позже выглядеть как
    валидный записываемый символ. Подстановки ``X0…`` -> ``X`` записываются в
    ``obj.aliased_variables`` (см. `MS43/docs/XDF_RU.md`).
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    header = re.search(r"<XDFHEADER>(.*?)</XDFHEADER>", text, re.S)
    header_body = header.group(1) if header else ""
    base = _int(re.search(r'<BASEOFFSET offset="([^"]+)"', header_body).group(1), 0) if re.search(
        r'<BASEOFFSET offset="([^"]+)"', header_body
    ) else 0
    region = re.search(r'<REGION[^>]*size="([^"]+)"', header_body)
    definition = Definition(
        path=path,
        title=_field(header_body, "deftitle") or path.stem,
        base_offset=base,
        region_size=_int(region.group(1), 0) if region else 0,
        categories={
            int(index, 0): name
            for index, name in re.findall(
                r'<CATEGORY index="([^"]+)" name="([^"]*)"', header_body
            )
        },
    )
    for block in OBJECT_PATTERN.findall(text):
        try:
            obj = parse_object(block)
        except XdfError as exc:
            definition.problems.append(str(exc))
            continue
        if obj:
            definition.objects.setdefault(obj.title, []).append(obj)
    return definition


def _report(definition: Definition) -> int:
    print(f"{definition.path.name}: {definition.title}, база адресов 0x{definition.base_offset:X}, "
          f"регион 0x{definition.region_size:X}")
    print(f"объектов: {definition.object_count} (уникальных имён: {len(definition.objects)})")
    print(f"проблемных объектов: {len(definition.problems)}")
    for problem in definition.problems[:10]:
        print("   проблема:", problem)
    aliased = definition.aliased()
    print(f"объектов с подстановкой переменной X0… -> X: {len(aliased)}")
    for obj in aliased:
        print(
            f"   {obj.title:32s} @0x{obj.address:05X}  "
            f"'{'+'.join(obj.aliased_variables)}' -> X,  уравнение {obj.equation}"
        )
    return 1 if definition.problems else 0


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()  # Windows: не падать на символах вне cp1251
    parser = argparse.ArgumentParser(
        description="Проверка и разбор определений TunerPro XDF для MS43."
    )
    parser.add_argument(
        "definitions",
        nargs="*",
        default=[str(XDF_512K), str(XDF_64K)],
        help="XDF-файлы (по умолчанию оба определения 430069)",
    )
    args = parser.parse_args(argv)
    code = 0
    for path in args.definitions:
        code |= _report(load_definition(path))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
