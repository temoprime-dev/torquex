/* TorqueX-сайт: общий слой интерфейса.
 *
 * Здесь: безопасное построение DOM (никакого innerHTML с данными), словарь,
 * язык и тема с сохранением в localStorage, шапка/подвал, безопасный рендер
 * markdown-наряда, тосты и скромный aria-live.
 *
 * Безопасность: единственный способ добавить текст — textContent. Скрипты не
 * вставляют HTML-строки; markdown превращается в узлы DOM, а не в разметку.
 */
(function () {
  "use strict";

  var TX = window.TX || (window.TX = {});
  var CONFIG = window.TORQUEX_CONFIG || {};
  var I18N = window.TORQUEX_I18N || { ru: {}, en: {} };

  TX.CONFIG = CONFIG;
  TX.MODE = "live";           /* "live" | "demo" — выставляет api.js */
  TX.lang = "ru";
  TX.theme = "dark";

  /* ---------- localStorage: приватный режим не должен ломать сайт ---------- */

  TX.store = {
    get: function (key) {
      try {
        return window.localStorage.getItem(key);
      } catch (error) {
        return null;
      }
    },
    set: function (key, value) {
      try {
        window.localStorage.setItem(key, value);
      } catch (error) {
        /* приватный режим или запрет хранилища — просто не запоминаем */
      }
    },
  };

  /* ---------- DOM ---------- */

  TX.qs = function (selector, root) {
    return (root || document).querySelector(selector);
  };

  TX.qsa = function (selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  };

  /* Строит узел. options: {class, text, attrs, on, children}; третий аргумент —
   * то же, что children (чтобы вызовы вида TX.el("p", {…}, [узел]) читались). */
  TX.el = function (tag, options, children) {
    var node = document.createElement(tag);
    options = options || {};
    if (options.class) node.className = options.class;
    if (options.text != null) node.textContent = String(options.text);
    var attrs = options.attrs || {};
    Object.keys(attrs).forEach(function (name) {
      if (attrs[name] == null) return;
      node.setAttribute(name, String(attrs[name]));
    });
    (options.on || []).forEach(function (pair) {
      node.addEventListener(pair[0], pair[1]);
    });
    var list = (options.children || []).concat(children || []);
    list.forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  };

  TX.clear = function (node) {
    if (!node) return node;
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  };

  TX.fill = function (node, children) {
    TX.clear(node);
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  };

  /* ---------- форматирование ---------- */

  TX.formatBytes = function (bytes) {
    if (bytes == null || isNaN(bytes)) return "—";
    if (bytes < 1024) return bytes + " Б";
    var kib = bytes / 1024;
    if (kib < 1024) return (Math.round(kib * 10) / 10) + " КиБ";
    return (Math.round((kib / 1024) * 100) / 100) + " МиБ";
  };

  TX.limitLabel = function () {
    return TX.formatBytes(CONFIG.maxUploadBytes || 1048576);
  };

  TX.shortHash = function (hash, length) {
    var text = String(hash || "");
    var size = length || 12;
    return text.length > size ? text.slice(0, size) + "…" : text;
  };

  /* ---------- словарь ---------- */

  TX.t = function (key, params) {
    var table = I18N[TX.lang] || I18N.ru || {};
    var text = table[key];
    if (text == null) text = (I18N.ru || {})[key];
    if (text == null) return key;
    if (params) {
      Object.keys(params).forEach(function (name) {
        text = text.split("{" + name + "}").join(String(params[name]));
      });
    }
    return text;
  };

  TX.applyI18n = function (root) {
    var host = root || document;
    document.documentElement.lang = TX.lang;
    TX.qsa("[data-i18n]", host).forEach(function (node) {
      var params = null;
      var raw = node.getAttribute("data-i18n-params");
      if (raw) {
        try {
          params = JSON.parse(raw);
        } catch (error) {
          params = null;
        }
      }
      node.textContent = TX.t(node.getAttribute("data-i18n"), params);
    });
    TX.qsa("[data-i18n-title]", host).forEach(function (node) {
      node.setAttribute("title", TX.t(node.getAttribute("data-i18n-title")));
    });
    TX.qsa("[data-i18n-attr]", host).forEach(function (node) {
      (node.getAttribute("data-i18n-attr") || "").split(";").forEach(function (pair) {
        var parts = pair.split(":");
        if (parts.length !== 2) return;
        var attr = parts[0].trim();
        var key = parts[1].trim();
        if (attr && key) node.setAttribute(attr, TX.t(key));
      });
    });
    var titleKey = document.documentElement.getAttribute("data-i18n-title");
    if (titleKey) document.title = TX.t(titleKey);
    document.dispatchEvent(new CustomEvent("tx:lang", { detail: { lang: TX.lang } }));
  };

  /* Режим VIN задаёт сервер (/health: vin_required). Тексты с data-i18n-vin
   * переключаются на второй ключ только в режиме «VIN обязателен»; базовый
   * ключ хранится в data-i18n-base. Событие tx:vin — для логики мастера. */
  TX.vinRequired = false;
  TX.setVinRequired = function (required) {
    TX.vinRequired = !!required;
    TX.qsa("[data-i18n-vin]").forEach(function (node) {
      if (!node.hasAttribute("data-i18n-base")) {
        node.setAttribute("data-i18n-base", node.getAttribute("data-i18n") || "");
      }
      node.setAttribute("data-i18n", TX.vinRequired
        ? node.getAttribute("data-i18n-vin")
        : node.getAttribute("data-i18n-base"));
    });
    TX.applyI18n(document);
    document.dispatchEvent(new CustomEvent("tx:vin", { detail: { required: TX.vinRequired } }));
  };

  TX.setLang = function (lang) {
    TX.lang = lang === "en" ? "en" : "ru";
    TX.store.set("torquex.lang", TX.lang);
    TX.applyI18n(document);
    TX.syncLangButtons();
  };

  TX.initLang = function () {
    /* Язык сайта — русский (проект и тексты генератора русские), английский
     * включается переключателем RU/EN и запоминается. Автовыбор по браузеру
     * сознательно не делаем: так сайт ведёт себя одинаково у всех. */
    var saved = TX.store.get("torquex.lang");
    TX.lang = saved === "en" ? "en" : "ru";
    document.documentElement.lang = TX.lang;
  };

  /* ---------- тема ---------- */

  TX.prefersReducedMotion = function () {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (error) {
      return false;
    }
  };

  TX.setTheme = function (theme, remember) {
    TX.theme = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", TX.theme);
    if (remember !== false) TX.store.set("torquex.theme", TX.theme);
    TX.syncThemeButtons();
  };

  TX.initTheme = function () {
    var saved = TX.store.get("torquex.theme");
    if (saved === "light" || saved === "dark") {
      TX.theme = saved;
    } else {
      var light = false;
      try {
        light = window.matchMedia("(prefers-color-scheme: light)").matches;
      } catch (error) {
        light = false;
      }
      TX.theme = light ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", TX.theme);
  };

  TX.toggleTheme = function () {
    TX.setTheme(TX.theme === "dark" ? "light" : "dark");
  };

  /* ---------- копирование ---------- */

  TX.copy = function (text) {
    var value = String(text == null ? "" : text);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value).then(
        function () { return true; },
        function () { return TX.copyFallback(value); }
      );
    }
    return Promise.resolve(TX.copyFallback(value));
  };

  TX.copyFallback = function (value) {
    try {
      var area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(area);
      return !!ok;
    } catch (error) {
      return false;
    }
  };

  /* Кнопка «скопировать» для моноширинных значений (sha256, команды). */
  TX.copyButton = function (text, label) {
    var button = TX.el("button", {
      class: "btn btn-icon copy-btn",
      attrs: {
        type: "button",
        "data-i18n-title": label || "common.copy",
        "aria-label": TX.t(label || "common.copy"),
      },
      text: "⧉",
    });
    button.addEventListener("click", function () {
      TX.copy(text).then(function (ok) {
        TX.toast(ok ? "ok" : "warn", TX.t(ok ? "common.copied" : "common.copy_failed"));
      });
    });
    return button;
  };

  /* Моноширинное значение: <code> + кнопка копирования. */
  TX.monoRow = function (text, options) {
    options = options || {};
    var code = TX.el("code", { class: "mono", text: text });
    var row = TX.el("div", { class: "mono-row" }, [code]);
    if (options.copy !== false) row.appendChild(TX.copyButton(text));
    if (options.hint) row.appendChild(TX.el("span", { class: "hint", text: options.hint }));
    return row;
  };

  /* ---------- тосты ---------- */

  TX.toast = function (kind, text) {
    var host = TX.qs("#tx-toasts");
    if (!host) return;
    var node = TX.el("div", {
      class: "toast toast-" + (kind || "ok"),
      attrs: { role: "status" },
    }, [
      TX.el("span", { class: "toast-text", text: text }),
      TX.el("button", {
        class: "btn btn-icon toast-close",
        text: "×",
        attrs: { type: "button", "aria-label": TX.t("common.close") },
        on: [["click", function () { host.removeChild(node); }]],
      }),
    ]);
    host.appendChild(node);
    window.setTimeout(function () {
      if (node.parentNode === host) host.removeChild(node);
    }, 6000);
  };

  /* Статус для скринридера: #tx-live (вежливо) и #tx-alert (срочно). */
  TX.announce = function (text, urgent) {
    var host = TX.qs(urgent ? "#tx-alert" : "#tx-live");
    if (!host) return;
    TX.fill(host, [TX.el("p", { class: "sr-only", text: text })]);
  };

  /* ---------- скелетоны и занятость ---------- */

  TX.skeleton = function (host, lines) {
    if (!host) return host;
    var count = lines || 3;
    var box = TX.el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } });
    for (var index = 0; index < count; index += 1) {
      box.appendChild(TX.el("div", { class: "skeleton-line" }));
    }
    return TX.fill(host, [box]);
  };

  TX.busy = function (button, busy, busyKey) {
    if (!button) return;
    if (busy) {
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      if (busyKey) {
        button.setAttribute("data-idle-text", button.textContent);
        button.textContent = TX.t(busyKey);
      }
    } else {
      button.removeAttribute("aria-busy");
      button.disabled = false;
      var idle = button.getAttribute("data-idle-text");
      if (idle) {
        button.textContent = idle;
        button.removeAttribute("data-idle-text");
      }
    }
  };

  /* ---------- markdown наряда: узлы, а не строка HTML ---------- */

  TX.renderInline = function (text) {
    /* Жирный **…** и моноширинный `…` — коротким разбором на текстовые узлы. */
    var fragment = document.createDocumentFragment();
    var pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    var last = 0;
    var match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) {
        fragment.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      var token = match[0];
      if (token.charAt(0) === "*") {
        fragment.appendChild(TX.el("strong", { text: token.slice(2, -2) }));
      } else {
        fragment.appendChild(TX.el("code", { class: "mono", text: token.slice(1, -1) }));
      }
      last = match.index + token.length;
    }
    if (last < text.length) fragment.appendChild(document.createTextNode(text.slice(last)));
    return fragment;
  };

  TX.renderMarkdown = function (markdown) {
    var root = document.createDocumentFragment();
    var lines = String(markdown || "").split(/\r?\n/);
    var list = null;
    var paragraph = [];
    var code = null;
    var codeLines = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      var text = paragraph.join(" ");
      root.appendChild(TX.el("p", {}, [TX.renderInline(text)]));
      paragraph = [];
    }

    function flushList() {
      if (!list) return;
      root.appendChild(list);
      list = null;
    }

    lines.forEach(function (line) {
      if (code !== null) {
        if (line.indexOf("```") === 0) {
          root.appendChild(TX.el("pre", {}, [TX.el("code", { text: codeLines.join("\n") })]));
          code = null;
          codeLines = [];
        } else {
          codeLines.push(line);
        }
        return;
      }
      if (line.indexOf("```") === 0) {
        flushParagraph();
        flushList();
        code = true;
        return;
      }
      var heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph();
        flushList();
        var level = heading[1].length;
        var tag = level <= 1 ? "h3" : level === 2 ? "h4" : "h5";
        root.appendChild(TX.el(tag, {}, [TX.renderInline(heading[2])]));
        return;
      }
      var bullet = /^\s*[-*]\s+(.*)$/.exec(line);
      if (bullet) {
        flushParagraph();
        if (!list) list = TX.el("ul", { class: "note-list" });
        list.appendChild(TX.el("li", {}, [TX.renderInline(bullet[1])]));
        return;
      }
      var quote = /^>\s?(.*)$/.exec(line);
      if (quote) {
        flushParagraph();
        flushList();
        root.appendChild(TX.el("blockquote", {}, [TX.renderInline(quote[1])]));
        return;
      }
      if (!line.trim()) {
        flushParagraph();
        flushList();
        return;
      }
      paragraph.push(line.trim());
    });
    flushParagraph();
    flushList();
    if (codeLines.length) {
      root.appendChild(TX.el("pre", {}, [TX.el("code", { text: codeLines.join("\n") })]));
    }
    return root;
  };

  /* ---------- шапка, подвал, демо-баннер ---------- */

  TX.syncLangButtons = function () {
    TX.qsa("[data-lang]").forEach(function (button) {
      var active = button.getAttribute("data-lang") === TX.lang;
      button.setAttribute("aria-pressed", active ? "true" : "false");
      if (active) {
        button.classList.add("is-active");
      } else {
        button.classList.remove("is-active");
      }
    });
  };

  TX.syncThemeButtons = function () {
    TX.qsa("[data-theme-toggle]").forEach(function (button) {
      var next = TX.theme === "dark" ? "common.theme_light" : "common.theme_dark";
      var label = TX.t(next);
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      var sun = TX.qs(".icon-sun", button);
      var moon = TX.qs(".icon-moon", button);
      if (sun && moon) {
        sun.style.display = TX.theme === "dark" ? "none" : "";
        moon.style.display = TX.theme === "dark" ? "" : "none";
      }
    });
  };

  TX.buildLangSwitch = function () {
    var group = TX.el("div", {
      class: "lang-switch",
      attrs: { role: "group", "data-i18n-attr": "aria-label:common.lang" },
    });
    ["ru", "en"].forEach(function (code) {
      var button = TX.el("button", {
        class: "btn btn-chip",
        text: code.toUpperCase(),
        attrs: { type: "button", "data-lang": code },
        on: [["click", function () { TX.setLang(code); }]],
      });
      group.appendChild(button);
    });
    return group;
  };

  TX.buildThemeSwitch = function () {
    var button = TX.el("button", {
      class: "btn btn-icon theme-switch",
      attrs: { type: "button", "data-theme-toggle": "1" },
      on: [["click", function () { TX.toggleTheme(); }]],
    });
    /* Иконки — встроенный SVG, без картинок из интернета. */
    var svg = "http://www.w3.org/2000/svg";
    var moon = document.createElementNS(svg, "svg");
    moon.setAttribute("viewBox", "0 0 24 24");
    moon.setAttribute("class", "icon icon-moon");
    moon.setAttribute("aria-hidden", "true");
    var moonPath = document.createElementNS(svg, "path");
    moonPath.setAttribute("d", "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z");
    moonPath.setAttribute("fill", "currentColor");
    moon.appendChild(moonPath);
    var sun = document.createElementNS(svg, "svg");
    sun.setAttribute("viewBox", "0 0 24 24");
    sun.setAttribute("class", "icon icon-sun");
    sun.setAttribute("aria-hidden", "true");
    var sunCircle = document.createElementNS(svg, "circle");
    sunCircle.setAttribute("cx", "12");
    sunCircle.setAttribute("cy", "12");
    sunCircle.setAttribute("r", "4.2");
    sunCircle.setAttribute("fill", "currentColor");
    sun.appendChild(sunCircle);
    [0, 45, 90, 135, 180, 225, 270, 315].forEach(function (angle) {
      var ray = document.createElementNS(svg, "line");
      var rad = (angle * Math.PI) / 180;
      ray.setAttribute("x1", String(12 + Math.cos(rad) * 6.5));
      ray.setAttribute("y1", String(12 + Math.sin(rad) * 6.5));
      ray.setAttribute("x2", String(12 + Math.cos(rad) * 9));
      ray.setAttribute("y2", String(12 + Math.sin(rad) * 9));
      ray.setAttribute("stroke", "currentColor");
      ray.setAttribute("stroke-width", "1.8");
      ray.setAttribute("stroke-linecap", "round");
      sun.appendChild(ray);
    });
    button.appendChild(sun);
    button.appendChild(moon);
    return button;
  };

  TX.navItems = function () {
    return [
      { id: "master", href: "index.html", key: "nav.master" },
      { id: "help", href: "help-read-write.html", key: "nav.help" },
      { id: "versions", href: "versions.html", key: "nav.versions" },
    ];
  };

  TX.href = function (href) {
    /* В демо-режиме сохраняем ?demo=1 и фрагмент ссылки на любой локальной странице. */
    if (!(TX.isDemo && TX.isDemo()) || typeof href !== "string") return href;
    var hash = "";
    var hashAt = href.indexOf("#");
    if (hashAt >= 0) {
      hash = href.slice(hashAt);
      href = href.slice(0, hashAt);
    }
    var query = "";
    var queryAt = href.indexOf("?");
    if (queryAt >= 0) {
      query = href.slice(queryAt + 1);
      href = href.slice(0, queryAt);
    }
    if (!/\.html?$/.test(href)) return href + (query ? "?" + query : "") + hash;
    if (!/(^|&)demo=1(&|$)/.test(query)) query += (query ? "&" : "") + "demo=1";
    return href + "?" + query + hash;
  };

  function preserveDemoLinks(root) {
    if (!(TX.isDemo && TX.isDemo())) return;
    TX.qsa("a[href]", root).forEach(function (link) {
      link.setAttribute("href", TX.href(link.getAttribute("href")));
    });
  }

  TX.mountChrome = function (page) {
    var activePage = page;
    if (page === "home" || page === "wizard" || page === "master") activePage = "master";
    else if (page === "read_write" || page === "test0" || page === "help") activePage = "help";

    var host = TX.qs("#tx-header");
    if (host) {
      var inner = TX.el("div", { class: "wrap header-inner" });
      var brand = TX.el("a", {
        class: "brand",
        attrs: { href: TX.href("index.html") },
      }, [
        TX.el("span", { class: "brand-mark", attrs: { "aria-hidden": "true" }, text: "TX" }),
        TX.el("span", { class: "brand-text" }, [
          TX.el("strong", { attrs: { "data-i18n": "app.name" } }),
          TX.el("span", { class: "brand-sub", attrs: { "data-i18n": "app.tagline" } }),
        ]),
      ]);
      inner.appendChild(brand);

      var nav = TX.el("nav", {
        class: "nav",
        attrs: { id: "site-navigation", "data-i18n-attr": "aria-label:common.menu" },
      });
      var toggle = TX.el("button", {
        class: "btn btn-icon nav-toggle",
        attrs: {
          type: "button",
          "aria-controls": "site-navigation",
          "aria-expanded": "false",
          "data-i18n-attr": "aria-label:common.menu_toggle",
        },
      }, [TX.el("span", { attrs: { "aria-hidden": "true" }, text: "☰" })]);
      var list = TX.el("ul", { class: "nav-list" });
      function closeMenu() {
        nav.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
      }
      toggle.addEventListener("click", function () {
        var open = !nav.classList.contains("is-open");
        nav.classList.toggle("is-open", open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
          var first = TX.qs(".nav-link", nav);
          if (first) first.focus();
        }
      });
      TX.navItems().forEach(function (item) {
        var link = TX.el("a", {
          class: "nav-link" + (item.id === activePage ? " is-active" : ""),
          attrs: { href: TX.href(item.href), "data-i18n": item.key },
        });
        if (item.id === activePage) link.setAttribute("aria-current", "page");
        link.addEventListener("click", closeMenu);
        list.appendChild(TX.el("li", {}, [link]));
      });
      nav.appendChild(toggle);
      nav.appendChild(list);
      inner.appendChild(nav);

      var controls = TX.el("div", { class: "header-controls" }, [
        TX.buildLangSwitch(),
        TX.buildThemeSwitch(),
      ]);
      inner.appendChild(controls);
      TX.fill(host, [inner]);

      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && nav.classList.contains("is-open")) {
          closeMenu();
          toggle.focus();
        }
      });
    }

    var footer = TX.qs("#tx-footer");
    if (footer) {
      var footerLinks = TX.el("p", { class: "footer-links" }, [
        TX.el("a", { attrs: { href: TX.href("support.html"), "data-i18n": "footer.support" } }),
        TX.el("span", { text: " · " }),
        TX.el("a", { attrs: { href: TX.href("logs.html"), "data-i18n": "footer.logs" } }),
        TX.el("span", { text: " · " }),
        TX.el("a", { attrs: { href: TX.href("feedback.html"), "data-i18n": "footer.feedback" } }),
        TX.el("span", { text: " · " }),
        TX.el("a", { attrs: { href: TX.href("privacy.html"), "data-i18n": "footer.privacy" } }),
      ]);
      var footerInner = TX.el("div", { class: "wrap footer-inner" }, [
        TX.el("p", { class: "footer-title", attrs: { "data-i18n": "footer.title" } }),
        footerLinks,
        TX.el("p", { class: "muted", attrs: { "data-i18n": "common.footer_legal" } }),
        TX.el("p", { class: "muted", attrs: { "data-i18n": "footer.thanks" } }),
      ]);
      TX.fill(footer, [footerInner]);
    }

    var banner = TX.qs("#tx-demo-banner");
    if (banner) {
      if (TX.isDemo && TX.isDemo()) {
        TX.fill(banner, [
          TX.el("p", { class: "demo-banner" }, [
            TX.el("span", { class: "badge badge-demo", attrs: { "data-i18n": "common.demo" } }),
            TX.el("span", { attrs: { "data-i18n": "wizard.demo_banner" } }),
          ]),
        ]);
      } else {
        TX.clear(banner);
      }
    }

    TX.applyI18n(document);
    TX.syncLangButtons();
    TX.syncThemeButtons();
    preserveDemoLinks(document);
  };

  TX.onReady = function (fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { fn(); });
    } else {
      fn();
    }
  };

  /* Инициализация при загрузке скрипта: язык и тема применяются до первой
   * отрисовки, поэтому страница не мигает и не показывает не тот язык. */
  TX.initLang();
  TX.initTheme();

  document.addEventListener("tx:lang", function () {
    TX.syncLangButtons();
    TX.syncThemeButtons();
  });
})();
