/* First-party JS. Plain ES modules, no bundler, no npm.
 *
 * Copied verbatim to static/dist/js/app.js by `make js`; there is no build step
 * for JS beyond copying, so keep it browser-ready.
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

  /* ======================================================================
   * The keyboard layer
   * ======================================================================
   * The bindings come from the server, out of apps/core/shortcuts.py, which is
   * also what the /shortcuts page renders. Nothing here has its own list — a
   * keyboard map documented in one place and implemented in another is wrong
   * within a month, and it is wrong for the person who learned it.
   *
   * A binding names an *action*, and a screen opts in by marking a control
   * `data-action="post"`. A screen with no such control simply has no binding,
   * which is why Alt+P is inert on a report rather than needing an exception.
   */
  let cachedMap = null;

  function keyMap() {
    if (cachedMap) return cachedMap;
    const raw = document.body && document.body.dataset ? document.body.dataset.shortcuts : "";
    try {
      cachedMap = raw ? JSON.parse(raw) : {};
    } catch (e) {
      cachedMap = {};
    }
    return cachedMap;
  }

  /* The pressed combination, in the form apps/core/shortcuts.py writes:
   * "alt+n", "enter", "escape". */
  function combo(event) {
    const key = (event.key || "").toLowerCase();
    return event.altKey && key.length === 1 ? `alt+${key}` : key;
  }

  /* Whether the event came from somewhere a bare Enter or Escape belongs to
   * the control rather than to us. Alt+ combinations are ours everywhere —
   * that is the point of using Alt — but hijacking Enter inside a <textarea>
   * would make a cancellation reason impossible to type. */
  function isTextEntry(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "textarea" || target.isContentEditable;
  }

  function fire(action, event) {
    /* Search is a focus, not a click: Alt+F should put the cursor in the box
     * and select what is there so the next keystroke replaces it. */
    if (action === "search") {
      const box = document.querySelector('[data-action="search"]');
      if (!box) return false;
      box.focus();
      if (typeof box.select === "function") box.select();
      return true;
    }

    /* Enter and Escape inside the grid are the grid's own — entry-grid.js
     * listens for these and knows which row is being edited. */
    if (action === "next-line" || action === "cancel-edit") {
      const grid = document.querySelector("[data-entry-grid]");
      if (!grid) return false;
      grid.dispatchEvent(
        new CustomEvent("erp:" + action, { bubbles: true, detail: { source: event.target } })
      );
      return true;
    }

    const control = document.querySelector(`[data-action="${action}"]`);
    if (!control || control.disabled) return false;
    control.click();
    return true;
  }

  document.addEventListener("keydown", function (event) {
    const map = keyMap();
    const pressed = combo(event);
    const action = map[pressed];
    if (!action) return;

    /* A bare Enter or Escape inside a textarea belongs to the textarea. */
    if (!event.altKey && isTextEntry(event.target)) return;

    if (fire(action, event)) {
      event.preventDefault();
    }
  });

  /* ======================================================================
   * The saved-row wash
   * ======================================================================
   * The one animation in the system: 120ms, on the row that just saved, so an
   * operator keying twenty lines without looking up can see the last one
   * landed. The duration and the reduced-motion fallback are both in
   * static/src/css/app.css — this only puts the class on and takes it off.
   */
  function flashSaved(row) {
    if (!row) return;
    row.classList.remove("row-saved");
    /* Reading offsetWidth restarts the animation when the same row saves
     * twice in a row; without it the class is already present and nothing
     * replays. */
    void row.offsetWidth;
    row.classList.add("row-saved");
  }

  /* htmx swaps a saved line in as a new <tr>. */
  document.body.addEventListener("htmx:afterSwap", function (event) {
    const row = event.target.closest ? event.target.closest("tr[data-line]") : null;
    if (row) flashSaved(row);
  });

  window.ERP = { formatPaisa, flashSaved };
})();
