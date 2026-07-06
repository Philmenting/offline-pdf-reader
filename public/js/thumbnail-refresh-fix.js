// Keep the ONLYOFFICE thumbnail rail scrollable after page mutations.
//
// ONLYOFFICE also creates its own thumbnail scrollbars (#id_*_scroll_th). After
// pages are appended through MergePagesBinary their thumb size/track can stay at
// the old page count. We hide only those thumbnail-specific engine scrollbars
// and let the real #thumbnails-list element provide native scrolling instead.
// Do not call arbitrary thumbnail internals here: some sdk-all.js methods expect
// callbacks and throw "callback is not a function" when invoked defensively.
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

  function hideEngineThumbnailScrollbars() {
    for (const id of ["id_vertical_scroll_th", "id_horizontal_scroll_th"]) {
      const node = document.getElementById(id);
      if (!node) continue;
      node.style.setProperty("display", "none", "important");
      node.style.setProperty("pointer-events", "none", "important");
    }
  }

  function keepNativeRailScrollable() {
    const rail = document.getElementById("thumbnails-list");
    if (!rail) return;
    rail.style.setProperty("overflow", "auto", "important");
    rail.style.setProperty("overflow-y", "auto", "important");
    rail.style.setProperty("scrollbar-gutter", "stable", "important");
    hideEngineThumbnailScrollbars();
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
