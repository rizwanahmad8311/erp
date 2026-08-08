/* Keyboard navigation for the sales entry grid. First-party, no bundler, no npm.
 *
 * ============================================================================
 * THIS FILE MUST NEVER DO ARITHMETIC ON MONEY OR QUANTITIES.
 * ============================================================================
 *
 * Every figure on the entry screen — line amount, tax, discount, total, cost,
 * the credit position, the general ledger preview — is computed in Python and
 * arrives as finished HTML. A browser adding up paisa in IEEE doubles is
 * CLAUDE.md section 1 broken in the one place nobody thinks to look, and it
 * would be broken quietly: 0.1 + 0.2 looks right until the day it does not.
 *
 * So this file moves focus and highlights rows. That is the whole job. There is
 * a test that fails the build if `parseInt`, `parseFloat`, `Number(`, `toFixed`
 * or the word `paisa` ever appears below — see tests/test_sales_views.py.
 *
 * What it provides:
 *
 *   - Arrow keys and Enter in an autocomplete list, so a shop or an item is
 *     chosen without reaching for the mouse.
 *   - Focus restoration after an htmx swap: an element carrying
 *     `data-autofocus` gets the caret. That is what makes Enter on the item
 *     list land on the quantity box.
 *   - Alt+P to post, Alt+N to jump back to the item search.
 *
 * htmx is loaded separately and vendored; neither file is fetched from a CDN,
 * because the production machine has no internet (CLAUDE.md section 7).
 */

(function () {
  "use strict";

  var RESULT = "[data-result]";
  var LIST = "[data-results]";

  /* ---------------------------------------------------------------- helpers */

  function results(container) {
    return Array.prototype.slice.call(container.querySelectorAll(RESULT));
  }

  function activeIndex(rows) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-active") === "true") return i;
    }
    return -1;
  }

  function highlight(rows, index) {
    rows.forEach(function (row, i) {
      var on = i === index;
      row.setAttribute("data-active", on ? "true" : "false");
      row.classList.toggle("bg-brand-50", on);
      if (on && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    });
  }

  /* Move the highlight by one row, wrapping at both ends. `step` is +1 or -1 —
   * a position in a list, never a value. */
  function move(container, step) {
    var rows = results(container);
    if (!rows.length) return;
    var current = activeIndex(rows);
    var next = current + step;
    if (next < 0) next = rows.length - 1;
    if (next >= rows.length) next = 0;
    highlight(rows, next);
  }

  function choose(container) {
    var rows = results(container);
    if (!rows.length) return false;
    var index = activeIndex(rows);
    rows[index < 0 ? 0 : index].click();
    return true;
  }

  function listFor(input) {
    var id = input.getAttribute("data-results-for");
    return id ? document.getElementById(id) : null;
  }

  /* ------------------------------------------------------- autocomplete keys */

  document.addEventListener("keydown", function (event) {
    var input = event.target.closest ? event.target.closest("[data-results-for]") : null;
    if (!input) return;

    var container = listFor(input);
    if (!container) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      move(container, 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      move(container, -1);
    } else if (event.key === "Enter") {
      /* Enter picks the highlighted row rather than submitting the form. On the
       * item box this is what "Enter moves to quantity" means: the pick swaps in
       * an entry row whose quantity box carries data-autofocus. */
      if (choose(container)) event.preventDefault();
    } else if (event.key === "Escape") {
      container.innerHTML = "";
    }
  });

  /* Highlight whatever the pointer is over, so mouse and keyboard agree. */
  document.addEventListener("mousemove", function (event) {
    var row = event.target.closest ? event.target.closest(RESULT) : null;
    if (!row) return;
    var container = row.closest(LIST);
    if (!container) return;
    var rows = results(container);
    highlight(rows, rows.indexOf(row));
  });

  /* ------------------------------------------------------ focus after a swap */

  function restoreFocus(root) {
    var target = (root || document).querySelector("[data-autofocus]");
    if (!target) return;
    target.focus();
    if (target.select) target.select();
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    restoreFocus(event.target);
  });

  document.addEventListener("DOMContentLoaded", function () {
    restoreFocus(document);
  });

  /* ------------------------------------------------------- picking a client */

  /* The client list is the one autocomplete that does not swap a server-rendered
   * row in, because there is no document yet to render one against — the header
   * form is what creates it. So the pick is a field assignment: copy the chosen
   * row's id into the hidden select, show its label, and move on to the next
   * field. Ids and labels, no numbers. */
  document.addEventListener("click", function (event) {
    var row = event.target.closest ? event.target.closest("[data-client-id]") : null;
    if (!row) return;

    var hidden = document.getElementById("id_client");
    var search = document.getElementById("client-search");
    var list = document.getElementById("client-results");

    if (hidden) hidden.value = row.getAttribute("data-client-id");
    if (search) search.value = row.getAttribute("data-client-label");
    if (list) list.innerHTML = "";

    var next = document.getElementById("id_warehouse");
    if (next) next.focus();
  });

  /* ----------------------------------------------------------- accelerators */

  document.addEventListener("keydown", function (event) {
    if (!event.altKey) return;

    var key = event.key.toLowerCase();
    if (key === "p") {
      var post = document.querySelector("[data-action='post']");
      if (post) {
        event.preventDefault();
        post.click();
      }
    } else if (key === "n") {
      var search = document.querySelector("[data-item-search]");
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
    }
  });
})();
