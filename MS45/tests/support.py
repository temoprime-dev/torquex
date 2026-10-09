"""Общие вспомогательные функции тестов MS45.

Модуль не содержит тестов: ``unittest discover`` просто импортирует его.
"""

from __future__ import annotations

from MS45.tools import checksums as ck

#: Идентификатор семейства, которого нет в политике, но который проходит разбор
#: ``45[67]0[BL][A-Z]\\d\\dS``. Нужен там, где проверяется отказ
#: ``CAL_VERSION_UNSUPPORTED``: с шага 3 все десять целевых семейств MS45
#: поддержаны, поэтому «чужой» сток приходится синтезировать подменой
#: ASCII-идентификатора в копии стока (адреса при этом не читаются).
UNKNOWN_FAMILY = "4560BZ00S"


def unknown_family_image(image: bytes) -> bytes:
    """Копия образа с подменённым идентификатором семьи (для отказов по версии)."""
    family = ck.read_family(image)
    if family is None:
        raise AssertionError("в образе нет разбираемого идентификатора семейства")
    data = bytearray(image)
    start = bytes(data[:0x50]).find(family.encode("ascii"))
    data[start : start + len(family)] = UNKNOWN_FAMILY.encode("ascii")
    return bytes(data)
