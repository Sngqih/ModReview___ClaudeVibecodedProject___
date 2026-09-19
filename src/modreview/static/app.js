// ModReview front end: scroll reveals, the "/" shortcut, and the live progress stream.
(() => {
  "use strict";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function reveal(root) {
    const els = root.querySelectorAll("[data-reveal]:not(.is-visible)");
    if (reduceMotion || !("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("is-visible"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            io.unobserve(entry.target);
          }
        });
      },
      { rootMargin: "0px 0px -40px 0px" }
    );
    const inView = (el) => {
      const r = el.getBoundingClientRect();
      return r.top < window.innerHeight && r.bottom > 0;
    };
    els.forEach((el, i) => {
      el.style.setProperty("--index", String(i % 6));
      // Content already on screen must never depend on the observer firing: some
      // browsers throttle it (background tabs, headless), which would leave it invisible.
      if (inView(el)) setTimeout(() => el.classList.add("is-visible"), 20);
      else io.observe(el);
    });
    // Safety net: anything still hidden inside the viewport after 2.5 s is shown.
    setTimeout(() => {
      els.forEach((el) => {
        if (!el.classList.contains("is-visible") && inView(el)) el.classList.add("is-visible");
      });
    }, 2500);
  }

  // Press "/" anywhere to jump to the search box.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;
    const box = document.querySelector("[data-search]");
    if (box) {
      e.preventDefault();
      box.focus();
      box.select();
    }
  });

  // Search box: Enter always submits (Chrome's suggestion popup can swallow it), and a
  // typed title such as "data structures" resolves to the top suggestion's module code.
  const CODE_RE = /^\s*[A-Za-z]{2,4}\s?\d{4}[A-Za-z]{0,3}\s*$/;

  function initSearch() {
    // The home page has one search box; Compare has two sharing one suggestion list.
    const inputs = Array.from(document.querySelectorAll("[data-search]"));
    if (!inputs.length || !inputs[0].form) return;
    const form = inputs[0].form;
    let submitted = false;
    const listOf = (input) => document.getElementById(input.getAttribute("list"));

    inputs.forEach((input) => {
      // Remember which box the current suggestions belong to.
      input.addEventListener("input", () => {
        const list = listOf(input);
        if (list) list.dataset.for = input.name;
      });
      input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || e.isComposing) return;
        // Let the browser apply a highlighted suggestion first, then submit if it didn't.
        setTimeout(() => {
          if (!submitted && input.value.trim()) form.requestSubmit();
        }, 0);
      });
    });
    form.addEventListener("submit", () => {
      submitted = true;
      inputs.forEach((input) => {
        const list = listOf(input);
        if (CODE_RE.test(input.value) || !list || list.dataset.for !== input.name) return;
        const first = list.querySelector("option");
        if (first) input.value = first.value;
      });
    });
  }

  const CHECK = '<svg class="icon" aria-hidden="true"><use href="#i-check"/></svg>';

  function settleRunning(list) {
    list.querySelectorAll(".progress-item.is-running").forEach((li) => {
      li.classList.replace("is-running", "is-done");
      const slot = li.querySelector(".progress-item__icon");
      if (slot) slot.innerHTML = CHECK;
    });
  }

  function startStream(card) {
    const list = card.querySelector("[data-progress-list]");
    // A Compare column replaces only itself; the report page replaces the whole report area.
    const column = card.closest("[data-stream-root]");
    const root = column || document.querySelector("[data-review-root]");
    let finished = false;
    const es = new EventSource(card.dataset.stream);

    es.addEventListener("progress", (e) => {
      const tpl = document.createElement("template");
      tpl.innerHTML = e.data.trim();
      const li = tpl.content.firstElementChild;
      if (!li) return;
      if (li.dataset.kind === "stage") settleRunning(list);
      list.appendChild(li);
    });

    es.addEventListener("done", (e) => {
      finished = true;
      es.close();
      settleRunning(list);
      root.innerHTML = e.data;
      reveal(root);
      if (window.htmx) window.htmx.process(root);
      if (column) return;
      // Drop ?force=1 so a reload shows the cached report instead of rebuilding it.
      const url = new URL(window.location.href);
      if (url.searchParams.has("force")) {
        url.searchParams.delete("force");
        history.replaceState(null, "", url.pathname + url.search);
      }
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });

    es.addEventListener("failed", (e) => {
      finished = true;
      es.close();
      list.querySelectorAll(".progress-item.is-running").forEach((li) => li.remove());
      card.insertAdjacentHTML("beforeend", e.data);
    });

    // EventSource reconnects on its own, which would restart the whole pipeline. Don't.
    es.onerror = () => {
      if (finished) return;
      finished = true;
      es.close();
      card.insertAdjacentHTML(
        "beforeend",
        '<div class="notice notice--bad" role="alert"><div><strong>Lost the connection to ModReview.</strong>' +
          "<p>Check that the server is still running, then reload this page.</p></div></div>"
      );
    };
  }

  function init() {
    reveal(document);
    initSearch();
    document.querySelectorAll("[data-stream]").forEach(startStream);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
