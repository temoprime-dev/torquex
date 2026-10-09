/* TorqueX-сайт: проверка состояний кнопки «Собрать файл» и словаря.
 *
 * Зачем это есть. В прошлом раунде сайт молча сломался от синтаксической
 * ошибки в web/i18n.js, а статические тесты её не заметили: они читают файл
 * регулярками, а не исполняют его. Здесь словарь и сборщик заявки именно
 * **исполняются** в node:vm — любая ошибка разбора или обращение к
 * несуществующему ключу валит проверку.
 *
 * Что проверяется:
 *  1. web/config.js, web/i18n.js, web/request.js исполняются без ошибок;
 *  2. наборы ключей RU и EN совпадают, подстановки {...} в паре совпадают;
 *  3. все состояния кнопки «Собрать файл» (таблица STATES ниже): для каждого
 *     известно, активна кнопка или нет, и какой текст стоит рядом с ней;
 *  4. каждый блокировщик называет поле (`focus`), и это поле есть в index.html;
 *  5. текст рядом с кнопкой не пустой и не содержит неподставленных {...}.
 *
 * Внешних зависимостей нет — только стандартная библиотека Node.
 * Запуск:  node web/tools/check_build_states.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const WEB = dirname(dirname(fileURLToPath(import.meta.url)));

/* ---------- загрузка браузерных файлов в песочницу ---------- */

function loadWeb() {
  const sandbox = { window: {}, console };
  sandbox.self = sandbox.window;
  sandbox.globalThis = sandbox;
  const context = vm.createContext(sandbox);
  for (const name of ["config.js", "i18n.js", "request.js"]) {
    const code = readFileSync(join(WEB, name), "utf8");
    new vm.Script(code, { filename: `web/${name}` }).runInContext(context);
  }
  const dict = sandbox.window.TORQUEX_I18N;
  const request = sandbox.window.TX && sandbox.window.TX.request;
  if (!dict || !dict.ru || !dict.en) throw new Error("web/i18n.js не отдал window.TORQUEX_I18N");
  if (!request || typeof request.buildRequest !== "function") {
    throw new Error("web/request.js не отдал TX.request.buildRequest");
  }
  return { dict, request, config: sandbox.window.TORQUEX_CONFIG || {} };
}

/* Подстановка такая же, как в ui.js: {name} заменяется значением. */
function translate(dict, lang, key, params) {
  const table = dict[lang] || {};
  if (!Object.prototype.hasOwnProperty.call(table, key)) {
    throw new Error(`нет ключа ${key} в словаре ${lang}`);
  }
  let text = table[key];
  Object.keys(params || {}).forEach((name) => {
    text = text.split(`{${name}}`).join(String(params[name]));
  });
  return text;
}

/* Текст рядом с кнопкой — ровно как его собирает wizard.js (blockerText). */
function blockerText(dict, lang, item) {
  const params = Object.assign({}, item.params || {});
  if (item.options && item.options.length) {
    params.options = item.options.map((key) => translate(dict, lang, key)).join(" · ");
  }
  return translate(dict, lang, item.key, params);
}

/* ---------- состояния кнопки ---------- */

const ACKS = {
  noCatalyst: true,
  legality: true,
  temperature: true,
};

const POPS_MS43 = {
  enabled: true,
  profile: "light",
  delayS: 0.5,
  declaration: "катализатор удалён, машина не для дорог общего пользования",
  thermalWatch: true,
  plan: "проверка на закрытой площадке, 3 прогрева",
};

/* Каждая строка: имя, состояние мастера, ожидание.
 * expect.enabled — активна ли кнопка «Собрать файл».
 * expect.key     — ключ первой строки рядом с кнопкой.
 * expect.focus   — id поля, которое мастер подсветит. */
const STATES = [
  {
    name: "1. ничего не выбрано",
    state: { platform: "MS43" },
    expect: { enabled: false, key: "wizard.block.pick_or_enable", focus: "opt-octane" },
  },
  {
    name: "2. выбран только RON95 (флажок зажигания снят)",
    state: { platform: "MS43", octane: "ron95" },
    expect: { enabled: false, key: "wizard.block.octane_only", focus: "opt-octane" },
  },
  {
    name: "3. RON98 включил добавку сам — ловушки нет",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 1.5 },
    expect: { enabled: true },
  },
  {
    name: "4. RON95 со своим потолком +1.5° — заявка корректна (раунд 4)",
    state: { platform: "MS43", octane: "ron95", ignitionDeg: 1.5 },
    expect: { enabled: true },
  },
  {
    name: "5. RON98 со своим потолком +3° — заявка корректна",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 3 },
    expect: { enabled: true },
  },
  {
    name: "5а. добавка выше потолка профиля — блок с правильным числом",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 3.1 },
    expect: { enabled: false, key: "wizard.block.ignition_range", focus: "opt-octane" },
  },
  {
    name: "5б. RON95 выше +1.5° — тот же блок, но с потолком RON95",
    state: { platform: "MS43", octane: "ron95", ignitionDeg: 1.6 },
    expect: { enabled: false, key: "wizard.block.ignition_range", focus: "opt-octane" },
  },
  {
    name: "5в. RON91 — права на добавку нет",
    state: { platform: "MS43", octane: "ron91", ignitionDeg: 1 },
    expect: { enabled: false, key: "wizard.block.ignition_no_addition", focus: "opt-octane" },
  },
  {
    name: "6. обороты вне диапазона",
    state: { platform: "MS43", revRpm: 12000 },
    expect: { enabled: false, key: "wizard.block.rpm_range", focus: "opt-rev-value" },
  },
  {
    name: "7. отсечка выше стока без подтверждения риска",
    state: { platform: "MS43", revRpm: 6500 },
    expect: { enabled: false, key: "wizard.block.rev_ack", focus: "ack-risk" },
  },
  {
    name: "8. обороты выше порога масляного насоса MS43 без подтверждения",
    state: { platform: "MS43", revRpm: 7000, ackRisk: true },
    expect: { enabled: false, key: "wizard.block.oil_pump", focus: "ack-oil" },
  },
  {
    name: "9. обороты выше порога демпфера MS45 без подтверждения",
    state: { platform: "MS45", revRpm: 7000, transmission: "mt", ackRisk: true, ackOilPump: true },
    expect: { enabled: false, key: "wizard.block.damper", focus: "ack-damper" },
  },
  {
    name: "10. MS41: повышение отсечки без подтверждения риска",
    state: { platform: "MS41", revRpm: 7000 },
    expect: { enabled: false, key: "wizard.block.ms41_ack", focus: "ack-risk" },
  },
  {
    name: "11. скорость вне диапазона",
    state: { platform: "MS43", vmaxKmh: 400 },
    expect: { enabled: false, key: "wizard.block.vmax_range", focus: "opt-vmax-value" },
  },
  {
    name: "12. скорость выше стока без подтверждения риска",
    state: { platform: "MS43", vmaxKmh: 250 },
    expect: { enabled: false, key: "wizard.block.vmax_ack", focus: "ack-risk" },
  },
  {
    name: "13. хлопки без «катализатор удалён»",
    state: { platform: "MS43", pops: POPS_MS43, legality: true, temperature: true },
    expect: { enabled: false, key: "wizard.block.pops_cat", focus: "pk-no-catalyst" },
  },
  {
    name: "14. хлопки без правовой справки",
    state: { platform: "MS43", pops: POPS_MS43, noCatalyst: true, temperature: true },
    expect: { enabled: false, key: "wizard.block.pops_legal", focus: "pk-legality" },
  },
  {
    name: "15. хлопки без контроля температуры",
    state: { platform: "MS43", pops: POPS_MS43, noCatalyst: true, legality: true },
    expect: { enabled: false, key: "wizard.block.pops_temp", focus: "pk-temperature" },
  },
  {
    name: "16. MS43: хлопки без описания условий",
    state: {
      platform: "MS43",
      pops: Object.assign({}, POPS_MS43, { declaration: "" }),
      ...ACKS,
    },
    expect: { enabled: false, key: "wizard.block.pops_declaration", focus: "pk-no-catalyst" },
  },
  {
    name: "17. MS43: хлопки без контроля температуры выпуска",
    state: {
      platform: "MS43",
      pops: Object.assign({}, POPS_MS43, { thermalWatch: false }),
      ...ACKS,
    },
    expect: { enabled: false, key: "wizard.block.pops_thermal", focus: "pk-temperature" },
  },
  {
    name: "18. MS43: хлопки без плана проверки",
    state: {
      platform: "MS43",
      pops: Object.assign({}, POPS_MS43, { plan: "" }),
      ...ACKS,
    },
    expect: { enabled: false, key: "wizard.block.pops_plan", focus: "pk-temperature" },
  },
  {
    name: "19. MS45: задержка хлопков вне диапазона",
    state: {
      platform: "MS45",
      transmission: "mt",
      pops: Object.assign({}, POPS_MS43, { delayS: 9 }),
      ...ACKS,
    },
    expect: { enabled: false, key: "wizard.block.pops_delay", focus: "opt-pops" },
  },
  {
    name: "20. MS41: хлопков нет на этом ЭБУ",
    state: { platform: "MS41", pops: POPS_MS43, ...ACKS },
    expect: { enabled: false, key: "wizard.s3.pops_disabled", focus: "opt-pops" },
  },
  {
    name: "21. MS41: отсечка с подтверждением риска",
    state: { platform: "MS41", revRpm: 7000, ackRisk: true, ackOilPump: true },
    expect: { enabled: true },
  },
  {
    name: "22. всё готово: RON98 + отсечка + скорость + хлопки",
    state: {
      platform: "MS43",
      octane: "ron98",
      ignitionDeg: 1.5,
      revRpm: 7000,
      vmaxKmh: 250,
      transmission: "manual",
      pops: POPS_MS43,
      ackRisk: true,
      ackOilPump: true,
      ...ACKS,
    },
    expect: { enabled: true },
  },
  /* VIN: сервер сообщает vin_required. Без требования VIN ничего не блокирует;
   * с требованием — 17 символов ISO 3779 (без I, O, Q), иначе кнопка серая. */
  {
    name: "23. VIN обязателен, поле пусто — кнопка серая, текст называет VIN",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 1.5, vinRequired: true, vin: "" },
    expect: { enabled: false, key: "wizard.block.vin_missing", focus: "opt-vin" },
  },
  {
    name: "23а. VIN обязателен, 16 символов — кнопка серая, проверка VIN",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 1.5, vinRequired: true, vin: "WBAEV11000KL1234" },
    expect: { enabled: false, key: "wizard.block.vin_invalid", focus: "opt-vin" },
  },
  {
    name: "23б. VIN обязателен, буква I в VIN — кнопка серая, проверка VIN",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 1.5, vinRequired: true, vin: "WBAEV11I00KL12345" },
    expect: { enabled: false, key: "wizard.block.vin_invalid", focus: "opt-vin" },
  },
  {
    name: "23в. VIN обязателен, верный VIN со строчными и пробелами — кнопка активна",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 1.5, vinRequired: true, vin: "wba ev11000kl12345" },
    expect: { enabled: true },
  },
  {
    name: "23г. VIN не обязателен (сервер не требует) — пустой и неверный VIN не блокируют",
    state: { platform: "MS43", octane: "ron98", ignitionDeg: 1.5, vinRequired: false, vin: "WBAEV1100OKL1234" },
    expect: { enabled: true },
  },
];

/* ---------- прогон ---------- */

const failures = [];
function check(condition, message) {
  if (!condition) failures.push(message);
}

const { dict, request, config } = loadWeb();
const html = readFileSync(join(WEB, "index.html"), "utf8");
const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
// подтверждения хлопков в интерфейсе подставляются самим включением (галочек нет),
// но request.js по-прежнему защищает заявку на уровне API
const UI_UNREACHABLE_FOCUS = new Set(["pk-no-catalyst", "pk-legality", "pk-temperature"]);

/* 1. Словарь: наборы ключей и подстановки. */
const ruKeys = Object.keys(dict.ru).sort();
const enKeys = Object.keys(dict.en).sort();
check(ruKeys.length === enKeys.length && ruKeys.every((key, index) => key === enKeys[index]),
  `наборы ключей RU/EN различаются: только RU ${ruKeys.filter((k) => !(k in dict.en))}, ` +
  `только EN ${enKeys.filter((k) => !(k in dict.ru))}`);
for (const key of ruKeys) {
  if (!(key in dict.en)) continue;
  const ru = new Set([...String(dict.ru[key]).matchAll(/\{(\w+)\}/g)].map((m) => m[1]));
  const en = new Set([...String(dict.en[key]).matchAll(/\{(\w+)\}/g)].map((m) => m[1]));
  check(ru.size === en.size && [...ru].every((name) => en.has(name)),
    `подстановки различаются в ${key}: RU {${[...ru]}} / EN {${[...en]}}`);
}

/* 2. Умолчания октана: RON98 включает добавку, RON95/RON91 снимают. */
check(request.octaneDefaults("MS43", "ron98").ignition === true,
  "RON98 обязан включать добавку к зажиганию");
check(request.octaneDefaults("MS43", "ron98").ignitionDeg === request.THRESHOLDS.ignitionMaxDeg,
  "RON98 обязан ставить безопасный максимум добавки");
/* Раунд 4 (§2.1): RON95 получил право Stage 1 — галочка включается со
 * своим потолком, а не снимается. */
check(request.octaneDefaults("MS43", "ron95").ignition === true,
  "RON95 обязан включать добавку +1.5° (потолок профиля без лога)");
check(request.octaneDefaults("MS43", "ron95").ignitionDeg === request.THRESHOLDS.octaneNoLogDeg.ron95,
  "RON95 обязан ставить именно свой потолок из зеркала спецификации");
check(request.octaneDefaults("MS43", "ron91").ignition === false,
  "RON91 обязан снимать добавку");
check(request.octaneDefaults("MS41", "ron98") === null,
  "у MS41 нет октана и зажигания — умолчаний быть не должно");

/* 3. Состояния кнопки. */
for (const item of STATES) {
  const built = request.buildRequest(item.state);
  const enabled = built.blockers.length === 0;
  check(enabled === item.expect.enabled,
    `${item.name}: кнопка ${enabled ? "активна" : "серая"}, ожидалось ` +
    `${item.expect.enabled ? "активна" : "серая"} (блокировщики: ` +
    `${built.blockers.map((b) => b.key).join(", ") || "нет"})`);
  if (item.expect.enabled) {
    check(built.request.intents && built.request.intents.length > 0,
      `${item.name}: заявка без намерений — генератор ответит NOTHING_REQUESTED`);
    continue;
  }
  const first = built.blockers[0];
  if (!first) continue;
  check(first.key === item.expect.key,
    `${item.name}: первый блокировщик ${first.key}, ожидался ${item.expect.key}`);
  check(first.focus === item.expect.focus,
    `${item.name}: focus ${first.focus}, ожидался ${item.expect.focus}`);
  check(!first.focus || ids.has(first.focus) || UI_UNREACHABLE_FOCUS.has(first.focus),
    `${item.name}: поля #${first.focus} нет в index.html`);
  for (const lang of ["ru", "en"]) {
    let text = "";
    try {
      text = blockerText(dict, lang, first);
    } catch (error) {
      failures.push(`${item.name} [${lang}]: ${error.message}`);
      continue;
    }
    check(text.trim().length > 0, `${item.name} [${lang}]: пустой текст подсказки`);
    check(!/\{\w+\}/.test(text), `${item.name} [${lang}]: неподставленная переменная в «${text}»`);
  }
}

/* 4. Все блокировщики и предупреждения вообще обязаны иметь focus и перевод. */
for (const item of STATES) {
  const built = request.buildRequest(item.state);
  for (const blocker of built.blockers) {
    check(typeof blocker.focus === "string" && blocker.focus.length > 0,
      `${item.name}: блокировщик ${blocker.key} не называет поле (focus)`);
  }
  for (const warning of built.warnings) {
    for (const lang of ["ru", "en"]) {
      try {
        blockerText(dict, lang, warning);
      } catch (error) {
        failures.push(`${item.name} [${lang}] предупреждение: ${error.message}`);
      }
    }
  }
}

/* 5. Сводка «Что уйдёт в заявку» переводится целиком. */
const FULL_STATE = STATES.find((item) => item.name.startsWith("22."));
const full = request.buildRequest(FULL_STATE.state);
const described = request.describeRequest(full.request);
check(described.length >= 4, "сводка полной заявки обязана перечислить все опции");
for (const line of described) {
  for (const lang of ["ru", "en"]) {
    try {
      translate(dict, lang, line.key, line.params);
    } catch (error) {
      failures.push(`сводка [${lang}]: ${error.message}`);
    }
  }
}

/* 6. config.js пригоден для раздачи с nginx: без схемы и хоста. */
check(!/^[a-z]+:\/\//i.test(String(config.apiBase || "")),
  `config.js: apiBase «${config.apiBase}» содержит схему и хост — сайт перестанет быть переносимым`);
check(!config.donateUrl, "config.js: donateUrl в репозитории обязан быть пустым");

/* ---------- итог ---------- */

if (failures.length) {
  console.error(`FAIL: ${failures.length} проблем(ы)`);
  failures.forEach((line) => console.error(`  - ${line}`));
  process.exit(1);
}
console.log(`OK: состояний кнопки ${STATES.length}, ключей словаря ${ruKeys.length} (RU=EN)`);
