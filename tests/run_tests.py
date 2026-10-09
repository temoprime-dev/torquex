"""Запуск всех тестов проекта.

В публичном репозитории нет чужих прошивок, описаний карт и калибровок
(Siemens, BMW, ZF, Alpina). Тесты, которым нужны эти файлы, перечислены в
tests/needs_data.txt и без данных помечаются «пропущено»; если положить
прошивки в MS4x/firmware и т. п. (см. README), запускаются все тесты.

    python tests/run_tests.py            # все тесты
    python tests/run_tests.py --record   # перезаписать needs_data.txt (в дереве без данных)
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SUITES = ["tests", "MS41/tests", "MS42/tests", "MS43/tests", "MS45/tests"]
LIST = ROOT / "tests" / "needs_data.txt"


def has_data():
    return any(ROOT.glob("MS4*/firmware*/**/*.bin"))


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def load():
    loader = unittest.TestLoader()
    tests = []
    for name in SUITES:
        if (ROOT / name).is_dir():
            tests.extend(flatten(loader.discover(str(ROOT / name), top_level_dir=str(ROOT))))
    return tests


def test_id(test):
    name = test.id()
    if name.startswith("unittest.loader._FailedTest."):
        return "import:" + name.rsplit(".", 1)[1]
    return name


def keys(test):
    name = test.id()
    if name.startswith("unittest.loader._FailedTest."):
        return {"import:" + name.rsplit(".", 1)[1]}
    parts = name.split(".")
    return {name, "class:" + ".".join(parts[:-1])}


def record_key(test):
    """Ошибка в setUpClass/setUpModule приходит как «setUpClass (модуль.Класс)»."""
    name = test.id()
    if name.startswith("setUpClass ("):
        return "class:" + name[len("setUpClass ("):-1]
    if name.startswith("setUpModule ("):
        return "module:" + name[len("setUpModule ("):-1]
    return test_id(test)


def main():
    record = "--record" in sys.argv
    tests = load()
    skip = set()
    if not record and not has_data() and LIST.exists():
        skip = set(LIST.read_text(encoding="utf-8").split())
    suite = unittest.TestSuite()
    skipped = 0
    for test in tests:
        mod = test.__class__.__module__
        if keys(test) & skip or ("module:" + mod) in skip:
            skipped += 1
            continue
        suite.addTest(test)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if record:
        bad = sorted({record_key(t) for t, _ in result.failures + result.errors})
        LIST.write_text("\n".join(bad) + "\n", encoding="utf-8")
        print(f"записано {len(bad)} тестов, которым нужны данные")
        return 0
    if skipped:
        print(f"Пропущено {skipped} тестов, которым нужны прошивки (нет данных, см. tests/needs_data.txt)")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
