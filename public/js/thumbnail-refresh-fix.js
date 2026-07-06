// Keep the ONLYOFFICE thumbnail rail in sync after page mutations.
// The PDF merge path updates the document model immediately, but the thumbnail
// control does not always recalculate its scroll strip until a later resize.
(function () {
  const PAGE_CHANGE_TOOLS = new Set(["page-add", "page-remove", "pdf-append"]);
  let lastPageCount = -1;
  let refreshTimer = 0;

  function getEditor() {
    return window.__pdfEditor || null;
  }

  function getRenderer(editor) {
    try {
      return editor && typeof editor.getDocumentRenderer === "function"
        ? editor.getDocumentRenderer()
        : null;
    } catch {
      return null;
    }
  }

  function getThumbnails(renderer) {
    return renderer && (renderer.Thumbnails || renderer.thumbnails || null);
  }

  function call(target, names, args) {
    if (!target) return;
    for (const name of names) {
      try {
        if (typeof target[name] === "function") target[name].apply(target, args || []);
      } catch (e) {
        console.debug(`[thumbnail-refresh] ${name} failed`, e);
      }
    }
  }

  function refreshThumbnailRail(reason) {
    const editor = getEditor();
    const renderer = getRenderer(editor);
    const thumbs = getThumbnails(renderer);
    const rail = document.getElementById("thumbnails-list");

    if (!editor || !renderer || !rail) return false;

    // Native overflow is our safety net if the engine's custom thumb scrollbar
    // lags behind after MergePagesBinary.
    rail.style.overflow = "auto";

    call(thumbs, [
      "resize", "Resize", "onResize", "OnResize",
      "update", "Update", "repaint", "Repaint", "paint", "Paint",
      "calculate", "Calculate", "checkTasks", "CheckTasks"
    ], [true]);
    call(renderer, ["resize", "Resize", "onResize", "OnResize", "paint", "Paint"], [true]);
    call(editor.WordControl, ["OnResize"], [true]);

    requestAnimationFrame(() => {
      const maxScroll = Math.max(0, rail.scrollHeight - rail.clientHeight);
      if (rail.scrollTop > maxScroll) rail.scrollTop = maxScroll;
      call(thumbs, ["resize", "Resize", "repaint", "Repaint", "paint", "Paint"], [true]);
    });

    console.debug(`[thumbnail-refresh] refreshed after ${reason || "page-count-change"}`);
    return true;
  }

  function scheduleRefresh(reason) {
    clearTimeout(refreshTimer);
    const delays = [0, 80, 250, 600, 1200];
    for (const delay of delays) {
      setTimeout(() => refreshThumbnailRail(reason), delay);
    }
    refreshTimer = setTimeout(() => refreshThumbnailRail(reason), 1800);
  }

  document.addEventListener("click", (event) => {
    const btn = event.target && event.target.closest && event.target.closest("[data-tool]");
    if (!btn || !PAGE_CHANGE_TOOLS.has(btn.getAttribute("data-tool"))) return;
    scheduleRefresh(btn.getAttribute("data-tool"));
  }, true);

  setInterval(() => {
    const editor = getEditor();
    if (!editor || typeof editor.getCountPages !== "function") return;
    let count = -1;
    try { count = editor.getCountPages() | 0; } catch { return; }
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
