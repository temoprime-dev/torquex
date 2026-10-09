/* TorqueX-сайт: клиент API и демо-заглушки.
 *
 * Живой режим: только относительные пути (apiBase из config.js), никаких
 * внешних хостов. Демо-режим включается тремя путями: TORQUEX_CONFIG.forceDemo,
 * параметр ?demo=1 или открытие страницы с диска (file://) — тогда ни один
 * запрос не уходит наружу, а ответы собираются здесь, в api.js, и помечены
 * как вымышленные.
 *
 * Безопасность: файл стока не сохраняется ни в localStorage, ни в URL; VIN
 * уходит только полем формы и никогда не попадает в адрес.
 */
(function () {
  "use strict";

  var TX = window.TX || (window.TX = {});
  var CONFIG = TX.CONFIG || {};

  var FALLBACK_DEMO = "d3m0";

  function base() {
    var value = CONFIG.apiBase == null ? "api" : String(CONFIG.apiBase);
    if (value === ".") return "";
    return value.replace(/\/+$/, "");
  }

  function url(path) {
    var prefix = base();
    return (prefix ? prefix : "") + path;
  }

  function ApiError(code, message, extra) {
    this.name = "ApiError";
    this.code = code || "API_ERROR";
    this.message = message || code || "API error";
    this.status = (extra && extra.status) || 0;
    this.payload = (extra && extra.payload) || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function timeoutFetch(resource, options, seconds) {
    var controller = null;
    var timer = null;
    var opts = options || {};
    if (typeof AbortController !== "undefined") {
      controller = new AbortController();
      opts.signal = controller.signal;
      timer = window.setTimeout(function () { controller.abort(); }, seconds * 1000);
    }
    return fetch(resource, opts).then(
      function (response) {
        if (timer) window.clearTimeout(timer);
        return response;
      },
      function (error) {
        if (timer) window.clearTimeout(timer);
        throw new ApiError(
          error && error.name === "AbortError" ? "API_TIMEOUT" : "API_UNREACHABLE",
          "network",
          {}
        );
      }
    );
  }

  function parseJson(response) {
    return response.text().then(function (text) {
      if (!text) return {};
      try {
        return JSON.parse(text);
      } catch (error) {
        throw new ApiError("API_BAD_JSON", "bad json", { status: response.status });
      }
    });
  }

  function refusalFrom(payload, status) {
    var error = new ApiError(
      (payload && payload.code) || "API_ERROR",
      (payload && (payload.title_ru || payload.title_en)) || "error",
      { status: status, payload: payload }
    );
    return error;
  }

  /* ---------- демо ---------- */

  var DEMO_VERSION = "demo-заглушка";

  var DEMO_FAMILIES = [
    "4560BO00S", "4570LO00S", "4560BN00S", "4560BM00S", "4560BL00S",
    "4560BL30S", "4570LL00S", "4570LL30S", "4570LM00S", "4570LN00S",
  ];

  var DEMO_VERSIONS = {
    generator_version: "demo-заглушка",
    max_upload_bytes: 1048576,
    donate_url: CONFIG.donateUrl || "",
    demo: true,
    ecus: {
      MS41: { supported: true, note: "калибровка 24K / полное чтение 256K (" + DEMO_VERSION + ")" },
      MS42: { supported: true, note: "OSID 0110C6, 32K/64K/512K (" + DEMO_VERSION + ")" },
      MS43: { supported: true, versions: ["430069", "430037", "430055", "430056", "430066"] },
      MS45: {
        supported: true,
        families: DEMO_FAMILIES.slice(),
        families_with_definitions: ["4560BO00S", "4570LO00S"],
        stock_derived_families: ["4560BN00S", "4560BM00S", "4560BL00S", "4560BL30S",
                                 "4570LL00S", "4570LL30S", "4570LM00S", "4570LN00S"],
      },
    },
  };

  var demoState = { scenario: "ok", file: null, fileSize: 0 };

  function demoHash(seed) {
    var text = String(seed || FALLBACK_DEMO);
    var hash = 0;
    var out = "";
    for (var index = 0; index < 64; index += 1) {
      hash = (hash * 31 + text.charCodeAt(index % text.length) + index) % 16;
      out += "0123456789abcdef".charAt(hash);
    }
    return out;
  }

  function localSha256(file) {
    /* Реальный sha256 файла считается локально, если браузер это позволяет
     * (на file:// crypto.subtle может быть недоступен — тогда честная заглушка). */
    if (!file || !window.crypto || !window.crypto.subtle || !file.arrayBuffer) {
      return Promise.resolve({ hash: demoHash(file ? file.name : "demo"), local: false });
    }
    return file.arrayBuffer().then(function (buffer) {
      return window.crypto.subtle.digest("SHA-256", buffer);
    }).then(function (digest) {
      var bytes = new Uint8Array(digest);
      var out = "";
      for (var index = 0; index < bytes.length; index += 1) {
        out += ("0" + bytes[index].toString(16)).slice(-2);
      }
      return { hash: out, local: true };
    }).catch(function () {
      return { hash: demoHash(file.name), local: false };
    });
  }

  function demoDetect(file, scenario) {
    return localSha256(file).then(function (hash) {
      var size = file ? file.size : 0;
      var base = {
        filename: file ? file.name : "demo.bin",
        sha256: hash.hash,
        demo: true,
        size: size,
        hash_local: hash.local,
        software: [],
        facts: {},
      };
      if (scenario === "junk" || size < 4096) {
        return Object.assign(base, {
          platform: null,
          version: null,
          supported: false,
          image_type: null,
          detail: "Имя файла не похоже на образ ЭБУ, а таблица карт для размера " +
            size + " байт не найдена. Это демо-ответ: настоящий /detect сказал бы код UNSUPPORTED_IMAGE.",
          code: "UNSUPPORTED_IMAGE",
        });
      }
      if (scenario === "bad_version") {
        return Object.assign(base, {
          platform: "MS45",
          version: "4570ZZ00S",
          supported: false,
          image_type: DEMO_VERSION,
          detail: "Семейство 4570ZZ00S не поддержано. Настоящий генератор отказал бы кодом " +
            "CAL_VERSION_UNSUPPORTED: адреса для этой версии не доказаны.",
          code: "CAL_VERSION_UNSUPPORTED",
        });
      }
      return Object.assign(base, {
        platform: "MS45",
        version: "4560BO00S",
        supported: true,
        image_type: "калибровка 116 КиБ (" + DEMO_VERSION + ")",
        detail: "Демо-ответ: настоящий /detect вернул бы ЭБУ, версию, семейство и sha256 вашего файла.",
        code: null,
      });
    });
  }

  function demoManifest() {
    var stock = demoHash("stock-" + demoState.scenario + demoState.fileSize);
    var result = demoHash("result-" + demoState.scenario + demoState.fileSize);
    return {
      manifest_version: 1,
      trust_level: "bytes+crc verified",
      product: "TorqueX (демо-заглушка)",
      stage: "stage1",
      status: "BUILT",
      customer: null,
      date: "2026-01-01",
      source_name: demoState.file ? demoState.file.name : "demo-stock.bin",
      source_sha256: stock,
      family: "4560BO00S",
      supported_families: DEMO_FAMILIES,
      policy_version: "demo",
      octane: { profile: "ron98", addition_deg: 1.5, note: DEMO_VERSION },
      legal_notice: null,
      rollback: {
        stock_sha256: stock,
        image_kind: "full read",
        instructions_ru: [
          "демо: исходный сток сохраняется копией до записи — ровно тот файл, чей sha256 в source_sha256",
          "демо: возврат — записью этого файла тем же каналом, что и калибровка",
          "демо: после возврата сверить sha256 файла с source_sha256 манифеста",
        ],
        verify_command: "python3 tools/torquex.py detect " + (demoState.file ? demoState.file.name : "stock.bin"),
      },
      packages: [
        { id: "limits", title: "Ограничители", status: "written", detail: "демо: c_vs_max 254 -> 250 км/ч", needs: [], requests: [] },
        { id: "ignition", title: "Зажигание", status: "written", detail: "демо: +1.5° на подтверждённой карте", needs: [], requests: [] },
        { id: "pops", title: "Хлопки", status: "written", detail: "демо: задержка 0.5 с, профиль light", needs: [], requests: [] },
        { id: "knock", title: "Защита от детонации", status: "not_requested", detail: "не трогается никогда", needs: [], requests: [] },
      ],
      requires_data: ["демо: карты для этой семьи ждут данных"],
      findings: [
        { code: "DEMO", level: "warning", message: "Это демонстрационный ответ, файл не собирался." },
      ],
      plan: { dry_run: true },
      result: {
        sha256: result,
        image_size: demoState.fileSize || 0,
        kind: "demo",
        changed_cells: 260,
        changed_bytes: 780,
        checksum_words: [{ address: "0x100", crc32: demoHash("crc") }],
      },
      demo: true,
    };
  }

  function demoRejected() {
    return {
      job_id: "demo-job",
      status: "REJECTED",
      image_sha256: null,
      image_base64: null,
      demo: true,
      rejections: [
        {
          code: "NO_CATALYST_REQUIRED",
          severity: "reject",
          message: "Хлопки — только для машины без катализатора: подтверди no_catalyst, " +
            "если катализатор физически снят (демо-заглушка).",
        },
        {
          code: "ACKNOWLEDGE_REQUIRED",
          severity: "reject",
          message: "Демо-заглушка: генератор не подставляет подтверждения за клиента.",
        },
      ],
      refusals: [
        {
          code: "NO_CATALYST_REQUIRED",
          title_ru: "NO_CATALYST_REQUIRED: Хлопки — только для машины без катализатора — Подтверди no_catalyst: true, если катализатор физически снят; на машине с катализатором хлопки не выпускаются.",
          title_en: "NO_CATALYST_REQUIRED: Pops are only for a car without a catalytic converter — Set no_catalyst: true if the converter is physically removed.",
        },
      ],
      count: 1,
      ttl_seconds: 900,
      note: null,
    };
  }

  function delay(ms) {
    return new Promise(function (resolve) { window.setTimeout(resolve, ms); });
  }

  /* ---------- публичный клиент ---------- */

  TX.api = {};

  TX.api.Error = ApiError;

  TX.api.init = function () {
    var fromDisk = window.location.protocol === "file:";
    var query = new URLSearchParams(window.location.search);
    var wantsDemo = query.get("demo") === "1";
    TX.MODE = wantsDemo || fromDisk || CONFIG.forceDemo ? "demo" : "live";
    return TX.MODE;
  };

  TX.isDemo = function () {
    return TX.MODE === "demo";
  };

  TX.api.demoVersions = function () {
    return delay(120).then(function () { return DEMO_VERSIONS; });
  };

  TX.api.versions = function () {
    if (TX.isDemo()) return TX.api.demoVersions();
    return timeoutFetch(url("/versions"), { headers: { Accept: "application/json" } }, 20)
      .then(function (response) {
        if (!response.ok) throw refusalFrom({ code: "VERSIONS_FAILED" }, response.status);
        return parseJson(response);
      });
  };

  TX.api.detect = function (file) {
    if (TX.isDemo()) {
      return delay(400).then(function () { return demoDetect(file, demoState.scenario); });
    }
    var form = new FormData();
    form.append("file", file, file.name);
    form.append("lang", TX.lang);
    return timeoutFetch(url("/detect"), { method: "POST", body: form }, 60).then(function (response) {
      return parseJson(response).then(function (payload) {
        if (!response.ok) throw refusalFrom(payload, response.status);
        return payload;
      });
    });
  };

  TX.api.build = function (options) {
    var file = options.file;
    var request = options.request;
    if (TX.isDemo()) {
      return delay(700).then(function () {
        if (demoState.scenario === "reject") {
          return { status: "REJECTED", data: demoRejected(), demo: true };
        }
        return {
          status: "BUILT",
          data: {
            job_id: "demo-job",
            status: "BUILT",
            image_sha256: demoManifest().result.sha256,
            manifest: demoManifest(),
            note: TX.t("wizard.s4.demo_note_md"),
            ttl_seconds: 900,
            image_base64: null,
            demo: true,
          },
          demo: true,
        };
      });
    }
    var form = new FormData();
    form.append("file", file, file.name);
    form.append("request", JSON.stringify(request));
    form.append("lang", TX.lang);
    if (options.account) form.append("account", options.account);
    if (options.vin) form.append("vin", options.vin);
    return timeoutFetch(url("/build"), { method: "POST", body: form }, 120).then(function (response) {
      return parseJson(response).then(function (payload) {
        if (response.ok) return { status: "BUILT", data: payload };
        if (response.status === 422) return { status: "REJECTED", data: payload };
        throw refusalFrom(payload, response.status);
      });
    });
  };

  /* ---------- логи: разбор и добровольная передача (logs.html) ---------- */

  TX.api.health = function () {
    if (TX.isDemo()) {
      /* Демо-параметр vin=required (в адресе, только для показа) имитирует сервер, который требует VIN. */
      var vinMode = new URLSearchParams(window.location.search).get("vin");
      return delay(60).then(function () {
        return {
          status: "ok",
          log_donation: true,
          log_retention_days: 365,
          demo: true,
          vin_required: vinMode === "required",
        };
      });
    }
    return timeoutFetch(url("/health"), { headers: { Accept: "application/json" } }, 15)
      .then(function (response) {
        if (!response.ok) throw refusalFrom({ code: "HEALTH_FAILED" }, response.status);
        return parseJson(response);
      });
  };

  /* Режим VIN: сервер сообщает vin_required в /health. Если ответа нет или поля
   * нет — VIN необязателен и ничего не блокирует. Событие tx:vin шлётся в ui.js. */
  TX.syncVinMode = function () {
    if (!TX.qsa("[data-i18n-vin]").length) return Promise.resolve(false);
    var probe;
    try {
      probe = TX.api.health();
    } catch (error) {
      probe = Promise.reject(error);
    }
    return probe.then(function (payload) {
      return !!(payload && payload.vin_required === true);
    }, function () {
      return false;
    }).then(function (required) {
      TX.setVinRequired(required);
      return required;
    });
  };

  /* Разбор лога. share=true — отдельное согласие передать очищенную копию.
   * Файл уходит только телом формы; ничего не кладётся в URL и хранилище. */
  TX.api.analyzeLog = function (options) {
    if (TX.isDemo()) {
      return delay(500).then(function () {
        var answer = {
          status: "ANALYZED",
          demo: true,
          analysis: {
            rows: 360, duration_s: 36, results: { wot_runs: 2 },
            pack_recommendation: {
              zones: [{ zone_ru: "n 3000–4000 / load 450–700 (демо)",
                        recommendation_ru: "демо-заглушка: настоящий разбор делает сервер" }],
              warnings: [],
            },
          },
        };
        if (options.share) {
          answer.share = { status: "STORED", log_id: "demo", delete_token: "demo", expires: "—" };
        }
        return answer;
      });
    }
    var form = new FormData();
    form.append("file", options.file, options.file.name);
    form.append("package", options.pack);
    form.append("consent", "true");
    form.append("share_consent", options.share ? "true" : "false");
    form.append("lang", TX.lang);
    return timeoutFetch(url("/logs"), { method: "POST", body: form }, 120).then(function (response) {
      return parseJson(response).then(function (payload) {
        if (!response.ok) throw refusalFrom(payload, response.status);
        return payload;
      });
    });
  };

  /* Удаление переданной копии: код — в заголовке, не в адресе. */
  TX.api.deleteLog = function (logId, token) {
    if (TX.isDemo()) return delay(200).then(function () { return { status: "DELETED", demo: true }; });
    return timeoutFetch(url("/logs/" + encodeURIComponent(logId)), {
      method: "DELETE",
      headers: { Accept: "application/json", "X-Delete-Token": token },
    }, 30).then(function (response) {
      return parseJson(response).then(function (payload) {
        if (!response.ok) throw refusalFrom(payload, response.status);
        return payload;
      });
    });
  };

  /* Обратная связь: короткое сообщение владельцу (feedback.html). */
  TX.api.sendFeedback = function (data) {
    if (TX.isDemo()) return delay(200).then(function () { return { status: "RECEIVED", demo: true }; });
    var form = new FormData();
    ["kind", "message", "contact", "website", "platform", "version", "sha12"].forEach(function (name) {
      form.append(name, data[name] == null ? "" : String(data[name]));
    });
    form.append("lang", TX.lang);
    return timeoutFetch(url("/feedback"), { method: "POST", body: form }, 30).then(function (response) {
      return parseJson(response).then(function (payload) {
        if (!response.ok) throw refusalFrom(payload, response.status);
        return payload;
      });
    });
  };

  TX.api.jobFileUrl = function (jobId) {
    if (TX.isDemo()) return "#demo-download";
    return url("/jobs/" + encodeURIComponent(jobId) + "/file");
  };

  /* Скачивание. В демо-режиме отдаём текстовое пояснение, а не файл. */
  TX.api.download = function (options) {
    if (TX.isDemo()) {
      var text = [
        "TorqueX — демо-режим.",
        "",
        "Настоящий файл калибровки здесь не собирается: скачивание в демо-режиме",
        "специально отдаёт эту записку. Как собрать настоящий файл — web/README_RU.md.",
        "",
        "Файл стока: " + (demoState.file ? demoState.file.name : "не выбран"),
      ].join("\n");
      var blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      return Promise.resolve({
        url: URL.createObjectURL(blob),
        filename: "torquex_demo_note.txt",
        revoke: true,
      });
    }
    return Promise.resolve({
      url: TX.api.jobFileUrl(options.jobId),
      filename: options.filename || "torquex_calibration.bin",
      revoke: false,
    });
  };

  TX.api.demoScenario = function (name) {
    if (name) demoState.scenario = name;
    return demoState.scenario;
  };

  TX.api.demoFileSize = function (size) {
    demoState.fileSize = size || 0;
  };

  TX.api.detectLocally = function (file) {
    demoState.file = file;
    demoState.fileSize = file ? file.size : 0;
    return localSha256(file);
  };

  TX.api.DEMO_FAMILIES = DEMO_FAMILIES;

  /* Режим определяется сразу при загрузке api.js: мастер обязан знать, демо
   * это или живой сервер, ещё до первой отрисовки. */
  TX.api.init();
})();
