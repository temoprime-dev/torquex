/* TorqueX-сайт: страница «Обратная связь» (feedback.html).
 *
 * Короткое сообщение владельцу: результат на машине, ошибка, идея. Из адреса
 * страницы берутся только блок, версия и первые 12 символов хеша результата
 * (ни имён, ни VIN). Контакт необязателен. Все тексты выводятся через
 * textContent, без HTML-строк.
 */
(function () {
  "use strict";

  var TX = window.TX;
  var $ = function (id) { return document.getElementById(id); };
  var context = { platform: "", version: "", sha12: "" };

  function readQuery() {
    var params = new URLSearchParams(window.location.search);
    var clean = function (value, limit) {
      return String(value || "").replace(/[^\w.\-+]/g, "").slice(0, limit);
    };
    context.platform = clean(params.get("platform"), 12);
    context.version = clean(params.get("version"), 24);
    context.sha12 = clean(params.get("sha"), 12);
    var kind = params.get("kind");
    if (kind === "result_ok" || kind === "result_problem" || kind === "bug" || kind === "idea") {
      $("fb-kind").value = kind;
    }
    var line = $("fb-context");
    if (context.platform || context.version) {
      line.textContent = TX.t("feedback.context", {
        file: [context.platform, context.version, context.sha12].filter(Boolean).join(" · "),
      });
      line.hidden = false;
    }
  }

  function submit(event) {
    event.preventDefault();
    var button = $("fb-submit");
    var status = $("fb-status");
    status.textContent = "";
    if (String($("fb-message").value || "").trim().length < 5) {
      status.className = "alert alert-warn";
      status.textContent = TX.t("feedback.too_short");
      $("fb-message").focus();
      return;
    }
    TX.busy(button, true, "feedback.busy");
    TX.api.sendFeedback({
      kind: $("fb-kind").value,
      message: $("fb-message").value,
      contact: $("fb-contact").value,
      website: $("fb-extra").value,
      platform: context.platform,
      version: context.version,
      sha12: context.sha12,
    }).then(function () {
      status.className = "alert alert-ok";
      status.textContent = TX.t("feedback.thanks");
      $("fb-message").value = "";
      TX.announce(TX.t("feedback.thanks"));
    }, function (error) {
      var payload = (error && error.payload) || {};
      var text = (TX.lang === "en" ? payload.title_en : payload.title_ru) || "";
      status.className = "alert alert-stop";
      status.textContent = error && error.code === "FEEDBACK_RATE_LIMIT"
        ? TX.t("feedback.limit")
        : (text || TX.t("feedback.failed"));
      TX.announce(status.textContent, true);
    }).then(function () {
      TX.busy(button, false);
    });
  }

  TX.onReady(function () {
    TX.mountChrome("feedback");
    readQuery();
    $("fb-form").addEventListener("submit", submit);
  });
})();
