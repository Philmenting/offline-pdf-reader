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

  /**
   * Print: write the PDF to a temp file and open it in the system's default
   * PDF application (which has a proper print dialog).
   * @returns {Promise<{ok: boolean, error?: string}>}
   */
  printPdf: (bytes, name) => ipcRenderer.invoke("print-pdf", bytes, name),

  /**
   * Open a compose window in the default Windows mail client with the PDF
   * attached. The main process keeps the temporary attachment alive until
   * the compose window is closed.
   * @returns {Promise<{ok: boolean, canceled?: boolean, error?: string}>}
   */
  sendPdfByEmail: (bytes, name) =>
    ipcRenderer.invoke("send-pdf-by-email", bytes, name),

  /**
   * Receive files the OS asked us to open ("Öffnen mit" / double-click /
   * second instance). Callback gets { name, data: Uint8Array }.
   */
  onOpenFile: (callback) => {
    ipcRenderer.on("open-file", (_event, payload) => callback(payload));
  },

  /** Tell the main process the editor can accept files now. */
  rendererReady: () => ipcRenderer.send("renderer-ready"),

  /**
   * Native open dialog (main process). Returns { opened } — the file itself
   * arrives through the regular onOpenFile channel, so the recent-files list
   * gets the real path.
   */
  openPdfDialog: () => ipcRenderer.invoke("open-pdf-dialog"),

  /** Recently opened files: [{ path, name, ts }], newest first. */
  listRecent: () => ipcRenderer.invoke("recent-list"),
  /** Reopen a recent file by path (delivered via onOpenFile). */
  openRecent: (path) => ipcRenderer.invoke("recent-open", path),

  /** Small persistent key/value store (survives restarts, unlike web storage). */
  storeGet: (key) => ipcRenderer.invoke("store-get", key),
  storeSet: (key, value) => ipcRenderer.invoke("store-set", key, value),

  /** Crash-recovery snapshot of the open document. */
  recoverySave: (bytes, name) => ipcRenderer.invoke("recovery-save", bytes, name),
  recoveryLoad: () => ipcRenderer.invoke("recovery-load"),
  recoveryClear: () => ipcRenderer.invoke("recovery-clear"),
});
