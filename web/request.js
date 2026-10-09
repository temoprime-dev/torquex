/* TorqueX: сборка заявки для генератора.
 *
 * Один и тот же код работает в браузере (мастер, шаг 3) и в Node
 * (web/tools/check_requests.mjs): тогда проверка «заявки сайта принимаются
 * генератором» проверяет именно то, что сайт отправляет.
 *
 * Что здесь есть:
 *  - CAPS: что каждая платформа умеет в этом релизе (только проверенные формы);
 *  - THRESHOLDS: пороги из политики генератора (масляный насос MS43 6750,
 *    демпфер MS45 6900, задержка хлопков 0.05–1.5 с, октан-добавка
 *    Stage 1 ≤ +3,0° (RON98) и ≤ +1,5° (RON95), Stage 2 с логом
 *    детонации ≤ +5,0° — цифры зеркалят docs/pack_log_requirements.json,
 *    сверяет тест tests/test_ignition_caps.py);
 *  - octaneDefaults: умолчания формы при выборе топлива (RON98 включает добавку);
 *  - availableOptions: какие опции вообще есть на этом ЭБУ (для подсказок);
 *  - buildRequest: превращает состояние мастера в заявку;
 *  - describeRequest: человеческая расшифровка заявки для сводки на экране.
 *
 * Правила честности: ни одно **подтверждение** не ставится «за клиента» —
 * галочки риска, снятого катализатора и правовой справки отмечает человек.
 * Умолчания обычных опций (например, +1.5° при выборе RON98) формой задавать
 * можно: они видны в блоке «Что уйдёт в заявку» и снимаются одной галочкой.
 * Если значение выше стока — заявку всё равно проверит генератор по файлу.
 *
 * Каждый блокировщик обязан сказать, **чего именно не хватает**: у него есть
 * ключ текста, параметры и `focus` — id поля, которое надо поправить. Мастер
 * показывает этот текст строкой рядом с неактивной кнопкой «Собрать файл» и
 * подсвечивает поле из `focus`.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.TX = root.TX || {};
    root.TX.request = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var VERSION = 1;

  /* Формы заявок проверены живыми сборками (--dry-run) на стоках репозитория:
   * MS45 — stages.py rev_limit/vmax/ignition/pops; MS43 — vmax/rpm_limit/
   * ignition/popcorn; MS42 — vmax/rpm_limit; MS41 — vmax(mode raise)/
   * rpm_limit(mode raise). */
  var CAPS = {
    MS41: {
      ignition: false,
      ignitionWhy: "wizard.s3.ms41_ign",
      octane: false,
      pops: false,
      popsWhy: "wizard.s3.pops_disabled",
      vmaxMode: "raise",
      revLimitMode: "raise",
      transmissions: [],
      transmissionWhy: "",
      disaIsa: false,
    },
    MS42: {
      fullLoad: true,
      vanos: true,
      ignition: true,
      ignitionWhy: "",
      octane: true,
      pops: false,
      popsWhy: "wizard.s3.pops_disabled",
      vmaxMode: "plain",
      revLimitMode: "plain",
      transmissions: ["manual", "auto"],
      disaIsa: false,
    },
    MS43: {
      fullLoad: true,
      vanos: true,
      ignition: true,
      ignitionWhy: "",
      octane: true,
      popsAc: true,
      pops: true,
      popsWhy: "",
      vmaxMode: "plain",
      revLimitMode: "plain",
      transmissions: ["manual", "auto"],
      disaIsa: false,
    },
    MS45: {
      ignition: true,
      ignitionWhy: "",
      octane: true,
      pops: true,
      popsWhy: "",
      vmaxMode: "plain",
      revLimitMode: "value",
      transmissions: ["mt", "at", "amt"],
      disaIsa: false,
    },
  };

  /* Пороги политики генератора. Не «примерные», а взятые из кода:
   * MS43/tools/stages.py OIL_PUMP_ACK_RPM, MS45/tools/stages.py DAMPER_ACK_RPM,
   * POPS_DELAY_RANGE_S, tools/variant1.py (октан). */
  var THRESHOLDS = {
    oilPumpRpm: 6750,
    ms45DamperRpm: 6900,
    popsDelayMinS: 0.05,
    popsDelayMaxS: 1.5,
    /* Потолки добавки зажигания — зеркало docs/pack_log_requirements.json
     * (packages.ignition.gates). Генератор всё равно проверяет по файлу:
     * сайт лишь не даёт собрать заведомо неверную заявку. */
    ignitionMaxDeg: 3,
    ignitionLogCapDeg: 5,
    octaneNoLogDeg: { ron98: 3, ron95: 1.5, ron91: 0, none: 1.5 },
    rpmMin: 500,
    rpmMax: 8500,
    vmaxMin: 1,
    vmaxMax: 255,
  };

  var POPS_PROFILES = ["light", "medium"];
  var OCTANES = { ron98: "ron98", ron95: "ron95", ron91: "ron91" };

  /* VIN: 17 символов ISO 3779, без I, O, Q. Контрольную цифру не проверяем
   * (у европейских BMW она не обязана сходиться). Пробелы убираем, буквы
   * делаем заглавными — так же, как поле мастера. */
  var VIN_PATTERN = /^[A-HJ-NPR-Z0-9]{17}$/;

  function cleanVin(raw) {
    return String(raw == null ? "" : raw).replace(/\s+/g, "").toUpperCase();
  }

  /* vinProblem(raw, required) -> null | "empty" | "invalid".
   * Блокирует только при required (сервер сообщил vin_required: true).
   * Без требования непустой VIN не блокирует ничего. */
  function vinProblem(raw, required) {
    var vin = cleanVin(raw);
    if (!required) return null;
    if (!vin) return "empty";
    return VIN_PATTERN.test(vin) ? null : "invalid";
  }

  function number(value) {
    if (value === "" || value == null) return null;
    var parsed = parseFloat(String(value).replace(",", "."));
    return isFinite(parsed) ? parsed : null;
  }

  function capsOf(platform) {
    return CAPS[String(platform || "").toUpperCase()] || {};
  }

  /* capNoLog(octane) -> потолок добавки без лога детонации для профиля
   * клиента (Stage 1). Таблица — зеркало спецификации; профиль не выбран —
   * консервативные +1,5°. */
  function capNoLog(octane) {
    var table = THRESHOLDS.octaneNoLogDeg;
    if (octane === "ron98") return table.ron98;
    if (octane === "ron95") return table.ron95;
    if (octane === "ron91") return table.ron91;
    return table.none;
  }

  /* octaneDefaults(platform, octane) -> {ignition, ignitionDeg} | null

     RON98 включает добавку со своим потолком Stage 1 (+3°), RON95 — с +1,5°,
     RON91 и «не менять» её снимают. Это умолчание формы, а не подтверждение:
     его видно в сводке и снимают одной галочкой. */
  function octaneDefaults(platform, octane) {
    var caps = capsOf(platform);
    if (!caps.octane || !caps.ignition) return null;
    var cap = capNoLog(octane);
    return {
      ignition: cap > 0,
      ignitionDeg: cap,
    };
  }

  /* availableOptions(platform) -> [ключи словаря]
   * Что вообще можно выбрать на этом ЭБУ. Нужно для честной подсказки
   * «включите одну из опций: …» вместо безадресного «выберите что-нибудь». */
  function availableOptions(platform) {
    var caps = capsOf(platform);
    var options = [];
    if (caps.octane && caps.ignition) options.push("wizard.opt.octane98");
    options.push("wizard.opt.rev");
    options.push("wizard.opt.vmax");
    if (caps.pops) options.push("wizard.opt.pops");
    return options;
  }

  /* buildRequest(state) -> {request, blockers, warnings}
   * state — состояние мастера:
   *   platform, octane, ignitionDeg, revRpm, transmission, vmaxKmh,
   *   pops {enabled, profile, delayS, declaration, thermalWatch, plan},
   *   ackRisk, ackOilPump, ackDamper, noCatalyst, legality, temperature,
   *   account, vin, vinRequired, notes
   * blockers — заявку отправлять нельзя (ключи словаря), warnings — можно. */
  function buildRequest(state) {
    state = state || {};
    var platform = String(state.platform || "").toUpperCase();
    var caps = capsOf(platform);
    var blockers = [];
    var warnings = [];
    var intents = [];
    var request = { stage: "stage1", variant: "v1" };

    if (state.account) request.customer = String(state.account);
    if (state.notes) request.notes = String(state.notes);
    var vin = cleanVin(state.vin);
    if (vin) request.vin = vin;
    var vinIssue = vinProblem(state.vin, !!state.vinRequired);
    if (vinIssue === "empty") {
      blockers.push({ key: "wizard.block.vin_missing", focus: "opt-vin" });
    } else if (vinIssue === "invalid") {
      blockers.push({ key: "wizard.block.vin_invalid", params: { count: vin.length }, focus: "opt-vin" });
    }

    /* --- октан --- */
    if (caps.octane && state.octane && OCTANES[state.octane]) {
      request.octane = state.octane;
    }

    /* --- зажигание --- */
    var ignitionDeg = number(state.ignitionDeg);
    if (ignitionDeg != null && ignitionDeg > 0) {
      var ignCap = capNoLog(state.octane);
      if (!caps.ignition) {
        blockers.push({ key: caps.ignitionWhy || "wizard.s3.ms41_ign", focus: "opt-octane" });
      } else if (ignCap <= 0) {
        blockers.push({ key: "wizard.block.ignition_no_addition", focus: "opt-octane" });
      } else if (ignitionDeg > ignCap) {
        blockers.push({
          key: "wizard.block.ignition_range",
          params: { max: ignCap, log: THRESHOLDS.ignitionLogCapDeg },
          focus: "opt-octane",
        });
      } else {
        intents.push({
          id: "ign1",
          kind: platform === "MS42" ? "ignition_stage1" : "ignition",
          delta_deg: ignitionDeg,
        });
      }
    }

    /* --- ранний режим полной нагрузки (только пороги id_pvs_fl*) --- */
    if (state.fullLoad && caps.fullLoad) {
      intents.push({
        id: "fl1", kind: "pedal_response", level: 1,
        transmission: state.transmission || "automatic",
        acknowledge_risk: true,
      });
    }

    /* --- мягкий VANOS (прогретые карты частичной нагрузки, ±1,5°) --- */
    if (state.vanos && caps.vanos) {
      intents.push({ id: "van1", kind: "vanos_stage1", acknowledge_risk: true });
    }

    /* --- ограничитель оборотов --- */
    var revRpm = number(state.revRpm);
    if (revRpm != null) {
      if (revRpm < THRESHOLDS.rpmMin || revRpm > THRESHOLDS.rpmMax) {
        blockers.push({
          key: "wizard.block.rpm_range",
          params: { min: THRESHOLDS.rpmMin, max: THRESHOLDS.rpmMax },
          focus: "opt-rev-value",
        });
      } else if (!state.ackRisk) {
        /* Любое повышение отсечки требует подтверждения риска. Генератор
         * сверяет значение со стоком самого файла (MS43/tools/stages.py и
         * MS45/tools/stages.py, код ACKNOWLEDGE_RISK_REQUIRED), а сайт стока
         * не знает — файл читает только генератор. Поэтому подтверждение
         * спрашивается всегда, когда ограничитель включён: лучше одна галочка
         * на месте, чем отказ после отправки. Живой прогон поймал ровно это:
         * MS43 430037, отсечка 6500 об/мин → 422 ACKNOWLEDGE_RISK_REQUIRED. */
        blockers.push({
          key: platform === "MS41" ? "wizard.block.ms41_ack" : "wizard.block.rev_ack",
          focus: "ack-risk",
        });
      } else if (platform === "MS45") {
        if (revRpm > THRESHOLDS.ms45DamperRpm && !state.ackDamper) {
          blockers.push({
            key: "wizard.block.damper",
            params: { rpm: THRESHOLDS.ms45DamperRpm },
            focus: "ack-damper",
          });
        } else {
          intents.push({
            id: "rpm1",
            kind: "rev_limit",
            value: revRpm,
            transmission: state.transmission || "mt",
            acknowledge_risk: !!state.ackRisk,
            acknowledge_damper: !!state.ackDamper,
          });
        }
      } else if (platform === "MS43") {
        if (revRpm > THRESHOLDS.oilPumpRpm && !state.ackOilPump) {
          blockers.push({
            key: "wizard.block.oil_pump",
            params: { rpm: THRESHOLDS.oilPumpRpm },
            focus: "ack-oil",
          });
        } else {
          intents.push({
            id: "rpm1",
            kind: "rpm_limit",
            transmission: state.transmission || "manual",
            soft_rpm: revRpm,
            acknowledge_risk: !!state.ackRisk,
            acknowledge_oil_pump: !!state.ackOilPump,
          });
        }
      } else if (platform === "MS42") {
        intents.push({
          id: "rpm1",
          kind: "rpm_limit",
          transmission: state.transmission || "manual",
          soft_rpm: revRpm,
          acknowledge_risk: !!state.ackRisk,
        });
      } else if (platform === "MS41") {
        intents.push({
          id: "rpm1",
          kind: "rpm_limit",
          mode: "raise",
          value_rpm: revRpm,
          acknowledge_risk: true,
        });
      }
    }

    /* --- дорожный лимитер --- */
    var vmax = number(state.vmaxKmh);
    if (vmax != null) {
      if (vmax < THRESHOLDS.vmaxMin || vmax > THRESHOLDS.vmaxMax) {
        blockers.push({
          key: "wizard.block.vmax_range",
          params: { min: THRESHOLDS.vmaxMin, max: THRESHOLDS.vmaxMax },
          focus: "opt-vmax-value",
        });
      } else if (!state.ackRisk) {
        /* Ограничитель скорости выше стока генератор тоже не выпускает без
         * acknowledge_risk (MS43/tools/stages.py, ветка kind == "vmax"). */
        blockers.push({ key: "wizard.block.vmax_ack", focus: "ack-risk" });
      } else if (caps.vmaxMode === "raise") {
        intents.push({ id: "vmax1", kind: "vmax", mode: "raise", value_kmh: vmax });
      } else if (platform === "MS45") {
        intents.push({ id: "vmax1", kind: "vmax", value: vmax });
      } else {
        var vmaxIntent = { id: "vmax1", kind: "vmax", value_kmh: vmax };
        if (platform === "MS43") {
          vmaxIntent.transmission = state.transmission || "manual";
          vmaxIntent.acknowledge_risk = true;
        }
        intents.push(vmaxIntent);
      }
    }

    /* --- хлопки --- */
    var pops = state.pops || {};
    if (pops.enabled) {
      if (!caps.pops) {
        blockers.push({
          key: "wizard.s3.pops_disabled",
          params: { platform: platform },
          focus: "opt-pops",
        });
      } else if (!state.noCatalyst) {
        blockers.push({ key: "wizard.block.pops_cat", focus: "pk-no-catalyst" });
      } else if (!state.legality) {
        blockers.push({ key: "wizard.block.pops_legal", focus: "pk-legality" });
      } else if (!state.temperature) {
        blockers.push({ key: "wizard.block.pops_temp", focus: "pk-temperature" });
      } else {
        var profile = POPS_PROFILES.indexOf(pops.profile) >= 0 ? pops.profile : "light";
        request.no_catalyst = true;
        request.acknowledge_legality = true;
        request.acknowledge_temperature = true;
        if (platform === "MS45") {
          var delay = number(pops.delayS);
          if (delay == null) delay = 0.5;
          if (delay < THRESHOLDS.popsDelayMinS || delay > THRESHOLDS.popsDelayMaxS) {
            blockers.push({
              key: "wizard.block.pops_delay",
              params: { min: THRESHOLDS.popsDelayMinS, max: THRESHOLDS.popsDelayMaxS },
              focus: "opt-pops",
            });
          } else {
            intents.push({ id: "pop1", kind: "pops", delay_s: delay, profile: profile });
          }
        } else if (platform === "MS43") {
          var declaration = String(pops.declaration || "").trim();
          var plan = String(pops.plan || "").trim();
          if (declaration.length < 10) {
            blockers.push({ key: "wizard.block.pops_declaration", focus: "pk-no-catalyst" });
          } else if (!pops.thermalWatch) {
            blockers.push({ key: "wizard.block.pops_thermal", focus: "pk-temperature" });
          } else if (plan.length < 10) {
            blockers.push({ key: "wizard.block.pops_plan", focus: "pk-temperature" });
          } else {
            var popIntent = {
              id: "pop1",
              kind: "popcorn",
              band: profile,
              no_catalyst: declaration,
              thermal_watch: true,
              road_validation_plan: plan,
            };
            if (pops.acOnly && caps.popsAc) popIntent.ac_only = true;
            intents.push(popIntent);
          }
        }
      }
    }

    /* Пустая заявка. Отдельный текст для «выбран только октан без добавки»:
     * RON95/RON91 сами по себе ничего не пишут, и человек должен видеть не
     * «выберите хотя бы одну опцию», а что именно включить. */
    if (!intents.length) {
      var options = availableOptions(platform);
      if (caps.octane && (state.octane === "ron95" || state.octane === "ron91")) {
        blockers.push({
          key: "wizard.block.octane_only",
          params: { octane: String(state.octane).toUpperCase() },
          options: options.filter(function (key) { return key !== "wizard.opt.octane98"; }),
          /* RON95 имеет право на +1,5° — ближайшая правка это флажок
           * зажигания; для RON91 добавки нет, подсвечиваем лимитеры. */
          focus: state.octane === "ron95" ? "opt-octane" : "opt-rev",
        });
      } else if (caps.octane) {
        /* Бензин «как есть» и ни одной функции: говорим, что выбрать. */
        blockers.push({ key: "wizard.block.pick_or_enable", focus: "opt-octane" });
      } else {
        blockers.push({
          key: "wizard.block.nothing",
          options: options,
          focus: "opt-rev",
        });
      }
    }
    if (!state.octane && caps.octane && intents.length && !blockers.length) {
      warnings.push({ key: "wizard.warn.octane_stock" });
    }
    if (state.octane === "ron91" && caps.octane) {
      warnings.push({ key: "wizard.warn.octane_no_addition" });
    }

    request.intents = intents;
    return { request: request, blockers: blockers, warnings: warnings };
  }

  /* describeRequest(request) -> [{key, params}] — человеческая сводка заявки. */
  function describeRequest(request) {
    var out = [];
    if (!request) return out;
    if (request.octane) {
      out.push({ key: "summary.octane", params: { value: String(request.octane).toUpperCase() } });
    }
    (request.intents || []).forEach(function (intent) {
      if (intent.kind === "ignition" || intent.kind === "ignition_stage1") {
        out.push({ key: "summary.ignition", params: { value: intent.delta_deg } });
      } else if (intent.kind === "rev_limit") {
        out.push({
          key: "summary.rev_limit_ms45",
          params: { rpm: intent.value, transmission: intent.transmission },
        });
      } else if (intent.kind === "rpm_limit") {
        out.push({
          key: "summary.rpm_limit",
          params: { rpm: intent.soft_rpm || intent.value_rpm, transmission: intent.transmission || "—" },
        });
      } else if (intent.kind === "vmax") {
        out.push({ key: "summary.vmax", params: { value: intent.value_kmh } });
      } else if (intent.kind === "gs8_restore") {
        out.push({ key: "summary.gs8_restore", params: {} });
      } else if (intent.kind === "gs8_alpina") {
        out.push({ key: "summary.gs8_alpina", params: {} });
      } else if (intent.kind === "vanos_stage1") {
        out.push({ key: "summary.vanos", params: {} });
      } else if (intent.kind === "pedal_response") {
        out.push({ key: "summary.fullload", params: {} });
      } else if (intent.kind === "pops" || intent.kind === "popcorn") {
        out.push({
          key: intent.ac_only ? "summary.pops_ac" : "summary.pops",
          params: {},
        });
      }
    });
    return out;
  }

  return {
    VERSION: VERSION,
    CAPS: CAPS,
    THRESHOLDS: THRESHOLDS,
    POPS_PROFILES: POPS_PROFILES,
    octaneDefaults: octaneDefaults,
    cleanVin: cleanVin,
    vinProblem: vinProblem,
    capNoLog: capNoLog,
    availableOptions: availableOptions,
    buildRequest: buildRequest,
    describeRequest: describeRequest,
  };
});
