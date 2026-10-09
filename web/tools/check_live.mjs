/* TorqueX-сайт: проход мастера против НАСТОЯЩЕГО API.
 *
 * check_wizard.mjs проверяет поведение на заглушках. Этот скрипт ничего не
 * подменяет: страница грузится по HTTP с работающего dev_server, скрипты
 * тянутся с него же, а /detect и /build уходят в живой генератор на реальном
 * стоке из репозитория. Именно так были найдены четыре ошибки отрисовки
 * результата, которых не видно на демо-данных.
 *
 * Подготовка (три терминала или фон):
 *   ../.venv/bin/uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8081
 *   python3 web/tools/dev_server.py --host 127.0.0.1 --port 8123 --api http://127.0.0.1:8081
 *   npm install jsdom        # вне дерева репозитория, см. web/README_RU.md
 *
 * Запуск:
 *   node web/tools/check_live.mjs
 *   node web/tools/check_live.mjs --base http://127.0.0.1:8123 \
 *        --stock MS43/firmware_archive/E39_530__c6668528_Ca430066_Stok.bin \
 *        --reject-stock MS43/firmware_archive/E39_525__b137b57g_Ca430037_Stok.bin
 *
 * Без jsdom или без поднятого сервера печатает SKIP и выходит с нулём:
 * это ручная проверка перед релизом, а не часть обязательного прогона.
 * Файлы стоков берутся из репозитория; клиентские стоки сюда не подставляйте.
 */
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, basename } from "node:path";

const ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const BASE = arg("base", "http://127.0.0.1:8123").replace(/\/+$/, "");
const STOCK = join(ROOT, arg("stock", "MS43/firmware_archive/E39_530__c6668528_Ca430066_Stok.bin"));
const REJECT_STOCK = join(ROOT, arg("reject-stock", "MS43/firmware_archive/E39_525__b137b57g_Ca430037_Stok.bin"));

let JSDOM;
try {
  ({ JSDOM } = await import("jsdom"));
} catch {
  console.log("SKIP: jsdom не установлен (npm install jsdom вне дерева проекта).");
  process.exit(0);
}
for (const path of [STOCK, REJECT_STOCK]) {
  if (!existsSync(path)) {
    console.log(`SKIP: нет файла стока ${path}`);
    process.exit(0);
  }
}
try {
  const probe = await fetch(`${BASE}/api/health`);
  const health = await probe.json();
  console.log(`API: ${JSON.stringify(health)}`);
} catch (error) {
  console.log(`SKIP: ${BASE}/api/health недоступен — поднимите dev_server и бэкенд.`);
  process.exit(0);
}

const failures = [];
let checks = 0;
function check(condition, message) {
  checks += 1;
  if (!condition) failures.push(message);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function openLive() {
  const dom = await JSDOM.fromURL(`${BASE}/index.html`, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  /* fetch у Node не знает base URL документа, а сайт ходит относительными
   * путями — разрешаем их так же, как это делает браузер. */
  window.fetch = (resource, options) =>
    globalThis.fetch(new URL(String(resource), `${BASE}/index.html`), options);
  window.FormData = globalThis.FormData;
  window.File = globalThis.File;
  window.Blob = globalThis.Blob;
  window.AbortController = globalThis.AbortController;
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  /* Ждём не только скрипты, но и инициализацию мастера: подсказка под шагами
   * заполняется в обработчике готовности, а до него нет и слушателей. */
  for (let index = 0; index < 100; index += 1) {
    const hint = window.document.getElementById("steps-hint");
    if (window.TX && hint && (hint.textContent || "").trim()) break;
    await sleep(100);
  }
  return dom;
}

function $(dom, id) {
  return dom.window.document.getElementById(id);
}
function text(dom, id) {
  const node = $(dom, id);
  return node ? (node.textContent || "").replace(/\s+/g, " ").trim() : "";
}
function click(dom, id) {
  $(dom, id).dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
}
function set(dom, id, value) {
  const node = $(dom, id);
  if (node.type === "checkbox") node.checked = !!value;
  else node.value = String(value);
  node.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  node.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
}
function dropStock(dom, path) {
  const file = new globalThis.File([readFileSync(path)], basename(path), {
    type: "application/octet-stream",
  });
  const event = new dom.window.Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", { value: { files: [file] } });
  $(dom, "dropzone").dispatchEvent(event);
}
async function waitFor(condition, attempts = 120) {
  for (let index = 0; index < attempts; index += 1) {
    if (condition()) return true;
    await sleep(250);
  }
  return false;
}

/* ---------- сценарий 1: успешная сборка ---------- */

async function liveBuild() {
  const dom = await openLive();
  check(!!dom.window.TX, "скрипты сайта не загрузились по HTTP");
  check(dom.window.TX.isDemo() === false, "страница обязана быть в живом режиме, а не в демо");

  dropStock(dom, STOCK);
  const detected = await waitFor(() => !$(dom, "functions").hidden);
  check(detected, `определение не прошло: ${text(dom, "detect-error").slice(0, 160)}`);
  if (!detected) { dom.window.close(); return; }
  const facts = text(dom, "detect-facts");
  console.log(`  /detect → ${facts}`);
  check(/MS4\d/.test(facts), "в фактах нет платформы");

  await sleep(200);

  const emptyHint = text(dom, "build-hint");
  console.log(`  пустая заявка → «${emptyHint}»`);
  check($(dom, "build").disabled, "на пустой заявке кнопка обязана быть серой");
  check(/Выберите бензин|выберите|включите/i.test(emptyHint), "подсказка обязана говорить, что выбрать");

  set(dom, "opt-octane", "ron98");
  await sleep(100);
  console.log(`  RON98 → добавка ${$(dom, "ignition-auto").hidden ? "не видна" : "видна"}, ` +
    `кнопка ${$(dom, "build").disabled ? "серая" : "активна"}`);
  check(!$(dom, "ignition-auto").hidden, "RON98 обязан включать добавку к зажиганию");
  check(!$(dom, "build").disabled, "после RON98 кнопка обязана быть активной");
  check(/3/.test(text(dom, "ignition-auto")),
    "мастер обязан объяснить включённую добавку: потолок профиля RON98 (3°) в подсказке");

  set(dom, "opt-rev", true);
  set(dom, "opt-rev-value", 7000);
  await sleep(100);
  const riskHint = text(dom, "build-hint");
  console.log(`  7000 об/мин → «${riskHint.slice(0, 90)}…»`);
  check(/риск|risk/i.test(riskHint), "повышение отсечки обязано требовать подтверждения риска");
  set(dom, "ack-risk", true);
  await sleep(100);
  const oilHint = text(dom, "build-hint");
  console.log(`  риск подтверждён → «${oilHint.slice(0, 90)}…»`);
  check(/6750/.test(oilHint), "выше порога масляного насоса обязано требоваться подтверждение");
  set(dom, "ack-oil", true);
  set(dom, "opt-vmax", true);
  set(dom, "opt-vmax-value", 250);
  await sleep(100);
  check(!$(dom, "build").disabled, "с подтверждениями кнопка обязана стать активной");

  click(dom, "build");
  const built = await waitFor(() => !$(dom, "result").hidden, 240);
  await sleep(500);
  check(built, "шаг 4 не открылся");
  const status = text(dom, "result-title");
  const body = text(dom, "result-body");
  console.log(`  /build → ${status}`);
  check(/собран|built/i.test(status), `ожидалась успешная сборка, пришло: ${status}`);

  const download = $(dom, "download");
  check(!!download, "кнопка «Скачать» не появилась");
  check(download && download.className.includes("btn-big"), "кнопка «Скачать» обязана быть крупной");
  check(!body.includes("[object Object]"), "в результате есть [object Object]");

  const changed = /Изменено ячеек \/ байт\s*(\d+)\s*\/\s*(\d+)/.exec(body);
  console.log(`  изменено ячеек/байт: ${changed ? `${changed[1]} / ${changed[2]}` : "НЕ ЗАПОЛНЕНО"}`);
  check(!!changed, "«Изменено ячеек / байт» обязано заполняться на живом манифесте");
  check(/Что мы изменили/.test(body) && /Что не изменено/.test(body),
    "на странице результата обязаны быть оба блока");
  check(/Откат/.test(body), "блок отката обязан быть на странице результата");
  check(/sha256/i.test(body), "sha256 результата обязан быть показан");

  /* Адрес файла обязан оставаться относительным: сайт должен работать и из
   * подкаталога, и за любым прокси. */
  const link = dom.window.TX.api.jobFileUrl("job-id");
  console.log(`  адрес файла (относительный): ${link}`);
  check(!/^https?:\/\//.test(link), "адрес файла обязан быть относительным");

  dom.window.close();
}

/* ---------- сценарий 2: живой отказ генератора ---------- */

async function liveRefusal() {
  const dom = await openLive();
  dropStock(dom, REJECT_STOCK);
  const detected = await waitFor(() => !$(dom, "functions").hidden);
  check(detected, "сток для отказа не определился (ожидалось успешное /detect)");
  if (!detected) { dom.window.close(); return; }

  await sleep(200);
  set(dom, "opt-rev", true);
  set(dom, "opt-rev-value", 6500);
  await sleep(100);
  check($(dom, "build").disabled, "без подтверждения риска кнопка обязана быть серой");
  console.log(`  без подтверждения риска → «${text(dom, "build-hint")}»`);
  set(dom, "ack-risk", true);
  await sleep(100);
  check(!$(dom, "build").disabled, "с подтверждением риска кнопка обязана стать активной");

  click(dom, "build");
  const shown = await waitFor(() => !$(dom, "result").hidden, 240);
  await sleep(500);
  check(shown, "шаг 4 не открылся на отказе");
  const status = text(dom, "result-title");
  const refusal = text(dom, "result-body");
  console.log(`  /build → ${status}`);
  console.log(`  причина: ${refusal.slice(0, 160)}`);
  check(/отказ|refus/i.test(status), `ожидался отказ, пришло: ${status}`);
  check(refusal.length > 0, "причина отказа не показана");
  check(!/Сервер недоступен/.test(refusal),
    "настоящая причина отказа не должна подменяться словами «сервер недоступен»");

  dom.window.close();
}

/* ---------- запуск ---------- */

const scenarios = [
  ["живая сборка на реальном стоке", liveBuild],
  ["живой отказ генератора", liveRefusal],
];

for (const [name, run] of scenarios) {
  const before = failures.length;
  console.log(`\n== ${name}`);
  try {
    await run();
  } catch (error) {
    failures.push(`${name}: ${error && error.stack ? error.stack.split("\n")[0] : error}`);
  }
  for (let index = before; index < failures.length; index += 1) {
    console.log(`  FAIL ${failures[index]}`);
  }
}

console.log("");
if (failures.length) {
  console.error(`FAIL: ${failures.length} из ${checks} проверок`);
  process.exit(1);
}
console.log(`OK: сценариев ${scenarios.length}, проверок ${checks}`);
