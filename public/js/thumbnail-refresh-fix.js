// Keep the ONLYOFFICE thumbnail rail scrollable after page mutations.
//
// Do not call arbitrary thumbnail internals here. Some methods in sdk-all.js
// expect a callback and throw "callback is not a function" when invoked with a
// boolean. A plain resize pulse is enough to make the editor recalculate its
// thumbnail strip after MergePagesBinary while leaving the merge path untouched.
(function () {
  const PAGE_CHANGE_TOOLS = new Set(["page-add", "page-remove", "pdf-append"]);
  let lastPageCount = -1;
  let scheduleToken = 0;

  function getEditor() {
    return window.__pdfEditor || null;
  }

  function getPageCount() {
    const editor = getEditor();
    if (!editor || typeof editor.getCountPages !== "function") return -1;
    try { return editor.getCountPages() | 0; } catch { return -1; }
  }

  function keepNativeRailScrollable() {
    const rail = document.getElementById("thumbnails-list");
    if (!rail) return;
    rail.style.overflow = "auto";
    rail.style.overflowY = "auto";
  }

  function pulseEditorResize() {
    keepNativeRailScrollable();

    const editor = getEditor();
    try {
      if (editor && editor.WordControl && typeof editor.WordControl.OnResize === "function") {
        editor.WordControl.OnResize(true);
      }
    } catch (e) {
      console.debug("[thumbnail-refresh] WordControl resize skipped", e);
    }

    try { window.dispatchEvent(new Event("resize")); } catch { /* best effort */ }
  }

  function scheduleRefresh(reason) {
    const token = ++scheduleToken;
    for (const delay of [0, 100, 300, 700, 1400]) {
      setTimeout(() => {
        if (token !== scheduleToken) return;
        pulseEditorResize();
      }, delay);
    }
    console.debug(`[thumbnail-refresh] scheduled after ${reason || "page-count-change"}`);
  }

  document.addEventListener("click", (event) => {
    const btn = event.target && event.target.closest && event.target.closest("[data-tool]");
    if (!btn || !PAGE_CHANGE_TOOLS.has(btn.getAttribute("data-tool"))) return;
    scheduleRefresh(btn.getAttribute("data-tool"));
  }, true);

  setInterval(() => {
    keepNativeRailScrollable();

    const count = getPageCount();
    if (count <= 0) return;

    if (lastPageCount === -1) {
      lastPageCount = count;
      scheduleRefresh("initial-open");
      return;
    }

    if (count !== lastPageCount) {
      lastPageCount = count;
      scheduleRefresh("page-count-change");
    }
  }, 500);
})();
