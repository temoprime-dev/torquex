/* TorqueX-сайт: поведение страницы «Разобрать лог и помочь проекту» (logs.html).
 *
 * Страница грузится в jsdom со своими скриптами (с диска, без сети), fetch
 * подменяется ответами «как у живого сервера». Проверяем:
 *   1. log_donation=false → второй флажок скрыт, в форму уходит share_consent=false;
 *   2. кнопка «Разобрать» неактивна без файла и без первого согласия;
 *   3. log_donation=true → флажок виден, но НЕ отмечен; отмеченный уходит
 *      share_consent=true, ответ STORED показывает код удаления «id.токен»;
 *   4. текст сервера выводится как текст (разметка в zone_ru не исполняется);
 *   5. удаление: DELETE на относительный api/logs/<id>, код — в заголовке
 *      X-Delete-Token, не в адресе; 404 показывает текст отказа;
 *   6. ошибка разбора показывает title_ru сервера;
 *   7. в localStorage ничего, кроме языка/темы; код удаления не сохраняется.
 *
 * jsdom не лежит в репозитории (npm install jsdom вне дерева проекта).
 *   node web/tools/check_logs.mjs
 * Без jsdom печатает «SKIP: …» и выходит с нулём.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const WEB = dirname(dirname(fileURLToPath(import.meta.url)));

let JSDOM;
try {
  ({ JSDOM } = await import("jsdom"));
} catch (error) {
  console.log("SKIP: jsdom не установлен (npm install jsdom). Проверка logs.html пропущена.");
  process.exit(0);
}

const failures = [];
let checks = 0;
function check(condition, message) {
  checks += 1;
  if (!condition) failures.push(message);
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settle(times = 30) {
  for (let index = 0; index < times; index += 1) await tick();
}

function jsonResponse(payload, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
}

const ANALYSIS = {
  status: "ANALYZED",
  package: "fuel",
  evidence_id: "ev",
  analysis: {
    rows: 366, duration_s: 36.5, results: { wot_runs: 2 },
    pack_recommendation: {
      zones: [{ zone_ru: "<img src=x id=pwn>", recommendation_ru: "λ min 0.850" }],
      warnings: [],
    },
  },
};

function openPage(fetchImpl) {
  const html = readFileSync(join(WEB, "logs.html"), "utf8");
  const dom = new JSDOM(html, { url: "http://localhost:8123/logs.html", runScripts: "dangerously", pretendToBeVisual: true });
  const { window } = dom;
  window.fetch = fetchImpl;
  window.scrollTo = () => {};
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

const $ = (dom, id) => dom.window.document.getElementById(id);

function chooseFile(dom, name = "drive.csv") {
  const input = $(dom, "log-file");
  const file = new dom.window.File(["time,rpm\n0,900\n"], name, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
}

function tickBox(dom, id, value) {
  const box = $(dom, id);
  box.checked = value;
  box.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
}

function submit(dom) {
  $(dom, "log-form").dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
}

function makeFetch(health, answers) {
  const calls = [];
  const fetchImpl = (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/health")) return jsonResponse(health);
    const next = answers.shift();
    return next ? jsonResponse(next.body, next.status) : jsonResponse({ code: "UNEXPECTED" }, 500);
  };
  return { calls, fetchImpl };
}

/* ---- 1–2: передача выключена ---- */
{
  const { calls, fetchImpl } = makeFetch({ status: "ok", log_donation: false }, [{ body: ANALYSIS, status: 200 }]);
  const dom = openPage(fetchImpl);
  await settle();
  check($(dom, "log-share-row").hidden, "log_donation=false: флажок передачи должен быть скрыт");
  check($(dom, "log-submit").disabled, "без файла кнопка должна быть неактивна");
  chooseFile(dom);
  check($(dom, "log-submit").disabled, "без первого согласия кнопка должна быть неактивна");
  tickBox(dom, "log-consent", true);
  check(!$(dom, "log-submit").disabled, "файл + согласие → кнопка активна");
  submit(dom);
  await settle();
  const post = calls.find((call) => call.options.method === "POST");
  check(post && post.url === "api/logs", `POST на относительный api/logs, а не ${post && post.url}`);
  check(post && post.options.body.get("share_consent") === "false", "без передачи share_consent=false");
  check(post && post.options.body.get("consent") === "true", "consent=true уходит формой");
  check(!$(dom, "log-result").hidden, "результат показан");
  check($(dom, "log-result").querySelector(".mono-row") === null, "без передачи код удаления не показывается");
  check($(dom, "log-delete-code").value === "", "без передачи поле удаления пустое");
  check($(dom, "log-result").querySelector("#pwn") === null, "разметка из ответа сервера не исполняется");
  check($(dom, "log-result").textContent.includes("<img src=x id=pwn>"), "текст сервера показан как текст");
}

/* ---- 3–5: передача включена ---- */
{
  const stored = JSON.parse(JSON.stringify(ANALYSIS));
  stored.share = { status: "STORED", log_id: "AbCdEfGhIjKlMnOpQrStUvWx", delete_token: "tok_SECRET-123", expires: "2027-10-08" };
  const { calls, fetchImpl } = makeFetch({ status: "ok", log_donation: true }, [
    { body: stored, status: 200 },
    { body: { code: "LOG_DELETE_NOT_FOUND", title_ru: "LOG_DELETE_NOT_FOUND: Лог не найден", title_en: "x" }, status: 404 },
    { body: { status: "DELETED" }, status: 200 },
  ]);
  const dom = openPage(fetchImpl);
  await settle();
  check(!$(dom, "log-share-row").hidden, "log_donation=true: флажок передачи виден");
  check(!$(dom, "log-share").checked, "флажок передачи по умолчанию выключен");
  chooseFile(dom);
  tickBox(dom, "log-consent", true);
  tickBox(dom, "log-share", true);
  $(dom, "log-package").value = "fuel";
  submit(dom);
  await settle();
  const post = calls.find((call) => call.options.method === "POST");
  check(post && post.options.body.get("share_consent") === "true", "отмеченная передача → share_consent=true");
  check(post && post.options.body.get("package") === "fuel", "пакет уходит формой");
  const code = "AbCdEfGhIjKlMnOpQrStUvWx.tok_SECRET-123";
  check($(dom, "log-result").textContent.includes(code), "код удаления показан один раз в результате");
  check($(dom, "log-delete-code").value === code, "код подставлен в поле удаления");

  $(dom, "log-delete-code").value = "AbCdEfGhIjKlMnOpQrStUvWx.wrong";
  $(dom, "log-delete").dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  await settle();
  check($(dom, "log-delete-status").textContent.includes("Лог не найден"), "404 показывает текст отказа сервера");

  $(dom, "log-delete-code").value = code;
  $(dom, "log-delete").dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  await settle();
  const deletes = calls.filter((call) => call.options.method === "DELETE");
  check(deletes.length === 2, `ожидалось два DELETE, было ${deletes.length}`);
  const last = deletes[deletes.length - 1];
  check(last && last.url === "api/logs/AbCdEfGhIjKlMnOpQrStUvWx", `DELETE на api/logs/<id>, а не ${last && last.url}`);
  check(last && last.options.headers["X-Delete-Token"] === "tok_SECRET-123", "токен в заголовке X-Delete-Token");
  check(calls.every((call) => !call.url.includes("tok_")), "токен не попадает в адрес");
  check($(dom, "log-delete-status").textContent.length > 0 && $(dom, "log-delete-code").value === "",
        "после удаления поле очищено и показан статус");
  const keys = Object.keys(dom.window.localStorage);
  check(keys.every((key) => key === "torquex.lang" || key === "torquex.theme"), `localStorage: ${keys.join(",")}`);
  check(!JSON.stringify(dom.window.localStorage).includes("tok_"), "код удаления не сохраняется в браузере");
}

/* ---- 6: отказ разбора ---- */
{
  const { fetchImpl } = makeFetch({ status: "ok", log_donation: true }, [
    { body: { code: "LOG_ANALYSIS_REJECTED", title_ru: "LOG_ANALYSIS_REJECTED: Лог не прошёл проверку", title_en: "rejected" }, status: 422 },
  ]);
  const dom = openPage(fetchImpl);
  await settle();
  chooseFile(dom);
  tickBox(dom, "log-consent", true);
  submit(dom);
  await settle();
  check($(dom, "log-result").textContent.includes("Лог не прошёл проверку"), "отказ сервера показан");
  check(!$(dom, "log-submit").disabled, "после отказа можно отправить снова");
}

if (failures.length) {
  console.log(`FAIL: ${failures.length} из ${checks}`);
  for (const failure of failures) console.log(" - " + failure);
  process.exit(1);
}
console.log(`OK: logs.html — ${checks} проверок`);
