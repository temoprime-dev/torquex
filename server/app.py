# -*- coding: utf-8 -*-
"""HTTP-слой TorqueX: FastAPI-обёртка над `server/core.py` (задание §5).

Слушает **только 127.0.0.1**: наружу его отдаёт nginx (см. `server/DEPLOY_RU.md`),
прод-сервер torquex.live этим файлом не занимается — владелец применяет его сам.

Эндпоинты:

* ``GET  /health``            — живость и версия генератора;
* ``GET  /versions``          — поддерживаемые ЭБУ/версии/семейства;
* ``POST /detect``            — загрузить сток (≤ 1 МиБ) → ЭБУ и версия;
* ``POST /build``             — сток + заявка → файл (base64), наряд, манифест;
* ``GET  /jobs/{job_id}``     — результат из короткого хранилища (+ наряд);
* ``GET  /jobs/{job_id}/file``— только готовый файл (application/octet-stream);
* ``POST /logs``              — разбор лога (+ добровольная передача копии);
* ``DELETE /logs/{log_id}``   — стереть переданную копию по коду удаления;
* ``GET  /journal``           — журнал (только с админ-токеном).

FastAPI импортируется внутри `create_app()`: ядро и тесты работают без него.
Никаких внешних сетевых обращений приложение не делает; чужие пути и имена
файлов не используются для доступа к диску.
"""

# Без `from __future__ import annotations`: FastAPI разрешает аннотации UploadFile/File
# по объектам, а не по строкам (иначе /detect и /build дают 500 на pydantic).
import os
import secrets
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core import MAX_UPLOAD_BYTES, ServiceError, TorquexService  # noqa: E402


def create_app(service: TorquexService | None = None) -> Any:
    """Собрать FastAPI-приложение. Без установленного FastAPI — понятная ошибка."""
    try:
        from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
        from fastapi.responses import JSONResponse, Response
    except ImportError as error:  # pragma: no cover - проверяется в deploy-доках
        raise RuntimeError(
            "для серверной обёртки нужен fastapi: pip install -r server/requirements.txt"
        ) from error

    import json

    service = service or TorquexService()
    app = FastAPI(title="TorqueX API", version="1")

    def _refusal(error: ServiceError, lang: str = "ru") -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={
                "code": error.code,
                "title_ru": service.refusal(error.code, "ru", str(error)),
                "title_en": service.refusal(error.code, "en"),
                "donate_url": service.donate_url,
            },
        )

    async def _read_upload(file: UploadFile) -> bytes:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ServiceError(
                "UPLOAD_TOO_LARGE",
                f"файл больше {MAX_UPLOAD_BYTES} байт (1 МиБ)",
                status=413,
            )
        return data

    @app.get("/health")
    async def health() -> dict[str, Any]:
        versions = service.versions()
        return {
            "status": "ok",
            "generator_version": versions["generator_version"],
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_log_bytes": versions.get("max_log_bytes", MAX_UPLOAD_BYTES),
            "bench_required": versions.get("bench_required", True),
            "vin_required": versions.get("vin_required", False),
            "log_donation": bool(service.log_donation and service.log_store is not None),
            "log_retention_days": (
                service.log_store.retention_days if service.log_store is not None else None),
        }

    @app.get("/versions")
    async def versions() -> dict[str, Any]:
        return service.versions()

    @app.post("/detect")
    async def detect(file: UploadFile = File(...), lang: str = Form("ru")) -> Any:
        try:
            data = await _read_upload(file)
            return service.detect(data, filename=file.filename or "")
        except ServiceError as error:
            return _refusal(error, lang)
        except Exception as error:  # noqa: BLE001 - наружу всегда код, не трейсбек
            return _refusal(ServiceError("INTERNAL_ERROR", str(error), status=500), lang)

    @app.post("/logs")
    async def logs(
        request: Request,
        file: UploadFile = File(...),
        package: str = Form(...),
        consent: str = Form("false"),
        share_consent: str = Form("false"),
        fuel_ron: str = Form(""),
        lang: str = Form("ru"),
        x_forwarded_for: str | None = Header(default=None),
    ) -> Any:
        """Analyze a CSV/Test0 DB on the server; no client path or VIN is accepted.

        ``share_consent=true`` — отдельное добровольное согласие сохранить
        очищенную копию (только если владелец включил TORQUEX_LOG_DONATION).
        IP используется лишь для лимита передач и сразу превращается в хеш.
        """
        try:
            data = await _read_upload(file)
            ron = None if not fuel_ron.strip() else float(fuel_ron.replace(",", "."))
            ip = (x_forwarded_for or "").split(",")[0].strip()
            if not ip and request.client is not None:
                ip = request.client.host or ""
            result = service.analyze_log(
                data,
                filename=file.filename or "",
                package=package,
                consent=consent.strip().lower() == "true",
                fuel_ron=ron,
                share_consent=share_consent.strip().lower() == "true",
                ip=ip,
            )
            return result
        except ServiceError as error:
            return _refusal(error, lang)
        except ValueError as error:
            return _refusal(ServiceError("LOG_INVALID", str(error), status=422), lang)
        except Exception as error:  # noqa: BLE001
            return _refusal(ServiceError("LOG_INVALID", str(error), status=422), lang)

    @app.delete("/logs/{log_id}")
    async def delete_log(
        log_id: str,
        x_delete_token: str | None = Header(default=None),
    ) -> Any:
        """Стереть переданную копию лога кодом удаления (заголовок X-Delete-Token).

        Ответ 404 одинаковый для «нет такой копии» и «код неверный». Работает и
        при выключенной передаче: обещание удалить действует всегда.
        """
        try:
            return service.delete_shared_log(log_id, x_delete_token or "")
        except ServiceError as error:
            return _refusal(error)
        except Exception:  # noqa: BLE001 - наружу только код
            return _refusal(ServiceError(
                "LOG_DELETE_NOT_FOUND", "лог не найден или код удаления неверный", status=404))

    @app.post("/build")
    async def build(
        file: UploadFile = File(...),
        request: str = Form(...),
        account: str = Form("anonymous"),
        vin: str = Form(""),
        lang: str = Form("ru"),
        x_forwarded_for: str | None = Header(default=None),
    ) -> Any:
        try:
            payload = json.loads(request)
        except json.JSONDecodeError:
            return _refusal(ServiceError("REQUEST_INVALID", "заявка не JSON"), lang)
        ip = (x_forwarded_for or "").split(",")[0].strip()
        try:
            data = await _read_upload(file)
            job = service.build(
                data, payload,
                account=account, vin=vin, ip=ip, filename=file.filename or "",
            )
            body = job.as_dict(with_image=True)
            body["rejections"] = [
                finding for finding in (job.manifest.get("findings") or [])
                if isinstance(finding, dict) and finding.get("severity") == "reject"
            ]
            return JSONResponse(status_code=200 if job.status == "BUILT" else 422, content=body)
        except ServiceError as error:
            return _refusal(error, lang)
        except Exception as error:  # noqa: BLE001
            return _refusal(ServiceError("INTERNAL_ERROR", str(error), status=500), lang)

    @app.get("/jobs/{job_id}")
    async def job_detail(job_id: str) -> Any:
        try:
            return service.job(job_id).as_dict(with_image=False)
        except ServiceError as error:
            return _refusal(error)

    @app.get("/jobs/{job_id}/file")
    async def job_file(job_id: str) -> Any:
        try:
            job = service.job(job_id)
        except ServiceError as error:
            return _refusal(error)
        if job.image is None:
            return _refusal(ServiceError("NO_OUTPUT", "у этой сборки нет файла"))
        headers = {"X-Generator-Version": str(service.versions()["generator_version"])}
        return Response(content=job.image, media_type="application/octet-stream", headers=headers)

    @app.get("/journal")
    async def journal(
        limit: int = 100,
        x_admin_token: str | None = Header(default=None),
    ) -> Any:
        expected = os.environ.get("TORQUEX_ADMIN_TOKEN")
        if not expected or not x_admin_token or not secrets.compare_digest(
            x_admin_token.encode("utf-8"), expected.encode("utf-8")
        ):
            return _refusal(ServiceError("REQUEST_INVALID", "нужен админ-токен", status=403))
        return {"entries": service.journal(limit=limit)}

    @app.post("/feedback")
    async def feedback(
        request: Request,
        kind: str = Form(...),
        message: str = Form(...),
        contact: str = Form(""),
        platform: str = Form(""),
        version: str = Form(""),
        sha12: str = Form(""),
        website: str = Form(""),
        lang: str = Form("ru"),
        x_forwarded_for: str | None = Header(default=None),
    ) -> Any:
        """Короткое сообщение владельцу: результат на машине, ошибка, идея.

        ``website`` — скрытое поле-приманка для ботов. IP превращается в хеш
        и нужен только для суточного лимита.
        """
        try:
            ip = (x_forwarded_for or "").split(",")[0].strip()
            if not ip and request.client is not None:
                ip = request.client.host or ""
            return service.submit_feedback(
                kind=kind, message=message, contact=contact, lang=lang, honeypot=website, ip=ip,
                context={"platform": platform, "version": version, "sha12": sha12},
            )
        except ServiceError as error:
            return _refusal(error, lang)
        except Exception as error:  # noqa: BLE001
            return _refusal(ServiceError("FEEDBACK_INVALID", str(error), status=422), lang)

    @app.get("/feedback")
    async def feedback_list(
        limit: int = 100,
        x_admin_token: str | None = Header(default=None),
    ) -> Any:
        expected = os.environ.get("TORQUEX_ADMIN_TOKEN")
        if not expected or not x_admin_token or not secrets.compare_digest(
            x_admin_token.encode("utf-8"), expected.encode("utf-8")
        ):
            return _refusal(ServiceError("REQUEST_INVALID", "нужен админ-токен", status=403))
        return {"entries": service.recent_feedback(limit=limit)}

    return app


def main() -> int:  # pragma: no cover - запускается на сервере
    import uvicorn

    host = os.environ.get("TORQUEX_HOST", "127.0.0.1")
    port = int(os.environ.get("TORQUEX_PORT", "8081"))
    uvicorn.run(create_app(), host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
