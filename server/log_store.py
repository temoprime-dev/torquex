# -*- coding: utf-8 -*-
"""Добровольная передача лога проекту: очистка и хранение (docs/LOG_DONATION_RU.md).

Обычный ``POST /logs`` ничего не хранит. Этот модуль работает только когда
пользователь отдельно отметил «передать очищенную копию» (``share_consent``),
а владелец включил передачу на сервере (``TORQUEX_LOG_DONATION=on``).

Принципы:

* **Allowlist, не blocklist.** Сохраняются только ось времени и числовые
  каналы, имена которых известны анализатору (``docs/pack_log_requirements.json``)
  и каталогу Test0 (``docs/test0_channels.json``). Всё остальное — колонки и
  результаты job-ов с VIN/FGNR/ISN, комментарии, пути, имена файлов, строки —
  не попадает в копию просто потому, что его нет в списке.
* **Пересборка, а не правка.** Лог читается тем же разборщиком, что и для
  анализа (``tools/pack_log_analyzer.read_log_table``: CSV или ``.db`` Test0
  только для чтения и только таблицы ``jobs``/``result_definitions``/
  ``job_executions``), и записывается заново как чистый CSV (UTF-8, LF,
  разделитель «,», точка как десятичный знак). Исходные байты не хранятся.
* **Время — от нуля.** Абсолютная отметка времени записи тоже говорит о
  человеке (когда и сколько он ездил), поэтому ось сдвигается к первой точке.
* **Самопроверка после очистки** (``verify_scrubbed``) не доверяет коду очистки:
  заголовок только из allowlist, ячейки только числа ограниченной длины, нет
  подстрок, похожих на VIN. Любое нарушение — ``LOG_SCRUB_FAILED``, запись
  отменяется.
* **Хранилище** — каталог вне ``web/``; имена файлов генерирует сервер
  (``secrets.token_urlsafe``), пользовательские строки в пути не попадают.
  Рядом лежит ``<id>.meta.json`` без IP, VIN, имени файла и без токена удаления
  (только его sha256).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Версия текста согласия: меняется при изменении того, что и зачем храним.
#: Пишется в метаданные каждой копии, чтобы знать, на что соглашался человек.
CONSENT_VERSION = "2026-10-08/1"
DEFAULT_RETENTION_DAYS = 365
#: Предел одной очищенной копии (после очистки).
MAX_STORED_LOG_BYTES = 20 * 1024 * 1024
#: Общий потолок хранилища.
DEFAULT_STORE_MAX_BYTES = 2 * 1024 * 1024 * 1024
#: Сохранений на один хеш IP за сутки.
DEFAULT_SHARES_PER_IP_PER_DAY = 5

#: Имя копии — только то, что выдал ``secrets.token_urlsafe``.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
#: VIN: 17 символов латиницы/цифр без I, O, Q (ищем без учёта регистра — строже).
VIN_LIKE = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.I)
#: Ячейка данных: только ASCII-цифры, не больше 9 знаков целой части и 4 дробной.
#: Так ни одно число не может сложиться в 17-символьную «VIN-подобную» строку.
_CELL_RE = re.compile(r"^-?[0-9]{1,9}(?:\.[0-9]{1,4})?$")
#: |значение| ≥ этого — не физическая величина двигателя: ячейка пустеет.
_MAX_ABS_VALUE = 1e9
_KNOCK_CYLINDERS = tuple(f"knock_cyl_{index}" for index in range(1, 7))


class ScrubError(ValueError):
    """Очистка или сохранение не удались: код отказа для пользователя."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ScrubResult:
    data: bytes
    rows: int
    channels: list[str]
    platform: str | None


def allowed_channels() -> frozenset[str]:
    """Имена каналов, которые могут попасть в очищенную копию."""
    from tools import pack_log_analyzer, test0_log

    spec = pack_log_analyzer.load_requirements()["channels"]
    names = {name for name in spec if name != "knock_cylinders"}
    names |= set(test0_log.channel_names())
    names |= set(_KNOCK_CYLINDERS)
    names.add("time")
    return frozenset(names)


def _format_number(value: Any) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    number = float(value)
    if not math.isfinite(number) or abs(number) >= _MAX_ABS_VALUE:
        return ""
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def scrub_table(table: dict, max_bytes: int = MAX_STORED_LOG_BYTES) -> tuple[bytes, int, list[str]]:
    """Таблица анализатора → чистый CSV: только allowlist и только числа.

    Возвращает ``(байты, число строк, каналы)``. Каналы без единого значения
    не пишутся. Размер считается по ходу записи, чтобы огромный вывод не
    собирался в памяти целиком.
    """
    allowed = allowed_channels()
    columns = table.get("channels") or {}
    times = list(columns.get("time") or [])
    if len(times) < 2:
        raise ScrubError("LOG_SCRUB_FAILED", "в логе нет оси времени")
    numeric_times = [float(t) for t in times
                     if isinstance(t, (int, float)) and not isinstance(t, bool)
                     and math.isfinite(float(t))]
    if not numeric_times:
        raise ScrubError("LOG_SCRUB_FAILED", "ось времени не числовая")
    origin = numeric_times[0]

    names = sorted(
        name for name, values in columns.items()
        if name != "time" and name in allowed and isinstance(values, list)
        and len(values) == len(times)
        and any(_format_number(value) for value in values)
    )
    header = ["time"] + names
    lines = [",".join(header)]
    size = len(lines[0]) + 1
    rows = 0
    for index, moment in enumerate(times):
        stamp = _format_number(moment - origin) if isinstance(moment, (int, float)) else ""
        if not stamp:
            continue  # строка без времени ничего не значит
        cells = [stamp] + [_format_number(columns[name][index]) for name in names]
        line = ",".join(cells)
        size += len(line) + 1
        if size > max_bytes:
            raise ScrubError("LOG_SHARE_TOO_LARGE",
                             f"очищенная копия больше {max_bytes} байт")
        lines.append(line)
        rows += 1
    data = ("\n".join(lines) + "\n").encode("ascii")
    return data, rows, header


def verify_scrubbed(data: bytes, max_bytes: int = MAX_STORED_LOG_BYTES) -> list[str]:
    """Независимая самопроверка очищенной копии. Пустой список — всё чисто."""
    problems: list[str] = []
    if len(data) > max_bytes:
        problems.append("копия больше предела")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return problems + ["в копии есть не-ASCII символы"]
    if "\r" in text or "\x00" in text:
        problems.append("в копии CR или NUL")
    if not text.endswith("\n"):
        problems.append("копия не заканчивается переводом строки")
    if VIN_LIKE.search(text):
        problems.append("найдена подстрока, похожая на VIN")
    if "@" in text:
        problems.append("найден символ @ (похоже на e-mail)")
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    if len(lines) < 3:
        problems.append("в копии меньше двух строк данных")
        return problems
    header = lines[0].split(",")
    allowed = allowed_channels()
    if header[0] != "time":
        problems.append("первая колонка не time")
    if len(set(header)) != len(header):
        problems.append("повторяющиеся колонки")
    unknown = [name for name in header if name not in allowed]
    if unknown:
        problems.append(f"колонки вне allowlist: {len(unknown)}")
    for number, line in enumerate(lines[1:], start=2):
        cells = line.split(",")
        if len(cells) != len(header):
            problems.append(f"строка {number}: число колонок не совпадает")
            break
        if not _CELL_RE.match(cells[0]):
            problems.append(f"строка {number}: время не число")
            break
        bad = [cell for cell in cells[1:] if cell and not _CELL_RE.match(cell)]
        if bad:
            problems.append(f"строка {number}: нечисловое значение")
            break
    return problems


def scrub_log_file(path: str | Path, max_bytes: int = MAX_STORED_LOG_BYTES) -> ScrubResult:
    """Прочитать лог (CSV или ``.db`` Test0) и вернуть проверенную чистую копию."""
    from tools import pack_log_analyzer

    try:
        table = pack_log_analyzer.read_log_table(Path(path))
    except Exception as error:  # разбор уже прошёл в анализе; сюда — только сбой
        raise ScrubError("LOG_SCRUB_FAILED", f"лог не перечитан: {type(error).__name__}") from error
    data, rows, channels = scrub_table(table, max_bytes=max_bytes)
    problems = verify_scrubbed(data, max_bytes=max_bytes)
    if problems:
        raise ScrubError("LOG_SCRUB_FAILED", "; ".join(problems))
    platform = pack_log_analyzer.detect_ecu(table)
    return ScrubResult(data=data, rows=rows, channels=channels, platform=platform)


def default_store_path() -> Path:
    return ROOT / "data" / "logs"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} не может быть отрицательным")
    return value


def _today() -> date:
    return datetime.now(timezone.utc).date()


class LogStore:
    """Каталог очищенных копий: ``<id>.csv`` + ``<id>.meta.json``."""

    def __init__(
        self,
        root: str | Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_total_bytes: int = DEFAULT_STORE_MAX_BYTES,
        max_file_bytes: int = MAX_STORED_LOG_BYTES,
    ) -> None:
        self.root = Path(root)
        resolved = self.root.resolve()
        web = (ROOT / "web").resolve()
        if resolved == web or web in resolved.parents:
            raise ValueError("хранилище логов не может лежать внутри web/ (отдаётся наружу)")
        self.retention_days = int(retention_days)
        self.max_total_bytes = int(max_total_bytes)
        self.max_file_bytes = int(max_file_bytes)

    @classmethod
    def from_env(cls) -> "LogStore":
        root = os.environ.get("TORQUEX_LOG_STORE") or str(default_store_path())
        return cls(
            root,
            retention_days=_env_int("TORQUEX_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
            max_total_bytes=_env_int("TORQUEX_LOG_STORE_MAX_BYTES", DEFAULT_STORE_MAX_BYTES),
            max_file_bytes=_env_int("TORQUEX_LOG_MAX_STORED_BYTES", MAX_STORED_LOG_BYTES),
        )

    # -- пути: только свои имена ---------------------------------------------
    def _paths(self, log_id: str) -> tuple[Path, Path] | None:
        if not isinstance(log_id, str) or not _ID_RE.match(log_id):
            return None
        csv_path = self.root / f"{log_id}.csv"
        meta_path = self.root / f"{log_id}.meta.json"
        root = self.root.resolve()
        for candidate in (csv_path, meta_path):
            if candidate.resolve().parent != root:
                return None
        return csv_path, meta_path

    def total_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        total = 0
        for entry in self.root.iterdir():
            if entry.is_file() and entry.name.endswith((".csv", ".meta.json")):
                total += entry.stat().st_size
        return total

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        """Атомарная запись с правами 0600 (на Windows права игнорируются)."""
        temp = path.with_name("." + path.name + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise

    def save(self, data: bytes, *, package: str, platform: str | None, rows: int,
             channels: list[str]) -> tuple[str, str, dict[str, Any]]:
        """Сохранить очищенную копию. Возвращает ``(id, токен удаления, meta)``."""
        if len(data) > self.max_file_bytes:
            raise ScrubError("LOG_SHARE_TOO_LARGE",
                             f"очищенная копия больше {self.max_file_bytes} байт")
        if self.total_bytes() + len(data) + 1024 > self.max_total_bytes:
            raise ScrubError("LOG_STORE_FULL", "хранилище логов заполнено")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for _attempt in range(5):
            log_id = secrets.token_urlsafe(18)
            paths = self._paths(log_id)
            if paths is not None and not paths[0].exists() and not paths[1].exists():
                break
        else:  # pragma: no cover - 144 бита случайности
            raise ScrubError("LOG_STORE_FULL", "не удалось выбрать имя файла")
        csv_path, meta_path = paths
        token = secrets.token_urlsafe(24)
        created = _today()
        meta = {
            "schema": 1,
            "id": log_id,
            "created": created.isoformat(),
            "expires": (created + timedelta(days=self.retention_days)).isoformat(),
            "platform": platform,
            "version": None,
            "package": package,
            "channels": list(channels),
            "size_bytes": len(data),
            "rows": int(rows),
            "sha256": hashlib.sha256(data).hexdigest(),
            "consent_version": CONSENT_VERSION,
            "delete_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        }
        self._write_private(csv_path, data)
        try:
            self._write_private(
                meta_path, (json.dumps(meta, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        except BaseException:
            csv_path.unlink(missing_ok=True)
            raise
        return log_id, token, meta

    def delete(self, log_id: str, token: str) -> bool:
        """Стереть копию по токену. ``False`` — нет такой или токен неверный."""
        given = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        paths = self._paths(log_id)
        expected = "0" * 64
        meta_path = csv_path = None
        if paths is not None:
            csv_path, meta_path = paths
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                expected = str(meta.get("delete_token_sha256") or expected)
            except (OSError, ValueError):
                paths = None
        # Сравнение выполняется всегда, чтобы «нет» и «неверно» не различались по времени.
        matches = secrets.compare_digest(given.encode("ascii"), expected.encode("ascii", "replace"))
        if paths is None or not token or not matches:
            return False
        csv_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        return True

    def purge_expired(self, today: date | None = None, *, dry_run: bool = False) -> int:
        """Удалить копии с истёкшим сроком; вернуть число удалённых копий.

        Срок — меньшее из ``expires`` в метаданных и ``created + retention_days``
        по текущей настройке: если владелец сократил срок, он применяется и к
        старым копиям. Копия без читаемых метаданных (обрыв записи) удаляется:
        без них нельзя доказать согласие. Чужие файлы не трогаются.
        """
        if not self.root.is_dir():
            return 0
        today = today or _today()
        removed = 0
        ids: set[str] = set()
        for entry in self.root.iterdir():
            name = entry.name
            if name.startswith(".") and name.endswith(".tmp"):
                stem = name[1:-4]
                if stem.endswith((".csv", ".meta.json")) and entry.is_file() and not dry_run:
                    entry.unlink(missing_ok=True)  # недописанная запись
                continue
            if name.endswith(".meta.json"):
                ids.add(name[: -len(".meta.json")])
            elif name.endswith(".csv"):
                ids.add(name[: -len(".csv")])
        for log_id in sorted(ids):
            paths = self._paths(log_id)
            if paths is None:
                continue
            csv_path, meta_path = paths
            expired = False
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = date.fromisoformat(str(meta["created"]))
                expires = date.fromisoformat(str(meta["expires"]))
                limit = min(expires, created + timedelta(days=self.retention_days))
                expired = today >= limit
            except (OSError, ValueError, KeyError, TypeError):
                expired = True
            if expired:
                removed += 1
                if dry_run:
                    continue
                csv_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
        return removed
