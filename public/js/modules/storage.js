// Persistent app storage with two backends:
//
//  • Desktop (Electron): IPC to the main process, which writes into the
//    userData directory. The renderer's own web storage does NOT survive an
//    app restart there — the internal HTTP server binds a random port each
//    launch, so the origin (and with it localStorage/IndexedDB) changes.
//  • Web build: IndexedDB (stable origin, e.g. the dev server's port).
//
// Everything returns Promises; callers never need to know the backend.

const DB_NAME = "offline-pdf-editor";
const STORE = "app-state";

const isDesktop = () => !!window.desktop;

// Single opener for the app's IndexedDB — EVERY consumer must go through
// this. Opening the same database with a lower explicit version (as the
// pending-open handover in main.js once did with version 1) throws
// VersionError as soon as this v2 exists, which silently broke opening a
// second document in the web build.
export function openAppDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 2);
    req.onupgradeneeded = () => {
      const db = req.result;
      // v1 created "pending-open" (see main.js); v2 adds this store.
      if (!db.objectStoreNames.contains("pending-open")) db.createObjectStore("pending-open");
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

const idb = openAppDb;

async function idbGet(key) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE).objectStore(STORE).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbSet(key, value) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    if (value === undefined) tx.objectStore(STORE).delete(key);
    else tx.objectStore(STORE).put(value, key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

/** Small JSON values (e.g. the saved signature data URL). */
export async function storeGet(key) {
  try {
    return isDesktop() ? await window.desktop.storeGet(key) : await idbGet(`kv:${key}`);
  } catch { return undefined; }
}
export async function storeSet(key, value) {
  try {
    if (isDesktop()) await window.desktop.storeSet(key, value);
    else await idbSet(`kv:${key}`, value);
  } catch (e) { console.warn(`storeSet(${key}) fehlgeschlagen:`, e); }
}

/** Recently opened files — desktop only (the web build has no file paths). */
export async function listRecentFiles() {
  if (!isDesktop() || typeof window.desktop.listRecent !== "function") return [];
  try { return (await window.desktop.listRecent()) || []; } catch { return []; }
}
export async function openRecentFile(path) {
  if (!isDesktop()) return { ok: false };
  try { return await window.desktop.openRecent(path); } catch (e) { return { ok: false, error: e.message }; }
}

/** Crash-recovery snapshot: { name, ts, data: Uint8Array } | null. */
export async function recoverySave(bytes, name) {
  try {
    if (isDesktop()) await window.desktop.recoverySave(bytes, name);
    else await idbSet("recovery", { name, ts: Date.now(), data: bytes });
  } catch (e) { console.warn("recoverySave fehlgeschlagen:", e); }
}
export async function recoveryLoad() {
  try {
    return isDesktop() ? await window.desktop.recoveryLoad() : (await idbGet("recovery")) || null;
  } catch { return null; }
}
export async function recoveryClear() {
  try {
    if (isDesktop()) await window.desktop.recoveryClear();
    else await idbSet("recovery", undefined);
  } catch { /* best effort */ }
}
