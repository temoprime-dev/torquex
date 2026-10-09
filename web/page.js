/* TorqueX-сайт: статические страницы помощи. Вся разметка — в HTML, тексты —
 * в i18n.js; скрипту остаётся собрать шапку/подвал и объявить страницу. */
(function () {
  "use strict";

  var TX = window.TX;

  TX.onReady(function () {
    var page = document.body.getAttribute("data-page") || "";
    TX.mountChrome(page);
    if (TX.syncVinMode) TX.syncVinMode();
  });
})();
