#!/usr/bin/env node
/* TorqueX: проверка того, что мастер сайта собирает заявки, которые настоящий
 * генератор принимает (и что без обязательных подтверждений он отказывает).
 *
 * Заявки строит тот же модуль, что и мастер, — web/request.js. Каждая заявка
 * прогоняется через `tools/torquex.py build --dry-run` на реальном стоке из
 * репозитория, поэтому проверка ловит расхождение между сайтом и политикой
 * генератора (например, смену порога или имени поля).
 *
 * Запуск из корня репозитория:
 *   node web/tools/check_requests.mjs
 *   node web/tools/check_requests.mjs --python .venv/bin/python
 *
 * Ничего не пишется (--dry-run) и ничего не отправляется в сеть.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/* web/request.js — тот самый модуль, которым пользуется мастер в браузере
 * (UMD: в Node он отдаёт CommonJS-экспорт). */
import REQUEST from "../request.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");

function arg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const PYTHON = arg("--python", process.platform === "win32" ? "python" : "python3");

/* По одному реальному стоку на каждый ЭБУ. */
const STOCKS = {
  MS45: "MS45/firmware/MS45.0_456BO00S_E83/7561448_0044560BO00S0CO0R.bin",
  MS43: "MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin",
  MS42: "MS42/firmware/Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin",
  MS41: "MS41/firmware/MS41_1429861_E39_520i_M52B20_1997_full.bin",
};

/* Заявки, которые мастер собирает на шагах 2–3 для варианта 1. */
const ACCEPTED = [
  {
    name: "MS45: зажигание + лимитеры + хлопки (вариант 1)",
    platform: "MS45",
    state: {
      platform: "MS45",
      octane: "ron98",
      ignitionDeg: 3.0,
      revRpm: 6800,
      transmission: "mt",
      vmaxKmh: 250,
      ackRisk: true,
      pops: { enabled: true, profile: "light", delayS: 0.5 },
      noCatalyst: true,
      legality: true,
      temperature: true,
      account: "site-check",
      notes: "проверка заявок сайта",
    },
    expect: ["стадия stage1: BUILT", "пакет ignition", "пакет limits", "пакет pops"],
  },
  {
    name: "MS43: зажигание + ранний режим полной нагрузки + мягкий VANOS",
    platform: "MS43",
    state: {
      platform: "MS43",
      octane: "ron98",
      ignitionDeg: 3,
      transmission: "auto",
      fullLoad: true,
      vanos: true,
    },
    expect: ["стадия stage1: BUILT", "пакет ignition", "пакет pedal_response", "пакет vanos_stage1"],
  },
  {
    name: "MS42: зажигание + ранний режим полной нагрузки + мягкий VANOS",
    platform: "MS42",
    state: {
      platform: "MS42",
      octane: "ron98",
      ignitionDeg: 3,
      transmission: "auto",
      fullLoad: true,
      vanos: true,
    },
    expect: ["стадия stage1: READY", "пакет ignition_stage1", "пакет pedal_response", "пакет vanos_stage1"],
  },
  {
    name: "MS43: хлопки только при включённом кондиционере",
    platform: "MS43",
    state: {
      platform: "MS43",
      transmission: "manual",
      pops: {
        enabled: true,
        profile: "light",
        acOnly: true,
        declaration: "катализатор физически снят, машина используется вне дорог общего пользования",
        thermalWatch: true,
        plan: "первые заезды на закрытой площадке, за температурой следит владелец",
      },
      noCatalyst: true,
      legality: true,
      temperature: true,
    },
    expect: ["стадия stage1: BUILT", "пакет popcorn", "только при включённом кондиционере"],
  },
  {
    name: "MS43: зажигание + лимитеры + popcorn (вариант 1)",
    platform: "MS43",
    state: {
      platform: "MS43",
      octane: "ron98",
      ignitionDeg: 1.5,
      revRpm: 6688,
      transmission: "manual",
      vmaxKmh: 250,
      ackRisk: true,
      pops: {
        enabled: true,
        profile: "light",
        declaration: "катализатор физически снят, машина используется вне дорог общего пользования",
        thermalWatch: true,
        plan: "первые заезды на закрытой площадке, за температурой следит владелец",
      },
      noCatalyst: true,
      legality: true,
      temperature: true,
    },
    expect: ["стадия stage1: BUILT", "пакет ignition", "пакет limits", "пакет popcorn"],
  },
  {
    name: "MS42: лимитеры (vmax и мягкая отсечка)",
    platform: "MS42",
    state: {
      platform: "MS42",
      revRpm: 6688,
      transmission: "manual",
      vmaxKmh: 240,
      ackRisk: true,
    },
    expect: ["стадия stage1: READY", "пакет limits"],
  },
  {
    name: "MS41: лимитеры (vmax и отсечка)",
    platform: "MS41",
    state: { platform: "MS41", revRpm: 6700, vmaxKmh: 250, ackRisk: true },
    expect: ["MS41: статус READY", "запрос vmax"],
  },
];

/* Порядок ворот: сервер обязан отказать, даже если клиент пришлёт заявку в
 * обход правил мастера (мастер такие заявки не отправляет — это его блокеры). */
const REFUSED = [
  {
    name: "MS45: хлопки без катализатора, справки и температуры — отказ",
    platform: "MS45",
    state: {
      platform: "MS45",
      revRpm: 6800,
      transmission: "mt",
      ackRisk: true,
      pops: { enabled: true, profile: "light", delayS: 0.5 },
      noCatalyst: true,
      legality: true,
      temperature: true,
    },
    tamper: (request) => {
      delete request.no_catalyst;
      delete request.acknowledge_legality;
      delete request.acknowledge_temperature;
    },
    expect: [/NO_CATALYST_REQUIRED|ACKNOWLEDGE/],
  },
  {
    name: "MS45: обороты выше стока без подтверждения — отказ",
    platform: "MS45",
    state: {
      platform: "MS45",
      octane: "ron98",
      revRpm: 6800,
      transmission: "mt",
      vmaxKmh: 250,
      /* Сайт сам не отпустит такую заявку (блокировщик wizard.block.rev_ack):
       * ставим подтверждение, чтобы заявка собралась, и снимаем его в tamper —
       * проверяем именно ворота генератора. */
      ackRisk: true,
      pops: { enabled: false },
    },
    tamper: (request) => {
      (request.intents || []).forEach((intent) => {
        delete intent.acknowledge_risk;
        delete intent.acknowledge_damper;
      });
    },
    expect: [/ACKNOWLEDGE_(REQUIRED|RISK)/],
  },
  {
    name: "MS43: popcorn без письменной декларации — отказ",
    platform: "MS43",
    state: {
      platform: "MS43",
      revRpm: 6688,
      ackRisk: true,
      pops: {
        enabled: true,
        profile: "light",
        declaration: "катализатор снят",
        thermalWatch: true,
        plan: "первые заезды на площадке под присмотром",
      },
      noCatalyst: true,
      legality: true,
      temperature: true,
    },
    tamper: (request) => {
      delete request.no_catalyst;
      (request.intents || []).forEach((intent) => {
        if (intent.kind === "popcorn") delete intent.no_catalyst;
      });
    },
    expect: [/POPCORN_NO_CATALYST_REQUIRED|NO_CATALYST_REQUIRED|ACKNOWLEDGE_LEGALITY_REQUIRED/],
  },
];

let failures = 0;

function fail(message, output) {
  failures += 1;
  console.error("✗ " + message);
  if (output) {
    console.error(
      String(output)
        .split("\n")
        .map((line) => "    " + line)
        .join("\n"),
    );
  }
}

function build(payload, platform, dir, index) {
  const stock = join(ROOT, STOCKS[platform]);
  if (!existsSync(stock)) {
    throw new Error(`нет стока ${STOCKS[platform]} — проверьте, что репозиторий полный`);
  }
  const file = join(dir, `request-${index}-${platform}.json`);
  writeFileSync(file, JSON.stringify(payload, null, 2), "utf-8");
  try {
    return execFileSync(
      PYTHON,
      ["tools/torquex.py", "build", "--file", stock, "--request", file, "--dry-run"],
      { cwd: ROOT, encoding: "utf-8", stdio: ["ignore", "pipe", "pipe"] },
    );
  } catch (error) {
    const text = `${error.stdout || ""}${error.stderr || ""}`;
    /* Штатный отказ генератора — это результат проверки, а не сбой запуска:
     * код возврата у отказа бывает и нулевым, и ненулевым. */
    if (/\[reject\]|REJECTED/.test(text)) return text;
    throw new Error(`генератор не запустился: ${text || error.message}`);
  }
}

function checkBlockers() {
  /* Блокеры мастера: то, что сайт не отправляет вообще. */
  const cases = [
    {
      name: "мастер блокирует добавку на профиле без права (RON91)",
      state: { platform: "MS45", octane: "ron91", ignitionDeg: 1.5, vmaxKmh: 250 },
      block: "wizard.block.ignition_no_addition",
    },
    {
      name: "мастер блокирует хлопки без подтверждения катализатора",
      state: { platform: "MS45", pops: { enabled: true, profile: "light", delayS: 0.5 } },
      block: "wizard.block.pops_cat",
    },
    {
      name: "мастер блокирует пустую заявку",
      state: { platform: "MS45" },
      block: "wizard.block.pick_or_enable",
    },
    {
      name: "мастер требует подтверждение масляного насоса выше 6750",
      state: { platform: "MS43", revRpm: 6800, ackRisk: true },
      block: "wizard.block.oil_pump",
    },
    {
      name: "мастер требует подтверждение демпфера выше 6900",
      state: { platform: "MS45", revRpm: 7000, ackRisk: true },
      block: "wizard.block.damper",
    },
  ];
  cases.forEach((item) => {
    const built = REQUEST.buildRequest(item.state);
    const keys = built.blockers.map((blocker) => blocker.key);
    if (keys.indexOf(item.block) < 0) {
      fail(`${item.name}: ждали блокер ${item.block}, получили [${keys.join(", ")}]`);
    } else {
      console.log(`✓ ${item.name}`);
    }
  });
}

function main() {
  const dir = mkdtempSync(join(tmpdir(), "torquex-web-requests-"));
  try {
    checkBlockers();

    ACCEPTED.forEach((item, index) => {
      const built = REQUEST.buildRequest(item.state);
      if (built.blockers.length) {
        return fail(
          `${item.name}: мастер не должен был блокировать`,
          built.blockers.map((blocker) => blocker.key).join(", "),
        );
      }
      let output;
      try {
        output = build(built.request, item.platform, dir, index);
      } catch (error) {
        return fail(`${item.name}: ${error.message}`);
      }
      const missing = item.expect.filter((needle) => output.indexOf(needle) < 0);
      if (missing.length) {
        return fail(`${item.name}: в выводе нет ${JSON.stringify(missing)}`, output);
      }
      console.log(`✓ ${item.name}`);
    });

    REFUSED.forEach((item, index) => {
      const built = REQUEST.buildRequest(item.state);
      item.tamper(built.request);
      let output;
      try {
        output = build(built.request, item.platform, dir, 100 + index);
      } catch (error) {
        return fail(`${item.name}: ${error.message}`);
      }
      const matched = item.expect.some((pattern) => pattern.test(output));
      if (!matched) {
        return fail(`${item.name}: генератор не отказал`, output);
      }
      console.log(`✓ ${item.name}`);
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }

  if (failures) {
    console.error(`\nПровалено проверок: ${failures}`);
    process.exit(1);
  }
  console.log("\nЗаявки мастера приняты генератором; обходы ворот отклонены.");
}

main();
