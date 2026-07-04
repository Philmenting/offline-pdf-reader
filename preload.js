// Preload bridge for the Electron desktop app. Exposes a minimal, explicit
// API to the renderer (contextIsolation stays on): native save dialog + file
// write, so "Speichern" behaves like a desktop app instead of a browser
// download. The web build simply never sees window.desktop and falls back.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  /**
   * Save PDF bytes via a native save dialog.
   * @param {Uint8Array} bytes
   * @param {string} suggestedName
   * @returns {Promise<{saved: boolean, path?: string, error?: string}>}
   */
  savePdf: (bytes, suggestedName) =>
    ipcRenderer.invoke("save-pdf", bytes, suggestedName),
});
