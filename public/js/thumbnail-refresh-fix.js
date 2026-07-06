// Rebuild the ONLYOFFICE thumbnail control after page mutations.
//
// Appending pages through CPDFDoc.MergePagesBinary updates the document model,
// but the existing ThumbnailsControl can keep its old scroll model. Native
// scrollbar/spacer workarounds make the first thumbnails look right but break
// the engine's virtualization further down the rail. Rebuilding the control lets
// ONLYOFFICE recalculate the thumbnail count, custom scrollbar, and lazy render
// window from the current document state.
(function () {
  const PAGE_CHANGE_TOOLS = new Set(["page-add", "page-remove", "pdf-append"]);
  let lastPageCount = -1;
  let rebuildToken = 0;

  function getEditor() {
    return window.__pdfEditor || null;
  }

  function getRenderer() {
    const editor = getEditor();
    try {
      return editor && typeof editor.getDocumentRenderer === "function"
        ? editor.getDocumentRenderer()
        : null;
    } catch {
      return null;
    }
  }

  function getPageCount() {
    const editor = getEditor();
    if (!editor || typeof editor.getCountPages !== "function") return -1;
    try { return editor.getCountPages() | 0; } catch { return -1; }
  }

  function resetRailElement() {
    const rail = document.getElementById("thumbnails-list");
    if (!rail) return null;
    rail.removeAttribute("style");
    rail.className = "thumbnails";
    rail.scrollTop = 0;
    rail.scrollLeft = 0;
    rail.replaceChildren();
    return rail;
  }

  function callNoArgs(target, names) {
    if (!target) return;
    for (const name of names) {
      try {
        if (typeof target[name] === "function") target[name]();
      } catch (e) {
        console.debug(`[thumbnail-refresh] ${name} skipped`, e);
      }
    }
  }

  function rebuildThumbnails(reason) {
    const editor = getEditor();
    const renderer = getRenderer();
    if (!editor || !renderer || !window.AscCommon || typeof window.AscCommon.ThumbnailsControl !== "function") {
      return false;
    }

    const rail = resetRailElement();
    if (!rail) return false;

    try {
      renderer.Thumbnails = new window.AscCommon.ThumbnailsControl("thumbnails-list");
      if (typeof renderer.setThumbnailsControl === "function") {
        renderer.setThumbnailsControl(renderer.Thumbnails);
      }

      callNoArgs(renderer.Thumbnails, ["resize", "Resize", "onResize", "OnResize"]);
      if (editor.WordControl && typeof editor.WordControl.OnResize === "function") {
        editor.WordControl.OnResize(true);
      }
      callNoArgs(renderer, ["paint", "Paint"]);
      console.debug(`[thumbnail-refresh] rebuilt thumbnails after ${reason || "page-count-change"}`);
      return true;
    } catch (e) {
      console.warn("[thumbnail-refresh] rebuild failed", e);
      return false;
    }
  }

  function scheduleRebuild(reason) {
    const token = ++rebuildToken;
    for (const delay of [80, 250, 600, 1200]) {
      setTimeout(() => {
        if (token !== rebuildToken) return;
        rebuildThumbnails(reason);
      }, delay);
    }
  }

  document.addEventListener("click", (event) => {
    const btn = event.target && event.target.closest && event.target.closest("[data-tool]");
    if (!btn || !PAGE_CHANGE_TOOLS.has(btn.getAttribute("data-tool"))) return;
    scheduleRebuild(btn.getAttribute("data-tool"));
  }, true);

  setInterval(() => {
    const count = getPageCount();
    if (count <= 0) return;

    if (lastPageCount === -1) {
      lastPageCount = count;
      return;
    }

    if (count !== lastPageCount) {
      lastPageCount = count;
      scheduleRebuild("page-count-change");
    }
  }, 500);
})();
