/* TorqueX-сайт: поведение страницы «Обратная связь» (feedback.html).
 *
 * Страница грузится в jsdom со своими скриптами, fetch подменяется. Проверяем:
 *   1. кнопка «Отправить» всегда активна; короткий текст не уходит на сервер и
 *      даёт понятное сообщение;
 *   2. нормальное сообщение уходит POST на api/feedback с видом, текстом,
 *      контактом и контекстом файла из адреса страницы;
 *   3. скрытое поле-приманка называется нейтрально (fb-extra, не «website»);
 *   4. лимит (429) и ошибка сервера показывают текст, а не молчат;
 *   5. в localStorage ничего, кроме языка и темы.
 *
 *   node web/tools/check_feedback.mjs
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
  console.log("SKIP: jsdom не установлен (npm install jsdom). Проверка feedback.html пропущена.");
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

function openPage(fetchImpl, query = "") {
  const html = readFileSync(join(WEB, "feedback.html"), "utf8");
  const dom = new JSDOM(html, {
    url: "http://localhost:8123/feedback.html" + query, runScripts: "dangerously", pretendToBeVisual: true,
  });
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

function type(dom, id, value) {
  const node = $(dom, id);
  node.value = value;
  node.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

function submit(dom) {
  $(dom, "fb-form").dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
}

function makeFetch(answers) {
  const calls = [];
  const fetchImpl = (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).includes("/feedback")) return jsonResponse(...(answers.shift() || [{ status: "RECEIVED" }]));
    return jsonResponse({ status: "ok" });
  };
  return { calls, fetchImpl };
}

/* ---- 1–3: короткий текст, нормальный, контекст ---- */
{
  const { calls, fetchImpl } = makeFetch([[{ status: "RECEIVED" }]]);
  const dom = openPage(fetchImpl, "?kind=result_problem&platform=MS43&version=430069&sha=1a2b3c4d5e6f");
  await settle();
  check(!$(dom, "fb-submit").disabled, "кнопка «Отправить» активна с самого начала");
  check($(dom, "fb-kind").value === "result_problem", "вид сообщения берётся из адреса");
  check($(dom, "fb-context").textContent.includes("MS43") && !$(dom, "fb-context").hidden, "контекст файла показан");
  check(!$(dom, "fb-extra") === false && !dom.window.document.getElementById("fb-website"),
    "приманка называется нейтрально (fb-extra), поля website нет");

  type(dom, "fb-message", "ой");
  submit(dom);
  await settle();
  check(calls.filter((c) => c.url.includes("/feedback")).length === 0, "короткий текст не уходит на сервер");
  check($(dom, "fb-status").textContent.length > 0, "короткий текст: показано, что нужно написать больше");

  type(dom, "fb-message", "Прошил, на холодную рывки при 2000 об/мин");
  type(dom, "fb-contact", "@driver");
  submit(dom);
  await settle();
  const sent = calls.filter((c) => c.url.includes("/feedback"));
  check(sent.length === 1 && sent[0].options.method === "POST", "нормальное сообщение уходит POST один раз");
  const form = sent[0] && sent[0].options.body;
  check(form && form.get("kind") === "result_problem" && form.get("message").includes("рывки")
    && form.get("contact") === "@driver" && form.get("platform") === "MS43" && form.get("sha12") === "1a2b3c4d5e6f",
    "в форме есть вид, текст, контакт и контекст файла");
  check(form && form.get("website") === "", "поле-приманка уходит пустым");
  check($(dom, "fb-status").textContent.includes("Спасибо"), "после отправки показано «Спасибо»");
  check($(dom, "fb-message").value === "", "поле сообщения очищено");
  const keys = Object.keys(dom.window.localStorage);
  check(keys.every((key) => key === "torquex.lang" || key === "torquex.theme"), `localStorage: ${keys.join(",")}`);
}

/* ---- 4: лимит и ошибка сервера ---- */
{
  const { fetchImpl } = makeFetch([
    [{ code: "FEEDBACK_RATE_LIMIT", title_ru: "лимит", title_en: "limit" }, 429],
    [{ code: "FEEDBACK_UNAVAILABLE", title_ru: "FEEDBACK_UNAVAILABLE: приём не настроен", title_en: "x" }, 503],
  ]);
  const dom = openPage(fetchImpl);
  await settle();
  type(dom, "fb-message", "достаточно длинное сообщение");
  submit(dom);
  await settle();
  check($(dom, "fb-status").textContent.includes("много сообщений"), "лимит: понятный текст");
  submit(dom);
  await settle();
  check($(dom, "fb-status").textContent.includes("приём не настроен"), "ошибка сервера показана текстом");
  check(!$(dom, "fb-submit").disabled, "после ошибки можно отправить снова");
}

if (failures.length) {
  console.log(`FAIL: ${failures.length} из ${checks}`);
  for (const failure of failures) console.log(" - " + failure);
  process.exit(1);
}
console.log(`OK: feedback.html — ${checks} проверок`);
