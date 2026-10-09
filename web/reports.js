/* TorqueX-сайт: журнал полевых проверок.
 * Пока реестр пуст (docs/FIELD_REPORTS_RU.md), таблица показывает прочерки, и
 * это честнее выдуманных отчётов. Записи добавляет владелец в TORQUEX_CONFIG
 * (web/config.js), когда появятся подтверждённые отчёты по протоколу. */
(function () {
  "use strict";

  var TX = window.TX;

  function renderRow(item) {
    var cells = [
      item.date || "—",
      (item.platform || "—") + (item.version ? " " + item.version : ""),
      item.variant || "v1",
      TX.shortHash(item.sha256_12 || "—", 12),
      item.car || "—",
      item.result || "—",
      item.log || "—",
    ];
    var tr = TX.el("tr");
    cells.forEach(function (text, index) {
      if (index === 3) {
        var td = TX.el("td");
        td.appendChild(TX.el("code", { class: "mono", text: String(text) }));
        tr.appendChild(td);
      } else {
        tr.appendChild(TX.el("td", { text: String(text) }));
      }
    });
    return tr;
  }

  function dashRow() {
    var tr = TX.el("tr");
    for (var index = 0; index < 7; index += 1) {
      tr.appendChild(TX.el("td", { class: "muted", text: "—" }));
    }
    return tr;
  }

  function render() {
    var body = document.getElementById("reports-body");
    var empty = document.getElementById("reports-empty");
    if (!body) return;
    var reports = (TX.CONFIG && TX.CONFIG.fieldReports) || [];
    TX.clear(body);
    if (!reports.length) {
      body.appendChild(dashRow());
      /* Реестр пуст — это честный результат, а не ошибка: показываем
       * заголовок и объяснение, а не одну строку прочерков. */
      if (empty) {
        TX.clear(empty);
        empty.hidden = false;
        empty.className = "empty-state";
        empty.appendChild(TX.el("h3", { text: TX.t("reports.empty_title") }));
        empty.appendChild(TX.el("p", { text: TX.t("reports.empty") }));
      }
      return;
    }
    reports.forEach(function (item) { body.appendChild(renderRow(item)); });
    if (empty) empty.hidden = true;
  }

  TX.onReady(function () {
    TX.mountChrome("reports");
    render();
    document.addEventListener("tx:lang", render);
  });
})();
