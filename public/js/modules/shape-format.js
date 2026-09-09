export function wireShapeFormat({ getEditor, refocusEditor, setStatus }) {
  const panel = document.getElementById("shape-format");
  const fill = document.getElementById("shape-fill-color");
  const transparent = document.getElementById("shape-fill-none");
  const stroke = document.getElementById("shape-border-color");
  const width = document.getElementById("shape-border-width");
  let pending = null;
  const supported = (shape) => shape && shape.IsShape?.() &&
    ["rect", "ellipse", "line", "lineWithArrow"].includes(shape.getPresetGeom?.());
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

  for (const input of [fill, transparent, stroke, width]) {
    input.addEventListener("change", () => {
      if (input === fill) transparent.checked = false;
      try { if (!pending) apply(input === fill || input === transparent ? "fill" : "stroke"); }
      catch (error) { console.error(error); setStatus("Formfarbe konnte nicht gesetzt werden."); }
      refocusEditor();
    });
  }

  return {
    start() {
      const doc = getEditor().getPDFDoc();
      pending = new Set(Array.from({ length: doc.GetPagesCount() }, (_, i) => doc.GetPageInfo(i).drawings || []).flat());
      show(true);
    },
    cancel() { pending = null; sync(); },
    sync,
    finish() {
      if (pending) {
        const shape = active();
        if (supported(shape) && !pending.has(shape)) {
          pending = null;
          try { apply(); }
          catch (error) { console.error(error); setStatus("Formfarbe konnte nicht gesetzt werden."); }
        }
      }
      sync();
    },
  };
}
