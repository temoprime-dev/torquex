#!/usr/bin/env python3
"""Мелочи консоли: CLI MS45 обязаны переживать cp1251 и перенаправленный вывод.

Windows-консоль по умолчанию пишет в кодировке системы (для русской — cp1251),
и ``print`` символа вне неё падает с ``UnicodeEncodeError``.  Поэтому текст CLI
мы держим cp1251-совместимым (это проверяют ``MS45/tests/test_cli_console.py``),
а ``prepare_stdout`` — страховка на случай древней консоли: печатается замена,
а не трейсбек.
"""

from __future__ import annotations

import sys


def prepare_stdout() -> None:
    """Разрешить замену символов, которых нет в кодировке консоли (Windows)."""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError):  # закрытый или нестандартный поток
        pass
