// Synchronize ONLYOFFICE's thumbnail page model after page mutations.
//
// The thumbnail rail owns its own page array and custom scrollbar. When a PDF is
// appended, the document page count changes before that thumbnail model is fully
// updated. Replacing DOM nodes or forcing native scroll metrics breaks the rail
// later while scrolling, so this keeps the existing control and asks ONLYOFFICE
// to add/delete thumbnail pages through its own methods.
(function () {
  const PAGE_CHANGE_TOOLS = new Set(["page-add", "page-remove", "pdf-append"]);
  let lastPageCount = -1;
  let syncToken = 0;

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

  function ensureThumbnails(renderer) {
    if (renderer && renderer.Thumbnails) return renderer.Thumbnails;
    if (!renderer || !window.AscCommon || typeof window.AscCommon.ThumbnailsControl !== "function") {
      return null;
    }

    const rail = document.getElementById("thumbnails-list");
    if (!rail) return null;

    try {
      renderer.Thumbnails = new window.AscCommon.ThumbnailsControl("thumbnails-list");
      if (typeof renderer.setThumbnailsControl === "function") {
        renderer.setThumbnailsControl(renderer.Thumbnails);
      }
      return renderer.Thumbnails;
    } catch (e) {
      console.warn("[thumbnail-sync] create failed", e);
      return null;
    }
  }

  function call(target, name, ...args) {
    try {
      if (target && typeof target[name] === "function") return target[name](...args);
    } catch (e) {
      console.debug(`[thumbnail-sync] ${name} skipped`, e);
    }
    return undefined;
  }

  function resizeAndRepaint(thumbs) {
    call(thumbs, "setNeedResize", true);
    if (typeof thumbs.resize === "function") {
      call(thumbs, "resize", false);
    } else {
      call(thumbs, "Resize", false);
    }
    call(thumbs, "repaint");
  }

  function clampCustomScrollbar(thumbs) {
    if (!thumbs || !thumbs.m_oScrollVerApi || typeof thumbs.m_oScrollVerApi.scrollToY !== "function") {
      return;
    }

    const maxY = Number.isFinite(thumbs.scrollMaxY) ? thumbs.scrollMaxY : 0;
    const currentY = Number.isFinite(thumbs.scrollY) ? thumbs.scrollY : 0;
    thumbs.m_oScrollVerApi.scrollToY(Math.max(0, Math.min(currentY, maxY)));
  }

  function rebuildFromViewer(thumbs) {
    if (typeof thumbs.init !== "function") return false;
    call(thumbs, "init");
    resizeAndRepaint(thumbs);
    clampCustomScrollbar(thumbs);
    return true;
  }

  function syncThumbnails(reason) {
    const renderer = getRenderer();
    const count = getPageCount();
    if (!renderer || count <= 0) return false;

    const thumbs = ensureThumbnails(renderer);
    if (!thumbs) return false;

    try {
      const have = Array.isArray(thumbs.pages) ? thumbs.pages.length : -1;
      let changed = have !== count;

      if (have >= 0 && have < count && typeof thumbs._addPage === "function") {
        for (let index = have; index < count; index += 1) {
          thumbs._addPage(index);
        }
      } else if (have >= 0 && have > count && typeof thumbs._deletePage === "function") {
        for (let index = have - 1; index >= count; index -= 1) {
          thumbs._deletePage(index);
        }
      } else if (changed && !rebuildFromViewer(thumbs)) {
        return false;
      }

      resizeAndRepaint(thumbs);
      clampCustomScrollbar(thumbs);
      console.debug(`[thumbnail-sync] synced after ${reason || "page-count-change"}: ${have} -> ${count}`);
      return true;
    } catch (e) {
      console.warn("[thumbnail-sync] sync failed; trying init()", e);
      try {
        return rebuildFromViewer(thumbs);
      } catch (fallbackError) {
        console.warn("[thumbnail-sync] fallback init failed", fallbackError);
        return false;
      }
    }
  }

  function scheduleSync(reason) {
    const token = ++syncToken;
    for (const delay of [80, 250, 600, 1200]) {
      setTimeout(() => {
        if (token !== syncToken) return;
        syncThumbnails(reason);
      }, delay);
    }
  }

  document.addEventListener("click", (event) => {
    const btn = event.target && event.target.closest && event.target.closest("[data-tool]");
    if (!btn || !PAGE_CHANGE_TOOLS.has(btn.getAttribute("data-tool"))) return;
    scheduleSync(btn.getAttribute("data-tool"));
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
      scheduleSync("page-count-change");
    }
  }, 500);
})();
