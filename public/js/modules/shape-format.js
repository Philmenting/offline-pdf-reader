export function wireShapeFormat({ getEditor, canFormat, refocusEditor, setStatus }) {
  const panel = document.getElementById("shape-format");
  const fill = document.getElementById("shape-fill-color");
  const transparent = document.getElementById("shape-fill-none");
  const stroke = document.getElementById("shape-border-color");
  const width = document.getElementById("shape-border-width");
  let pending = null;
  let completionTimer = null;
  // PDF shapes can have custom geometry without a preset name. The SDK's
  // object interface, not the geometry label, determines style support.
  const supported = (shape) => shape && shape.IsShape?.() && !shape.IsEditFieldShape?.();
  const active = () => getEditor()?.getPDFDoc().GetActiveObject();
  const color = (hex) => new window.Asc.asc_CColor(
    parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16));
  const hex = (c) => "#" + [c.R, c.G, c.B].map(v => Math.round(v).toString(16).padStart(2, "0")).join("");
  function show(visible) {
    if (panel.hidden === !visible) return;
    panel.hidden = !visible;
    getEditor()?.WordControl?.OnResize(true);
  }

  function apply(part = "all") {
    if (!canFormat()) return;
    if (!supported(active())) return;
    const Asc = window.Asc;
    const props = new Asc.asc_CShapeProperty();
    if (part === "all" || part === "fill") {
      const shapeFill = new Asc.asc_CShapeFill();
      shapeFill.asc_putType(transparent.checked ? Asc.c_oAscFill.FILL_TYPE_NOFILL : Asc.c_oAscFill.FILL_TYPE_SOLID);
      if (!transparent.checked) {
        const solid = new Asc.asc_CFillSolid();
        solid.asc_putColor(color(fill.value));
        shapeFill.asc_putFill(solid);
      }
      props.asc_putFill(shapeFill);
    }
    if (part === "all" || part === "stroke") {
      const border = new Asc.asc_CStroke();
      border.asc_putType(Number(width.value) === 0 ? Asc.c_oAscStrokeType.STROKE_NONE : Asc.c_oAscStrokeType.STROKE_COLOR);
      border.asc_putColor(color(stroke.value));
      border.asc_putWidth(Number(width.value) * 25.4 / 72);
      props.asc_putStroke(border);
    }
    getEditor().ShapeApply(props);
  }

  function sync() {
    // Recognized PDF text is also a CShape. Never resize the viewer or
    // apply drawing properties while the user is placing a text caret.
    if (!canFormat()) {
      pending = null;
      show(false);
      return;
    }
    if (pending) return;
    const shape = active();
    show(!!supported(shape));
    if (panel.hidden) return;
    const c = shape.brush?.fill?.color?.RGBA;
    transparent.checked = !c;
    if (c) fill.value = hex(c);
    const border = shape.pen?.Fill?.fill?.color?.RGBA;
    if (border) stroke.value = hex(border);
    const points = border ? (shape.pen.w ?? 12700) / 12700 : 0;
    if (![...width.options].some(option => Number(option.value) === points)) {
      width.add(new Option(`${points.toFixed(2)} pt`, String(points)));
    }
    width.value = String(points);
  }

  function completePending() {
    if (!pending || !canFormat()) return false;
    const shape = active();
    if (!supported(shape) || pending.has(shape)) return false;
    pending = null;
    clearTimeout(completionTimer);
    apply();
    return true;
  }

  function finish(attempt = 0) {
    if (!canFormat()) { sync(); return; }
    try { completePending(); }
    catch (error) { console.error(error); setStatus("Formfarbe konnte nicht gesetzt werden."); }
    if (pending && attempt < 20) {
      clearTimeout(completionTimer);
      completionTimer = setTimeout(() => finish(attempt + 1), 50);
    }
    sync();
  }

  for (const input of [fill, transparent, stroke, width]) {
    input.addEventListener("change", () => {
      if (input === fill) transparent.checked = false;
      try {
        // A native color picker may be used before the SDK reports the
        // newly drawn object as active. Complete that hand-off here too.
        if (!completePending() && !pending) apply(input === fill || input === transparent ? "fill" : "stroke");
      }
      catch (error) { console.error(error); setStatus("Formfarbe konnte nicht gesetzt werden."); }
      refocusEditor();
    });
  }

  return {
    start() {
      clearTimeout(completionTimer);
      const doc = getEditor().getPDFDoc();
      pending = new Set(Array.from({ length: doc.GetPagesCount() }, (_, i) => doc.GetPageInfo(i).drawings || []).flat());
      show(true);
    },
    cancel() { clearTimeout(completionTimer); pending = null; sync(); },
    sync,
    finish,
  };
}
