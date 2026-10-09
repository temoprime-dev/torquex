/* TorqueX-сайт: проверка текста на повторы и на единый источник чисел.
 *
 * Зачем. Главная страница — один путь без шагов и вкладок. Текст легко снова
 * размножить (фраза про русскоязычный наряд, «Подробнее» на каждом шаге), поэтому
 * этот чекер статикой держит инварианты нового потока:
 *   1. общая фраза про язык наряда — ровно один раз (в подвале, web/ui.js);
 *   2. на главной нет аккордеонов «Подробнее», FAQ и шагов;
 *   3. первая карточка — «Базовая настройка (Stage 1)», её строки берутся из
 *      словаря (wizard.s1.s1_*), каждая строка — одна фраза, без «Подробнее»;
 *   4. числа потолков в подписях совпадают с зеркалом спецификации в
 *      web/request.js (один источник — docs/pack_log_requirements.json);
 *   5. честный статус и правовая ссылка по хлопкам остаются в тексте сайта;
 *   6. одна и та же RU-фраза (≥6 слов) не стоит в двух ключах.
 *
 * Внешних зависимостей нет.  Запуск:  node web/tools/check_copy.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const WEB = dirname(dirname(fileURLToPath(import.meta.url)));
const read = (name) => readFileSync(join(WEB, name), "utf8");

const failures = [];
let checks = 0;
function check(condition, message) {
  checks += 1;
  if (!condition) failures.push(message);
}

const index = read("index.html");
const flow = read("flow.js");
const ui = read("ui.js");
const requestJs = read("request.js");

/* словарь — исполняем так же, как check_build_states.mjs */
const sandbox = { window: {}, console };
sandbox.self = sandbox.window;
sandbox.globalThis = sandbox;
const context = vm.createContext(sandbox);
for (const name of ["config.js", "i18n.js", "request.js"]) {
  new vm.Script(read(name), { filename: `web/${name}` }).runInContext(context);
}
const dict = sandbox.window.TORQUEX_I18N;
const request = sandbox.window.TX && sandbox.window.TX.request;
if (!dict || !request) throw new Error("словарь/заявка не загрузились");
const ru = dict.ru;
const en = dict.en;

/* 1. Фраза про русский наряд — один раз на весь интерфейс. */
const languageNote = "common.engine_language_note";
check(!index.includes(languageNote), "index.html не должен повторять фразу про язык наряда (она в подвале)");
check((flow.match(new RegExp(languageNote, "g")) || []).length === 1,
  "фраза про язык наряда обязана остаться ровно один раз — в блоке «Для специалистов» результата (web/flow.js)");
check(!ui.includes(languageNote), "в подвале (web/ui.js) фразы про язык наряда быть не должно: подвал только ссылки");

/* 2. Главная без аккордеонов «Подробнее», FAQ и шагов. */
check(!/data-i18n="common\.more"/.test(index), "на главной не должно быть «Подробнее»");
check(!/more-information|class="faq"|class="why"|variant-more/.test(index),
  "на главной не должно остаться блоков «Больше информации», FAQ и вариантов");
check(!/data-step=|class="steps"|id="step-/.test(index), "на главной не должно быть шагов");

/* 3. Stage 1: заголовок и строки «что даёт» — из словаря, без «Подробнее». */
check(/data-i18n="wizard\.fn\.stage1"/.test(index), "первая карточка должна называться «Базовая настройка (Stage 1)»");
check(/id="stage1-lines"/.test(index), "в index.html нет списка «что даёт» (stage1-lines)");
const briefKeys = ["wizard.s1.s1_ign_writes", "wizard.s1.s1_ign_gated", "wizard.s1.s1_limiters",
  "wizard.s1.s1_pops", "wizard.s1.s1_file"];
for (const key of briefKeys) {
  check(typeof ru[key] === "string" && ru[key].length > 0, `нет RU-ключа ${key}`);
  check(typeof en[key] === "string" && en[key].length > 0, `нет EN-ключа ${key}`);
  check(!/подробнее|more\b/i.test(String(ru[key] || "")), `строка ${key} не должна звать «Подробнее»`);
  check(String(ru[key] || "").split(/[.!?]\s/).filter(Boolean).length <= 1,
    `строка ${key} длиннее одной фразы`);
}
check(/\{noLog\}/.test(ru["wizard.s1.s1_ign_writes"]) && /\{ron95\}/.test(ru["wizard.s1.s1_ign_writes"]),
  "числа добавки должны подставляться из THRESHOLDS, а не быть написаны в словаре");

/* 4. Числа подписей = зеркало спецификации из request.js (один источник). */
const th = request.THRESHOLDS;
check(th.octaneNoLogDeg.ron98 === th.ignitionMaxDeg,
  "потолок RON98 в заявке и потолок формы обязаны быть одним числом");
const expected = { ron98: "3", ron95: "1,5", ron91: "0" };
for (const [profile, text] of Object.entries(expected)) {
  const shown = `${th.octaneNoLogDeg[profile]}`.replace(".", ",");
  check(shown === text, `request.js: потолок ${profile} = ${th.octaneNoLogDeg[profile]}, а подпись обещает ${text}`);
}

/* 5. Честный статус и правовая ссылка — вырезать нельзя. */
check(/bytes\+crc verified/.test(ru["trust.bytes_crc"]) && /нет/.test(ru["trust.field_none"]),
  "строка статуса «bytes+crc verified · field verified: нет» обязана остаться");
check(/ещё не проверялась/.test(ru["wizard.s4.trust_line"]),
  "под результатом обязана стоять строка «на реальной машине эта версия ещё не проверялась»");
check(/715\/2007/.test(ru["wizard.s3.pops_help"]), "справка по хлопкам обязана называть 715/2007");
check(/legality/.test(requestJs), "request.js обязан требовать правовое подтверждение хлопков");

/* 6. Повторы целых фраз: одна и та же RU-строка (≥6 слов) не может быть
 *    значением двух ключей. */
const seen = new Map();
for (const [key, value] of Object.entries(ru)) {
  if (typeof value !== "string") continue;
  const words = value.split(/\s+/).filter(Boolean);
  if (words.length < 6) continue;
  const normalized = value.trim().toLowerCase().replace(/[.»"„”]/g, "");
  if (seen.has(normalized)) {
    check(false, `фраза повторяется в ключах ${seen.get(normalized)} и ${key}`);
  } else {
    seen.set(normalized, key);
  }
}

if (failures.length) {
  console.log(`FAIL: ${failures.length} из ${checks}`);
  for (const line of failures) console.log(`  - ${line}`);
  process.exit(1);
}
console.log(`OK: проверок ${checks}`);
