import { test } from "node:test";
import assert from "node:assert/strict";
import { wireShapeFormat } from "../public/js/modules/shape-format.js";

test("text mode never reads drawing selection, applies styles, or resizes on clicks", () => {
  const elements = new Map();
  const previousDocument = globalThis.document;
  globalThis.document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      hidden: true, events: {},
      addEventListener(name, callback) { this.events[name] = callback; },
    });
    return elements.get(id);
  } };
  let editorAccesses = 0;
  try {
    const format = wireShapeFormat({
      getEditor() { editorAccesses++; throw new Error("Text selection must stay untouched"); },
      canFormat: () => false,
      refocusEditor() {},
      setStatus(message) { assert.fail(message); },
    });
    format.sync();
    format.finish();
    elements.get("shape-fill-color").events.change();
    assert.equal(editorAccesses, 0);
    assert.equal(elements.get("shape-format").hidden, true);
  } finally {
    globalThis.document = previousDocument;
  }
});

test("a late shape selection accepts the first color change and border removal", () => {
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  const nodes = new Map();
  let selected = null;
  const applied = [];
  class Properties {
    asc_putType(v) { this.type = v; }
    asc_putColor(v) { this.color = v; }
    asc_putWidth(v) { this.width = v; }
    asc_putFill(v) { this.fill = v; }
    asc_putStroke(v) { this.stroke = v; }
  }
  globalThis.document = { getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, { hidden: true, checked: false,
      value: id === "shape-border-width" ? "1" : "#ffffff", events: {},
      addEventListener(event, fn) { this.events[event] = fn; },
    });
    return nodes.get(id);
  } };
  globalThis.window = { Asc: {
    asc_CShapeProperty: Properties, asc_CShapeFill: Properties,
    asc_CFillSolid: Properties, asc_CStroke: Properties,
    asc_CColor: class { constructor(r, g, b) { Object.assign(this, { r, g, b }); } },
    c_oAscFill: { FILL_TYPE_NOFILL: 2, FILL_TYPE_SOLID: 3 },
    c_oAscStrokeType: { STROKE_NONE: 0, STROKE_COLOR: 1 },
  } };
  const editor = { getPDFDoc: () => ({ GetActiveObject: () => selected,
    GetPagesCount: () => 1, GetPageInfo: () => ({ drawings: [] }) }),
    ShapeApply: (props) => applied.push(props),
  };
  try {
    const format = wireShapeFormat({ getEditor: () => editor, canFormat: () => true,
      refocusEditor() {}, setStatus: (message) => assert.fail(message) });
    format.start();
    format.finish(); // The SDK has not selected the new object yet.
    selected = { IsShape: () => true }; // PDF geometry need not have a preset label.
    nodes.get("shape-fill-color").events.change();
    assert.equal(applied.length, 1);
    assert.deepEqual({ ...applied[0].fill.fill.color }, { r: 255, g: 255, b: 255 });
    selected.brush = { fill: { color: { RGBA: { R: 79, G: 129, B: 189 } } } };
    format.sync(); // SDK paint properties still carry the old theme color.
    assert.equal(nodes.get("shape-fill-color").value, "#ffffff");
    nodes.get("shape-border-width").value = "0";
    nodes.get("shape-border-width").events.change();
    assert.equal(applied[1].stroke.type, 0);
  } finally {
    globalThis.document = previousDocument;
    globalThis.window = previousWindow;
  }
});
