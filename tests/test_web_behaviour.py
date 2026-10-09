"""Поведенческие проверки сайта: запуск, а не чтение разметки.

Статические тесты (tests/test_web_static.py) читают файлы регулярками. Этого
оказалось мало: в прошлом раунде сайт молча сломался от синтаксической ошибки
в web/i18n.js, а все проверки остались зелёными. Здесь скрипты сайта
**исполняются**:

  * web/tools/check_build_states.mjs — без внешних зависимостей: словарь и
    сборщик заявки грузятся в node:vm, проверяются все состояния кнопки
    «Собрать файл» и совпадение наборов ключей RU/EN;
  * web/tools/check_wizard.mjs — проход мастера в настоящем DOM (jsdom):
    файл → определение → вариант → опции → «Собрать» → «Скачать», отказы,
    сетевая ошибка, смена языка.

jsdom в репозитории нет и не будет (правило: никаких зависимостей npm в
проекте). Ставится рядом, вне дерева проекта:

    npm install jsdom          # в любой папке выше web/, например в корне

Если node или jsdom не найдены — тест пропускается (skip), а не падает:
на машине без Node проверки остаются зелёными. Поэтому прогон с jsdom —
обязательная часть ревью, её результат пишется в описании PR.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "web" / "tools"


def _node() -> str | None:
    return shutil.which("node")


def _run(script: Path) -> subprocess.CompletedProcess:
    node = _node()
    assert node is not None
    # encoding/errors заданы явно: на Windows консоль cp1251 и русский текст
    # в выводе проверок иначе валит сам запуск.
    return subprocess.run(
        [node, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


class BuildStateTests(unittest.TestCase):
    """Состояния кнопки «Собрать файл» и словарь — без внешних зависимостей."""

    def test_script_exists(self) -> None:
        self.assertTrue((TOOLS / "check_build_states.mjs").is_file())

    def test_build_states(self) -> None:
        if _node() is None:
            self.skipTest("node не установлен: состояния кнопки не проверены")
        result = _run(TOOLS / "check_build_states.mjs")
        message = (result.stdout or "") + (result.stderr or "")
        self.assertEqual(0, result.returncode, message)
        self.assertIn("OK:", message)


class WizardBehaviourTests(unittest.TestCase):
    """Проход мастера в DOM. Пропускается, если jsdom не установлен."""

    def test_script_exists(self) -> None:
        self.assertTrue((TOOLS / "check_wizard.mjs").is_file())

    def test_wizard_flow(self) -> None:
        if _node() is None:
            self.skipTest("node не установлен: мастер в DOM не проверен")
        result = _run(TOOLS / "check_wizard.mjs")
        message = (result.stdout or "") + (result.stderr or "")
        if "SKIP:" in message:
            self.skipTest("jsdom не установлен: npm install jsdom вне дерева проекта")
        self.assertEqual(0, result.returncode, message)
        self.assertIn("OK:", message)

    def test_feedback_page(self) -> None:
        if _node() is None:
            self.skipTest("node не установлен: страница обратной связи в DOM не проверена")
        result = _run(TOOLS / "check_feedback.mjs")
        message = (result.stdout or "") + (result.stderr or "")
        if "SKIP:" in message:
            self.skipTest("jsdom не установлен: npm install jsdom вне дерева проекта")
        self.assertEqual(0, result.returncode, message)
        self.assertIn("OK:", message)


class ToolsHygieneTests(unittest.TestCase):
    """Проверки сами не должны тащить зависимости в репозиторий."""

    def test_no_npm_files_in_repo(self) -> None:
        for name in ("package.json", "package-lock.json", "node_modules"):
            self.assertFalse(
                (ROOT / name).exists(),
                f"{name} не должен появляться в репозитории: jsdom ставится вне дерева проекта",
            )
            self.assertFalse((ROOT / "web" / name).exists(), f"web/{name} — то же правило")

    def test_jsdom_is_optional(self) -> None:
        source = (TOOLS / "check_wizard.mjs").read_text(encoding="utf-8")
        self.assertIn("SKIP:", source, "проверка обязана пропускаться без jsdom")
        self.assertIn("process.exit(0)", source, "без jsdom выход обязан быть нулевым")

    def test_build_states_has_no_imports_outside_node(self) -> None:
        source = (TOOLS / "check_build_states.mjs").read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("import "):
                self.assertIn(
                    'from "node:',
                    line,
                    "check_build_states.mjs обязан работать без внешних пакетов",
                )

    def test_readme_documents_the_checks(self) -> None:
        readme = (ROOT / "web" / "README_RU.md").read_text(encoding="utf-8")
        for needle in ("check_build_states.mjs", "check_wizard.mjs", "npm install jsdom"):
            self.assertIn(needle, readme, f"README: нет описания {needle}")


if __name__ == "__main__":
    unittest.main()
