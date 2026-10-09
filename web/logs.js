/* TorqueX-сайт: страница «Разобрать лог и помочь проекту» (logs.html).
 *
 * Два отдельных согласия: «разобрать мой лог» (обязательно для отправки) и
 * «передать очищенную копию проекту» (выключено по умолчанию; скрыто, если
 * сервер говорит log_donation=false). Код удаления показывается один раз и
 * нигде не сохраняется: ни в localStorage, ни в адресе страницы.
 * Все данные сервера выводятся через TX.el (textContent), без HTML-строк.
 */
(function () {
  "use strict";

  var TX = window.TX;
  var $ = function (id) { return document.getElementById(id); };
  var donation = false;

  function sync() {
    var file = $("log-file").files && $("log-file").files[0];
    $("log-submit").disabled = !(file && $("log-consent").checked);
  }

  function showDonation(enabled) {
    donation = !!enabled;
    $("log-share-row").hidden = !donation;
    $("log-share-note").hidden = !donation;
    if (!donation) $("log-share").checked = false;
  }

  function alertBox(kind, text) {
    return TX.el("p", { class: "alert alert-" + kind, text: text });
  }

  /* Код удаления — одна строка «id.токен»: так его проще сохранить целиком. */
  function deleteCode(share) {
    return String(share.log_id) + "." + String(share.delete_token);
  }

  function renderShare(share, host) {
    if (!share) return;
    if (share.status === "STORED") {
      var code = deleteCode(share);
      host.appendChild(TX.el("h3", { text: TX.t("logs.thanks") }));
      host.appendChild(TX.el("p", { text: TX.t("logs.code_label") }));
      host.appendChild(TX.monoRow(code));
      host.appendChild(alertBox("warn", TX.t("logs.code_save", { date: share.expires || "—" })));
      $("log-delete-code").value = code;
      return;
    }
    var title = TX.lang === "en" ? share.title_en : share.title_ru;
    host.appendChild(alertBox("info", title || share.code || TX.t("logs.not_stored")));
  }

  function renderResult(payload) {
    var host = $("log-result");
    var analysis = payload.analysis || {};
    var rec = analysis.pack_recommendation || {};
    var nodes = [TX.el("h2", { text: TX.t("logs.result_title") })];
    if (payload.demo) nodes.push(alertBox("info", TX.t("common.demo")));
    nodes.push(TX.el("p", {
      text: TX.t("logs.summary", {
        rows: analysis.rows != null ? analysis.rows : "—",
        seconds: analysis.duration_s != null ? Math.round(analysis.duration_s) : "—",
        runs: (analysis.results || {}).wot_runs != null ? analysis.results.wot_runs : "—",
      }),
    }));
    var zones = (rec.zones || []).slice(0, 8);
    if (zones.length) {
      var list = TX.el("ul", { class: "support-list" });
      zones.forEach(function (zone) {
        list.appendChild(TX.el("li", {}, [
          TX.el("strong", { text: String(zone.zone_ru || "") }),
          TX.el("span", { text: " — " + String(zone.recommendation_ru || "") }),
        ]));
      });
      nodes.push(TX.el("p", { text: TX.t("logs.zones") }), list);
    } else {
      nodes.push(TX.el("p", { text: TX.t("logs.no_zones") }));
    }
    (rec.warnings || []).slice(0, 5).forEach(function (warning) {
      nodes.push(alertBox("warn", String(warning)));
    });
    nodes.push(TX.el("p", { class: "hint", text: TX.t("logs.result_note") }));
    TX.fill(host, nodes);
    renderShare(payload.share, host);
    host.hidden = false;
  }

  function renderError(error) {
    var host = $("log-result");
    var payload = (error && error.payload) || {};
    var title = TX.lang === "en" ? payload.title_en : payload.title_ru;
    TX.fill(host, [alertBox("stop", title || TX.t("logs.failed", { code: (error && error.code) || "API_ERROR" }))]);
    host.hidden = false;
  }

  function submit(event) {
    event.preventDefault();
    var file = $("log-file").files && $("log-file").files[0];
    if (!file || !$("log-consent").checked) return;
    var limit = TX.CONFIG.maxUploadBytes || 1048576;
    if (file.size > limit) {
      renderError({ code: "UPLOAD_TOO_LARGE", payload: { title_ru: TX.t("logs.too_big", { size: TX.limitLabel() }), title_en: TX.t("logs.too_big", { size: TX.limitLabel() }) } });
      return;
    }
    var button = $("log-submit");
    TX.busy(button, true, "logs.busy");
    TX.api.analyzeLog({
      file: file,
      pack: $("log-package").value,
      share: donation && $("log-share").checked,
    }).then(function (payload) {
      renderResult(payload);
      TX.announce(TX.t("logs.result_title"));
    }, function (error) {
      renderError(error);
      TX.announce(TX.t("logs.failed", { code: (error && error.code) || "" }), true);
    }).then(function () {
      TX.busy(button, false);
      sync();
    });
  }

  function remove() {
    var raw = String($("log-delete-code").value || "").trim();
    var dot = raw.indexOf(".");
    var status = $("log-delete-status");
    if (dot < 1 || dot === raw.length - 1) {
      status.textContent = TX.t("logs.delete_bad");
      return;
    }
    var button = $("log-delete");
    TX.busy(button, true);
    TX.api.deleteLog(raw.slice(0, dot), raw.slice(dot + 1)).then(function () {
      status.textContent = TX.t("logs.deleted");
      $("log-delete-code").value = "";
    }, function (error) {
      var payload = (error && error.payload) || {};
      status.textContent = (TX.lang === "en" ? payload.title_en : payload.title_ru) || TX.t("logs.delete_failed");
    }).then(function () {
      TX.busy(button, false);
    });
  }

  TX.onReady(function () {
    TX.mountChrome("logs");
    showDonation(false);
    $("log-file").addEventListener("change", sync);
    $("log-consent").addEventListener("change", sync);
    $("log-form").addEventListener("submit", submit);
    $("log-delete").addEventListener("click", remove);
    sync();
    TX.api.health().then(function (health) {
      showDonation(health && health.log_donation === true);
    }, function () {
      showDonation(false);
    });
  });
})();
