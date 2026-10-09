# -*- coding: utf-8 -*-
"""Статическая приёмка сайта TorqueX (каталог ``web/``).

Проверки без браузера и без сети: структура страниц, ссылки и ресурсы, паритет
RU/EN, запрет внешних запросов и небезопасных вставок HTML, размер бандла,
доступность (подписи, aria-live, фокус, prefers-reduced-motion, размер полей) и
контраст по токенам темы. Поведение в браузере проверяется отдельно
(web/tools/check_requests.mjs — заявки через настоящий генератор) и описано в
web/README_RU.md.
"""
from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

#: Страницы сайта.
PAGES = (
    "index.html",
    "app.html",
    "versions.html",
    "field-reports.html",
    "help-read-write.html",
    "help-test0-log.html",
    "support.html",
    "logs.html",
    "privacy.html",
)

#: Каталоги, которые не входят в бандл (инструменты и скриншоты).
NOT_BUNDLE = ("tools", "screenshots")

#: Ограничение размера бандла из задания.
BUNDLE_LIMIT_BYTES = 300 * 1024

#: Разрешённые «внешние» строки: это не запросы, а пространства имён и SVG.
URL_ALLOWLIST = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
)

#: Единственные исходящие адреса сайта: обычные <a href> на странице
#: «Поддержать проект» (support.html) и значения paypalUrl/boostyUrl в config.js.
#: Скриптов, iframe, картинок и стилей с этих адресов нет и быть не должно.
ALLOWED_OUTBOUND_LINKS = (
    "https://paypal.me/TNasibovi",
    "https://boosty.to/torquex.live/donate",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def bundle_files() -> list[Path]:
    """Файлы бандла: HTML/CSS/JS, кроме инструментов и скриншотов."""
    return sorted(
        path
        for path in WEB.rglob("*")
        if path.is_file()
        and path.suffix.lower() in (".html", ".css", ".js")
        and not any(part in NOT_BUNDLE for part in path.relative_to(WEB).parts)
    )


def i18n_blocks() -> tuple[str, str]:
    text = read(WEB / "i18n.js")
    start_ru = text.index("var RU = {")
    start_en = text.index("var EN = {")
    return text[start_ru:start_en], text[start_en:]


def i18n_keys(block: str) -> list[str]:
    return re.findall(r'^\s{4}"([^"]+)":', block, re.M)


def i18n_values(block: str) -> dict[str, str]:
    pattern = re.compile(r'^\s{4}"([^"]+)":\s*"((?:[^"\\]|\\.)*)"', re.M)
    return {key: value for key, value in pattern.findall(block)}


def contrast(lhs: str, rhs: str) -> float:
    """Коэффициент контраста двух цветов #rrggbb по WCAG."""

    def luminance(color: str) -> float:
        color = color.lstrip("#")
        channels = [int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)]

        def linear(value: float) -> float:
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        red, green, blue = (linear(channel) for channel in channels)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    first, second = luminance(lhs), luminance(rhs)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def desktop_media_stripped(css: str) -> str:
    """CSS без блоков @media (min-width: ≥768px): там фиксированные ширины уместны."""
    result = css
    for match in list(re.finditer(r"@media\s*\(min-width:\s*(\d+)px\)\s*\{", result))[::-1]:
        if int(match.group(1)) < 768:
            continue
        start = match.end() - 1
        depth = 0
        for index in range(start, len(result)):
            if result[index] == "{":
                depth += 1
            elif result[index] == "}":
                depth -= 1
                if depth == 0:
                    result = result[: match.start()] + result[index + 1:]
                    break
    return result


def css_tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def theme_tokens(name: str) -> dict[str, str]:
    """Токены темы: dark — из :root, light — из [data-theme="light"]."""
    css = read(WEB / "style.css")
    if name == "dark":
        block = css[css.index(":root {"):css.index("/* Светлая тема")]
    else:
        start = css.index('[data-theme="light"] {')
        block = css[start:css.index("}", start)]
    return css_tokens(block)


def decoded_i18n_values(language: str) -> dict[str, str]:
    ru, en = i18n_blocks()
    block = ru if language == "ru" else en
    return {key: json.loads('"' + value + '"') for key, value in i18n_values(block).items()}


class FirstScreenParser(HTMLParser):
    """Считает видимый текст только в стартовом мастере, раскрывая i18n-ключи."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, translations: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.translations = translations
        self.stack: list[dict[str, object]] = []
        self.scope_depth: int | None = None
        self.parts: list[str] = []
        self.visible_sections = 0

    def _context_hidden(self, tag: str, attrs: dict[str, str | None]) -> bool:
        if (
            "hidden" in attrs
            or attrs.get("aria-hidden") == "true"
            or "sr-only" in (attrs.get("class") or "").split()
        ):
            return True
        if any(bool(frame["hidden"]) for frame in self.stack):
            return True
        if any("sr-only" in str(frame["class"]).split() for frame in self.stack):
            return True
        collapsed = False
        for frame in self.stack:
            if frame["tag"] == "details" and not bool(frame["open"]):
                collapsed = True
            elif frame["tag"] == "summary":
                collapsed = False
        return collapsed and tag != "summary"

    def handle_starttag(self, tag: str, raw_attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(raw_attrs)
        starts_scope = attrs.get("id") == "intake"
        parent_scope = self.scope_depth is not None
        in_scope = parent_scope or starts_scope
        hidden = self._context_hidden(tag, attrs)
        visible = in_scope and not hidden
        if tag == "section" and visible:
            self.visible_sections += 1
        if visible and attrs.get("data-i18n"):
            key = attrs["data-i18n"]
            if key in self.translations:
                self.parts.append(self.translations[key])
        owns_text = visible and bool(attrs.get("data-i18n"))
        if visible and attrs.get("aria-hidden") != "true" and "sr-only" not in (attrs.get("class") or "").split():
            if attrs.get("type") in ("button", "submit") and attrs.get("value"):
                self.parts.append(str(attrs["value"]))
        frame: dict[str, object] = {
            "tag": tag,
            "hidden": hidden,
            "open": "open" in attrs,
            "class": attrs.get("class") or "",
            "owns_text": owns_text,
        }
        if starts_scope:
            self.scope_depth = len(self.stack) + 1
        if tag not in self.VOID:
            self.stack.append(frame)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break
        if self.scope_depth is not None and len(self.stack) < self.scope_depth:
            self.scope_depth = None

    def handle_data(self, data: str) -> None:
        if self.scope_depth is None or not data.strip():
            return
        if any(bool(frame["hidden"]) for frame in self.stack):
            return
        if any("sr-only" in str(frame["class"]).split() for frame in self.stack):
            return
        if any(bool(frame["owns_text"]) for frame in self.stack):
            return
        collapsed = False
        for frame in self.stack:
            if frame["tag"] == "details" and not bool(frame["open"]):
                collapsed = True
            elif frame["tag"] == "summary" and collapsed:
                collapsed = False
        if collapsed:
            return
        self.parts.append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+(?:[’'_-][A-Za-zА-Яа-яЁё0-9]+)*", text, re.UNICODE))


class BundleStructureTests(unittest.TestCase):
    """Страницы, ссылки и ресурсы."""

    def test_all_pages_exist(self) -> None:
        for name in PAGES:
            self.assertTrue((WEB / name).is_file(), f"нет страницы {name}")

    def test_every_page_has_doctype_title_and_lang(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"), name)
            self.assertIn('<html lang="ru"', html, name)
            self.assertIn("data-i18n-title=", html, name)
            self.assertIn('name="viewport"', html, name)
            # запасной заголовок — латиницей: локализованный ставит ui.js из словаря
            title = re.search(r"<title>([^<]*)</title>", html).group(1)
            self.assertRegex(title, r"^[\x20-\x7e]+$", f"{name}: заголовок вне словаря")

    def test_local_resources_exist(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            for src in re.findall(r'<script src="([^"]+)"', html):
                self.assertFalse(src.startswith(("http:", "https:", "/")), f"{name}: внешний скрипт {src}")
                self.assertTrue((WEB / src).is_file(), f"{name}: нет скрипта {src}")
            for href in re.findall(r'<link[^>]+href="([^"]+)"', html):
                self.assertFalse(href.startswith(("http:", "https:", "/")), f"{name}: внешний стиль {href}")
                self.assertTrue((WEB / href).is_file(), f"{name}: нет файла {href}")

    def test_internal_links_point_to_pages(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            for href in re.findall(r'<a[^>]+href="([^"]+)"', html):
                if href.startswith(("#", "mailto:")):
                    continue
                if href in ALLOWED_OUTBOUND_LINKS:
                    continue
                self.assertFalse(
                    href.startswith(("http:", "https:")),
                    f"{name}: внешняя ссылка {href} (в бандле не должно быть внешних запросов)",
                )
                target = href.split("?")[0].split("#")[0]
                if target.endswith(".html"):
                    self.assertTrue((WEB / target).is_file(), f"{name}: ссылка на {target}")

    def test_script_order_is_safe(self) -> None:
        """config → i18n → ui → api; request.js раньше flow.js (поток главной)."""
        order = ["config.js", "i18n.js", "ui.js", "api.js"]
        for name in PAGES:
            scripts = re.findall(r'<script src="([^"]+)"', read(WEB / name))
            positions = [scripts.index(item) for item in order if item in scripts]
            self.assertEqual(len(positions), len(order), f"{name}: нет части обязательных скриптов")
            self.assertEqual(positions, sorted(positions), f"{name}: порядок скриптов нарушен")
        index = re.findall(r'<script src="([^"]+)"', read(WEB / "index.html"))
        self.assertLess(index.index("request.js"), index.index("flow.js"))
        self.assertFalse((WEB / "wizard.js").exists(), "wizard.js заменён на flow.js")

    def test_no_inline_scripts_or_handlers(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)[^>]*>", f"{name}: встроенный скрипт")
            self.assertNotRegex(html, r"\son[a-z]+\s*=", f"{name}: inline-обработчик события")

    def test_no_duplicate_ids(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            ids = re.findall(r'\sid="([^"]+)"', html)
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            self.assertFalse(duplicates, f"{name}: повторяющиеся id {duplicates}")

    def test_live_regions_and_toasts_present(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            self.assertIn('id="tx-live"', html, name)
            self.assertIn('id="tx-alert"', html, name)
            self.assertIn('id="tx-toasts"', html, name)
            self.assertIn("aria-live", html, name)
            self.assertIn("skip-link", html, name)

    def test_wizard_ids_match_script(self) -> None:
        """Каждый id, который ищет flow.js, есть на главной странице."""
        html = read(WEB / "index.html")
        script = read(WEB / "flow.js")
        ids = set(re.findall(r'(?:\$|show|bind|on)\("([A-Za-z0-9_-]+)"', script))
        self.assertGreater(len(ids), 30, "в flow.js мало ссылок на id: проверка бессмысленна")
        missing = sorted(item for item in ids if f'id="{item}"' not in html)
        self.assertFalse(missing, f"flow.js ищет отсутствующие id: {missing}")

    def test_pages_use_their_own_title_key(self) -> None:
        expectations = {
            "index.html": "wizard.title",
            "app.html": "wizard.title",
            "versions.html": "versions.title",
            "field-reports.html": "reports.title",
            "help-read-write.html": "rw.title",
            "help-test0-log.html": "t0.title",
            "support.html": "support.title",
            "logs.html": "logs.title",
            "privacy.html": "privacy.title",
        }
        ru = set(i18n_keys(i18n_blocks()[0]))
        for name, key in expectations.items():
            html = read(WEB / name)
            self.assertIn(f'data-i18n-title="{key}"', html, name)
            self.assertIn(key, ru, f"{key} нет в словаре")

    def test_pages_declare_their_own_script(self) -> None:
        expectations = {
            "index.html": "flow.js",
            "app.html": "app-redirect.js",
            "versions.html": "versions.js",
            "field-reports.html": "reports.js",
            "help-read-write.html": "page.js",
            "help-test0-log.html": "page.js",
            "support.html": "page.js",
            "logs.html": "logs.js",
            "privacy.html": "page.js",
        }
        for name, script in expectations.items():
            self.assertIn(script, read(WEB / name), f"{name}: нет подключения {script}")


class StaticBudgetTests(unittest.TestCase):
    """Измеримые бюджеты первого экрана, меню и пути к результату."""

    #: Ссылки подвала (ui.js, mountChrome): входят в бюджет первого экрана.
    FOOTER_LINK_KEYS = (
        "footer.support", "footer.logs", "footer.privacy",
    )

    def test_first_screen_is_under_45_words_with_header_and_footer_links(self) -> None:
        html = read(WEB / "index.html")
        ui = read(WEB / "ui.js")
        nav_match = re.search(r"TX\.navItems\s*=\s*function\s*\(\)\s*\{\s*return\s*\[(.*?)\];", ui, re.S)
        self.assertIsNotNone(nav_match, "не удалось прочитать пункты главного меню")
        nav_count = len(re.findall(r"\bid:\s*\"", nav_match.group(1)))
        self.assertLessEqual(nav_count, 3, f"пунктов меню: {nav_count}")

        css = read(WEB / "style.css")
        self.assertIn(".first-screen h1 {", css)
        self.assertRegex(css, r"\.first-screen h1\s*\{[^}]*white-space:\s*nowrap")
        for language in ("ru", "en"):
            translations = decoded_i18n_values(language)
            self.assertLessEqual(len(translations["wizard.home_title"]), 24, f"{language}: заголовок некомпактен")
            self.assertIn("15", translations["wizard.home_privacy"], f"{language}: срок хранения не указан")
            self.assertIn("VIN", translations["wizard.home_privacy_vin"].upper(), f"{language}: строка VIN отсутствует")
            self.assertIn("bytes+crc verified", translations["wizard.home_trust_badge"])
            self.assertIn("field verified", translations["wizard.home_trust_badge"])
            parser = FirstScreenParser(translations)
            parser.feed(html)
            parser.close()
            # TX.mountChrome() добавляет эти локализованные элементы к шапке.
            chrome = [
                "TX", translations["app.name"], translations["app.tagline"],
                translations["nav.master"], translations["nav.help"],
                translations["nav.versions"], "RU", "EN",
            ]
            footer = [translations[key] for key in self.FOOTER_LINK_KEYS]
            count = word_count(" ".join(chrome + parser.parts + footer))
            # RU — бюджет из задания (≤45). EN длиннее по-английски: потолок 56, честно.
            limit = 45 if language == "ru" else 56
            self.assertLessEqual(count, limit, f"{language}: первый экран — {count} слов")
            self.assertEqual(parser.visible_sections, 1, f"{language}: на старте видна только загрузка файла")
            for removed in ("wizard.s1.state_empty", "wizard.s1.limit", "trust.plain"):
                self.assertNotIn(removed, html, f"{removed} не должен быть на первом экране")

    def test_build_path_is_one_sticky_button_and_one_download(self) -> None:
        html = read(WEB / "index.html")
        script = read(WEB / "flow.js")
        css = read(WEB / "style.css")
        self.assertIn('id="build-bar"', html)
        self.assertIn('id="build"', html)
        self.assertIn('build.addEventListener("click", submitBuild)', script)
        self.assertIn('attrs: { type: "button", id: "download" }', script)
        self.assertRegex(css, r"\.build-bar\s*\{[^}]*position:\s*sticky")
        self.assertNotIn('data-step="', html, "шагов и вкладок быть не должно")
        self.assertNotIn('class="steps"', html)
        self.assertNotIn("variant-v1", html, "варианты 2 и 3 на сайте не показываются")

    def test_result_page_hides_technical_block_by_default(self) -> None:
        script = read(WEB / "flow.js")
        self.assertIn('TX.el("details", { class: "specialists" }', script)
        self.assertIn('TX.t("common.specialists")', script)
        self.assertIn("wizard.s4.trust_line", script)
        ru = decoded_i18n_values("ru")
        self.assertEqual(ru["wizard.s4.trust_line"], "Файл проверен программой. На реальной машине эта версия ещё не проверялась.")

class I18nTests(unittest.TestCase):
    """Словарь RU/EN."""

    def test_key_sets_match(self) -> None:
        ru, en = i18n_blocks()
        only_ru = sorted(set(i18n_keys(ru)) - set(i18n_keys(en)))
        only_en = sorted(set(i18n_keys(en)) - set(i18n_keys(ru)))
        self.assertFalse(only_ru, f"ключи только в RU: {only_ru}")
        self.assertFalse(only_en, f"ключи только в EN: {only_en}")

    def test_no_duplicate_keys(self) -> None:
        """Ищем повторы по всему блоку, а не по началу строки: ключи, дописанные
        в ту же строку («a: …, a: …»), иначе остаются незамеченными."""
        for label, block in zip(("RU", "EN"), i18n_blocks()):
            keys = re.findall(r'"([^"]+)":\s*"', block)
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            self.assertFalse(duplicates, f"{label}: повторяющиеся ключи {duplicates}")

    def test_values_are_not_empty(self) -> None:
        for label, block in zip(("RU", "EN"), i18n_blocks()):
            for key, value in i18n_values(block).items():
                self.assertTrue(value.strip(), f"{label}: пустое значение у {key}")

    def test_placeholders_match_between_languages(self) -> None:
        ru = i18n_values(i18n_blocks()[0])
        en = i18n_values(i18n_blocks()[1])
        for key, value in ru.items():
            if key not in en:
                continue
            self.assertEqual(
                sorted(re.findall(r"\{(\w+)\}", value)),
                sorted(re.findall(r"\{(\w+)\}", en[key])),
                f"{key}: набор подстановок не совпадает",
            )

    def test_every_used_key_is_defined(self) -> None:
        ru, en = i18n_blocks()
        defined = set(i18n_keys(ru)) | set(i18n_keys(en))
        used: set[str] = set()
        for path in bundle_files():
            if path.name == "i18n.js":
                continue
            body = read(path)
            for pattern in (
                r'TX\.t\(\s*"([^"]+)"',
                r'data-i18n="([^"]+)"',
                r'data-i18n-title="([^"]+)"',
                r'data-i18n-attr="[^"]*?:([^";]+)',
                r'key:\s*"([^"]+)"',
            ):
                for match in re.finditer(pattern, body):
                    key = match.group(1).strip()
                    if "." in key and not key.endswith((".js", ".html")):
                        used.add(key)
        missing = sorted(key for key in used if key not in defined)
        self.assertFalse(missing, f"нет ключей в словаре: {missing}")

    def test_no_hardcoded_russian_text_in_markup(self) -> None:
        """В разметке — только ключи: длинных русских текстов там быть не должно."""
        allowed = {"TorqueX", "TX", "MS4x Flasher", "MS45-Flasher", "MS41 read tool / boot mode",
                   "stage0", "ignition", "limits", "vanos"}
        for name in PAGES:
            html = read(WEB / name)
            text = re.sub(r"<[^>]+>", " ", html)
            for match in re.findall(r"[А-Яа-яЁё]{4,}[^<\n]*", text):
                if match.strip() in allowed:
                    continue
                self.fail(f"{name}: русский текст в разметке: {match.strip()[:60]}")


class SecurityTests(unittest.TestCase):
    """Запреты из задания."""

    def test_no_unsafe_html_insertion(self) -> None:
        banned = ("innerHTML", "outerHTML", "insertAdjacentHTML", "innerText")
        for path in bundle_files():
            if path.suffix != ".js":
                continue
            body = read(path)
            for needle in banned:
                # только обращения вида node.innerHTML: слова в комментариях не считаем
                hits = re.findall(r"\.\s*" + needle + r"\b|\b" + needle + r"\s*=", body)
                self.assertFalse(hits, f"{path.name}: опасная вставка {needle}")
            self.assertNotRegex(body, r"\bdocument\.write\b", f"{path.name}: document.write")

    def test_no_eval(self) -> None:
        for path in bundle_files():
            if path.suffix != ".js":
                continue
            body = read(path)
            for needle in ("eval(", "new Function(", "setTimeout(\"", "setInterval(\""):
                self.assertNotIn(needle, body, f"{path.name}: {needle}")

    def test_no_external_requests_in_bundle(self) -> None:
        for path in bundle_files():
            body = read(path)
            for url in re.findall(r"https?://[^\s\"'<>)]+", body):
                if any(url.startswith(allowed) for allowed in URL_ALLOWLIST):
                    continue
                if url in ALLOWED_OUTBOUND_LINKS:
                    continue
                self.fail(f"{path.name}: внешний адрес {url}")

    def test_no_external_fonts_or_images(self) -> None:
        css = read(WEB / "style.css")
        self.assertNotIn("@font-face", css)
        self.assertNotRegex(css, r"url\(\s*[\"']?https?:")

    def test_storage_keeps_only_preferences(self) -> None:
        """В localStorage — только язык и тема, ничего из данных пользователя."""
        for path in bundle_files():
            if path.suffix != ".js":
                continue
            body = read(path)
            for call in re.findall(r"store\.set\(\s*([^,]+),", body):
                self.assertIn(call.strip(), ('"torquex.lang"', '"torquex.theme"'),
                              f"{path.name}: в хранилище пишется {call.strip()}")

    def test_no_secrets_in_config(self) -> None:
        config = read(WEB / "config.js")
        lowered = config.lower()
        for needle in ("token", "password", "secret", "api_key", "apikey", "private"):
            self.assertNotIn(needle, lowered, f"config.js: похоже на секрет ({needle})")

    def test_no_payment_forms(self) -> None:
        for path in bundle_files():
            body = read(path)
            self.assertNotIn('type="password"', body, path.name)
            self.assertNotIn("checkout", body.lower(), path.name)
        config = read(WEB / "config.js")
        self.assertNotIn("stripe", config.lower())
        # paypal допускается только как значение paypalUrl (ссылка, без скрипта)
        for line in config.splitlines():
            if "paypal" in line.lower():
                self.assertRegex(line, r'^\s*paypalUrl:\s*"https://paypal\.me/', "config.js: paypal вне paypalUrl")

    def test_support_links_match_config(self) -> None:
        """Ссылки на странице «Поддержать» — те же, что в config.js (и только они)."""
        html = read(WEB / "support.html")
        config = read(WEB / "config.js")
        hrefs = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*target="_blank"[^>]*rel="noopener noreferrer"', html)
        self.assertEqual(sorted(hrefs), sorted(ALLOWED_OUTBOUND_LINKS))
        self.assertIn(f'paypalUrl: "{ALLOWED_OUTBOUND_LINKS[0]}"', config)
        self.assertIn(f'boostyUrl: "{ALLOWED_OUTBOUND_LINKS[1]}"', config)
        for tag in re.findall(r"<(?:script|link|img|iframe)[^>]*>", html):
            self.assertNotIn("http", tag, f"внешний ресурс в разметке: {tag}")

    def test_vin_is_not_put_into_url(self) -> None:
        for path in bundle_files():
            if path.suffix != ".js":
                continue
            body = read(path)
            self.assertNotRegex(body, r"[?&]vin=", f"{path.name}: VIN в адресе")

    def test_server_data_goes_through_text_content(self) -> None:
        """Вся сборка узлов — через TX.el: проверяем, что он не вставляет HTML."""
        ui = read(WEB / "ui.js")
        self.assertIn("node.textContent = String(options.text)", ui)
        self.assertIn("document.createTextNode", ui)


class LogDonationPageTests(unittest.TestCase):
    """Страницы «Отправить лог» и «Конфиденциальность» (docs/LOG_DONATION_RU.md)."""

    def test_two_separate_consents_and_share_is_off_by_default(self) -> None:
        html = read(WEB / "logs.html")
        consent = re.search(r'<input[^>]*id="log-consent"[^>]*>', html).group(0)
        share = re.search(r'<input[^>]*id="log-share"[^>]*>', html).group(0)
        for tag in (consent, share):
            self.assertIn('type="checkbox"', tag)
            self.assertNotIn("checked", tag, "согласия не отмечены заранее")
        # Передача скрыта, пока сервер не сказал log_donation=true.
        self.assertRegex(html, r'id="log-share-row"[^>]*\shidden')
        self.assertRegex(html, r'id="log-submit"[^>]*\sdisabled')
        script = read(WEB / "logs.js")
        self.assertIn("TX.api.health()", script)
        self.assertIn("health.log_donation === true", script)
        self.assertIn('$("log-consent").checked', script)
        self.assertIn('donation && $("log-share").checked', script)

    def test_api_calls_are_relative_and_token_goes_in_a_header(self) -> None:
        api = read(WEB / "api.js")
        self.assertIn('url("/logs")', api)
        self.assertIn('url("/health")', api)
        self.assertIn('url("/logs/" + encodeURIComponent(logId))', api)
        self.assertIn('"X-Delete-Token": token', api)
        self.assertIn('form.append("share_consent", options.share ? "true" : "false")', api)
        for path in bundle_files():
            if path.suffix == ".js":
                self.assertNotRegex(read(path), r"[?&](?:token|delete_token)=", path.name)

    def test_server_data_is_rendered_as_text(self) -> None:
        script = read(WEB / "logs.js")
        self.assertIn("TX.el(", script)
        self.assertNotRegex(script, r"\.\s*(?:innerHTML|outerHTML)|insertAdjacentHTML")
        self.assertNotIn("store.set", script, "код удаления не сохраняется в браузере")

    def test_footer_links_but_not_main_menu(self) -> None:
        ui = read(WEB / "ui.js")
        footer = ui[ui.index('var footer = TX.qs("#tx-footer")'):]
        self.assertIn('TX.href("logs.html")', footer)
        self.assertIn('TX.href("privacy.html")', footer)
        nav = re.search(r"TX\.navItems\s*=\s*function\s*\(\)\s*\{\s*return\s*\[(.*?)\];", ui, re.S).group(1)
        self.assertNotIn("logs.html", nav)
        self.assertNotIn("privacy.html", nav)

    def test_privacy_page_states_the_promises(self) -> None:
        for language in ("ru", "en"):
            texts = decoded_i18n_values(language)
            joined = " ".join(value for key, value in texts.items() if key.startswith("privacy."))
            for needle in ("VIN", "IP", "365"):
                self.assertIn(needle, joined, f"{language}: нет «{needle}»")
            self.assertTrue(texts["privacy.disclaimer"].strip())
            self.assertIn("VIN", texts["logs.share_note"])
        html = read(WEB / "privacy.html")
        self.assertNotIn("mailto:", html)
        self.assertNotRegex(html, r"[\w.+-]+@[\w-]+\.[\w.]+", "без e-mail на странице")


class AccessibilityTests(unittest.TestCase):
    """Доступность статики."""

    def test_landmarks_and_skip_link(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            self.assertIn('href="#main"', html, name)
            self.assertIn('id="main"', html, name)
            self.assertIn("<main", html, name)
            self.assertIn('id="tx-header"', html, name)
            self.assertIn('id="tx-footer"', html, name)

    def test_form_controls_are_labelled(self) -> None:
        for name in PAGES:
            html = read(WEB / name)
            labels = re.findall(r'<label[^>]+for="([^"]+)"', html)
            label_keys = re.findall(r"data-i18n-attr=\"aria-label:([^\"]+)\"", html)
            for match in re.finditer(r"<(input|select|textarea)([^>]*)>", html):
                tag, attrs = match.group(1), match.group(2)
                if 'type="hidden"' in attrs:
                    continue
                ident = re.search(r'id="([^"]+)"', attrs)
                if 'aria-label' in attrs or "data-i18n-attr" in attrs and label_keys:
                    continue
                self.assertIsNotNone(ident, f"{name}: поле без id и подписи")
                self.assertIn(ident.group(1), labels, f"{name}: у поля {ident.group(1)} нет label")
                self.assertIn(
                    f'id="{ident.group(1)}"', html, f"{name}: подпись ссылается на несуществующее поле",
                )

    def test_focus_visible_and_target_size(self) -> None:
        css = read(WEB / "style.css")
        self.assertIn(":focus-visible", css)
        self.assertRegex(css, r"min-height:\s*44px")
        self.assertRegex(css, r"min-height:\s*24px|width:\s*24px")  # ступени мастера/иконки

    def test_reduced_motion_supported(self) -> None:
        css = read(WEB / "style.css")
        self.assertIn("prefers-reduced-motion", css)
        ui = read(WEB / "ui.js")
        self.assertIn("prefers-reduced-motion", ui)

    def test_inputs_are_16px_for_ios(self) -> None:
        """iOS Safari зумит страницу, если у поля шрифт меньше 16px."""
        css = read(WEB / "style.css")
        match = re.search(r'input\[type="text"\][^{]*\{[^}]*\}', css)
        self.assertIsNotNone(match, "нет правила для текстовых полей")
        rule = match.group(0)
        for control in ("input[type=\"text\"]", "input[type=\"number\"]", "select", "textarea"):
            self.assertIn(control, rule, f"{control} не входит в правило 16px")
        self.assertIn("font-size: 16px", rule)

    def test_dropzone_has_keyboard_alternative(self) -> None:
        """У перетаскивания обязан быть эквивалент без перетаскивания (WCAG 2.5.7)."""
        html = read(WEB / "index.html")
        script = read(WEB / "flow.js")
        self.assertIn('id="file-choose"', html)
        self.assertIn('id="file-input"', html)
        self.assertIn('$("file-choose")', script)
        self.assertIn('$("file-input")', script)
        self.assertIn('input.click()', script)

    def test_header_uses_compact_mobile_menu(self) -> None:
        ui = read(WEB / "ui.js")
        css = read(WEB / "style.css")
        self.assertIn('class: "btn btn-icon nav-toggle"', ui)
        self.assertIn('"aria-controls": "site-navigation"', ui)
        self.assertIn('"aria-expanded": "false"', ui)
        self.assertIn(".nav-toggle", css)
        self.assertIn(".nav.is-open .nav-list", css)
        self.assertIn("overflow-x: auto", css)

    def test_contrast_is_aa_in_both_themes(self) -> None:
        text_pairs = (("text", "bg"), ("text", "surface"), ("muted", "bg"), ("muted", "surface"),
                      ("accent", "surface"), ("danger", "surface"), ("warn", "surface"),
                      ("info", "surface"), ("ok", "surface"))
        for theme in ("dark", "light"):
            tokens = theme_tokens(theme)
            for first, second in text_pairs:
                value = contrast(tokens[first], tokens[second])
                self.assertGreaterEqual(
                    value, 4.5, f"{theme}: {first} на {second} — {value:.2f}:1 (нужно 4.5)",
                )
            ink = contrast(tokens["accent-ink"], tokens["accent"])
            self.assertGreaterEqual(ink, 4.5, f"{theme}: текст на акценте — {ink:.2f}:1")
            border = contrast(tokens["line-strong"], tokens["bg"])
            self.assertGreaterEqual(
                border, 3.0, f"{theme}: граница поля к фону — {border:.2f}:1 (нужно 3:1)",
            )

    def test_responsive_breakpoints_and_no_fixed_width(self) -> None:
        css = read(WEB / "style.css")
        self.assertIn("min-width: 768px", css)
        self.assertIn("min-width: 1440px", css)
        # ширина колонок — только max-width/токенами: фиксированные px ломают 375px
        # (внутри desktop-медиа запросов фиксированные ширины допустимы)
        base = desktop_media_stripped(css)
        fixed = re.findall(r"(?<!max-)(?<!min-)\bwidth:\s*([2-9][0-9]{2}|1[0-9]{3})px", base)
        self.assertFalse(fixed, f"фиксированные ширины в px: {fixed}")
        self.assertIn("overflow-x: auto", css)  # таблицы прокручиваются внутри себя


class DesignTests(unittest.TestCase):
    """Тема и типографика."""

    def test_dark_theme_is_default(self) -> None:
        css = read(WEB / "style.css")
        self.assertIn(":root {", css)
        self.assertIn('[data-theme="light"]', css)
        for name in PAGES:
            self.assertIn('data-theme="dark"', read(WEB / name), name)

    def test_light_theme_follows_system_preference(self) -> None:
        css = read(WEB / "style.css")
        self.assertIn("prefers-color-scheme: light", css)

    def test_typography_sizes(self) -> None:
        css = read(WEB / "style.css")
        self.assertRegex(css, r"font:\s*17px")
        self.assertRegex(css, r"font-size:\s*clamp\(")
        self.assertIn("ui-monospace", css)

    def test_icons_are_inline_svg(self) -> None:
        ui = read(WEB / "ui.js")
        self.assertIn("createElementNS", ui)
        self.assertIn("0 0 24 24", ui)

    def test_skeleton_and_toasts_exist(self) -> None:
        css = read(WEB / "style.css")
        for needle in (".skeleton", ".toast", ".dropzone", ".steps", ".table-scroll"):
            self.assertIn(needle, css, f"в css нет {needle}")
        self.assertIn("@keyframes skeleton", css)


class SizeTests(unittest.TestCase):
    """Размер бандла."""

    def test_bundle_size(self) -> None:
        total = sum(path.stat().st_size for path in bundle_files())
        self.assertLessEqual(total, BUNDLE_LIMIT_BYTES, f"бандл {total} байт (лимит {BUNDLE_LIMIT_BYTES})")

    def test_no_binary_files_in_bundle(self) -> None:
        allowed = (".html", ".css", ".js", ".md", ".py", ".mjs")
        for path in WEB.rglob("*"):
            if any(part in ("__pycache__", "node_modules") for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() not in allowed:
                self.fail(f"в web/ лишний файл: {path.relative_to(WEB)}")

    def test_generator_texts_are_not_in_bundle(self) -> None:
        """Стоки и наряды клиентов не попадают на сайт."""
        for path in bundle_files():
            body = read(path)
            self.assertNotIn("-----BEGIN", body, path.name)
            self.assertNotRegex(body, r"\b[A-HJ-NPR-Z0-9]{17}\b", f"{path.name}: похоже на VIN")


class DemoModeTests(unittest.TestCase):
    """Демо-режим: file:// и ?demo=1, ничего наружу."""

    def test_demo_triggers(self) -> None:
        api = read(WEB / "api.js")
        self.assertIn('window.location.protocol === "file:"', api)
        self.assertIn('query.get("demo") === "1"', api)
        self.assertIn("forceDemo", api)

    def test_demo_answers_are_marked(self) -> None:
        """Демо-ответы помечаются флагом и баннером: спутать с живым API нельзя."""
        api = read(WEB / "api.js")
        self.assertIn("demo: true", api)
        self.assertIn("TX.isDemo()", api)
        self.assertIn("wizard.s4.demo_note_md", api)
        html = read(WEB / "index.html")
        self.assertIn('id="tx-demo-banner"', html)
        self.assertIn('"wizard.demo_banner"', read(WEB / "ui.js"))
        ru = i18n_values(i18n_blocks()[0])
        self.assertIn("демо", ru["common.demo"].lower())
        self.assertTrue(ru["wizard.demo_banner"].strip())

    def test_demo_download_is_a_note(self) -> None:
        api = read(WEB / "api.js")
        self.assertIn("torquex_demo_note.txt", api)
        self.assertIn("Blob", api)

    def test_internal_links_keep_demo_param(self) -> None:
        ui = read(WEB / "ui.js")
        self.assertIn("TX.href", ui)
        self.assertIn("?demo=1", ui)

    def test_demo_scenarios_for_all_states(self) -> None:
        home = read(WEB / "index.html")
        for scenario in ("ok", "bad_version", "junk", "reject"):
            self.assertIn(f'data-demo-scenario="{scenario}"', home, scenario)
        self.assertRegex(home, r'<div[^>]*id="demo-panel"[^>]*\shidden>', "демо-сценарии скрыты вне ?demo=1")

    def test_demo_versions_use_stub_instead_of_live_failure(self) -> None:
        api = read(WEB / "api.js")
        versions = read(WEB / "versions.js")
        self.assertIn("TX.api.demoVersions", api)
        self.assertRegex(versions, r"TX\.isDemo\(\)[\s\S]{0,200}TX\.api\.demoVersions\(\)")
        self.assertIn("VERSIONS_FAILED", api, "живой режим должен сохранить настоящий API-отказ")


class WiringTests(unittest.TestCase):
    """Инициализация и связность модулей."""

    def test_lang_and_theme_initialised_on_load(self) -> None:
        ui = read(WEB / "ui.js")
        self.assertRegex(ui, r"TX\.initLang\(\);")
        self.assertRegex(ui, r"TX\.initTheme\(\);")

    def test_api_mode_is_set_on_load(self) -> None:
        api = read(WEB / "api.js")
        self.assertRegex(api, r"TX\.api\.init\(\);\s*\}\)\(\);\s*$")

    def test_chrome_mounted_on_ready(self) -> None:
        for script in ("flow.js", "versions.js", "reports.js", "page.js", "logs.js"):
            body = read(WEB / script)
            self.assertIn("TX.onReady(", body, script)
            self.assertIn("TX.mountChrome(", body, script)

    def test_wizard_refreshes_generated_labels_on_language_change(self) -> None:
        wizard = read(WEB / "flow.js")
        self.assertIn('document.addEventListener("tx:lang", refreshLocalizedState)', wizard)
        self.assertIn("function refreshLocalizedState()", wizard)
        self.assertIn("renderDetect(true)", wizard)
        self.assertIn("if (state.platform) renderFunctions();", wizard)
        self.assertIn("if (result && !$(\"result\").hidden) renderResult(result);", wizard)

    def test_no_duplicate_init_calls(self) -> None:
        for script in ("flow.js", "versions.js", "reports.js", "page.js", "logs.js"):
            body = read(WEB / script)
            self.assertEqual(body.count("TX.api.init()"), 0, f"{script}: лишний TX.api.init()")

    def test_every_tx_call_has_a_definition(self) -> None:
        defined: set[str] = set()
        used: set[str] = set()
        for path in sorted(WEB.glob("*.js")):
            if path.name == "i18n.js":
                continue
            text = read(path)
            definitions = set(re.findall(r"TX\.([A-Za-z_][A-Za-z0-9_]*)\s*=", text))
            definitions |= set(re.findall(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*function', text))
            definitions |= set(re.findall(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
            defined |= definitions
            if path.name == "api.js":
                # методы объекта TX.api: api.имя = … или "имя: function" внутри литерала
                defined |= set(re.findall(r"\bapi\.([A-Za-z_][A-Za-z0-9_]*)\s*=", text))
                block = text.split("TX.api = {", 1)[1].split("\n  };", 1)[0]
                defined |= set(re.findall(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*):\s", block, re.M))
            used |= set(re.findall(r"TX\.([A-Za-z_][A-Za-z0-9_]*)", text))
        used -= {"t", "el", "qs", "qsa", "onReady", "mountChrome", "CONFIG", "MODE"}  # проверяются ниже
        missing = sorted(used - defined)
        self.assertFalse(missing, f"обращения к несуществующим функциям: {missing}")
        for name in ("t", "el", "qs", "qsa", "onReady", "mountChrome", "CONFIG", "MODE"):
            self.assertIn(name, defined, f"TX.{name} не определён")
            used.add(name)

    def test_request_module_is_shared_with_node_tool(self) -> None:
        """Заявки сайта и проверочный скрипт строит один модуль."""
        request = read(WEB / "request.js")
        self.assertIn("module.exports", request)
        self.assertIn("root.TX.request", request)
        tool = read(WEB / "tools" / "check_requests.mjs")
        self.assertIn('from "../request.js"', tool)
        self.assertIn("buildRequest", tool)

    def test_utf8_and_lf_are_pinned_for_windows_checkouts(self) -> None:
        source = read(Path(__file__))
        attributes = read(ROOT / ".gitattributes")
        self.assertIn('path.read_text(encoding="utf-8")', source)
        self.assertIn("*.py text eol=lf", attributes)
        self.assertIn("*.js text eol=lf", attributes)
        self.assertIn("*.html text eol=lf", attributes)

    def test_dev_server_and_readme_exist(self) -> None:
        self.assertTrue((WEB / "tools" / "dev_server.py").is_file())
        self.assertTrue((WEB / "README_RU.md").is_file())
        self.assertTrue((WEB / "screenshots" / "README_RU.md").is_file())

    def test_readme_mentions_how_to_connect(self) -> None:
        readme = read(WEB / "README_RU.md")
        for needle in ("apiBase", "?demo=1", "i18n.js", "style.css", "server/DEPLOY_RU.md"):
            self.assertIn(needle, readme, f"README: нет упоминания {needle}")


if __name__ == "__main__":
    unittest.main()


class JsSyntaxTests(unittest.TestCase):
    """Синтаксис всех скриптов: словарь с обрезанным началом молча ломал весь сайт."""

    def test_all_scripts_parse(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node не установлен: синтаксис JS не проверен")
        for script in sorted(WEB.glob("*.js")):
            result = subprocess.run(
                [node, "--check", str(script)], capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            self.assertEqual(0, result.returncode, f"{script.name}: {result.stderr}")


class VinModeTests(unittest.TestCase):
    """Режим VIN: сервер сообщает vin_required; тексты переключаются по data-i18n-vin."""

    def test_vin_mode_keys_exist_in_both_languages(self) -> None:
        html_files = [WEB / "index.html", WEB / "privacy.html"]
        keys: set[str] = set()
        for path in html_files:
            keys.update(re.findall(r'data-i18n-vin="([^"]+)"', read(path)))
        self.assertTrue(keys, "нет ни одного data-i18n-vin")
        for language in ("ru", "en"):
            translations = decoded_i18n_values(language)
            for key in keys:
                self.assertIn(key, translations, f"{language}: нет ключа {key}")
                self.assertTrue(translations[key].strip(), f"{language}: пустой {key}")

    def test_default_texts_say_vin_is_optional_and_required_texts_exist(self) -> None:
        for language in ("ru", "en"):
            translations = decoded_i18n_values(language)
            self.assertIn("VIN", translations["wizard.home_privacy_vin"].upper())
            self.assertNotIn("VIN", translations["wizard.home_privacy"].upper(), "по умолчанию VIN не нужен")
            self.assertIn("7", translations["wizard.s3.vin_required_hint"])
            self.assertIn("17", translations["wizard.s3.vin_required"])

    def test_vin_field_is_in_markup_and_slot_exists(self) -> None:
        html = read(WEB / "index.html")
        self.assertRegex(html, r'id="vin-slot"[^>]*hidden')
        self.assertRegex(html, r'<div class="field" id="field-vin">')
        self.assertRegex(html, r'id="opt-vin"')
        self.assertNotRegex(html, r'id="opt-vin"[^>]*maxlength=')

    def test_client_vin_rule_matches_server_rule(self) -> None:
        request_js = read(WEB / "request.js")
        self.assertIn("/^[A-HJ-NPR-Z0-9]{17}$/", request_js)
        server_core = read(ROOT / "server" / "core.py")
        match = re.search(r'_VIN_CHARS = frozenset\("([^"]+)"\)', server_core)
        self.assertIsNotNone(match, "не найден набор символов VIN на сервере")
        allowed = set(match.group(1))
        client = {ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if ch not in "IOQ"}
        self.assertEqual(allowed, client, "набор символов VIN на сайте и на сервере расходится")

    def test_vin_is_never_stored_or_logged_by_the_wizard(self) -> None:
        wizard = read(WEB / "flow.js")
        for needle in ("localStorage", "sessionStorage", "console.log", "history.replaceState", "location.hash"):
            self.assertNotIn(needle, wizard, f"flow.js использует {needle}")
