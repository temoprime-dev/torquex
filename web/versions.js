/* TorqueX-сайт: страница «Поддерживаемые версии».
 * Таблица — из ответа генератора /versions; формулировки — из словаря
 * (docs/RELEASE_CHECKLIST_RU.md §4). Если API недоступен — честный текст,
 * никаких выдуманных списков. */
(function () {
  "use strict";

  var TX = window.TX;

  function fillRow(ecu, text, demo) {
    var head = TX.el("th", { attrs: { scope: "row" } }, [TX.el("span", { text: ecu })]);
    if (demo) head.appendChild(TX.el("span", { class: "badge badge-demo", text: TX.t("common.demo") }));
    return TX.el("tr", {}, [head, TX.el("td", { text: text })]);
  }

  function render(payload) {
    var body = document.getElementById("versions-body");
    var ecus = (payload && payload.ecus) || {};
    var demo = !!(payload && payload.demo);
    var ms43 = (ecus.MS43 && ecus.MS43.versions) || [];
    var families = (ecus.MS45 && ecus.MS45.families) || [];
    var withDefs = (ecus.MS45 && ecus.MS45.families_with_definitions) || [];

    if (body) {
      TX.clear(body);
      body.appendChild(fillRow("MS41", TX.t("versions.ms41_text") +
        (ecus.MS41 && ecus.MS41.note ? " — " + ecus.MS41.note : ""), demo));
      body.appendChild(fillRow("MS42", TX.t("versions.ms42_text") +
        (ecus.MS42 && ecus.MS42.note ? " — " + ecus.MS42.note : ""), demo));
      body.appendChild(fillRow("MS43", TX.t("versions.ms43_text", {
        versions: ms43.length ? ms43.join(", ") : "—",
      }), demo));
      var gs8 = (ecus.GS8 && ecus.GS8.systems) || {};
      var gs8Lines = Object.keys(gs8).map(function (name) {
        return TX.t("versions.gs8_row", {
          system: name, kib: gs8[name].image_kib, programs: gs8[name].programs, factory: gs8[name].factory,
        });
      });
      body.appendChild(fillRow("GS8", TX.t("versions.gs8_text") + (gs8Lines.length ? " " + gs8Lines.join(" ") : ""), demo));
      body.appendChild(fillRow("MS45", TX.t("versions.ms45_text", {
        families: families.length ? families.join(", ") : "—",
      }) + (withDefs.length ? " (" + withDefs.join(", ") + ")" : ""), demo));
    }

    var ms43Host = document.getElementById("versions-ms43");
    if (ms43Host) {
      ms43Host.textContent = TX.t("versions.ms43_text", {
        versions: ms43.length ? ms43.join(", ") : "—",
      });
    }
    var ms45Host = document.getElementById("versions-ms45");
    if (ms45Host) {
      ms45Host.textContent = TX.t("versions.ms45_text", {
        families: families.length ? families.join(", ") : "—",
      });
    }
    var alpinaHost = document.getElementById("versions-alpina");
    if (alpinaHost) {
      var systems = (ecus.GS8 && ecus.GS8.systems) || {};
      TX.clear(alpinaHost);
      Object.keys(systems).forEach(function (name) {
        var list = systems[name].alpina || [];
        alpinaHost.appendChild(TX.el("li", {
          text: name + ": " + (list.length ? list.join("; ") : TX.t("versions.gs8_alpina_none")),
        }));
      });
    }
    TX.announce(TX.t("versions.title") + ": " + (payload && payload.generator_version || "—"));
  }

  function load() {
    var body = document.getElementById("versions-body");
    if (body) {
      TX.fill(body, [TX.el("tr", {}, [
        TX.el("td", { attrs: { colspan: "2" }, class: "muted", text: TX.t("versions.loading") }),
      ])]);
    }
    var request = TX.isDemo() && TX.api.demoVersions
      ? TX.api.demoVersions()
      : TX.api.versions();
    request.then(render).catch(function (error) {
      if (TX.isDemo() && TX.api.demoVersions) {
        TX.api.demoVersions().then(render);
        return;
      }
      var host = document.getElementById("versions-body");
      if (!host) return;
      /* Пустое состояние вместо пустой таблицы: что случилось и что делать. */
      TX.clear(host);
      host.appendChild(TX.el("tr", {}, [
        TX.el("td", { attrs: { colspan: "2" } }, [
          TX.el("div", { class: "empty-state" }, [
            TX.el("h3", { text: TX.t("versions.empty_title") }),
            TX.el("p", { text: TX.t("versions.empty_text") }),
            TX.el("p", { class: "hint", text: (error && error.code) || TX.t("wizard.err.api_unreachable") }),
          ]),
        ]),
      ]));
    });
  }

  TX.onReady(function () {
    TX.mountChrome("versions");
    load();
    document.addEventListener("tx:lang", load);
  });
})();
