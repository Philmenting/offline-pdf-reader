// Synchronize ONLYOFFICE's thumbnail page model after page mutations.
//
// The thumbnail rail owns its own page array and custom scrollbar. The renderer
// also keeps a separate file.pages array that ThumbnailsControl._addPage() reads
// from. PDF merges can update the document page count without extending that
// renderer-side page list, so the thumbnail scrollbar keeps its old geometry.
(function () {
  const PAGE_CHANGE_TOOLS = new Set(["page-add", "page-remove", "pdf-append"]);
  let lastPageCount = -1;
  let syncToken = 0;
  let patchedEditor = null;
  let patchedDoc = null;

  function getEditor() { return window.__pdfEditor || null; }

  function getRenderer() {
    const editor = getEditor();
    try { return editor && typeof editor.getDocumentRenderer === "function" ? editor.getDocumentRenderer() : null; }
    catch { return null; }
  }

  function getPdfDoc() {
    const editor = getEditor();
    try { return editor && typeof editor.getPDFDoc === "function" ? editor.getPDFDoc() : null; }
    catch { return null; }
  }

  function getPageCount() {
    const editor = getEditor();
    if (!editor || typeof editor.getCountPages !== "function") return -1;
    try { return editor.getCountPages() | 0; } catch { return -1; }
  }

  function call(target, name, ...args) {
    try { if (target && typeof target[name] === "function") return target[name](...args); }
    catch (e) { console.debug(`[thumbnail-sync] ${name} skipped`, e); }
    return undefined;
  }

  function ensureThumbnails(renderer) {
    if (renderer && renderer.Thumbnails) return renderer.Thumbnails;
    if (!renderer || !window.AscCommon || typeof window.AscCommon.ThumbnailsControl !== "function") return null;

    const rail = document.getElementById("thumbnails-list");
    if (!rail) return null;

    try {
      renderer.Thumbnails = new window.AscCommon.ThumbnailsControl("thumbnails-list");
      if (typeof renderer.setThumbnailsControl === "function") renderer.setThumbnailsControl(renderer.Thumbnails);
      return renderer.Thumbnails;
    } catch (e) {
      console.warn("[thumbnail-sync] create failed", e);
      return null;
    }
  }

  function getPageObject(doc, index) {
    return call(doc, "GetPage", index)
      || call(doc, "getPage", index)
      || (doc && Array.isArray(doc.pages) ? doc.pages[index] : null)
      || (doc && Array.isArray(doc.Pages) ? doc.Pages[index] : null);
  }

  function readNumber(target, names) {
    for (const name of names) {
      const value = call(target, name);
      if (Number.isFinite(value) && value > 0) return value;
      if (target && Number.isFinite(target[name]) && target[name] > 0) return target[name];
    }
    return 0;
  }

  function makeFilePage(doc, index, fallback) {
    const page = getPageObject(doc, index);
    const width = readNumber(page, ["GetWidth", "getWidth", "GetW", "getW", "W", "width"])
      || (fallback && (fallback.W || fallback.width)) || 595;
    const height = readNumber(page, ["GetHeight", "getHeight", "GetH", "getH", "H", "height"])
      || (fallback && (fallback.H || fallback.height)) || 842;
    const dpi = (fallback && (fallback.Dpi || fallback.dpi)) || 72;
    const rotate = readNumber(page, ["GetRotate", "getRotate", "Rotate", "rotate"])
      || (fallback && (fallback.Rotate || fallback.rotate)) || 0;

    return { Dpi: dpi, W: width, H: height, originIndex: index, Rotate: rotate };
  }

  function syncRendererFilePages(renderer, count) {
    const file = renderer && renderer.file;
    const doc = getPdfDoc();
    if (!file || !Array.isArray(file.pages) || count <= 0) return;

    const fallback = file.pages[file.pages.length - 1] || file.pages[0] || null;
    while (file.pages.length < count) file.pages.push(makeFilePage(doc, file.pages.length, fallback));
    if (file.pages.length > count) file.pages.splice(count);
  }

  function resizeAndRepaint(thumbs) {
    call(thumbs, "setNeedResize", true);
    if (typeof thumbs.resize === "function") call(thumbs, "resize", false);
    else call(thumbs, "Resize", false);
    call(thumbs, "repaint");
  }

  function clampCustomScrollbar(thumbs) {
    if (!thumbs || !thumbs.m_oScrollVerApi || typeof thumbs.m_oScrollVerApi.scrollToY !== "function") return;
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

    syncRendererFilePages(renderer, count);
    const thumbs = ensureThumbnails(renderer);
    if (!thumbs) return false;

    try {
      const have = Array.isArray(thumbs.pages) ? thumbs.pages.length : -1;
      const changed = have !== count;

      if (have >= 0 && have < count && typeof thumbs._addPage === "function") {
        for (let index = have; index < count; index += 1) thumbs._addPage(index);
      } else if (have >= 0 && have > count && typeof thumbs._deletePage === "function") {
        for (let index = have - 1; index >= count; index -= 1) thumbs._deletePage(index);
      } else if (changed && !rebuildFromViewer(thumbs)) {
        return false;
      }

      resizeAndRepaint(thumbs);
      clampCustomScrollbar(thumbs);
      lastPageCount = count;
      console.debug(`[thumbnail-sync] synced after ${reason || "page-count-change"}: ${have} -> ${count}`);
      return true;
    } catch (e) {
      console.warn("[thumbnail-sync] sync failed; trying init()", e);
      try {
        syncRendererFilePages(renderer, count);
        const ok = rebuildFromViewer(thumbs);
        if (ok) lastPageCount = count;
        return ok;
      } catch (fallbackError) {
        console.warn("[thumbnail-sync] fallback init failed", fallbackError);
        return false;
      }
    }
  }

  function scheduleSync(reason) {
    const token = ++syncToken;
    for (const delay of [0, 80, 250, 600, 1200, 2000]) {
      setTimeout(() => { if (token === syncToken) syncThumbnails(reason); }, delay);
    }
  }

  function wrapMethod(target, name, reason) {
    if (!target || typeof target[name] !== "function" || target[name].__thumbnailSyncWrapped) return;
    const original = target[name];
    target[name] = function (...args) {
      const result = original.apply(this, args);
      scheduleSync(reason || name);
      return result;
    };
    target[name].__thumbnailSyncWrapped = true;
  }

  function installHooks() {
    const editor = getEditor();
    if (editor && editor !== patchedEditor) {
      patchedEditor = editor;
      wrapMethod(editor, "asc_AddPage", "asc_AddPage");
      wrapMethod(editor, "asc_RemovePage", "asc_RemovePage");
    }

    const doc = getPdfDoc();
    if (doc && doc !== patchedDoc) {
      patchedDoc = doc;
      wrapMethod(doc, "MergePagesBinary", "MergePagesBinary");
      wrapMethod(doc, "AddPage", "AddPage");
      wrapMethod(doc, "RemovePage", "RemovePage");
      wrapMethod(doc, "RemovePages", "RemovePages");
    }
  }

  window.__syncOnlyOfficeThumbnails = scheduleSync;

  document.addEventListener("click", (event) => {
    const btn = event.target && event.target.closest && event.target.closest("[data-tool]");
    if (!btn || !PAGE_CHANGE_TOOLS.has(btn.getAttribute("data-tool"))) return;
    installHooks();
    scheduleSync(btn.getAttribute("data-tool"));
  }, true);

  setInterval(() => {
    installHooks();
    const count = getPageCount();
    if (count <= 0) return;

    if (lastPageCount === -1) {
      lastPageCount = count;
      syncThumbnails("initial-count");
      return;
    }

    const renderer = getRenderer();
    const filePages = renderer && renderer.file && Array.isArray(renderer.file.pages) ? renderer.file.pages.length : count;
    const thumbPages = renderer && renderer.Thumbnails && Array.isArray(renderer.Thumbnails.pages) ? renderer.Thumbnails.pages.length : count;
    if (count !== lastPageCount || filePages !== count || thumbPages !== count) scheduleSync("page-model-drift");
  }, 500);
})();

// Fix the PDF comment toolbar action for editor bundles that expose the
// comment payload as asc_CCommentDataWord instead of asc_CCommentData.
(function () {
  function getEditor() { return window.__pdfEditor || null; }

  function createCommentData(text) {
    const asc = window.Asc || {};
    const Ctor = asc.asc_CCommentDataWord || asc.asc_CCommentData;
    if (typeof Ctor !== "function") throw new Error("Kommentar-Datenklasse ist in diesem ONLYOFFICE-Build nicht verfügbar.");

    const data = new Ctor(null);
    if (typeof data.asc_putText === "function") data.asc_putText(text);
    else data.m_sText = text;

    const editor = getEditor();
    const userName = editor && editor.User && typeof editor.User.asc_getUserName === "function" ? editor.User.asc_getUserName() : "Offline";
    const userId = editor && editor.documentUserId ? editor.documentUserId : "offline-user";

    if (typeof data.asc_putUserName === "function") data.asc_putUserName(userName);
    else data.m_sUserName = userName;
    if (typeof data.asc_putUserId === "function") data.asc_putUserId(userId);
    else data.m_sUserId = userId;
    if (typeof data.asc_putOnlyOfficeTime === "function") data.asc_putOnlyOfficeTime(String(Date.now()));

    return data;
  }

  function setStatus(message) {
    const status = document.getElementById("status");
    if (status) status.textContent = message;
  }

  function showInAppPrompt(message) {
    const dialog = document.getElementById("prompt-dialog");
    const msg = document.getElementById("prompt-message");
    const input = document.getElementById("prompt-input");
    const ok = document.getElementById("prompt-ok");
    const cancel = document.getElementById("prompt-cancel");

    if (!dialog || !msg || !input || !ok || !cancel) {
      const fallback = typeof window.prompt === "function" ? window.prompt(message) : "";
      return Promise.resolve(fallback || "");
    }

    return new Promise((resolve) => {
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        dialog.hidden = true;
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        input.removeEventListener("keydown", onKeyDown);
        resolve(value || "");
      };
      const onOk = () => finish(input.value.trim());
      const onCancel = () => finish("");
      const onKeyDown = (event) => {
        if (event.key === "Enter") onOk();
        else if (event.key === "Escape") onCancel();
      };

      msg.textContent = message;
      input.value = "";
      dialog.hidden = false;
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      input.addEventListener("keydown", onKeyDown);
      setTimeout(() => input.focus(), 0);
    });
  }

  async function addComment() {
    const editor = getEditor();
    if (!editor || typeof editor.asc_addComment !== "function") {
      setStatus("Kommentar konnte nicht hinzugefügt werden: Editor ist noch nicht bereit.");
      return;
    }

    const text = await showInAppPrompt("Kommentartext:");
    if (!text) return;

    try {
      editor.asc_addComment(createCommentData(text));
      setStatus("Kommentar hinzugefügt.");
    } catch (error) {
      console.error("Kommentar fehlgeschlagen:", error);
      setStatus("Kommentar konnte nicht hinzugefügt werden (Text markieren oder Position wählen).");
    }
  }

  document.addEventListener("click", function (event) {
    const button = event.target && event.target.closest && event.target.closest('[data-tool="comment"]');
    if (!button || button.disabled) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    addComment();
  }, true);
})();
