// Text formatting bridge for the offline PDF editor toolbar.
//
// The ONLYOFFICE PDF editor exposes the same text-property API as the document
// editor (put_TextPrFontName, put_TextPrFontSize, put_TextPrBold). This module
// keeps a compact toolbar in sync with the current selection and applies the
// chosen formatting both to selected text and immediately before new text is
// inserted.
(function installTextFormatToolbar() {
  const FONT_FALLBACK = "Liberation Sans";
  const DEFAULT_SIZE = 12;
  const POLL_MS = 400;

  const state = {
    family: FONT_FALLBACK,
    size: DEFAULT_SIZE,
    bold: false,
  };
  let controls = null;
  let lastEditor = null;
  let suppressSyncUntil = 0;

  function byId(id) { return document.getElementById(id); }

  function getEditor() {
    return window.Asc && window.Asc.editor ? window.Asc.editor : null;
  }

  function hasOpenPdf(editor) {
    try { return !!(editor && typeof editor.getPDFDoc === "function" && editor.getPDFDoc()); }
    catch { return false; }
  }

  function canUseTextApi(editor) {
    return !!(editor
      && typeof editor.put_TextPrFontName === "function"
      && typeof editor.put_TextPrBold === "function");
  }

  function normalizeSize(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return DEFAULT_SIZE;
    return Math.max(6, Math.min(96, Math.round(n * 10) / 10));
  }

  function getTextPr(editor) {
    if (!editor || typeof editor.get_TextProps !== "function") return null;
    try {
      const props = editor.get_TextProps();
      return props && (props.TextPr || (typeof props.get_TextPr === "function" && props.get_TextPr())) || null;
    } catch {
      return null;
    }
  }

  function readSelectionFormat(editor) {
    const textPr = getTextPr(editor);
    if (!textPr) return null;

    const next = {};
    const family = textPr.FontFamily && textPr.FontFamily.Name;
    if (typeof family === "string" && family && !family.startsWith("Embedded: ")) next.family = family;
    if (typeof textPr.FontSize === "number" && textPr.FontSize > 0) next.size = normalizeSize(textPr.FontSize);
    if (typeof textPr.Bold === "boolean") next.bold = textPr.Bold;
    return next;
  }

  function setControlsEnabled(enabled) {
    if (!controls) return;
    for (const control of [controls.family, controls.size, controls.bold]) {
      if (control) control.disabled = !enabled;
    }
  }

  function renderControls() {
    if (!controls) return;
    if (controls.family && document.activeElement !== controls.family) controls.family.value = state.family;
    if (controls.size && document.activeElement !== controls.size) controls.size.value = String(state.size);
    if (controls.bold) {
      controls.bold.classList.toggle("active", !!state.bold);
      controls.bold.setAttribute("aria-pressed", state.bold ? "true" : "false");
    }
  }

  function applyFormat(options) {
    const editor = getEditor();
    if (!hasOpenPdf(editor) || !canUseTextApi(editor)) return false;

    try {
      if (typeof editor.put_TextPrFontName === "function") editor.put_TextPrFontName(state.family || FONT_FALLBACK);
      if (typeof editor.put_TextPrFontSize === "function") editor.put_TextPrFontSize(normalizeSize(state.size));
      if (typeof editor.put_TextPrBold === "function") editor.put_TextPrBold(!!state.bold);
      if (!options || !options.silent) console.log(`[format] text: ${state.family}, ${state.size}pt, bold=${state.bold}`);
      return true;
    } catch (error) {
      console.warn("[format] Textformat konnte nicht angewendet werden:", error);
      return false;
    }
  }

  function patchEnterText(editor) {
    if (!editor || editor.__offlineTextFormatEnterPatched || typeof editor.asc_enterText !== "function") return;
    const origEnterText = editor.asc_enterText;
    editor.asc_enterText = function () {
      applyFormat({ silent: true });
      return origEnterText.apply(this, arguments);
    };
    editor.__offlineTextFormatEnterPatched = true;
    console.log("[format] asc_enterText formatting bridge installed");
  }

  function syncFromSelection() {
    if (Date.now() < suppressSyncUntil) return;
    const editor = getEditor();
    const enabled = hasOpenPdf(editor) && canUseTextApi(editor);
    setControlsEnabled(enabled);
    if (!enabled) return;

    patchEnterText(editor);
    const selected = readSelectionFormat(editor);
    if (!selected) return;

    Object.assign(state, selected);
    renderControls();
  }

  function installControlHandlers() {
    controls = {
      family: byId("text-font-family"),
      size: byId("text-font-size"),
      bold: byId("text-bold"),
    };
    if (!controls.family || !controls.size || !controls.bold) return false;

    controls.family.addEventListener("change", () => {
      state.family = controls.family.value || FONT_FALLBACK;
      suppressSyncUntil = Date.now() + 1200;
      applyFormat();
      renderControls();
    });
    controls.size.addEventListener("change", () => {
      state.size = normalizeSize(controls.size.value);
      suppressSyncUntil = Date.now() + 1200;
      applyFormat();
      renderControls();
    });
    controls.bold.addEventListener("click", () => {
      state.bold = !state.bold;
      suppressSyncUntil = Date.now() + 1200;
      applyFormat();
      renderControls();
    });

    setControlsEnabled(false);
    renderControls();
    console.log("[format] text toolbar installed");
    return true;
  }

  function tick() {
    const editor = getEditor();
    if (editor && editor !== lastEditor) {
      lastEditor = editor;
      patchEnterText(editor);
    }
    syncFromSelection();
  }

  if (!installControlHandlers()) {
    console.warn("[format] text toolbar controls not found");
    return;
  }

  setInterval(tick, POLL_MS);
  document.addEventListener("selectionchange", syncFromSelection);
})();
