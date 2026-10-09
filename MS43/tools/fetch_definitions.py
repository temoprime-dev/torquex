#!/usr/bin/env python3
"""Скачать определения MS4X (XDF) для версий MS43 и проверить sha256.

Определения — источник адресов политик версий, поэтому в репозитории они лежат
зеркалом (`MS43/definitions/`) и их суммы записаны в
``MS43/checksums/DEFINITIONS_SHA256SUMS.txt``.  Инструмент нужен, когда файла
нет (например, 430064 — версия без полного стока: политики нет, определения в
зеркало не положены) или когда надо перепроверить зеркало против upstream.

Источник: официальный репозиторий ``ms4x-net/ms43``, каталог ``definitions``.

    python3 MS43/tools/fetch_definitions.py --check          # только проверить
    python3 MS43/tools/fetch_definitions.py --version 430064 # скачать одну версию
    python3 MS43/tools/fetch_definitions.py --all            # все шесть версий

Скачивание идёт через ``curl``; если он в вашей среде закрыт, тот же файл можно
получить через GitHub CLI:

    gh api -H "Accept: application/vnd.github.raw" \\
        repos/ms4x-net/ms43/contents/definitions/MS430056_Extended_Load_512K.xdf \\
        > MS43/definitions/MS430056_Extended_Load_512K.xdf
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

try:
    from MS43.tools.console import prepare_stdout
    from MS43.tools.policy import DEFINITIONS_DIR, VERSION_DEFINITIONS
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    from console import prepare_stdout  # type: ignore[no-redef]
    from policy import DEFINITIONS_DIR, VERSION_DEFINITIONS  # type: ignore[no-redef]

RAW_URL = "https://raw.githubusercontent.com/ms4x-net/ms43/main/definitions"

#: Имена файлов на upstream: у 430069 они с префиксом Siemens_MS43_, у остальных —
#: с MS<версия>.  Ключ — имя файла в нашем зеркале.
UPSTREAM_NAME = {
    "Siemens_MS43_430069_Extended_Load_512K.xdf": "Siemens_MS43_430069_Extended_Load_512K.xdf",
    "Siemens_MS43_430069_Extended_Load_64K.xdf": "Siemens_MS43_430069_Extended_Load_64K.xdf",
}

#: Ожидаемые sha256 (сверено 2026-10-06 с upstream).  Файлы без записи скачиваются
#: без проверки, но инструмент печатает их сумму и предупреждает.
EXPECTED_SHA256: dict[str, str] = {
    "Siemens_MS43_430069_Extended_Load_512K.xdf":
        "e8c7f1a4b9b933b950d9fb67d508203e4bed14830c1d48adb9deb3fc63782d47",
    "Siemens_MS43_430069_Extended_Load_64K.xdf":
        "747bad5d53707d75fd5b786be5906aa4ae70bba0c5edd8f5dd0d9c01daf60255",
    "MS430056_Extended_Load_512K.xdf":
        "0deca53db7925d664412d8ea9f542a03442b06321bd4fadd04390ada55d5c257",
    "MS430056_Extended_Load_64K.xdf":
        "5f05b8d04aac1f5303d19f1db970ce2353402573046a13c49f7eb65ab116686e",
    "MS430037_Extended_Load_512K.xdf":
        "1fbdc569318e052ae85d7eb3111691392466fec38b6ad8a544a5a6aa51570c22",
    "MS430037_Extended_Load_64K.xdf":
        "97cf533d638d59b45d5f82fb60ffc847cfc2dc9d4bf425c11561cfaf99dad9bc",
    "MS430055_Extended_Load_512K.xdf":
        "b55c22790fcbb113a5e1583f9aa09932a34c4cd6a8ec048c5bf5436f42210ce4",
    "MS430055_Extended_Load_64K.xdf":
        "f63d5a84ed38fda4b6b46910316c54b55dbda9f9dbbc95f28d72a2ea1a6af514",
    "MS430066_Extended_Load_512K.xdf":
        "002e8a3ff2b3b48150735063a949a6de137f04e0467d0d5b3051582c8dbf4156",
    "MS430066_Extended_Load_64K.xdf":
        "548cfe92955e8bfc4fd0c352cc31e0bb8ab8f8a3a296f05b600282eda765fea6",
}


def expected_files(versions: list[str]) -> list[Path]:
    files: list[Path] = []
    for version in versions:
        pair = VERSION_DEFINITIONS.get(version)
        if pair is None:
            continue
        files.extend(pair)
    return files


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(files: list[Path]) -> int:
    failures = 0
    for path in files:
        if not path.exists():
            print(f"нет файла: {path} (скачать: --version ...)")
            failures += 1
            continue
        digest = sha256(path)
        expected = EXPECTED_SHA256.get(path.name)
        if expected is None:
            print(f"{path.name}: сумма не записана, посчитана {digest}")
        elif digest == expected:
            print(f"{path.name}: sha256 совпала с записанной")
        else:
            print(f"{path.name}: sha256 НЕ совпала: {digest} != {expected}")
            failures += 1
    return failures


def download(files: list[Path], force: bool) -> int:
    failures = 0
    DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.exists() and not force:
            continue
        name = UPSTREAM_NAME.get(path.name, path.name)
        url = f"{RAW_URL}/{name}"
        print(f"скачиваю {url}")
        result = subprocess.run(["curl", "-sSL", "-o", str(path), url], check=False)
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            print(
                f"не скачалось: {path.name}; попробуйте через gh api (см. докстринг файла)",
                file=sys.stderr,
            )
            failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()
    parser = argparse.ArgumentParser(description="Определения MS4X для версий MS43.")
    parser.add_argument("--version", action="append", help="версия (430056, ...)")
    parser.add_argument("--all", action="store_true", help="все версии из реестра")
    parser.add_argument("--check", action="store_true", help="только проверить суммы")
    parser.add_argument("--force", action="store_true", help="перекачать поверх")
    args = parser.parse_args(argv)

    if args.all:
        versions = list(VERSION_DEFINITIONS)
    elif args.version:
        versions = list(dict.fromkeys(args.version))
    else:
        versions = list(VERSION_DEFINITIONS)
    files = expected_files(versions)
    if not files:
        print("нечего делать: версии не найдены", file=sys.stderr)
        return 2
    failures = check(files) if args.check else download(files, args.force)
    if not args.check:
        failures += check(files)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
