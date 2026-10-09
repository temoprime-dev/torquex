/* Старый URL app.html ведёт в мастер на index.html. query и hash сохраняются,
 * поэтому app.html?demo=1 остаётся демо-ссылкой без второй реализации мастера. */
(function () {
  "use strict";

  var TX = window.TX;
  TX.onReady(function () {
    TX.mountChrome("master");
    var destination = "index.html" + window.location.search + window.location.hash;
    window.location.replace(destination);
  });
})();
