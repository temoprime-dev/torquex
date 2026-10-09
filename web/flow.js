/* TorqueX-сайт: поток на одной странице (index.html).
 *
 * Путь без шагов и вкладок: файл → определение блока → функции по порядку
 * (Stage 1 первым) → «Собрать файл» → результат. Заявку строит request.js
 * (тот же модуль проверяет web/tools/check_requests.mjs).
 *
 * Никаких innerHTML: только узлы DOM и textContent. Тексты — из i18n.js.
 */
(function () {
  "use strict";

  var TX = window.TX;
  var req = TX.request;

  /* Отказы генератора по коду — понятными словами; код уходит в «Для специалистов». */
  var REFUSAL_TEXT = {
    NOTHING_REQUESTED: "wizard.refuse.NOTHING_REQUESTED",
    ACKNOWLEDGE_RISK_REQUIRED: "wizard.refuse.ACKNOWLEDGE_RISK_REQUIRED",
    NO_CATALYST_REQUIRED: "wizard.refuse.NO_CATALYST_REQUIRED",
    UNSUPPORTED_IMAGE: "wizard.refuse.UNSUPPORTED_IMAGE",
    CAL_VERSION_UNSUPPORTED: "wizard.refuse.CAL_VERSION_UNSUPPORTED",
    REQUIRES_DATA: "wizard.refuse.REQUIRES_DATA",
    VIN_REQUIRED: "wizard.refuse.VIN_REQUIRED",
    VIN_INVALID: "wizard.refuse.VIN_INVALID",
  };

  var OCTANES = ["", "ron98", "ron95", "ron91"];
  var ZONE_CLASS = { over: "is-over", busy: "is-busy", ok: "is-ready", error: "is-error" };

  function freshState(file) {
    return {
      file: file || null,
      platform: null,
      version: null,
      octane: "",
      revOn: false,
      revRpm: null,
      transmission: "",
      vmaxOn: false,
      vmaxKmh: null,
      popsOn: false,
      gs8: false,
      popsAcOn: false,
      fullLoadOn: false,
      vanosOn: false,
      noCatalyst: false,
      legality: false,
      temperature: false,
      ackRisk: false,
      ackOilPump: false,
      ackDamper: false,
      vin: "",
    };
  }

  var state = freshState(null);
  /* Ответ определения: payload (блок найден или нет) либо error (сеть). */
  var detect = { payload: null, error: null };
  var fileStatus = { key: null, params: null, kind: "", note: null };
  var result = null;
  var lastBuilt = null;
  var busy = false;
  var vinRequired = false;
  var transmissionFor = null;

  function $(id) {
    return document.getElementById(id);
  }

  function show(id, visible) {
    var node = $(id);
    if (node) node.hidden = !visible;
  }

  function caps() {
    return req.CAPS[state.platform] || {};
  }

  /* Стендовый gate (config.js bench_required): MS42/MS43 без зажигания. */
  function ignitionGated() {
    return TX.CONFIG.bench_required !== false && (state.platform === "MS42" || state.platform === "MS43");
  }

  function fullLoadActive() {
    return state.fullLoadOn && !!caps().fullLoad && !ignitionGated();
  }

  function vanosActive() {
    return state.vanosOn && !!caps().vanos && !ignitionGated();
  }

  function octaneAllowed() {
    return !!caps().octane && !ignitionGated();
  }

  /* Добавка зажигания появляется только из выбора бензина (request.js
   * octaneDefaults). «Оставить как есть» и 91-й её не включают. */
  function ignitionFor() {
    if (!octaneAllowed() || !caps().ignition || !state.octane) return { on: false, deg: 0 };
    var defaults = req.octaneDefaults(state.platform, state.octane);
    return defaults && defaults.ignition ? { on: true, deg: defaults.ignitionDeg } : { on: false, deg: 0 };
  }

  /* Текст блокировщика: ключи опций переводим здесь (request.js — без словаря). */
  function blockerText(item) {
    if (!item) return "";
    var params = {};
    Object.keys(item.params || {}).forEach(function (name) {
      params[name] = item.params[name];
    });
    if (item.options && item.options.length) {
      params.options = item.options.map(function (key) { return TX.t(key); }).join(" · ");
    }
    return TX.t(item.key, params);
  }

  /* ---------- файл ---------- */

  function refreshThresholdText() {
    var oil = $("ack-oil-label");
    if (oil) oil.textContent = TX.t("wizard.s3.oil_pump", { rpm: req.THRESHOLDS.oilPumpRpm });
    var damper = $("ack-damper-label");
    if (damper) damper.textContent = TX.t("wizard.s3.damper", { rpm: req.THRESHOLDS.ms45DamperRpm });
  }

  function setFileStatus(key, params, kind, note) {
    fileStatus = { key: key, params: params || null, kind: kind || "", note: note || null };
    var zone = $("dropzone");
    if (zone) {
      zone.classList.remove("is-over", "is-busy", "is-ready", "is-error");
      if (ZONE_CLASS[kind]) zone.classList.add(ZONE_CLASS[kind]);
    }
    var host = $("file-state");
    if (!host) return;
    host.hidden = !key;
    host.textContent = key ? TX.t(key, params) : "";
    if (key && note) host.textContent += " " + TX.t(note);
    host.className = "file-state" + (kind ? " is-" + kind : "");
  }

  function validateFile(file) {
    var limit = TX.CONFIG.maxUploadBytes || 1048576;
    if (!file || !file.size) return { key: "wizard.s1.empty" };
    if (file.size > limit) return { key: "wizard.s1.too_big", params: { limit: TX.limitLabel() } };
    return null;
  }

  /* Всё, что относится к прошлому файлу, убираем до показа нового. */
  function resetOutput() {
    detect.payload = null;
    detect.error = null;
    result = null;
    lastBuilt = null;
    show("functions", false);
    show("build-bar", false);
    show("result", false);
    show("detect-line", false);
    show("detect-tech", false);
    show("detect-error", false);
  }

  function onFile(file) {
    if (busy) return;
    var error = validateFile(file);
    resetOutput();
    state = freshState(error ? null : file);
    syncControls();
    if (error) {
      setFileStatus(error.key, error.params, "error");
      TX.announce(TX.t(error.key, error.params), true);
      return;
    }
    var noBin = !/\.bin$/i.test(file.name);
    setFileStatus("wizard.s1.state_local", { name: file.name, size: TX.formatBytes(file.size) },
      "ok", noBin ? "wizard.s1.not_bin" : null);
    if (TX.isDemo()) TX.api.detectLocally(file);
    checkFile();
  }

  function setControlsDisabled(disabled) {
    var chooser = $("file-choose");
    var input = $("file-input");
    if (chooser) chooser.disabled = disabled;
    if (input) input.disabled = disabled;
  }

  function checkFile() {
    if (busy || !state.file) return;
    var file = state.file;
    var skeleton = $("detect-skeleton");
    busy = true;
    setControlsDisabled(true);
    resetOutput();
    setFileStatus("wizard.s1.checking", null, "busy");
    TX.announce(TX.t("wizard.s1.checking"));
    if (skeleton) {
      skeleton.hidden = false;
      TX.skeleton(skeleton, 3);
    }
    TX.api.detect(file).then(function (payload) {
      if (state.file !== file) return;
      detect.payload = payload;
      renderDetect(false);
    }).catch(function (error) {
      if (state.file !== file) return;
      detect.error = error || {};
      renderDetectError(false);
    }).then(function () {
      busy = false;
      setControlsDisabled(false);
      if (skeleton) {
        skeleton.hidden = true;
        TX.clear(skeleton);
      }
      if (state.file) {
        setFileStatus("wizard.s1.state_local", { name: state.file.name, size: TX.formatBytes(state.file.size) }, "ok");
      }
    });
  }

  function errorMessage(error) {
    if (error && error.code === "API_UNREACHABLE") return TX.t("wizard.err.api_unreachable");
    if (error && error.code === "API_BAD_JSON") return TX.t("wizard.err.bad_json");
    if (error && error.status === 413) return TX.t("wizard.err.upload_too_large", { size: TX.limitLabel() });
    if (error && error.status === 429) return TX.t("wizard.err.limit");
    if (error && error.status) return TX.t("wizard.err.api_error", { status: error.status });
    return TX.t("wizard.err.api_unreachable");
  }

  /* Технический блок: код и текст сервера — только здесь. */
  function techBlock(items) {
    var details = TX.el("details", { class: "specialists" }, [
      TX.el("summary", { text: TX.t("common.specialists") }),
    ]);
    items.forEach(function (item) {
      if (item.code) {
        details.appendChild(TX.el("p", { class: "refusal-code" }, [
          TX.el("span", { text: TX.t("wizard.refusal.code") + ": " }),
          TX.el("code", { class: "mono", text: item.code }),
        ]));
      }
      if (item.message) details.appendChild(TX.el("p", { text: item.message }));
    });
    return details;
  }

  function renderDetect(quiet) {
    var payload = detect.payload;
    if (!payload) return;
    show("detect-error", false);
    if (!payload.supported) {
      state.platform = null;
      state.version = null;
      show("functions", false);
      show("build-bar", false);
      show("detect-line", false);
      show("detect-tech", false);
      var key = REFUSAL_TEXT[payload.code] || "wizard.s1.unsupported";
      var host = $("detect-error");
      TX.fill(host, [TX.el("div", { class: "alert alert-stop" }, [
        TX.el("p", { text: TX.t(key) }),
        techBlock([{ code: payload.code, message: payload.detail || "" }]),
      ])]);
      host.hidden = false;
      if (!quiet) TX.announce(TX.t("wizard.status.detect_error"), true);
      return;
    }
    if (String(payload.platform || "").toUpperCase() === "GS8") {
      /* Блок АКПП: только определение и состояние калибровки, сборки нет. */
      state.platform = null;
      state.version = null;
      show("functions", false);
      show("build-bar", false);
      var gs8 = payload.calibration || {};
      $("detect-text").textContent = TX.t("wizard.gs8.found", { system: payload.version || "GS8" });
      show("detect-line", true);
      renderDetectFacts(payload);
      var gs8Key = "wizard.gs8." + (gs8.state || "unknown");
      var gs8Nodes = [TX.el("p", { text: TX.t(gs8Key, { vehicle: gs8.vehicle || "—" }) })];
      state.gs8 = true;
      var gs8Options = Array.isArray(payload.options) ? payload.options : [];
      if ((gs8.state === "modified" || gs8.state === "alpina") && gs8Options.length) {
        gs8Nodes.push(gs8Form(gs8Options, "gs8_restore", "restore"));
      }
      var gs8Alpina = Array.isArray(payload.alpina_options) ? payload.alpina_options : [];
      if ((gs8.state === "stock" || gs8.state === "modified") && gs8Alpina.length) {
        gs8Nodes.push(gs8Form(gs8Alpina, "gs8_alpina", "alpina"));
      }
      gs8Nodes.push(TX.el("p", { class: "muted", text: TX.t("wizard.gs8.note") }));
      TX.fill($("detect-error"), [TX.el("div", {
        class: "alert " + (gs8.state === "stock" ? "alert-ok" : "alert-warn"),
      }, gs8Nodes)]);
      show("detect-error", true);
      if (!quiet) TX.announce(TX.t(gs8Key, { vehicle: gs8.vehicle || "" }));
      return;
    }
    state.gs8 = false;
    state.platform = String(payload.platform || "").toUpperCase();
    state.version = payload.version || "";
    var text = TX.t("wizard.s1.found", { platform: state.platform, version: state.version || "—" });
    $("detect-text").textContent = text;
    show("detect-line", true);
    renderDetectFacts(payload);
    var calState = payload.calibration && payload.calibration.state;
    if (calState === "tuned") {
      /* Не сток: надбавки поверх чужой правки не ставим, советуем откат. */
      show("functions", false);
      show("build-bar", false);
      TX.fill($("detect-error"), [TX.el("div", { class: "alert alert-stop" }, [
        TX.el("p", { text: TX.t("wizard.cal.tuned") }),
        TX.el("p", { text: TX.t("wizard.cal.tuned_stage2") }),
        techBlock([{ code: "CAL_NOT_STOCK", message: (payload.calibration.reasons || []).join(", ") }]),
      ])]);
      show("detect-error", true);
      if (!quiet) TX.announce(TX.t("wizard.cal.tuned"), true);
      return;
    }
    var calReasons = (payload.calibration && payload.calibration.reasons) || [];
    if (calState === "unknown" && calReasons.some(function (r) { return r.indexOf("not_in_db") === 0; })) {
      TX.fill($("detect-error"), [TX.el("div", { class: "alert alert-warn" }, [
        TX.el("p", { text: TX.t("wizard.cal.unknown") }),
      ])]);
      show("detect-error", true);
    }
    show("functions", true);
    show("build-bar", true);
    renderFunctions();
    refreshBuild();
    if (!quiet) {
      TX.announce(TX.t("wizard.status.detected", { platform: state.platform, version: state.version }));
      /* На телефоне строка результата и первая карточка должны быть видны сразу. */
      var line = $("detect-line");
      if (line && line.scrollIntoView) {
        line.scrollIntoView({ block: "start", behavior: TX.prefersReducedMotion() ? "auto" : "smooth" });
      }
    }
  }

  function renderDetectFacts(payload) {
    var soft = payload.software && payload.software.length ? payload.software.join(", ") : "—";
    var rows = [
      { label: TX.t("wizard.s1.ecu"), value: payload.platform || "—" },
      { label: TX.t("wizard.s1.version"), value: payload.version || "—" },
      { label: TX.t("wizard.s1.family"), value: (payload.facts && payload.facts.family) || "—" },
      { label: TX.t("wizard.s1.image_type"), value: payload.image_type || "—" },
      { label: TX.t("wizard.s1.sha"), value: payload.sha256 || "—", mono: true },
      { label: TX.t("wizard.s1.software"), value: soft },
    ];
    var host = $("detect-facts");
    TX.clear(host);
    rows.forEach(function (row) {
      host.appendChild(TX.el("dt", { text: row.label }));
      var dd = TX.el("dd");
      if (row.mono) dd.appendChild(TX.monoRow(row.value, { copy: true }));
      else dd.textContent = String(row.value);
      host.appendChild(dd);
    });
    show("detect-tech", true);
  }

  function renderDetectError(quiet) {
    var error = detect.error || {};
    var host = $("detect-error");
    show("detect-line", false);
    show("detect-tech", false);
    var message = errorMessage(error);
    var retry = TX.el("button", {
      class: "btn btn-ghost",
      text: TX.t("wizard.err.retry"),
      attrs: { type: "button" },
      on: [["click", checkFile]],
    });
    TX.fill(host, [TX.el("div", { class: "alert alert-stop" }, [
      TX.el("p", { text: message }),
      TX.el("div", { class: "row-actions" }, [retry]),
    ].concat(error.code ? [techBlock([{ code: error.code, message: "" }])] : [])),
    ]);
    host.hidden = false;
    if (!quiet) TX.announce(message, true);
  }

  /* ---------- функции ---------- */

  function renderStage1Lines() {
    var list = $("stage1-lines");
    if (!list) return;
    TX.clear(list);
    var thresholds = req.THRESHOLDS;
    var lines = [
      ignitionFor().on || (octaneAllowed() && caps().ignition)
        ? TX.t("wizard.s1.s1_ign_writes", {
            noLog: thresholds.octaneNoLogDeg.ron98,
            ron95: thresholds.octaneNoLogDeg.ron95,
          })
        : TX.t("wizard.s1.s1_ign_gated"),
      TX.t("wizard.s1.s1_limiters"),
    ];
    if (caps().pops) lines.push(TX.t("wizard.s1.s1_pops"));
    lines.push(TX.t("wizard.s1.s1_file"));
    lines.forEach(function (text) {
      list.appendChild(TX.el("li", { text: text }));
    });
  }

  /* Список коробок — один раз на платформу (чтобы не сбивать фокус в select). */
  function renderTransmissions() {
    var select = $("opt-transmission");
    var list = caps().transmissions || [];
    show("field-transmission", list.length > 0);
    if (!select || transmissionFor === state.platform + "|" + TX.lang) return;
    transmissionFor = state.platform + "|" + TX.lang;
    TX.clear(select);
    list.forEach(function (code) {
      var key = code === "mt" || code === "manual" ? "wizard.s3.trans_mt"
        : code === "amt" ? "wizard.s3.trans_amt" : "wizard.s3.trans_at";
      select.appendChild(TX.el("option", { text: TX.t(key), attrs: { value: code } }));
    });
    if (list.indexOf(state.transmission) < 0) state.transmission = list[0] || "";
    select.value = state.transmission;
  }

  function renderFunctions() {
    if (!state.platform) return;
    var c = caps();
    var octane = octaneAllowed();
    if (!octane) state.octane = "";
    show("field-octane", octane);
    var ign = ignitionFor();
    var note = $("ignition-auto");
    if (note) {
      note.hidden = !ign.on;
      note.textContent = ign.on ? TX.t("wizard.s3.ign_auto_on", { deg: ign.deg }) : "";
    }
    renderStage1Lines();
    renderTransmissions();
    show("body-rev", state.revOn);
    show("body-vmax", state.vmaxOn);
    show("fn-fullload", !!c.fullLoad && !ignitionGated());
    show("fn-vanos", !!c.vanos && !ignitionGated());
    show("fn-trans", (state.revOn || fullLoadActive()) && (c.transmissions || []).length > 0);
    show("fn-pops", !!c.pops);
    show("body-pops", !!c.pops && !!c.popsAc && state.popsOn);
    show("fn-ack", state.revOn || state.vmaxOn);
    var rpm = state.revOn ? state.revRpm || 0 : 0;
    show("row-ack-oil", state.platform === "MS43" && rpm > req.THRESHOLDS.oilPumpRpm);
    show("row-ack-damper", state.platform === "MS45" && rpm > req.THRESHOLDS.ms45DamperRpm);
    show("vin-slot", vinRequired);
  }

  /* Всё, что уйдёт в заявку: ignitionDeg берём из бензина, не из флажков. */
  function wizardState() {
    var ign = ignitionFor();
    return {
      platform: state.platform,
      octane: state.octane || null,
      ignitionDeg: ign.on ? ign.deg : null,
      revRpm: state.revOn ? state.revRpm : null,
      transmission: state.transmission,
      fullLoad: fullLoadActive(),
      vanos: vanosActive(),
      vmaxKmh: state.vmaxOn ? state.vmaxKmh : null,
      pops: {
        enabled: state.popsOn,
        profile: "light",
        delayS: 0.5,
        declaration: state.popsOn ? TX.t("wizard.pops.auto_declaration") : "",
        acOnly: state.popsOn && state.popsAcOn,
        thermalWatch: state.popsOn,
        plan: state.popsOn ? TX.t("wizard.pops.auto_plan") : "",
      },
      ackRisk: state.ackRisk,
      ackOilPump: state.ackOilPump,
      ackDamper: state.ackDamper,
      noCatalyst: state.popsOn,
      legality: state.popsOn,
      temperature: state.popsOn,
      vin: state.vin,
      vinRequired: vinRequired,
    };
  }

  /* Кнопка «Собрать файл»: серая, пока чего-то не хватает; строка рядом говорит чего. */
  function refreshBuild() {
    var built = req.buildRequest(wizardState());
    var hint = $("build-hint");
    var button = $("build");
    var blocked = built.blockers.length > 0;
    if (hint) {
      if (blocked) {
        hint.className = "hint build-hint is-warning";
        hint.textContent = blockerText(built.blockers[0]);
      } else if (built.warnings.length) {
        hint.className = "hint build-hint";
        hint.textContent = blockerText(built.warnings[0]);
      } else {
        hint.className = "hint build-hint is-ready";
        hint.textContent = TX.t("wizard.s3.ready");
      }
    }
    if (button) {
      var ign = ignitionFor();
      button.textContent = ign.on
        ? TX.t("wizard.s3.to_result_stage1", { octane: String(state.octane).toUpperCase() })
        : TX.t("wizard.s3.to_result");
      button.disabled = blocked || busy;
      button.setAttribute("aria-disabled", button.disabled ? "true" : "false");
    }
    highlightBlocking(built.blockers);
  }

  /* Подсветить поле из блокировщика: «чего не хватает» видно и на самом поле. */
  function highlightBlocking(blockers) {
    TX.qsa(".is-blocking").forEach(function (node) { node.classList.remove("is-blocking"); });
    if (!blockers.length || !blockers[0].focus) return;
    var node = $(blockers[0].focus);
    if (!node) return;
    var box = node.closest ? (node.closest(".switch-row") || node.closest(".field")) : null;
    (box || node).classList.add("is-blocking");
  }

  /* ---------- заявка и результат ---------- */

  function submitBuild() {
    if (busy) return;
    var built = req.buildRequest(wizardState());
    if (built.blockers.length) {
      refreshBuild();
      TX.announce(blockerText(built.blockers[0]), true);
      return;
    }
    busy = true;
    result = null;
    lastBuilt = built.request;
    var button = $("build");
    TX.busy(button, true, "wizard.s3.busy");
    TX.announce(TX.t("wizard.status.building"));
    showPending();
    TX.api.build({
      file: state.file,
      request: built.request,
      vin: state.vin,
    }).then(function (response) {
      renderResult(response);
    }).catch(function (error) {
      renderResult({
        status: "REJECTED",
        data: {
          rejections: [(error && error.code ? error.code : "API_ERROR") + ": " + (error && error.message ? error.message : "")],
          refusals: error && error.payload && error.payload.title_ru
            ? [{ title_ru: error.payload.title_ru, title_en: error.payload.title_en }]
            : [],
        },
      });
    }).then(function () {
      busy = false;
      TX.busy(button, false);
    });
  }

  /* Блок АКПП GS8: выбор машины (фабричная BMW) или модели Alpina и сборка файла. */
  var GS8_TEXT = {
    restore: {
      title: "wizard.gs8.restore_title", hint: "wizard.gs8.restore_hint",
      pick: "wizard.gs8.restore_pick", btn: "wizard.gs8.restore_btn",
    },
    alpina: {
      title: "wizard.gs8.alpina_title", hint: "wizard.gs8.alpina_hint",
      pick: "wizard.gs8.alpina_pick", btn: "wizard.gs8.alpina_btn",
    },
  };

  function gs8Form(options, kind, name) {
    var text = GS8_TEXT[name];
    var selectId = "gs8-" + name + "-select";
    var select = TX.el("select", { attrs: { id: selectId } });
    options.forEach(function (item) {
      select.appendChild(TX.el("option", { text: item.vehicle || item.reference || item.hash, attrs: { value: item.hash } }));
    });
    var button = TX.el("button", {
      class: "btn btn-primary", text: TX.t(text.btn),
      attrs: { type: "button", id: "gs8-" + name },
      on: [["click", function () { submitGs8(kind, select.value, button); }]],
    });
    var nodes = [
      TX.el("h4", { text: TX.t(text.title) }),
      TX.el("p", { class: "muted", text: TX.t(text.hint) }),
    ];
    if (name === "alpina") nodes.push(TX.el("p", { class: "fn-warn", text: TX.t("wizard.gs8.alpina_warn") }));
    nodes.push(TX.el("label", { text: TX.t(text.pick), attrs: { for: selectId } }));
    nodes.push(select);
    nodes.push(button);
    return TX.el("div", { class: "field" }, nodes);
  }

  function submitGs8(kind, hash, button) {
    if (busy || !hash) return;
    busy = true;
    result = null;
    lastBuilt = { stage: "gs8", intents: [{ id: "r1", kind: kind, calibration: hash }] };
    TX.busy(button, true, "wizard.s3.busy");
    TX.announce(TX.t("wizard.status.building"));
    showPending();
    TX.api.build({ file: state.file, request: lastBuilt, vin: state.vin }).then(function (response) {
      renderResult(response);
    }).catch(function (error) {
      renderResult({
        status: "REJECTED",
        data: {
          rejections: [(error && error.code ? error.code : "API_ERROR") + ": " + (error && error.message ? error.message : "")],
          refusals: error && error.payload && error.payload.title_ru
            ? [{ title_ru: error.payload.title_ru, title_en: error.payload.title_en }]
            : [],
        },
      });
    }).then(function () {
      busy = false;
      TX.busy(button, false);
    });
  }

  function showPending() {
    show("functions", false);
    show("build-bar", false);
    show("result", true);
    $("result-title").textContent = TX.t("wizard.status.building");
    var body = $("result-body");
    TX.fill(body, [TX.el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } }, [
      TX.el("div", { class: "skeleton-line" }),
      TX.el("div", { class: "skeleton-line" }),
      TX.el("div", { class: "skeleton-line" }),
    ])]);
  }

  function backToFunctions() {
    show("result", false);
    if (state.gs8) {
      show("functions", false);
      show("build-bar", false);
      var again = document.getElementById("gs8-restore") || document.getElementById("gs8-alpina");
      if (again) again.focus();
      return;
    }
    show("functions", true);
    show("build-bar", true);
    refreshBuild();
    var first = $("opt-octane");
    if (first && !first.closest("[hidden]")) first.focus();
  }

  function noteFilename(manifest, data) {
    if (manifest && manifest.result_name) return String(manifest.result_name);
    var platform = (manifest && manifest.family) || (data && data.platform) || state.platform || "ecu";
    var version = (data && data.version) || "";
    return ("torquex_" + platform + (version ? "_" + version : "") + ".bin").replace(/\s+/g, "");
  }

  /* Отказы сервера приходят двумя формами: строки «[reject] КОД: текст»
   * (наряд) и объекты {code, severity, message}. Приводим к одному виду. */
  function normalizeRejections(items) {
    return (items || []).map(function (item) {
      if (typeof item === "string") {
        var match = /\[reject\]\s*([A-Z0-9_]+)\s*:\s*([\s\S]*)/.exec(item) ||
          /^([A-Z][A-Z0-9_]+)\s*:\s*([\s\S]*)/.exec(item);
        return { code: match ? match[1] : null, message: match ? match[2] : item, severity: "reject" };
      }
      return { code: item.code || null, message: item.message || "", severity: item.severity || "reject" };
    });
  }

  /* Живой сервер кладёт причину отказа иногда только в наряд. */
  function rejectionsFromNote(note) {
    var out = [];
    String(note || "").split(/\r?\n/).forEach(function (line) {
      var match = /\[reject\]\s*([A-Z0-9_]+)\s*:\s*(.+)$/.exec(line);
      if (match) out.push({ code: match[1], message: match[2].trim(), severity: "reject" });
    });
    return out;
  }

  /* Манифесты двух семейств устроены по-разному (MS45: manifest.result; MS43: в корне). */
  function resultFacts(manifest, data) {
    var nested = (manifest && manifest.result) || {};
    var pick = function (first, second, third) {
      if (first != null) return first;
      if (second != null) return second;
      return third;
    };
    return {
      sha256: pick(nested.sha256, manifest.result_sha256, data.image_sha256) || "",
      imageSize: pick(nested.image_size, manifest.image_size, data.image_size),
      changedCells: pick(nested.changed_cells, manifest.changed_cells, null),
      changedBytes: pick(nested.changed_bytes, manifest.changed_bytes, null),
    };
  }

  /* Пункт «ждёт данных»: строка или объект {id,title,code,why,needs}. */
  function requiresDataItem(item) {
    if (item == null) return null;
    if (typeof item !== "object") return TX.el("li", { text: String(item) });
    var node = TX.el("li");
    var title = item.title || item.id || item.code || "";
    if (title) node.appendChild(TX.el("strong", { text: String(title) }));
    if (item.code) {
      node.appendChild(document.createTextNode(" "));
      node.appendChild(TX.el("code", { class: "mono", text: String(item.code) }));
    }
    if (item.why) node.appendChild(TX.el("p", { text: String(item.why) }));
    var needs = Array.isArray(item.needs) ? item.needs : [];
    if (needs.length) {
      node.appendChild(TX.el("p", { class: "hint", text: TX.t("wizard.s4.rd_needs") }));
      var list = TX.el("ul", { class: "list" });
      needs.forEach(function (need) { list.appendChild(TX.el("li", { text: String(need) })); });
      node.appendChild(list);
    }
    return node;
  }

  function severityClass(severity) {
    if (severity === "reject" || severity === "error") return "stop";
    if (severity === "warning") return "warn";
    return "info";
  }

  /* Отказ для обычного человека: понятная фраза из словаря, иначе текст генератора. */
  function plainAlert(code, message, severity) {
    var box = TX.el("div", { class: "alert alert-" + severityClass(severity) });
    var key = code && REFUSAL_TEXT[code];
    box.appendChild(TX.el("p", { text: key ? TX.t(key) : (message || TX.t("wizard.s4.refusal_generic")) }));
    return box;
  }

  function renderResult(response) {
    result = response;
    show("functions", false);
    show("build-bar", false);
    show("result", true);
    var data = response.data || {};
    var built = response.status === "BUILT" && (data.status || "BUILT") === "BUILT";
    var title = $("result-title");
    title.textContent = TX.t(built ? "wizard.s4.built" : "wizard.s4.refused");
    var body = $("result-body");
    TX.clear(body);
    if (built) renderBuilt(body, data);
    else renderRefused(body, data);
    title.focus();
    TX.announce(TX.t(built ? "wizard.status.built" : "wizard.status.refused"), !built);
  }

  /* Окно после «Скачать»: сервис бесплатный, помощь нужна на развитие.
   * Файл выдаётся только после таймера: через 10 секунд в окне оживает
   * кнопка «Скачать файл», по её нажатию начинается скачивание. */
  var SUPPORT_POPUP_SECONDS = 10;

  function showSupportPopup(startDownload) {
    if (document.getElementById("support-popup")) return;
    var opener = document.activeElement;
    var left = SUPPORT_POPUP_SECONDS;
    var timer = null;
    var close = function () {
      if (timer) clearInterval(timer);
      var node = document.getElementById("support-popup");
      if (node && node.parentNode) node.parentNode.removeChild(node);
      document.removeEventListener("keydown", onKey);
      if (opener && opener.focus) opener.focus();
    };
    var onKey = function (event) { if (event.key === "Escape" && left <= 0) close(); };
    var getBtn = TX.el("button", {
      class: "btn btn-primary", attrs: { type: "button", disabled: "disabled" },
      on: [["click", function () {
        if (left > 0) return;
        close();
        startDownload();
      }]],
    });
    var label = function () {
      getBtn.textContent = left > 0
        ? TX.t("support_popup.wait", { seconds: left })
        : TX.t("support_popup.get");
    };
    label();
    var link = TX.el("a", {
      class: "btn", text: TX.t("support_popup.cta"),
      attrs: { href: TX.href("support.html"), target: "_blank", rel: "noopener" },
    });
    var overlay = TX.el("div", {
      class: "popup-overlay", attrs: { id: "support-popup" },
      on: [["click", function (event) { if (event.target === overlay && left <= 0) close(); }]],
    }, [TX.el("div", {
      class: "popup-box",
      attrs: { role: "dialog", "aria-modal": "true", "aria-labelledby": "support-popup-title" },
    }, [
      TX.el("h3", { text: TX.t("support_popup.title"), attrs: { id: "support-popup-title" } }),
      TX.el("p", { text: TX.t("support_popup.text") }),
      TX.el("div", { class: "popup-actions" }, [getBtn, link]),
    ])]);
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    timer = setInterval(function () {
      left -= 1;
      if (left <= 0) {
        clearInterval(timer);
        timer = null;
        left = 0;
        getBtn.removeAttribute("disabled");
        getBtn.focus();
      }
      label();
    }, 1000);
  }

  function renderBuilt(body, data) {
    var manifest = data.manifest || {};
    var packages = Array.isArray(manifest.packages) ? manifest.packages : [];
    var pending = Array.isArray(manifest.requires_data) && manifest.requires_data.length
      ? manifest.requires_data
      : packages.filter(function (item) { return item && item.status === "requires_data"; });

    var download = TX.el("button", {
      class: "btn btn-primary btn-big btn-block",
      text: TX.t("wizard.s4.download"),
      attrs: { type: "button", id: "download" },
      on: [["click", function () {
        var startDownload = function () {
          TX.api.download({ jobId: data.job_id, filename: noteFilename(manifest, data) }).then(function (file) {
            var anchor = TX.el("a", { attrs: { href: file.url, download: file.filename, hidden: "hidden" } });
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
            if (file.revoke) URL.revokeObjectURL(file.url);
            TX.toast(TX.isDemo() ? "warn" : "ok",
              TX.t(TX.isDemo() ? "wizard.s4.demo_download_hint" : "wizard.s4.download_note"));
          });
        };
        if (TX.isDemo()) startDownload();
        else showSupportPopup(startDownload);
      }]],
    });
    body.appendChild(download);
    var fbParams = new URLSearchParams({
      kind: "result_ok",
      platform: String(data.platform || ""),
      version: String(data.version || ""),
      sha: String(data.image_sha256 || "").slice(0, 12),
    });
    body.appendChild(TX.el("p", { class: "hint" }, [
      TX.el("a", { text: TX.t("feedback.result_link"), attrs: { href: TX.href("feedback.html") + "?" + fbParams.toString() } }),
    ]));
    if (data.ttl_seconds) {
      body.appendChild(TX.el("p", {
        class: "hint",
        text: TX.t("wizard.s4.download_first", { minutes: Math.round(data.ttl_seconds / 60) }),
      }));
    }

    var changed = TX.el("section", { class: "card result-block" });
    changed.appendChild(TX.el("h3", { text: TX.t("wizard.s4.what_changed") }));
    var lines = req.describeRequest(lastBuilt).filter(function (item) {
      return item.key !== "summary.octane";
    }).slice(0, 5);
    if (lines.length) {
      var list = TX.el("ul", { class: "list" });
      lines.forEach(function (item) {
        list.appendChild(TX.el("li", { text: TX.t(item.key, item.params) }));
      });
      changed.appendChild(list);
    } else {
      changed.appendChild(TX.el("p", { class: "muted", text: TX.t("wizard.s4.changed_none") }));
    }
    if (pending.length) changed.appendChild(TX.el("p", { class: "hint", text: TX.t("wizard.s4.pending_note") }));
    body.appendChild(changed);

    body.appendChild(TX.el("p", { class: "back-line", text: TX.t("wizard.s4.back_line") }));
    body.appendChild(TX.el("button", {
      class: "btn btn-ghost",
      text: TX.t("wizard.s4.new"),
      attrs: { type: "button" },
      on: [["click", resetAll]],
    }));
    body.appendChild(TX.el("p", { class: "hint trust-line", text: TX.t("wizard.s4.trust_line") }));

    var donateUrl = data.donate_url || TX.CONFIG.donateUrl;
    if (donateUrl) {
      body.appendChild(TX.el("p", {}, [TX.el("a", {
        class: "btn btn-ghost",
        text: TX.t("wizard.s4.donate"),
        attrs: { href: donateUrl, rel: "noopener" },
      })]));
    }

    /* Всё техническое — свёрнутым блоком «Для специалистов». */
    var spec = TX.el("details", { class: "specialists" }, [TX.el("summary", { text: TX.t("common.specialists") })]);
    // отчёт генератора по-русски: фраза про это живёт только здесь, не в подвале
    spec.appendChild(TX.el("p", { class: "muted", attrs: { "data-i18n": "common.engine_language_note" } }));
    var facts = resultFacts(manifest, data);
    var dl = TX.el("dl", { class: "kv" });
    [
      { label: TX.t("wizard.s4.result_sha"), value: facts.sha256 || "—", mono: true },
      {
        label: TX.t("wizard.s4.changed"),
        value: (facts.changedCells != null ? facts.changedCells : "—") + " / " +
          (facts.changedBytes != null ? facts.changedBytes : "—"),
      },
      { label: TX.t("wizard.s4.image_size"), value: TX.formatBytes(facts.imageSize) },
      { label: TX.t("wizard.s4.trust"), value: TX.t("wizard.home_trust_badge") },
    ].forEach(function (row) {
      dl.appendChild(TX.el("dt", { text: row.label }));
      var dd = TX.el("dd");
      if (row.mono) dd.appendChild(TX.monoRow(row.value, { copy: true }));
      else dd.textContent = String(row.value == null ? "—" : row.value);
      dl.appendChild(dd);
    });
    spec.appendChild(dl);

    var written = packages.filter(function (item) { return item && item.status === "written"; });
    var writtenList = TX.el("ul", { class: "list" });
    written.forEach(function (item) {
      var entry = TX.el("li");
      entry.appendChild(TX.el("strong", { text: String(item.title || item.id || "") }));
      if (item.detail) entry.appendChild(TX.el("p", { text: String(item.detail) }));
      writtenList.appendChild(entry);
    });
    spec.appendChild(TX.el("h4", { text: TX.t("wizard.s4.changed_title") }));
    spec.appendChild(written.length ? writtenList : TX.el("p", { class: "muted", text: TX.t("wizard.s4.changed_none") }));

    spec.appendChild(TX.el("h4", { text: TX.t("wizard.s4.unchanged_title") }));
    spec.appendChild(TX.el("p", { text: TX.t("wizard.s4.unchanged_always") }));
    if (pending.length) {
      spec.appendChild(TX.el("h4", { text: TX.t("wizard.s4.requires_data") }));
      var pendingList = TX.el("ul", { class: "list" });
      pending.forEach(function (item) {
        var node = requiresDataItem(item);
        if (node) pendingList.appendChild(node);
      });
      spec.appendChild(pendingList);
    }

    if (manifest.rollback) {
      spec.appendChild(TX.el("h4", { text: TX.t("wizard.s4.rollback_title") }));
      spec.appendChild(TX.el("p", { text: TX.t("wizard.s4.rollback_lead") }));
      spec.appendChild(TX.monoRow(manifest.rollback.stock_sha256 || "—"));
      if (manifest.rollback.verify_command) {
        spec.appendChild(TX.el("p", {
          text: TX.t("wizard.s4.rollback_step2", { cmd: manifest.rollback.verify_command }),
        }));
        spec.appendChild(TX.monoRow(manifest.rollback.verify_command));
      }
      var raw = manifest.rollback.instructions_ru || [];
      var instructions = Array.isArray(raw) ? raw : [String(raw)];
      if (instructions.length) {
        spec.appendChild(TX.el("h4", { text: TX.t("wizard.s4.rollback_server") }));
        var rollbackList = TX.el("ul", { class: "list" });
        instructions.forEach(function (line) { rollbackList.appendChild(TX.el("li", { text: line })); });
        spec.appendChild(rollbackList);
      }
    }

    if (packages.length) {
      var table = TX.el("table", { class: "table" });
      packages.forEach(function (item) {
        table.appendChild(TX.el("tr", {}, [
          TX.el("th", { attrs: { scope: "row" }, text: item.title || item.id }),
          TX.el("td", {}, [TX.el("span", { class: "badge badge-" + (item.status || ""), text: item.status || "—" })]),
          TX.el("td", { text: item.detail || "" }),
        ]));
      });
      spec.appendChild(TX.el("h4", { text: TX.t("wizard.s4.packages") }));
      spec.appendChild(TX.el("div", { class: "table-scroll" }, [table]));
    }

    (manifest.findings || []).forEach(function (item) {
      spec.appendChild(TX.el("div", { class: "alert alert-" + severityClass(item.severity || item.level) }, [
        TX.el("p", { class: "refusal-code" }, [
          TX.el("span", { text: TX.t("wizard.refusal.code") + ": " }),
          TX.el("code", { class: "mono", text: String(item.code || "—") }),
        ]),
        TX.el("p", { text: String(item.message || "") }),
      ]));
    });
    if (manifest.octane && manifest.octane.note_ru) {
      spec.appendChild(TX.el("p", { class: "hint", text: manifest.octane.note_ru }));
    }
    if (data.note) {
      spec.appendChild(TX.el("details", { class: "note-details" }, [
        TX.el("summary", { text: TX.t("wizard.s4.note_title") }),
        TX.renderMarkdown(data.note),
      ]));
    }
    body.appendChild(spec);
  }

  function renderRefused(body, data) {
    var rejections = normalizeRejections(data.rejections);
    if (!rejections.length) rejections = rejectionsFromNote(data.note);
    var technical = [];
    rejections.forEach(function (item) {
      body.appendChild(plainAlert(item.code, item.message, item.severity));
      technical.push(item);
    });
    (data.refusals || []).forEach(function (item) {
      var title = TX.lang === "en" ? (item.title_en || item.title_ru) : (item.title_ru || item.title_en);
      var codeMatch = /^([A-Z][A-Z0-9_]+):\s*/.exec(String(title || ""));
      if (codeMatch) {
        technical.push({ code: codeMatch[1], message: "" });
        title = String(title).slice(codeMatch[0].length);
      }
      if (title) body.appendChild(TX.el("div", { class: "alert alert-info" }, [TX.el("p", { text: title })]));
    });
    if (!rejections.length && !(data.refusals || []).length) {
      body.appendChild(plainAlert(data.code, data.message || "", "reject"));
      if (data.code) technical.push({ code: data.code, message: data.message || "" });
    }
    body.appendChild(TX.el("div", { class: "row-actions" }, [
      TX.el("button", {
        class: "btn btn-primary",
        text: TX.t("wizard.s4.again"),
        attrs: { type: "button" },
        on: [["click", backToFunctions]],
      }),
      TX.el("button", {
        class: "btn btn-ghost",
        text: TX.t("wizard.s4.new"),
        attrs: { type: "button" },
        on: [["click", resetAll]],
      }),
    ]));
    if (technical.length || data.note) {
      var spec = techBlock(technical);
      if (data.note) spec.appendChild(TX.renderMarkdown(data.note));
      body.appendChild(spec);
    }
  }

  function resetAll() {
    state = freshState(null);
    detect.payload = null;
    detect.error = null;
    result = null;
    lastBuilt = null;
    var input = $("file-input");
    if (input) input.value = "";
    setFileStatus(null);
    resetOutput();
    syncControls();
    refreshBuild();
  }

  /* ---------- связывание полей ---------- */

  function syncControls() {
    var set = function (id, value, attr) {
      var node = $(id);
      if (!node) return;
      if (attr === "checked") node.checked = !!value;
      else node.value = value == null ? "" : String(value);
    };
    set("opt-octane", state.octane);
    set("opt-rev", state.revOn, "checked");
    set("opt-rev-value", state.revRpm);
    set("opt-vmax", state.vmaxOn, "checked");
    set("opt-vmax-value", state.vmaxKmh);
    set("opt-pops", state.popsOn, "checked");
    set("opt-pops-ac", state.popsAcOn, "checked");
    set("opt-fullload", state.fullLoadOn, "checked");
    set("opt-vanos", state.vanosOn, "checked");
    set("ack-risk", state.ackRisk, "checked");
    set("ack-oil", state.ackOilPump, "checked");
    set("ack-damper", state.ackDamper, "checked");
    set("opt-vin", state.vin);
    if (!state.platform) show("functions", false);
  }

  function setupControls() {
    var bind = function (id, type, apply) {
      var node = $(id);
      if (!node) return;
      node.addEventListener(type, function () {
        apply(node);
        renderFunctions();
        refreshBuild();
      });
    };
    var number = function (node) {
      return parseFloat(String(node.value).replace(",", ".")) || null;
    };
    bind("opt-octane", "change", function (node) {
      state.octane = OCTANES.indexOf(node.value) >= 0 ? node.value : "";
    });
    bind("opt-rev", "change", function (node) { state.revOn = node.checked; });
    bind("opt-rev-value", "input", function (node) { state.revRpm = number(node); });
    bind("opt-transmission", "change", function (node) { state.transmission = node.value; });
    bind("opt-vmax", "change", function (node) { state.vmaxOn = node.checked; });
    bind("opt-vmax-value", "input", function (node) { state.vmaxKmh = number(node); });
    bind("opt-vanos", "change", function (node) { state.vanosOn = node.checked; });
    bind("opt-fullload", "change", function (node) { state.fullLoadOn = node.checked; });
    bind("opt-pops-ac", "change", function (node) { state.popsAcOn = node.checked; });
    bind("opt-pops", "change", function (node) { state.popsOn = node.checked; });
    bind("ack-risk", "change", function (node) { state.ackRisk = node.checked; });
    bind("ack-oil", "change", function (node) { state.ackOilPump = node.checked; });
    bind("ack-damper", "change", function (node) { state.ackDamper = node.checked; });
    bind("opt-vin", "input", function (node) {
      /* Пробелы убираем, буквы — заглавными: VIN вставляют с пробелами. */
      var clean = req.cleanVin(node.value);
      if (node.value !== clean) node.value = clean;
      state.vin = clean;
    });
  }

  function setupHelp() {
    TX.qsa(".help-btn").forEach(function (button) {
      button.addEventListener("click", function () {
        var panel = $(button.getAttribute("aria-controls"));
        if (!panel) return;
        var open = panel.hidden;
        panel.hidden = !open;
        button.setAttribute("aria-expanded", open ? "true" : "false");
      });
    });
  }

  function setupDropzone() {
    var zone = $("dropzone");
    var input = $("file-input");
    if (!zone || !input) return;
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        setFileStatus("wizard.s1.state_over", null, "over");
      });
    });
    ["dragleave", "dragend"].forEach(function (name) {
      zone.addEventListener(name, function () {
        if (state.file) {
          setFileStatus("wizard.s1.state_local", { name: state.file.name, size: TX.formatBytes(state.file.size) }, "ok");
        } else {
          setFileStatus(null, null, "");
        }
      });
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      if (busy) return;
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) onFile(files[0]);
    });
    input.addEventListener("change", function () {
      if (input.files && input.files.length) onFile(input.files[0]);
    });
    var choose = $("file-choose");
    if (choose) choose.addEventListener("click", function () { input.click(); });
  }

  function setupDemo() {
    var panel = $("demo-panel");
    if (!panel) return;
    panel.hidden = !TX.isDemo();
    if (!TX.isDemo()) return;
    TX.qsa("[data-demo-scenario]", panel).forEach(function (button) {
      button.addEventListener("click", function () {
        TX.api.demoScenario(button.getAttribute("data-demo-scenario"));
        TX.toast("ok", button.textContent);
        if (state.file) checkFile();
      });
    });
  }

  function refreshLocalizedState() {
    transmissionFor = null;
    refreshThresholdText();
    if (fileStatus.key) setFileStatus(fileStatus.key, fileStatus.params, fileStatus.kind, fileStatus.note);
    if (detect.payload) renderDetect(true);
    if (detect.error) renderDetectError(true);
    if (state.platform) renderFunctions();
    if (result && !$("result").hidden) renderResult(result);
    refreshBuild();
  }

  TX.onReady(function () {
    TX.mountChrome("master");
    setupDropzone();
    setupControls();
    setupHelp();
    setupDemo();
    refreshThresholdText();
    syncControls();
    show("functions", false);
    show("build-bar", false);
    show("result", false);
    refreshBuild();
    var build = $("build");
    if (build) build.addEventListener("click", submitBuild);
    /* Режим VIN приходит с сервера; до ответа VIN необязателен. */
    document.addEventListener("tx:vin", function (event) {
      vinRequired = !!(event.detail && event.detail.required);
      renderFunctions();
      refreshBuild();
    });
    TX.syncVinMode();
    document.addEventListener("tx:lang", refreshLocalizedState);
  });
})();
