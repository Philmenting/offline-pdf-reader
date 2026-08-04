// Auto-recovery: while the document has unsaved changes, periodically
// serialize it and stash the bytes (userData on desktop, IndexedDB on web).
// On the next start, an existing snapshot is offered for restore. The
// snapshot is cleared on save and on clean close — it only survives crashes
// and hard kills.
import { setStatus } from "./dom.js";
import { recoverySave, recoveryLoad, recoveryClear } from "./storage.js";

const SNAPSHOT_INTERVAL_MS = 90 * 1000;
// serializing commits the active form field / text box, so never snapshot
// while the user is actively typing — wait for a pause.
const IDLE_REQUIRED_MS = 10 * 1000;

let deps = null; // { isDirty, isDocOpen, collectPdfBytes, getDocName, openArrayBuffer, markSaved }
let lastInputTs = 0;
let lastSnapshotTs = 0;
let snapshotting = false;

async function maybeSnapshot() {
  if (snapshotting || !deps.isDocOpen() || !deps.isDirty()) return;
  const now = Date.now();
  if (now - lastInputTs < IDLE_REQUIRED_MS) return;
  if (now - lastSnapshotTs < SNAPSHOT_INTERVAL_MS) return;
  snapshotting = true;
  try {
    const bytes = await deps.collectPdfBytes();
    if (bytes) {
      await recoverySave(bytes, deps.getDocName());
      lastSnapshotTs = now;
      console.log(`[recovery] Snapshot gespeichert (${bytes.length} Bytes)`);
    }
  } catch (e) {
    console.warn("[recovery] Snapshot fehlgeschlagen:", e);
  } finally {
    snapshotting = false;
  }
}

/** Call when the document was saved or intentionally discarded. */
export function clearRecoverySnapshot() {
  lastSnapshotTs = 0;
  return recoveryClear();
}

/** Offer a leftover snapshot (crash last time) for restore. */
export async function offerRecovery() {
  const snap = await recoveryLoad();
  if (!snap || !snap.data || deps.isDocOpen()) return;
  const when = new Date(snap.ts).toLocaleString("de-DE");
  const restore = window.confirm(
    `Es wurde eine nicht gespeicherte Sitzung gefunden:\n„${snap.name}" (${when}).\n\nWiederherstellen?`);
  if (restore) {
    const bytes = snap.data instanceof Uint8Array ? snap.data : new Uint8Array(snap.data);
    deps.openArrayBuffer(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
      snap.name || "wiederhergestellt.pdf");
    setStatus(`Sitzung „${snap.name}" wiederhergestellt — bitte speichern.`);
  }
  // either way the snapshot is consumed; a restored doc re-snapshots on edit
  await recoveryClear();
}

export function initRecovery(dependencies) {
  deps = dependencies;
  lastInputTs = Date.now();
  window.addEventListener("keydown", () => { lastInputTs = Date.now(); }, true);
  window.addEventListener("mousedown", () => { lastInputTs = Date.now(); }, true);
  setInterval(maybeSnapshot, 15 * 1000);
}
