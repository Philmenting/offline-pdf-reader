// Keep the ONLYOFFICE thumbnail rail scrollable after page mutations.
//
// ONLYOFFICE also creates its own thumbnail scrollbars (#id_*_scroll_th). After
// pages are appended through MergePagesBinary their thumb size/track can stay at
// the old page count. We hide only those thumbnail-specific engine scrollbars
// and let the real #thumbnails-list element provide native scrolling instead.
//
// The thumbnail canvases are positioned by the engine and can be virtualized, so
// the container's native scrollHeight does not always grow when pages are merged.
// A small invisible spacer, sized from the current page count and measured
// thumbnail spacing, gives the native scrollbar the correct range without
// touching the PDF merge path.
(function () {
  const PAGE_CHANGE_TOOLS = new Set(["page-add", "page-remove", "pdf-append"]);
  const SPACER_ID = "thumbnail-scroll-height-spacer";
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

  function getRail() {
    return document.getElementById("thumbnails-list");
  }

  function getSpacer(rail) {
    let spacer = document.getElementById(SPACER_ID);
    if (!spacer) {
      spacer = document.createElement("div");
      spacer.id = SPACER_ID;
      spacer.setAttribute("aria-hidden", "true");
      spacer.style.cssText = "display:block;width:1px;height:0;opacity:0;pointer-events:none;";
      rail.appendChild(spacer);
    }
    return spacer;
  }

  function hideEngineThumbnailScrollbars() {
    for (const id of ["id_vertical_scroll_th", "id_horizontal_scroll_th"]) {
      const node = document.getElementById(id);
      if (!node) continue;
      node.style.setProperty("display", "none", "important");
      node.style.setProperty("pointer-events", "none", "important");
    }
  }

  function measuredThumbnailBoxes(rail) {
    const railRect = rail.getBoundingClientRect();
    const boxes = [];
    const candidates = rail.querySelectorAll("canvas, [id], [class]");

    for (const node of candidates) {
      if (!(node instanceof HTMLElement)) continue;
      if (node.id === SPACER_ID || /^id_.*scroll_th$/.test(node.id)) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 20) continue;
      if (rect.bottom <= railRect.top || rect.top >= railRect.bottom + rail.scrollHeight + 400) continue;

      const top = rect.top - railRect.top + rail.scrollTop;
      const bottom = rect.bottom - railRect.top + rail.scrollTop;
      boxes.push({ top, bottom, height: rect.height });
    }

    boxes.sort((a, b) => a.top - b.top);
    return boxes;
  }

  function median(values) {
    if (!values.length) return 0;
    const sorted = values.slice().sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  }

  function estimateContentHeight(rail, pageCount) {
    const boxes = measuredThumbnailBoxes(rail);
    if (!boxes.length) return 0;

    const tops = [];
    for (const box of boxes) {
      if (!tops.some((top) => Math.abs(top - box.top) < 8)) tops.push(box.top);
    }

    const deltas = [];
    for (let i = 1; i < tops.length; i++) {
      const delta = tops[i] - tops[i - 1];
      if (delta > 20) deltas.push(delta);
    }

    const firstTop = Math.max(0, tops[0] || boxes[0].top || 0);
    const itemStep = median(deltas) || median(boxes.map((box) => box.height).filter((h) => h > 20)) || 180;
    const itemHeight = median(boxes.map((box) => box.height).filter((h) => h > 20)) || itemStep;
    const measuredBottom = Math.max(...boxes.map((box) => box.bottom));
    const estimatedBottom = firstTop + Math.max(0, pageCount - 1) * itemStep + itemHeight + 18;

    return Math.ceil(Math.max(measuredBottom + 18, estimatedBottom));
  }

  function syncNativeScrollRange() {
    const rail = getRail();
    if (!rail) return;

    const pageCount = getPageCount();
    const spacer = getSpacer(rail);

    rail.style.setProperty("overflow", "auto", "important");
    rail.style.setProperty("overflow-y", "auto", "important");
    rail.style.setProperty("scrollbar-gutter", "stable", "important");
    hideEngineThumbnailScrollbars();

    spacer.style.height = "0px";
    if (pageCount <= 0) return;

    const desiredHeight = estimateContentHeight(rail, pageCount);
    if (!desiredHeight) return;

    const missingHeight = Math.max(0, desiredHeight - rail.scrollHeight);
    spacer.style.height = `${missingHeight}px`;
  }

  function pulseEditorResize() {
    syncNativeScrollRange();

    const editor = getEditor();
    try {
      if (editor && editor.WordControl && typeof editor.WordControl.OnResize === "function") {
        editor.WordControl.OnResize(true);
      }
    } catch (e) {
      console.debug("[thumbnail-refresh] WordControl resize skipped", e);
    }

    requestAnimationFrame(syncNativeScrollRange);
  }

  function scheduleRefresh(reason) {
    const token = ++scheduleToken;
    for (const delay of [0, 100, 300, 700, 1400, 2200]) {
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
    syncNativeScrollRange();

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
