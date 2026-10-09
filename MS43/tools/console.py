#!/usr/bin/env python3
"""Мелочи консоли: CLI MS43 обязаны переживать cp1251 и перенаправленный вывод.

Windows-консоль по умолчанию пишет в кодировке системы (для русской — cp1251),
и ``print`` символа вне неё падал с ``UnicodeEncodeError`` (реальный случай:
``≤`` в отчёте политики, см. ``MS43/REVIEW_CLAUDE_RU.md``, задание 2).  Тексты
CLI мы держим cp1251-совместимыми — это проверяет
``MS43/tests/test_cli_console.py``, — а ``prepare_stdout`` служит страховкой:
если в вывод всё же попадёт новый символ или консоль окажется древней,
инструмент напечатает замену, а не упадёт с трейсбеком.
"""

from __future__ import annotations

import sys


def prepare_stdout() -> None:
    """Разрешить замену символов, которых нет в кодировке консоли (Windows).

    На перенаправленном выводе и в ``io.StringIO`` (тесты) вызывать нечего:
    ``reconfigure`` есть только у настоящего ``TextIOWrapper``.
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError):  # закрытый или нестандартный поток
        pass
