# -*- coding: utf-8 -*-
"""Ядро серверной обёртки TorqueX (задание `docs/TASK_FINISH_RU.md` §5).

Здесь вся логика, независимая от веб-фреймворка: определение ЭБУ, сборка,
учёт по VIN и учётной записи, журнал, короткое хранение результатов. HTTP-слой
(`server/app.py`) только вызывает эти функции — так ядро проверяется тестами без
FastAPI и не тянет зависимости.

Правила, зашитые в код:

* **Никаких файлов клиента на диске надолго.** Загрузка живёт во временном файле
  только на время обработки; результаты — в памяти до `TTL` (по умолчанию
  15 минут), дольше не хранятся. В журнале — только хеши и размеры.
  Единственное исключение — добровольная передача лога (`share_consent=true`
  плюс `TORQUEX_LOG_DONATION=on`): тогда хранится ОЧИЩЕННАЯ копия, см.
  `server/log_store.py` и `docs/LOG_DONATION_RU.md`.
* **VIN не логируется в открытом виде.** Хранится `sha256(vin)` и последние
  7 символов — этого достаточно, чтобы клиент узнал свою машину, и мало, чтобы
  утечка журнала раскрыла VIN.
* **Никаких внешних сетевых обращений** и никакого выполнения чужого кода: только
  импорт наших же инструментов и работа с байтами в памяти.
* **Лимиты** считаются на VIN и на учётную запись за период; при превышении —
  отказ с понятным текстом, а не «тихое» урезание.
* Пути не собираются из пользовательских данных: имена файлов используются
  только как подпись в наряде, временные файлы создаёт наш код.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import feedback_store as feedback_module  # noqa: E402
from server import log_store as log_store_module  # noqa: E402
from server import notify as notify_module  # noqa: E402
from tools import cal_state  # noqa: E402
from tools import gs8_state  # noqa: E402
from tools import refusals  # noqa: E402
from tools import torquex  # noqa: E402
from MS4x.tools import stages as common_stages  # noqa: E402

#: Максимальный размер загрузки: 1 МиБ (полное чтение MS45 — ровно 1 МиБ).
MAX_UPLOAD_BYTES = 1024 * 1024
#: Логи используют тот же жёсткий лимит; сервер никогда не принимает путь к
#: клиентскому файлу вместо байтов.
MAX_LOG_BYTES = MAX_UPLOAD_BYTES
LOG_PACKAGES = frozenset({"ignition", "fuel", "vanos"})

#: Сколько живут результаты в памяти (можно переопределить переменной окружения).
DEFAULT_JOB_TTL_SECONDS = 900

#: Версия генератора для журнала: задаётся окружением, иначе честное «dev».
GENERATOR_VERSION = os.environ.get("TORQUEX_GENERATOR_VERSION") or "dev"


class ServiceError(ValueError):
    """Ошибка сервиса с кодом человекочитаемого отказа."""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Limits:
    """Лимиты на периоды (значения по умолчанию — стартовые, меняются окружением)."""

    vin_per_day: int = 5
    account_per_day: int = 20
    account_per_hour: int = 10

    @classmethod
    def from_env(cls) -> "Limits":
        def number(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = int(raw)
            except ValueError as error:  # pragma: no cover - защита от опечаток
                raise ServiceError("REQUEST_INVALID", f"{name}={raw!r} не число") from error
            if value < 0:
                raise ServiceError("REQUEST_INVALID", f"{name} не может быть отрицательным")
            return value

        return cls(
            vin_per_day=number("TORQUEX_LIMIT_VIN_PER_DAY", 5),
            account_per_day=number("TORQUEX_LIMIT_ACCOUNT_PER_DAY", 20),
            account_per_hour=number("TORQUEX_LIMIT_ACCOUNT_PER_HOUR", 10),
        )


@dataclass
class JournalEntry:
    """Строка журнала: ничего, что нельзя показывать в случае утечки."""

    time: str
    account_hash: str
    vin_hash: str
    vin_tail: str
    ip_hash: str
    stock_sha256: str
    result_sha256: str
    generator_version: str
    status: str
    code: str
    platform: str
    version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "time": self.time,
            "account_hash": self.account_hash,
            "vin_hash": self.vin_hash,
            "vin_tail": self.vin_tail,
            "ip_hash": self.ip_hash,
            "stock_sha256": self.stock_sha256,
            "result_sha256": self.result_sha256,
            "generator_version": self.generator_version,
            "status": self.status,
            "code": self.code,
            "platform": self.platform,
            "version": self.version,
        }


@dataclass
class Job:
    """Результат сборки в памяти: файл, наряд, манифест, время жизни."""

    job_id: str
    created_at: float
    ttl_seconds: int
    status: str
    platform: str
    version: str
    image: bytes | None
    note: str
    manifest: dict[str, Any]
    log: str
    donate_url: str
    ttl_remaining: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return ((now if now is not None else time.monotonic()) - self.created_at) > self.ttl_seconds

    def as_dict(self, with_image: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "status": self.status,
            "platform": self.platform,
            "version": self.version,
            "note": self.note,
            "manifest": self.manifest,
            "donate_url": self.donate_url,
            "ttl_seconds": self.ttl_seconds,
            "image_sha256": (
                hashlib.sha256(self.image).hexdigest() if self.image is not None else None
            ),
            "image_size": len(self.image) if self.image is not None else 0,
        }
        if with_image and self.image is not None:
            import base64

            payload["image_base64"] = base64.b64encode(self.image).decode("ascii")
        return payload



#: ключи заявки, которые генератор трактует как путь к файлу на диске.
#: В веб-запросе путь выбирает клиент, поэтому принять его значит дать чтение
#: произвольных файлов сервера (проверено: абсолютный путь в ``evidence`` открывался).
_PATH_KEYS = frozenset({
    "evidence", "evidence_path", "popcorn", "popcorn_file", "log", "log_path",
    "source", "source_path", "file_path",
})


def _reject_server_paths(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PATH_KEYS and isinstance(value, str):
                raise ServiceError(
                    "REQUEST_INVALID",
                    f"поле «{key}» с путём к файлу в веб-заявке не принимается "
                    "(доказательства по логам через сервер пока недоступны)",
                )
            _reject_server_paths(value)
    elif isinstance(node, list):
        for item in node:
            _reject_server_paths(item)

def hash_value(value: str, salt: str = "") -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def vin_required() -> bool:
    """Требовать ли VIN при сборке. **Заглушка (решение владельца 2026-10-08):** по
    умолчанию VIN не нужен, привязка по VIN отложена до взвешивания «за и против»
    (см. docs/VIN_BINDING_DECISION_RU.md). Включается ``TORQUEX_VIN_REQUIRED=on``."""
    return (os.environ.get("TORQUEX_VIN_REQUIRED") or "off").strip().lower() in {"on", "1", "true", "yes"}


_VIN_CHARS = frozenset("ABCDEFGHJKLMNPRSTUVWXYZ0123456789")  # без I, O, Q (ISO 3779)


def normalise_vin(raw: Any, *, required: bool | None = None) -> tuple[str, str]:
    """VIN → (хеш, последние 7 символов).

    При обязательном VIN (``TORQUEX_VIN_REQUIRED=on``) нужно ровно 17 символов ISO 3779 (без I, O, Q);
    контрольную цифру не проверяем (у европейских BMW она не обязана сходиться).
    При ``required=False`` пустой VIN разрешён, а непустой проверяется мягко, как раньше.
    """
    if required is None:
        required = vin_required()
    vin = str(raw or "").strip().upper().replace(" ", "")
    if not vin:
        if required:
            raise ServiceError("VIN_REQUIRED", "укажите VIN автомобиля (17 символов)", status=422)
        return "", ""
    if required:
        if len(vin) != 17 or not all(ch in _VIN_CHARS for ch in vin):
            raise ServiceError("VIN_INVALID", "VIN должен состоять из 17 латинских букв и цифр (без I, O, Q)", status=422)
        return hash_value(vin), vin[-7:]
    if len(vin) > 32:
        raise ServiceError("VIN_INVALID", "VIN длиннее 32 символов — это не VIN")
    if not all(ch.isalnum() or ch in "-_" for ch in vin):
        raise ServiceError("VIN_INVALID", "в VIN допустимы только буквы, цифры, '-' и '_'")
    tail = vin[-7:] if len(vin) >= 7 else vin
    return hash_value(vin), tail


class TorquexService:
    """Сервис: сборка + учёт + журнал + короткое хранение результатов."""

    def __init__(
        self,
        *,
        limits: Limits | None = None,
        ip_salt: str | None = None,
        ttl_seconds: int | None = None,
        journal_path: Path | None = None,
        donate_url: str = "",
        log_store: "log_store_module.LogStore | None" = None,
        log_donation: bool | None = None,
        log_shares_per_ip_per_day: int | None = None,
    ) -> None:
        self.limits = limits or Limits.from_env()
        self.ip_salt = ip_salt or os.environ.get("TORQUEX_IP_SALT") or secrets.token_hex(8)
        self.ttl_seconds = (
            ttl_seconds
            if ttl_seconds is not None
            else int(os.environ.get("TORQUEX_JOB_TTL_SECONDS") or DEFAULT_JOB_TTL_SECONDS)
        )
        journal = journal_path or os.environ.get("TORQUEX_JOURNAL_PATH")
        self.journal_path = Path(journal) if journal else None
        feedback = os.environ.get("TORQUEX_FEEDBACK_PATH")
        if not feedback and self.journal_path is not None:
            feedback = str(self.journal_path.with_name("feedback.jsonl"))
        self.notifier = notify_module.TelegramNotifier()
        self.feedback = (
            feedback_module.FeedbackStore(feedback, on_saved=self.notifier.notify)
            if feedback else None
        )
        self.donate_url = donate_url or os.environ.get("TORQUEX_DONATE_URL") or ""
        self._journal: list[JournalEntry] = []
        self._jobs: dict[str, Job] = {}
        # Short-lived raw log bytes: only the opaque id crosses the API.  The
        # log is never journalled, associated with VIN, or written under a
        # client-provided path.
        self._logs: dict[str, dict[str, Any]] = {}
        # Добровольная передача лога проекту (docs/LOG_DONATION_RU.md): по
        # умолчанию выключена; включает владелец TORQUEX_LOG_DONATION=on.
        self.log_donation = (
            bool(log_donation) if log_donation is not None
            else _env_flag("TORQUEX_LOG_DONATION")
        )
        self.log_store = log_store if log_store is not None else _log_store_from_env()
        self.log_shares_per_ip_per_day = (
            log_shares_per_ip_per_day if log_shares_per_ip_per_day is not None
            else int(os.environ.get("TORQUEX_LOG_SHARES_PER_IP_PER_DAY")
                     or log_store_module.DEFAULT_SHARES_PER_IP_PER_DAY)
        )
        # Автоудаление при старте — и при выключенной передаче: срок обещан
        # людям, которые уже передали лог.
        if self.log_store is not None:
            self.log_store.purge_expired()

    # -- общее -------------------------------------------------------------
    def versions(self) -> dict[str, Any]:
        """Список поддерживаемых ЭБУ/версий — то, что нужно показать клиенту."""
        from MS43.tools import policy as ms43_policy
        from MS45.tools import policy as ms45_policy

        return {
            "generator_version": GENERATOR_VERSION,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_log_bytes": MAX_LOG_BYTES,
            "bench_required": common_stages.bench_required(),
            "vin_required": vin_required(),
            "ecus": {
                "MS41": {"supported": True, "note": "калибровка 24K / полное чтение 256K"},
                "MS42": {"supported": True, "note": "OSID 0110C6, 32K/64K/512K"},
                "MS43": {"supported": True, "versions": list(ms43_policy.covered_software())},
                "GS8": {"supported": True, "systems": gs8_state.catalog_summary()},
                "MS45": {
                    "supported": True,
                    "families": list(ms45_policy.SUPPORTED_FAMILIES),
                    "families_with_definitions": list(ms45_policy.XML_FAMILIES),
                    "stock_derived_families": list(ms45_policy.STOCK_DERIVED_FAMILIES),
                },
            },
            "donate_url": self.donate_url,
        }

    # -- учёт --------------------------------------------------------------
    def _journal_add(self, entry: JournalEntry) -> None:
        self._journal.append(entry)
        if self.journal_path is not None:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")

    def journal(self, limit: int = 100) -> list[dict[str, str]]:
        return [entry.as_dict() for entry in self._journal[-limit:]]

    def _count(self, field_name: str, value: str, since: float) -> int:
        return sum(
            1 for entry in self._journal
            if getattr(entry, field_name) == value
            and _parse_time(entry.time) >= since
        )

    def _check_limits(self, account_hash: str, vin_hash: str) -> None:
        now = time.time()
        if vin_hash and self._count("vin_hash", vin_hash, now - 86400) >= self.limits.vin_per_day:
            raise ServiceError(
                "LIMIT_EXCEEDED",
                f"на этот VIN сегодня уже собрано {self.limits.vin_per_day} файлов "
                f"(лимит {self.limits.vin_per_day}/сутки)",
                status=429,
            )
        if self._count("account_hash", account_hash, now - 3600) >= self.limits.account_per_hour:
            raise ServiceError(
                "LIMIT_EXCEEDED",
                f"учётная запись исчерпала {self.limits.account_per_hour} сборок за час",
                status=429,
            )
        if self._count("account_hash", account_hash, now - 86400) >= self.limits.account_per_day:
            raise ServiceError(
                "LIMIT_EXCEEDED",
                f"учётная запись исчерпала {self.limits.account_per_day} сборок за сутки",
                status=429,
            )

    # -- работа с файлами --------------------------------------------------
    @contextlib.contextmanager
    def _temp_image(self, data: bytes) -> Iterator[Path]:
        """Временный файл только на время обработки; имя — наше, не клиента."""
        handle = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        try:
            handle.write(data)
            handle.flush()
            handle.close()
            yield Path(handle.name)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)

    def detect(self, data: bytes, filename: str = "") -> dict[str, Any]:
        """Определить ЭБУ и версию по загруженному файлу (≤ 1 МиБ)."""
        self._check_upload(data)
        with self._temp_image(data) as path:
            try:
                detection = torquex.detect(path)
            except torquex.TorquexError as error:
                raise ServiceError(error.code, str(error)) from error
        return {
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            **detection.as_dict(),
            "calibration": (
                {k: v for k, v in detection.facts["gs8"].items() if k != "size"}
                if detection.platform == "GS8"
                else cal_state.analyze(detection.platform, data) if detection.supported else None
            ),
            "options": (
                gs8_state.factory_options(detection.facts["gs8"]["system"])
                if detection.platform == "GS8" and detection.facts.get("gs8", {}).get("system") else []
            ),
            "alpina_options": (
                gs8_state.factory_options(
                    detection.facts["gs8"]["system"], source="alpina",
                    program=detection.facts["gs8"].get("program"))
                if detection.platform == "GS8" and detection.facts.get("gs8", {}).get("system") else []
            ),
            "donate_url": self.donate_url,
        }

    def submit_feedback(
        self, *, kind: str, message: str, contact: str = "", context: dict[str, Any] | None = None,
        lang: str = "ru", honeypot: str = "", ip: str = "",
    ) -> dict[str, Any]:
        """Принять короткое сообщение владельцу проекта (если задан путь хранения)."""
        if self.feedback is None:
            raise ServiceError("FEEDBACK_UNAVAILABLE", "приём сообщений на сервере не настроен", status=503)
        try:
            return self.feedback.add(
                kind=kind, message=message, contact=contact, context=context, lang=lang,
                honeypot=honeypot, ip_hash=hash_value(ip, self.ip_salt) if ip else "",
            )
        except feedback_module.FeedbackError as error:
            raise ServiceError(error.code, str(error), status=error.status) from error

    def recent_feedback(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.feedback.recent(limit) if self.feedback is not None else []

    def _gc_logs(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        for key, record in list(self._logs.items()):
            if float(record.get("created_at", 0.0)) < cutoff:
                del self._logs[key]

    def analyze_log(
        self, data: bytes, *, filename: str, package: str,
        consent: bool, fuel_ron: float | None = None,
        share_consent: bool = False, ip: str = "",
    ) -> dict[str, Any]:
        """Analyze an uploaded CSV/Test0 DB and return server-issued evidence.

        Only an opaque ``evidence_id`` is returned for later builds.  A client
        cannot submit a hand-edited JSON result or a path: the raw bytes are
        retained briefly in memory and the analyzer is rerun from those bytes
        when a build consumes the id.

        ``share_consent=True`` (отдельное от ``consent`` согласие) — после
        успешного анализа сохранить ОЧИЩЕННУЮ копию лога (см.
        ``server/log_store.py``). Любая неудача сохранения не отменяет анализ:
        ответ получает поле ``share`` с кодом отказа.
        """
        if consent is not True:
            raise ServiceError(
                "LOG_CONSENT_REQUIRED",
                "для анализа лога нужно явное согласие consent=true",
                status=422,
            )
        package = str(package or "").lower()
        if package not in LOG_PACKAGES:
            raise ServiceError("LOG_PACKAGE_INVALID", "допустимы пакеты ignition, fuel и vanos", status=422)
        if not data:
            raise ServiceError("LOG_INVALID", "пустой лог", status=422)
        if len(data) > MAX_LOG_BYTES:
            raise ServiceError("UPLOAD_TOO_LARGE", f"лог больше {MAX_LOG_BYTES} байт", status=413)
        name = str(filename or "")
        if not name or "/" in name or "\\" in name or Path(name).name != name:
            raise ServiceError("LOG_PATH_FORBIDDEN", "имя лога не должно содержать путь", status=422)
        suffix = Path(name).suffix.lower()
        if suffix not in {".csv", ".db"}:
            raise ServiceError("LOG_FORMAT_UNSUPPORTED", "принимаются только CSV и .db Test0", status=422)
        if suffix == ".db" and not data.startswith(b"SQLite format 3\x00"):
            raise ServiceError("LOG_INVALID", "файл с расширением .db не является SQLite Test0", status=422)
        if suffix == ".csv" and b"\x00" in data:
            raise ServiceError("LOG_INVALID", "CSV содержит двоичный мусор", status=422)

        from tools import pack_log_analyzer

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
        share: dict[str, Any] | None = None
        try:
            try:
                outcome = pack_log_analyzer.analyze_pack_log(
                    temp_path, package, fuel_ron=fuel_ron)
            except Exception as error:  # analyzer errors become a stable API code
                raise ServiceError("LOG_INVALID", f"лог не разобран: {error}", status=422) from error
            if not outcome.get("valid"):
                issues = "; ".join(str(item) for item in (outcome.get("issues") or [])[:3])
                raise ServiceError(
                    "LOG_ANALYSIS_REJECTED",
                    "лог не прошёл физические/структурные ворота" + (f": {issues}" if issues else ""),
                    status=422,
                )
            if share_consent is True:
                # Очистка читает тот же временный файл, пока он существует;
                # исходные байты в хранилище не попадают никогда.
                share = self._share_log(temp_path, package=package, ip=ip)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

        # The evidence object is generated by us; hide the temporary path in
        # the response and replace it with the upload's harmless basename.
        evidence = dict(outcome)
        evidence["source"] = name
        evidence_id = secrets.token_urlsafe(18)
        self._gc_logs()
        self._logs[evidence_id] = {
            "created_at": time.monotonic(),
            "bytes": bytes(data),
            "suffix": suffix,
            "package": package,
            "evidence": evidence,
        }
        public = dict(evidence)
        public["evidence_id"] = evidence_id
        public["source"] = name
        result = {"status": "ANALYZED", "package": package, "evidence_id": evidence_id,
                  "analysis": public}
        if share is not None:
            result["share"] = share
        return result

    # -- добровольная передача лога ------------------------------------------
    def _share_refusal(self, code: str, message: str = "") -> dict[str, Any]:
        return {
            "status": "NOT_STORED",
            "code": code,
            "title_ru": self.refusal(code, "ru", message),
            "title_en": self.refusal(code, "en"),
        }

    def _share_log(self, temp_path: Path, *, package: str, ip: str) -> dict[str, Any]:
        """Очистить и сохранить копию; вернуть объект ``share`` для ответа."""
        if not self.log_donation or self.log_store is None:
            return self._share_refusal("LOG_SHARE_DISABLED")
        ip_hash = hash_value(ip or "unknown", self.ip_salt)
        since = time.time() - 86400
        shared_today = sum(
            1 for entry in self._journal
            if entry.status == "LOG_SHARED" and entry.ip_hash == ip_hash
            and _parse_time(entry.time) >= since
        )
        if shared_today >= self.log_shares_per_ip_per_day:
            return self._share_refusal(
                "LOG_RATE_LIMITED",
                f"не больше {self.log_shares_per_ip_per_day} передач в сутки")
        store = self.log_store
        try:
            scrubbed = log_store_module.scrub_log_file(
                temp_path, max_bytes=store.max_file_bytes)
            log_id, token, meta = store.save(
                scrubbed.data, package=package, platform=scrubbed.platform,
                rows=scrubbed.rows, channels=scrubbed.channels)
        except log_store_module.ScrubError as error:
            return self._share_refusal(error.code, str(error))
        except OSError:
            return self._share_refusal("LOG_STORE_FULL", "ошибка записи в хранилище")
        except Exception as error:  # noqa: BLE001 - сбой очистки не должен отнять анализ
            return self._share_refusal("LOG_SCRUB_FAILED", type(error).__name__)
        # Журнал: только хеш IP (как у сборок), без id копии и без sha —
        # запись журнала нельзя напрямую связать с сохранённым файлом.
        self._journal_add(JournalEntry(
            time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            account_hash="", vin_hash="", vin_tail="", ip_hash=ip_hash,
            stock_sha256="", result_sha256="",
            generator_version=GENERATOR_VERSION,
            status="LOG_SHARED", code="LOG_SHARED",
            platform=str(scrubbed.platform or ""), version="",
        ))
        return {
            "status": "STORED",
            "log_id": log_id,
            "delete_token": token,
            "expires": meta["expires"],
            "consent_version": meta["consent_version"],
            "rows": meta["rows"],
            "channels": meta["channels"],
        }

    def delete_shared_log(self, log_id: str, token: str) -> dict[str, str]:
        """Стереть переданную копию по коду удаления (одинаковый 404 на всё)."""
        store = self.log_store
        if store is None or not store.delete(str(log_id or ""), str(token or "")):
            raise ServiceError(
                "LOG_DELETE_NOT_FOUND", "лог не найден или код удаления неверный", status=404)
        self._journal_add(JournalEntry(
            time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            account_hash="", vin_hash="", vin_tail="", ip_hash="",
            stock_sha256="", result_sha256="",
            generator_version=GENERATOR_VERSION,
            status="LOG_DELETED", code="LOG_DELETED", platform="", version="",
        ))
        return {"status": "DELETED"}

    def purge_expired_logs(self) -> int:
        return self.log_store.purge_expired() if self.log_store is not None else 0

    def _request_with_log(self, request: dict[str, Any], tmp_dir: Path) -> dict[str, Any]:
        """Resolve a server-issued log id into a temporary analyzer source path."""
        evidence_id = request.get("log_evidence_id")
        if not evidence_id:
            return request
        self._gc_logs()
        record = self._logs.get(str(evidence_id))
        if record is None:
            raise ServiceError("LOG_EVIDENCE_NOT_FOUND", "лог устарел или evidence_id неизвестен", status=422)
        intents = request.get("intents")
        if not isinstance(intents, list):
            raise ServiceError("REQUEST_INVALID", "для server log нужны intents", status=422)
        package = str(record["package"])
        if not any(isinstance(item, dict) and str(item.get("kind")) == package
                   for item in intents):
            raise ServiceError("LOG_PACKAGE_MISMATCH", "лог не соответствует пакету заявки", status=422)
        path = tmp_dir / ("server_log" + str(record["suffix"]))
        path.write_bytes(record["bytes"])
        evidence = dict(record["evidence"])
        evidence["source"] = str(path)
        payload = json.loads(json.dumps(request, ensure_ascii=False))
        payload.pop("log_evidence_id", None)
        for item in payload["intents"]:
            if isinstance(item, dict) and str(item.get("kind")) == package:
                # A server-issued proof is authoritative; a client cannot
                # replace it with a hand-written result.
                item["evidence"] = evidence
        return payload

    def build(
        self,
        data: bytes,
        request: dict[str, Any],
        *,
        account: str,
        vin: Any = "",
        ip: str = "",
        filename: str = "",
    ) -> Job:
        """Собрать файл по заявке: лимиты → детекция → сборка → журнал."""
        self._check_upload(data)
        if not isinstance(request, dict):
            raise ServiceError("REQUEST_INVALID", "заявка должна быть JSON-объектом")
        _reject_server_paths(request)
        # Detect before consuming account/VIN quotas and apply the release gate
        # at the API boundary.  Direct stage assembly remains available for
        # byte-level tests, but the public service with the tracked default
        # refuses new MS42/MS43 features until the bench report exists.
        with self._temp_image(data) as probe:
            try:
                detection = torquex.detect(probe)
            except torquex.TorquexError as error:
                raise ServiceError(error.code, str(error)) from error
        if detection.platform == "GS8":
            return self._build_gs8(data, request, account=account, vin=vin, ip=ip)
        verdict = cal_state.analyze(detection.platform, data)
        if verdict["state"] == cal_state.STATE_TUNED:
            raise ServiceError(
                "CAL_NOT_STOCK",
                "файл уже изменён (не заводской): верните заводскую прошивку и загрузите её",
                status=422,
            )
        intents = request.get("intents") or []
        gate = common_stages.bench_gate_finding(
            detection.platform, intents, version=detection.version)
        if gate is not None:
            raise ServiceError(gate["code"], gate["message"], status=422)
        # ``advanced`` is used only for the auditable release annotation.  The
        # actual gate rejects pedal/VANOS; ignition is allowed through the
        # conservative public writer and gets linked MBT only when this exact
        # ECU/version is registry-cleared (or the explicit gate is disabled).
        advanced = common_stages.advanced_intents(intents, detection.platform)
        linked_allowed = common_stages.release_advanced_allowed(
            detection.platform, detection.version
        )
        account_hash = hash_value(account.strip().lower() or "anonymous")
        vin_hash, vin_tail = normalise_vin(vin)
        self._check_limits(account_hash, vin_hash)

        stock_sha = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            stock = tmp_dir / "stock.bin"
            stock.write_bytes(data)
            out = tmp_dir / "result.bin"
            note_path = tmp_dir / "note.md"
            manifest_path = tmp_dir / "manifest.json"
            request_for_build = self._request_with_log(request, tmp_dir)
            # Never trust a client-supplied internal release switch.  The API
            # decides linked-MBT rights from the detected exact version and the
            # registry, then passes the decision to both MS42 and MS43 cores.
            request_for_build = json.loads(json.dumps(request_for_build, ensure_ascii=False))
            if str(detection.platform).upper() in {"MS42", "MS43"}:
                request_for_build["_allow_linked_ignition"] = linked_allowed
            request_path = tmp_dir / "request.json"
            request_path.write_text(
                json.dumps(request_for_build, ensure_ascii=False), encoding="utf-8")
            args = argparse.Namespace(
                file=str(stock),
                request=str(request_path),
                variant=request_for_build.get("variant"),
                out=str(out),
                note=str(note_path),
                manifest=str(manifest_path),
                no_catalyst=bool(request_for_build.get("no_catalyst")),
                legality=bool(request_for_build.get("acknowledge_legality")),
                customer=str(request_for_build.get("customer") or ""),
                notes=str(request_for_build.get("notes") or ""),
                dry_run=False,
                lang="ru",
            )
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    detection = torquex.detect(stock)
                    code = torquex.build(args)
            except torquex.TorquexError as error:
                raise ServiceError(error.code, str(error)) from error
            log = buffer.getvalue()
            image = out.read_bytes() if out.exists() else None
            note = note_path.read_text(encoding="utf-8") if note_path.exists() else log
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists() else {}
            )
            # Never expose our temporary filesystem path in a returned note or
            # manifest; the client supplied bytes, not a path.
            temp_marker = str(tmp_dir)
            log = log.replace(temp_marker, "server-upload")
            note = note.replace(temp_marker, "server-upload")
            manifest_text = json.dumps(manifest, ensure_ascii=False).replace(
                temp_marker, "server-upload")
            manifest = json.loads(manifest_text)

        status = "BUILT" if code == 0 and image is not None else "REJECTED"
        if advanced:
            required = common_stages.bench_required()
            verified = common_stages.bench_verified(detection.platform, detection.version)
            manifest["bench_required"] = required
            manifest["bench_registry_match"] = verified
            manifest["trust_level"] = common_stages.trust_level(bench_verified=verified)
            if verified:
                manifest["bench_verification"] = (
                    "exact platform/version is explicitly recorded in "
                    "config/bench_verified.json"
                )
            elif not required:
                manifest["released_without_bench"] = True
                manifest["bench_verification"] = (
                    "TORQUEX_BENCH_REQUIRED=false; no matching registry record — "
                    "release is bytes+crc verified, not bench verified"
                )
                note = note.rstrip() + (
                    "\n\nВыпуск без стенда: реестр bench_verified не содержит "
                    f"{detection.platform}/{detection.version}; проверены только байты и CRC."
                )
            else:
                manifest["bench_verification"] = (
                    "public conservative path only; linked MBT/pedal/VANOS stay gated"
                )
        result_sha = hashlib.sha256(image).hexdigest() if image is not None else ""
        job = Job(
            job_id=secrets.token_urlsafe(12),
            created_at=time.monotonic(),
            ttl_seconds=self.ttl_seconds,
            status=status,
            platform=detection.platform,
            version=detection.version,
            image=image,
            note=note,
            manifest=manifest,
            log=log,
            donate_url=self.donate_url,
        )
        self._journal_add(JournalEntry(
            time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            account_hash=account_hash,
            vin_hash=vin_hash,
            vin_tail=vin_tail,
            ip_hash=hash_value(ip, self.ip_salt) if ip else "",
            stock_sha256=stock_sha,
            result_sha256=result_sha,
            generator_version=GENERATOR_VERSION,
            status=status,
            code="BUILT" if status == "BUILT" else _first_reject_code(manifest, log),
            platform=detection.platform,
            version=detection.version,
        ))
        if status == "BUILT":
            self._jobs[job.job_id] = job
            self._gc_jobs()
        return job

    def _build_gs8(self, data: bytes, request: dict[str, Any], *, account: str, vin: Any, ip: str) -> Job:
        """Блок АКПП GS8: откат калибровки на фабричную (заявка ``gs8_restore``)."""
        intents = [i for i in (request.get("intents") or []) if isinstance(i, dict)]
        asked = [i for i in intents if i.get("kind") in ("gs8_restore", "gs8_alpina")]
        if len(asked) != 1 or len(intents) != 1 or not isinstance(asked[0].get("calibration"), str):
            raise ServiceError(
                "TCU_READ_ONLY",
                "для блока АКПП доступны только фабричная калибровка BMW или калибровка Alpina",
                status=422)
        source = "alpina" if asked[0]["kind"] == "gs8_alpina" else "bmw"
        account_hash = hash_value(account.strip().lower() or "anonymous")
        vin_hash, vin_tail = normalise_vin(vin)
        self._check_limits(account_hash, vin_hash)
        try:
            image, report = gs8_state.restore(data, asked[0]["calibration"], source=source)
        except gs8_state.Gs8Error as error:
            raise ServiceError(error.code, str(error), status=422) from error
        stock_sha = hashlib.sha256(data).hexdigest()
        result_sha = hashlib.sha256(image).hexdigest()
        sums = ", ".join(f"{row['name']} {row['value']}" for row in report["checksums"])
        note = (
            f"# Наряд GS8 — {'калибровка Alpina' if source == 'alpina' else 'откат калибровки'}\n\n"
            f"* блок: {report['system']}\n"
            f"* калибровка: {'Alpina' if source == 'alpina' else 'фабричная BMW'}, {report['vehicle']} (метка {report['label']})\n"
            f"* исходный файл sha256: {stock_sha[:16]}…\n"
            f"* результат sha256: {result_sha[:16]}…\n"
            f"* изменено байт: {report['changed_bytes']} (только окно калибровки, программа и загрузчик не тронуты)\n"
            f"* контрольные суммы пересчитаны: {sums}\n\n"
            + ("Калибровка Alpina рассчитана на усиленную коробку и свой двигатель: на обычной коробке "
               "блокировка и переключения могут отличаться от заводских.\n" if source == "alpina" else "")
            + "Перед записью сохраните исходный файл блока. Откат: запишите сохранённый исходный файл.\n"
            "Адаптации коробки после записи не сбрасываются автоматически.\n"
        )
        manifest = {
            "manifest_version": 1,
            "stage": "gs8_alpina" if source == "alpina" else "gs8_restore",
            "status": "BUILT",
            "trust_level": "bytes+crc verified",
            "source_sha256": stock_sha,
            "result_sha256": result_sha,
            "changed_bytes": report["changed_bytes"],
            "changed_cells": None,
            "packages": [{
                "id": "gs8_alpina" if source == "alpina" else "gs8_restore",
                "title": "Калибровка Alpina блока GS8" if source == "alpina" else "Калибровка блока GS8",
                "status": "written",
                "detail": f"{report['vehicle']}; метка {report['label']}",
            }],
            "checksums": report["checksums"],
            "findings": [],
        }
        job = Job(
            job_id=secrets.token_urlsafe(12),
            created_at=time.monotonic(),
            ttl_seconds=self.ttl_seconds,
            status="BUILT",
            platform="GS8",
            version=report["system"],
            image=image,
            note=note,
            manifest=manifest,
            log="",
            donate_url=self.donate_url,
        )
        self._journal_add(JournalEntry(
            time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            account_hash=account_hash, vin_hash=vin_hash, vin_tail=vin_tail,
            ip_hash=hash_value(ip, self.ip_salt) if ip else "",
            stock_sha256=stock_sha, result_sha256=result_sha,
            generator_version=GENERATOR_VERSION, status="BUILT", code="BUILT",
            platform="GS8", version=report["system"],
        ))
        self._jobs[job.job_id] = job
        self._gc_jobs()
        return job

    def job(self, job_id: str) -> Job:
        self._gc_jobs()
        job = self._jobs.get(job_id)
        if job is None:
            raise ServiceError("JOB_NOT_FOUND", "результат не найден или уже удалён (короткий срок хранения)", status=404)
        job.ttl_remaining = max(0.0, job.ttl_seconds - (time.monotonic() - job.created_at))
        return job

    def _gc_jobs(self) -> None:
        for key in [key for key, job in self._jobs.items() if job.expired()]:
            del self._jobs[key]

    @staticmethod
    def _check_upload(data: bytes) -> None:
        if not data:
            raise ServiceError("REQUEST_INVALID", "пустая загрузка")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ServiceError(
                "UPLOAD_TOO_LARGE",
                f"файл {len(data)} байт больше предела {MAX_UPLOAD_BYTES} (1 МиБ)",
                status=413,
            )

    # -- тексты ------------------------------------------------------------
    @staticmethod
    def refusal(code: str, lang: str = "ru", message: str = "") -> str:
        return refusals.render(code, lang, message)


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"on", "true", "1", "yes"}


def _log_store_from_env() -> "log_store_module.LogStore":
    return log_store_module.LogStore.from_env()


def _parse_time(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:  # pragma: no cover - журнал всегда пишем сами
        return 0.0


def _first_reject_code(manifest: dict[str, Any], log: str) -> str:
    """Код отказа для журнала: из манифеста, иначе из вывода сборки."""
    for finding in (manifest.get("findings") or []):
        if isinstance(finding, dict) and finding.get("severity") == "reject":
            return str(finding.get("code") or "REJECTED")
    for line in log.splitlines():
        if line.strip().startswith("[reject]"):
            head = line.split("]", 1)[1].strip()
            return head.split(":", 1)[0].strip()
    return "REJECTED"
