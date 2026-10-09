#!/usr/bin/env python3
"""Пути в артефактах MS45: всегда прямые слэши, независимо от платформы.

Политика, манифест и наряд — документы, которые собираются и на Windows, и на
Linux. Если путь попадёт в них в Windows-нотации (``definitions\\file.xdf``),
JSON перестанет совпадать побайтово, а диффы будут «прыгать» при смене ОС.
Поэтому каждый путь, который уходит в артефакт, проходит через :func:`to_posix`.
"""

from __future__ import annotations

from pathlib import PureWindowsPath


def to_posix(value: object) -> str:
    """Строка пути с прямыми слэшами (Windows-нотация тоже принимается)."""
    text = str(value)
    if "\\" in text:
        return PureWindowsPath(text).as_posix()
    return text


def relative_posix(path: object, root: object) -> str:
    """Путь относительно корня в POSIX-нотации (для первоисточников определений).

    Сравнение строк, а не ``Path.relative_to``: так функция одинаково работает с
    путями любой платформы и не бросает исключение, если файл лежит вне корня.
    """
    text = to_posix(path)
    prefix = to_posix(root).rstrip("/")
    if prefix and text.startswith(prefix + "/"):
        return text[len(prefix) + 1 :]
    return text
