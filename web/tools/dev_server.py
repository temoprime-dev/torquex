#!/usr/bin/env python3
"""Локальный сервер для сайта TorqueX: статика web/ + прокси /api/ к серверу.

В песочнице и на машине разработчика сайт удобнее смотреть так, как он будет
работать в бою (nginx по server/DEPLOY_RU.md): статика и API на одном домене.

    python3 tools/dev_server.py                       # сайт на 127.0.0.1:8123
    python3 tools/dev_server.py --host 0.0.0.0        # видно извне (превью)
    python3 tools/dev_server.py --api http://127.0.0.1:8081

Запросы к /api/... уходят на бэкенд без префикса: /api/versions → /versions.
Если бэкенд не поднят, страница честно скажет, что API недоступно, а демо
(?demo=1) продолжит работать без сервера.

Зависимостей нет — только стандартная библиотека. Прод-сервер этим скриптом
не заменяется.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import pathlib
import socketserver
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_WEB = ROOT / "web"

#: Заголовки, которые не должен пересылать прокси (hop-by-hop).
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length"}


class Handler(http.server.SimpleHTTPRequestHandler):
    """Статика каталога web/ и прокси на бэкенд под /api/."""

    api_base = "http://127.0.0.1:8081"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - так требует базовый класс
        sys.stderr.write("[dev] " + (fmt % args) + "\n")

    def _is_api(self) -> bool:
        return self.path == "/api" or self.path.startswith("/api/")

    def _proxy(self, method: str) -> None:
        target = self.api_base.rstrip("/") + self.path[4:]  # срезаем "/api"
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS
        }
        request = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.send_response(error.code)
            content_type = error.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)
        except (urllib.error.URLError, OSError) as error:
            payload = (
                '{"code":"API_UNREACHABLE","title_ru":"API недоступно: '
                + str(error).replace('"', "'")
                + '","title_en":"API is unreachable"}'
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - имя из базового класса
        if self._is_api():
            self._proxy("GET")
        else:
            super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._is_api():
            self._proxy("HEAD")
        else:
            super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        if self._is_api():
            self._proxy("POST")
        else:
            self.send_error(405, "только GET: это статический сервер")

    def do_DELETE(self) -> None:  # noqa: N802 - удаление переданного лога (logs.html)
        if self._is_api():
            self._proxy("DELETE")
        else:
            self.send_error(405, "только GET: это статический сервер")

    def end_headers(self) -> None:
        # Никаких запретов на встраивание: сайт показывают в превью-окне.
        if not self._is_api():
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TorqueX: статика web/ + прокси /api/")
    parser.add_argument("--host", default="127.0.0.1", help="по умолчанию только localhost")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--api", default="http://127.0.0.1:8081", help="базовый адрес API")
    parser.add_argument("--web-dir", default=str(DEFAULT_WEB))
    args = parser.parse_args(argv)

    web_dir = pathlib.Path(args.web_dir).resolve()
    if not (web_dir / "index.html").is_file():
        parser.error(f"нет {web_dir/'index.html'} — это не каталог сайта")
    Handler.api_base = args.api
    handler = functools.partial(Handler, directory=str(web_dir))
    with Server((args.host, args.port), handler) as httpd:
        print(f"TorqueX dev server: http://{args.host}:{args.port}/  (API -> {args.api})")
        print("демо-режим: ?demo=1")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nостановлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
