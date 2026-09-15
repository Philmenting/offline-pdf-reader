import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { guardTextShaper, keepPageContents } from "../scripts/engine-patches.mjs";
import { maskRegions } from "../public/js/modules/document-tools.js";

test("required engine patch accepts Windows and Unix newlines, rejects drift", () => {
  const src = "\t\tlet oFontInfo = this.GetFontInfo(this.FontSlot);\n"
    + "\t\tlet nFontId   = AscCommon.FontNameMap.GetId(this.FontId.m_pFaceInfo.family_name);";
  assert.equal(guardTextShaper(src), guardTextShaper(src.replaceAll("\n", "\r\n")));
  assert.match(guardTextShaper(src), /!this.FontId/);
  assert.throws(() => guardTextShaper("changed upstream"));
  assert.throws(() => guardTextShaper(src + src));
  const clear = "let bClearPage = !!oFile.pages[curIndex].isRecognized;";
  assert.equal(keepPageContents(clear + clear).match(/bClearPage = false/g).length, 2);
  assert.throws(() => keepPageContents(clear));
});

test("redaction covers boundary pixels and rejects invalid rectangles", () => {
  const calls = [];
  const canvas = { width: 100, height: 200, getContext: () => ({ fillRect: (...v) => calls.push(v) }) };
  maskRegions(canvas, [{ x: .2, y: .2, w: .1, h: .1 }]);
  assert.deepEqual(calls, [[18, 38, 15, 25]]);
  assert.throws(() => maskRegions(canvas, [{ x: -1, y: 0, w: 1, h: 1 }]));
  assert.throws(() => maskRegions(canvas, [{ x: 0, y: 0, w: NaN, h: 1 }]));
});

async function recoveryHarness(open) {
  const source = (await readFile(new URL("../public/js/modules/recovery.js", import.meta.url), "utf8"))
    .replace(/^import .*$/gm, "").replace(/export /g, "");
  const events = [];
  let tick, now = 0, finish;
  const bytes = new Promise(resolve => { finish = resolve; });
  class Clock extends Date { static now() { return now; } }
  const api = new Function("setStatus", "recoverySave", "recoveryLoad", "recoveryClear", "window", "setInterval", "Date",
    source + "\nreturn { initRecovery, offerRecovery, clearRecoverySnapshot };")(
    s => events.push(s), async () => events.push("saved"),
    async () => ({ data: new Uint8Array([37, 80, 68, 70, 45]), name: "test.pdf", ts: 0 }),
    async () => events.push("cleared"), { confirm: () => true, addEventListener() {} }, fn => { tick = fn; }, Clock);
  api.initRecovery({ isDirty: () => true, isDocOpen: () => false, getDocName: () => "test.pdf", collectPdfBytes: () => bytes, openArrayBuffer: open });
  return { api, events, bytes, finish, snapshot: () => {
    api.initRecovery({ isDirty: () => true, isDocOpen: () => true, getDocName: () => "test.pdf", collectPdfBytes: () => bytes });
    now = 120001; return tick();
  } };
}

test("restore waits for open and retains the snapshot on both success and failure", async () => {
  let resolve;
  const opened = new Promise(r => { resolve = r; });
  const h = await recoveryHarness((_bytes, _name, dirty) => { assert.equal(dirty, true); return opened; });
  const pending = h.api.offerRecovery();
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(h.events, []);
  resolve(); await pending;
  assert.ok(!h.events.includes("cleared"));
  const failed = await recoveryHarness(async () => { throw new Error("open failed"); });
  await failed.api.offerRecovery();
  assert.ok(!failed.events.includes("cleared"));
  assert.match(failed.events[0], /Sicherung bleibt erhalten/);
});

test("a snapshot finishing after explicit discard cannot recreate a backup", async () => {
  const h = await recoveryHarness(() => {});
  const pending = h.snapshot();
  await h.api.clearRecoverySnapshot();
  h.finish(new Uint8Array([1])); await pending;
  assert.deepEqual(h.events, ["cleared"]);
});
