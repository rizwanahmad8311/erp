/* First-party JS. Plain ES modules, no bundler, no npm.
 *
 * Copied verbatim to static/dist/js/app.js by `make css`-adjacent tooling or by
 * hand; there is no build step for JS beyond copying, so keep it browser-ready.
 */

(function () {
  "use strict";

  /* Format integer paisa for display. Mirrors apps/core/money.py — the server
   * stays the source of truth; this is only for unsaved UI arithmetic. */
  function formatPaisa(paisa, symbol) {
    const sign = paisa < 0 ? "-" : "";
    const abs = Math.abs(paisa);
    const rupees = Math.floor(abs / 100);
    const rem = String(abs % 100).padStart(2, "0");
    return `${symbol || "Rs"} ${sign}${rupees.toLocaleString("en-PK")}.${rem}`;
  }

  window.ERP = { formatPaisa };
})();
