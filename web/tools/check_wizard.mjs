/* TorqueX-сайт: поведенческая проверка потока на главной в настоящем DOM.
 *
 * Зачем. Статические тесты читают разметку регулярками и не видят, работает ли
 * сайт. Здесь index.html грузится в jsdom, скрипты исполняются в том же порядке,
 * что в браузере, а поток проходится кликами: файл → определение блока →
 * функции (Stage 1 первым) → «Собрать файл» → «Скачать».
 *
 * Сценарии:
 *  1. загрузка и определение: строка «Блок: MS43 · 430066 — поддерживается»,
 *     Stage 1 первым, функции открыты, демо-панель скрыта;
 *  2. Stage 1 с бензином 98: добавка включается сама, кнопка активна, сборка,
 *     результат ≤120 слов без «Для специалистов», строка про реальную машину;
 *  3. «оставить как есть» без функций: кнопка серая и говорит, что выбрать;
 *  4. блокировщики: хлопки без подтверждений, обороты выше заводских без
 *     подтверждения, масляный насос выше 6750;
 *  5. неподдерживаемая версия: понятная строка, функции скрыты, зона загрузки жива;
 *  6. сетевая ошибка: честное сообщение, повтор доступен;
 *  7. отказ, причина только в наряде: понятная фраза, «Изменить опции» возвращает;
 *  8. VIN обязателен: поле видно, кнопка ждёт VIN, VIN уходит в запрос;
 *  9. VIN необязателен: поле скрыто, сборка идёт без VIN;
 * 10. /health недоступен: VIN необязателен;
 * 11. демо-панель: видна только с ?demo=1;
 * 12. смена языка посреди потока: состояние и значения не теряются.
 *
 * jsdom не лежит в репозитории: ставится рядом, вне дерева проекта.
 *   npm install jsdom            (в любой папке выше web/, например в корне)
 *   node web/tools/check_wizard.mjs
 * Если jsdom не найден — печатается «SKIP: …» и код выхода 0.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const WEB = dirname(dirname(fileURLToPath(import.meta.url)));

let JSDOM;
try {
  ({ JSDOM } = await import("jsdom"));
} catch (error) {
  console.log("SKIP: jsdom не установлен (npm install jsdom). Проверка потока пропущена.");
  process.exit(0);
}

/* ---------- помощники ---------- */

const failures = [];
let checks = 0;
function check(condition, message) {
  checks += 1;
  if (!condition) failures.push(message);
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settle(times = 25) {
  for (let index = 0; index < times; index += 1) await tick();
}

/* Страница целиком: index.html + скрипты в порядке из разметки. Файлы читаются
 * с диска и подставляются текстом, чтобы jsdom не ходил в сеть. */
function openPage({ fetchImpl, url = "http://localhost:8123/index.html" }) {
  const html = readFileSync(join(WEB, "index.html"), "utf8");
  const dom = new JSDOM(html, { url, runScripts: "dangerously", pretendToBeVisual: true });
  const { window } = dom;
  window.fetch = fetchImpl;
  window.scrollTo = () => {};
  window.URL.createObjectURL = () => "blob:stub";
  window.URL.revokeObjectURL = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  /* Скачивание — ссылка с download: записываем попытку, навигацию не выполняем. */
  window.__downloads = [];
  window.HTMLAnchorElement.prototype.click = function () {
    window.__downloads.push({ href: this.getAttribute("href"), download: this.getAttribute("download") });
  };
  window.matchMedia = window.matchMedia || (() => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }));
  const sources = [...html.matchAll(/<script src="([^"]+)"><\/script>/g)].map((m) => m[1]);
  for (const name of sources) {
    const script = window.document.createElement("script");
    script.textContent = readFileSync(join(WEB, name), "utf8");
    window.document.head.appendChild(script);
  }
  window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
  return dom;
}

function $(dom, id) {
  return dom.window.document.getElementById(id);
}

function click(dom, id) {
  const node = $(dom, id);
  if (!node) throw new Error(`нет элемента #${id}`);
  node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
}

function setValue(dom, id, value) {
  const node = $(dom, id);
  if (!node) throw new Error(`нет элемента #${id}`);
  if (node.type === "checkbox") node.checked = !!value;
  else node.value = String(value);
  node.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  node.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
}

/* Файл кладём через drop: обработчик читает event.dataTransfer.files. */
function dropFile(dom, { name = "stock.bin", size = 524288 } = {}) {
  const file = new dom.window.File([new Uint8Array(size)], name, { type: "application/octet-stream" });
  const event = new dom.window.Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", { value: { files: [file] } });
  $(dom, "dropzone").dispatchEvent(event);
}

function jsonResponse(payload, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
}

function buildText(dom) {
  return ($(dom, "build-hint").textContent || "").trim();
}

/* Слова видимой части узла, без свёрнутых «Для специалистов». */
function visibleWordCount(node) {
  const clone = node.cloneNode(true);
  clone.querySelectorAll("details.specialists").forEach((item) => item.remove());
  clone.querySelectorAll("[hidden]").forEach((item) => item.remove());
  const text = (clone.textContent || "").replace(/\s+/g, " ").trim();
  return text.split(" ").filter((word) => /[A-Za-zА-Яа-яЁё0-9]/.test(word)).length;
}

/* ---------- ответы «как у живого сервера» ---------- */

const DETECT_MS43 = {
  supported: true,
  platform: "MS43",
  version: "430066",
  image_type: "полное чтение 512 КиБ",
  sha256: "c6668528".padEnd(64, "0"),
  software: ["430066"],
  facts: { family: "MS43" },
};

/* MS45: топливо и зажигание доступны (MS42/MS43 закрыты стендовым gate). */
const DETECT_MS45 = {
  supported: true,
  platform: "MS45",
  version: "4560BO00S",
  image_type: "калибровка 116 КиБ",
  sha256: "d1e2".padEnd(64, "0"),
  software: [],
  facts: { family: "4560BO00S" },
};

const DETECT_UNSUPPORTED = {
  supported: false,
  platform: null,
  version: null,
  code: "UNSUPPORTED_IMAGE",
  detail: "размер 65536 байт не похож ни на одно чтение MS41/42/43/45",
};

const BUILT_MS43 = {
  job_id: "test-job",
  status: "BUILT",
  image_sha256: "a".repeat(64),
  image_size: 524288,
  ttl_seconds: 900,
  note: "# Наряд\n\nСборка выполнена.",
  manifest: {
    result_sha256: "a".repeat(64),
    changed_cells: 24,
    changed_bytes: 52,
    packages: [
      { id: "limits", title: "Ограничители", status: "written", detail: "отсечка 7000" },
      { id: "ignition", title: "Зажигание", status: "written", detail: "+3.0°" },
      { id: "launch_control", title: "Лаунч-контроль", status: "requires_data", detail: "нет карты" },
    ],
    requires_data: [
      {
        id: "launch_control",
        title: "Лаунч-контроль",
        code: "LAUNCH_CONTROL_NO_DATA",
        why: "нет проверенного адреса для этой версии",
        needs: ["лог Test0", "подтверждённая карта"],
      },
    ],
    rollback: { stock_sha256: "b".repeat(64), instructions_ru: ["Записать сток обратно."] },
  },
};

/* Отказ живого сервера: rejections пустой, причина только в наряде. */
const REJECTED_NOTE_ONLY = {
  status: "REJECTED",
  rejections: [],
  note: "# Наряд\n\n[reject] NOTHING_REQUESTED: заявка не содержит ни одного намерения\n",
};

const VIN = "WBAPH5C55BA000001";

/* Сервер: /detect, /health (с режимом VIN), /build (пишет VIN в журнал). */
function serverFetch({ detect = DETECT_MS43, vinRequired = false, build = BUILT_MS43, builds = [], healthFails = false } = {}) {
  return (resource, options) => {
    const url = String(resource);
    if (url.includes("/detect")) return jsonResponse(detect);
    if (url.includes("/health")) {
      if (healthFails) return Promise.reject(new Error("offline"));
      return jsonResponse({ status: "ok", vin_required: vinRequired });
    }
    if (url.includes("/build")) {
      builds.push({ vin: options && options.body ? options.body.get("vin") : null });
      return jsonResponse(build, build.status === "REJECTED" ? 422 : 200);
    }
    return jsonResponse({});
  };
}

/* Страница с уже загруженным файлом. */
async function openWithFile(options, file = {}) {
  const dom = openPage(options);
  await settle(5);
  dropFile(dom, file);
  await settle();
  return dom;
}

/* ---------- сценарии ---------- */

async function scenarioDetection() {
  const dom = openPage({ fetchImpl: serverFetch() });
  await settle(5);
  check($(dom, "functions").hidden, "до определения функции скрыты");
  check(!$(dom, "intake").hidden, "первый экран — выбор файла");
  check($(dom, "demo-panel").hidden, "демо-панель скрыта без ?demo=1");

  dropFile(dom);
  await settle();
  check(!$(dom, "detect-line").hidden, "после определения обязана появиться строка-результат");
  const line = $(dom, "detect-text").textContent || "";
  check(/MS43/.test(line) && /430066/.test(line) && /поддерживается/.test(line),
    `строка определения «${line}» не называет блок, версию и статус`);
  check(!$(dom, "functions").hidden, "после определения функции открываются");
  check(!$(dom, "build-bar").hidden, "кнопка «Собрать файл» видна сразу");
  const order = [...$(dom, "functions").querySelectorAll("article.fn")]
    .filter((node) => !node.hidden)
    .map((node) => node.id);
  check(order[0] === "fn-stage1", `первая карточка — Stage 1, порядок: ${order.join(", ")}`);
  const expected = ["fn-stage1", "fn-rev", "fn-vmax", "fn-fullload", "fn-vanos", "fn-pops"];
  check(order.every((id) => expected.includes(id)) &&
    JSON.stringify(order) === JSON.stringify(expected.filter((id) => order.includes(id))),
  `порядок функций: ${order.join(", ")}`);
  check($(dom, "vin-slot").hidden, "VIN без требования сервера не показан");
  dom.window.close();
}

async function scenarioStage1Build() {
  const builds = [];
  const dom = await openWithFile({ fetchImpl: serverFetch({ builds, detect: DETECT_MS45 }) });
  check($(dom, "build").disabled, "без выбора функций кнопка серая");
  setValue(dom, "opt-octane", "ron98");
  await settle(3);

  const ignition = $(dom, "ignition-auto");
  check(!ignition.hidden && /\+3/.test(ignition.textContent || ""),
    "при 98-м добавка зажигания включается сама и называет +3°");
  check(!$(dom, "build").disabled, "Stage 1 с 98-м бензином — кнопка активна");

  click(dom, "build");
  await settle(10);
  check(builds.length === 1, `запрос сборки должен уйти один раз (ушло ${builds.length})`);
  check(!$(dom, "result").hidden, "результат заменяет список функций");
  check($(dom, "functions").hidden, "список функций скрыт на результате");
  check($(dom, "build-bar").hidden, "кнопка «Собрать» скрыта на результате");
  check(/Готово|Done/.test($(dom, "result-title").textContent || ""), "заголовок результата — «Готово»");
  const download = $(dom, "download");
  check(download && /Скачать файл|Download the file/.test(download.textContent || ""), "есть крупная кнопка «Скачать файл»");
  const body = $(dom, "result-body");
  const headings = [...body.querySelectorAll("h3")].map((node) => node.textContent);
  check(headings.some((text) => /Что мы изменили/.test(text)), "есть блок «Что мы изменили»");
  check(/Как вернуть всё как было/.test(body.textContent || ""), "есть строка «Как вернуть всё как было»");
  check(/реальной машине эта версия ещё не проверялась/.test(body.textContent || ""),
    "есть честная строка про реальную машину");
  const words = visibleWordCount($(dom, "result"));
  check(words <= 120, `страница результата: ${words} слов без «Для специалистов» (лимит 120)`);
  const specialists = body.querySelector("details.specialists");
  check(!!specialists && specialists.open === false, "технический блок свёрнут");
  check(/sha256|Контрольная сумма/i.test(specialists ? specialists.textContent : ""),
    "контрольная сумма живёт в «Для специалистов»");
  click(dom, "download");
  await settle(5);
  const downloads = dom.window.__downloads;
  check(downloads.length === 0, `до таймера файл не выдаётся (запросов ${downloads.length})`);
  const popup = dom.window.document.getElementById("support-popup");
  const getBtn = popup ? popup.querySelector("button") : null;
  check(!!popup && !!popup.querySelector("a[href$='support.html']"), "после «Скачать» открывается окно поддержки со ссылкой");
  check(!!getBtn && getBtn.disabled, "кнопка скачивания в окне неактивна первые секунды");
  await new Promise((resolve) => setTimeout(resolve, 10600));
  check(!!getBtn && !getBtn.disabled, "через 10 секунд кнопка скачивания активна");
  if (getBtn) getBtn.click();
  await settle(5);
  check(!dom.window.document.getElementById("support-popup"), "окно поддержки закрывается после нажатия");
  check(downloads.length === 1, `скачивание должно запросить файл один раз (запросов ${downloads.length})`);
  check(downloads.length === 1 && /^torquex_MS45/.test(downloads[0].download || ""),
    `имя файла скачивания: ${downloads.length ? downloads[0].download : "нет"}`);
  check(!!dom.window.document.querySelector("#tx-toasts .toast"), "скачивание показывает подтверждение");
  dom.window.close();
}

async function scenarioTunedFile() {
  const tuned = { ...DETECT_MS43, calibration: { state: "tuned", reasons: ["ignition_raised:ron98"], checked: 10, matched: 7 } };
  const dom = await openWithFile({ fetchImpl: serverFetch({ detect: tuned }) });
  check($(dom, "functions").hidden, "не сток: функции скрыты");
  check($(dom, "build-bar").hidden, "не сток: кнопка сборки скрыта");
  const text = $(dom, "detect-error").textContent || "";
  check(/не заводской/.test(text) && /после логов/.test(text),
    `не сток: совет вернуть сток и про Stage 2 только после логов («${text.slice(0, 60)}»)`);
  dom.window.close();
  const unknown = { ...DETECT_MS43, calibration: { state: "unknown", reasons: ["not_in_db:fl"], checked: 10, matched: 9 } };
  const dom2 = await openWithFile({ fetchImpl: serverFetch({ detect: unknown }) });
  check(!$(dom2, "functions").hidden, "файла нет в базе: функции остаются");
  check(/нет в нашей базе/.test($(dom2, "detect-error").textContent || ""), "файла нет в базе: предупреждение");
  dom2.window.close();
}

async function scenarioGs8Restore() {
  const detect = {
    ...DETECT_MS43, platform: "GS8", version: "GS8.60.0", supported: true,
    calibration: { system: "GS8.60.0", program: "1423642A", state: "alpina", vehicle: "Alpina B3 (24 10 620)" },
    options: [
      { hash: "aaaaaaaaaaaaaaaaaaaa", vehicle: "E39 M52B25 523i ab 9/98", reference: "B22K4_0419C0KA20" },
      { hash: "bbbbbbbbbbbbbbbbbbbb", vehicle: "E46 M52B25 323i", reference: "B22K4_0419C0K920" },
    ],
  };
  const dom = await openWithFile({ fetchImpl: serverFetch({ detect }) });
  check($(dom, "functions").hidden && $(dom, "build-bar").hidden, "блок АКПП: функций двигателя нет");
  const select = dom.window.document.getElementById("gs8-restore-select");
  const button = dom.window.document.getElementById("gs8-restore");
  check(!!select && select.options.length === 2, "блок АКПП: выбор машины из фабричных калибровок");
  check(!!button, "блок АКПП: кнопка отката есть");
  check(/Alpina/.test($(dom, "detect-error").textContent || ""), "блок АКПП: показано, что это калибровка Alpina");
  check(!dom.window.document.getElementById("gs8-alpina"), "блок АКПП: на Alpina-файл Alpina повторно не предлагается");
  dom.window.close();
}

async function scenarioStockNoFunctions() {
  const dom = await openWithFile({ fetchImpl: serverFetch() });
  check($(dom, "build").disabled, "«оставить как есть» без функций — кнопка серая");
  check(/Выберите бензин или включите функцию|Choose a fuel or turn on a function/.test(buildText(dom)),
    `строка рядом должна говорить, что выбрать: «${buildText(dom)}»`);
  dom.window.close();
}

async function scenarioBlockers() {
  const builds = [];
  const dom = await openWithFile({ fetchImpl: serverFetch({ builds }) });
  setValue(dom, "opt-octane", "ron91");
  await settle(3);

  setValue(dom, "opt-pops", true);
  await settle(3);
  check(!$(dom, "build").disabled, "хлопки: кнопка активна (подтверждения подставляются самим включением)");
  check(!$(dom, "fn-pops").hidden && $(dom, "fn-pops").querySelector(".fn-warn").textContent.length > 20,
    "в карточке хлопков всегда виден короткий текст-предупреждение");
  check(!$(dom, "pk-no-catalyst") && !$(dom, "pk-legality") && !$(dom, "pk-temperature"),
    "галочек подтверждения хлопков в интерфейсе нет");
  await settle(3);
  setValue(dom, "opt-pops", false);
  await settle(3);

  setValue(dom, "opt-rev", true);
  setValue(dom, "opt-rev-value", 7000);
  await settle(3);
  check($(dom, "build").disabled, "обороты выше заводских без подтверждения — кнопка серая");
  check(/подтверждения риска|risk/.test(buildText(dom)), `обороты: строка не называет риск: «${buildText(dom)}»`);
  check(!$(dom, "fn-ack").hidden, "строка подтверждения риска видна");

  setValue(dom, "ack-risk", true);
  await settle(3);
  check($(dom, "build").disabled, "на MS43 выше 6750 нужен масляный насос");
  check(/насос|pump/i.test(buildText(dom)), `строка должна назвать масляный насос: «${buildText(dom)}»`);
  check(!$(dom, "row-ack-oil").hidden, "галочка масляного насоса видна");
  setValue(dom, "ack-oil", true);
  await settle(3);
  check(!$(dom, "build").disabled, "с подтверждением риска и насоса кнопка активна");

  click(dom, "build");
  await settle(10);
  check(builds.length === 1, "заявка с подтверждениями уходит в сборку");
  dom.window.close();
}

async function scenarioUnsupported() {
  const dom = await openWithFile({ fetchImpl: serverFetch({ detect: DETECT_UNSUPPORTED }) });
  const error = $(dom, "detect-error");
  check(!error.hidden, "для неподдерживаемого файла обязана появиться причина");
  check(/копию|файл/i.test(error.textContent || ""), `причина должна быть понятной: «${(error.textContent || "").trim()}»`);
  check(/UNSUPPORTED_IMAGE/.test(error.textContent || ""), "код есть во вложенном «Для специалистов»");
  check($(dom, "functions").hidden, "функции при неподдержанном файле скрыты");
  check(!$(dom, "intake").hidden && !$(dom, "dropzone").hidden, "зона загрузки остаётся для повтора");
  check(!$(dom, "file-choose").disabled, "выбрать другой файл можно");
  dom.window.close();
}

async function scenarioNetworkError() {
  const dom = openPage({ fetchImpl: () => Promise.reject(new Error("network down")) });
  await settle(5);
  dropFile(dom);
  await settle();
  const error = $(dom, "detect-error");
  check(!error.hidden, "при сетевой ошибке обязано появиться сообщение");
  const text = error.textContent || "";
  check(/недоступ|unreachable/i.test(text), `сообщение должно говорить о недоступном сервере: «${text.trim()}»`);
  check(!!error.querySelector("button"), "есть кнопка повтора");
  check(!$(dom, "file-choose").disabled, "после ошибки выбор файла снова работает");
  dom.window.close();
}

async function scenarioRefusalNoteOnly() {
  const dom = await openWithFile({ fetchImpl: serverFetch({ build: REJECTED_NOTE_ONLY, detect: DETECT_MS45 }) });
  setValue(dom, "opt-octane", "ron98");
  await settle(3);
  click(dom, "build");
  await settle(10);
  check(!$(dom, "result").hidden, "отказ показывается на месте результата");
  check(/Файл не собран/.test($(dom, "result-title").textContent || ""), "заголовок — «Файл не собран»");
  const text = $(dom, "result-body").textContent || "";
  check(/Ничего не выбрано/.test(text), `причина из наряда должна стать понятной фразой: «${text.slice(0, 120)}»`);
  const back = [...$(dom, "result-body").querySelectorAll("button")].find((node) => /Изменить опции/.test(node.textContent));
  check(!!back, "есть кнопка «Изменить опции»");
  if (back) {
    back.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await settle(3);
    check(!$(dom, "functions").hidden && $(dom, "result").hidden, "«Изменить опции» возвращает список функций");
    check($(dom, "opt-octane").value === "ron98", "выбор бензина сохраняется");
  }
  dom.window.close();
}

async function scenarioVinRequired() {
  const builds = [];
  const dom = openPage({ fetchImpl: serverFetch({ builds, vinRequired: true, detect: DETECT_MS45 }) });
  await settle(8);
  dropFile(dom);
  await settle();
  check(!$(dom, "vin-slot").hidden, "при требовании сервера поле VIN видно");
  check($(dom, "vin-slot").contains($(dom, "opt-vin")), "поле VIN на видном месте");
  check(/VIN/.test($(dom, "opt-vin-label").textContent || ""), "подпись поля называет VIN");
  setValue(dom, "opt-octane", "ron98");
  await settle(3);
  check($(dom, "build").disabled, "без VIN сборка заблокирована");
  check(/VIN/.test(buildText(dom)), `строка должна называть VIN: «${buildText(dom)}»`);
  setValue(dom, "opt-vin", "wba ph5c55 ba000001");
  await settle(3);
  check($(dom, "opt-vin").value === VIN, "VIN приводится к заглавным без пробелов");
  check(!$(dom, "build").disabled, "с корректным VIN кнопка активна");
  click(dom, "build");
  await settle(10);
  check(builds.length === 1 && builds[0].vin === VIN, "VIN уходит в запрос сборки");
  dom.window.close();
}

async function scenarioVinOptional() {
  const builds = [];
  const dom = openPage({ fetchImpl: serverFetch({ builds, vinRequired: false, detect: DETECT_MS45 }) });
  await settle(8);
  dropFile(dom);
  await settle();
  check($(dom, "vin-slot").hidden, "если сервер VIN не требует, поле скрыто");
  setValue(dom, "opt-octane", "ron98");
  await settle(3);
  check(!$(dom, "build").disabled, "без VIN сборка идёт");
  click(dom, "build");
  await settle(10);
  check(builds.length === 1 && !builds[0].vin, "VIN в запрос не попадает, если его нет");
  dom.window.close();
}

async function scenarioVinHealthDown() {
  const dom = openPage({ fetchImpl: serverFetch({ healthFails: true }) });
  await settle(8);
  dropFile(dom);
  await settle();
  check($(dom, "vin-slot").hidden, "при недоступном /health VIN необязателен");
  dom.window.close();
}

async function scenarioDemoPanel() {
  const demo = openPage({ fetchImpl: serverFetch(), url: "http://localhost:8123/index.html?demo=1" });
  await settle(5);
  check(!$(demo, "demo-panel").hidden, "в демо-режиме панель примеров видна");
  check(demo.window.document.querySelectorAll("[data-demo-scenario]").length === 4, "четыре демо-сценария");
  demo.window.close();
}

async function scenarioLanguageSwitch() {
  const dom = await openWithFile({ fetchImpl: serverFetch({ detect: DETECT_MS45 }) });
  setValue(dom, "opt-octane", "ron98");
  setValue(dom, "opt-rev", true);
  setValue(dom, "opt-rev-value", 7000);
  setValue(dom, "ack-risk", true);
  setValue(dom, "ack-damper", true);
  await settle(3);
  const beforeDisabled = $(dom, "build").disabled;

  const toggle = dom.window.document.querySelector('[data-lang="en"]');
  check(!!toggle, "на странице обязан быть переключатель языка");
  if (toggle) {
    toggle.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await settle(5);
  }
  check(dom.window.TX.lang === "en", "язык обязан переключиться на английский");
  check(/supported/.test($(dom, "detect-text").textContent || ""), "строка определения обязана стать английской");
  check(/Basic tuning/.test($(dom, "functions").textContent || ""), "заголовок Stage 1 переведён");
  check($(dom, "opt-rev-value").value === "7000", "введённые значения не должны теряться");
  check($(dom, "ack-risk").checked && $(dom, "ack-damper").checked, "галочки не должны сбрасываться");
  check($(dom, "build").disabled === beforeDisabled, "состояние кнопки не должно меняться от языка");
  check(!/^wizard\./.test(buildText(dom)), "строка рядом с кнопкой переводится, а не показывает ключ");
  dom.window.close();
}

/* ---------- запуск ---------- */

const scenarios = [
  ["загрузка и определение блока", scenarioDetection],
  ["Stage 1 с бензином 98: сборка и «Скачать»", scenarioStage1Build],
  ["не заводской файл: отказ и совет отката", scenarioTunedFile],
  ["блок АКПП GS8: откат на фабричную калибровку", scenarioGs8Restore],
  ["«оставить как есть» без функций", scenarioStockNoFunctions],
  ["блокировщики: хлопки, обороты, масляный насос", scenarioBlockers],
  ["неподдерживаемая версия", scenarioUnsupported],
  ["сетевая ошибка", scenarioNetworkError],
  ["отказ, причина только в наряде", scenarioRefusalNoteOnly],
  ["VIN обязателен: поле, проверка, запрос", scenarioVinRequired],
  ["VIN необязателен: поле скрыто, сборка идёт", scenarioVinOptional],
  ["VIN: /health недоступен — необязателен", scenarioVinHealthDown],
  ["демо-панель только с ?demo=1", scenarioDemoPanel],
  ["смена языка посреди потока", scenarioLanguageSwitch],
];

for (const [name, run] of scenarios) {
  const before = failures.length;
  try {
    await run();
  } catch (error) {
    failures.push(`${name}: упало с ошибкой — ${error && error.stack ? error.stack : error}`);
  }
  const added = failures.length - before;
  console.log(`${added ? "FAIL" : "ok  "}  ${name}`);
  for (let index = before; index < failures.length; index += 1) {
    console.log(`        ${failures[index]}`);
  }
}

if (failures.length) {
  console.error(`FAIL: ${failures.length} из ${checks} проверок`);
  process.exit(1);
}
console.log(`OK: сценариев ${scenarios.length}, проверок ${checks}`);
