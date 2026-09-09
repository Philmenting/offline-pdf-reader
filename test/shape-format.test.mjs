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
