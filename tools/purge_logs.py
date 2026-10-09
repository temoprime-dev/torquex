# -*- coding: utf-8 -*-
"""Автоудаление переданных логов с истёкшим сроком (для cron/systemd-timer).

Читает те же переменные окружения, что и сервер:

* ``TORQUEX_LOG_STORE`` — каталог хранилища (по умолчанию ``data/logs``);
* ``TORQUEX_LOG_RETENTION_DAYS`` — срок хранения в днях (по умолчанию 365).

Срок копии — меньшее из ``expires`` в её метаданных и ``created + срок`` по
текущей настройке. Копии без читаемых метаданных удаляются (согласие не
доказать), чужие файлы в каталоге не трогаются.

    python3 tools/purge_logs.py            # удалить и напечатать число
    python3 tools/purge_logs.py --dry-run  # только посчитать
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.log_store import LogStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Удалить переданные логи с истёкшим сроком")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не удалять, только посчитать")
    args = parser.parse_args(argv)
    store = LogStore.from_env()
    if args.dry_run:
        print(f"к удалению (dry-run): {store.purge_expired(dry_run=True)}")
        return 0
    count = store.purge_expired()
    print(f"удалено копий: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
