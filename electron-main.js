const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const { createServer } = require("http");
const { readFile, stat, writeFile } = require("fs/promises");
const { appendFileSync } = require("fs");
const { join, posix, extname, dirname } = require("path");
const os = require("os");

// Diagnostic log written next to the executable (falls back to temp dir if the
// install folder is read-only). Captures renderer console output and failed
// resource loads so rendering problems can be diagnosed without DevTools.
let LOG_PATH = join(dirname(app.getPath("exe")), "offline-pdf-editor.log");
function logLine(line) {
  const stamped = `[${new Date().toISOString()}] ${line}\n`;
  try {
    appendFileSync(LOG_PATH, stamped);
  } catch {
    try {
      LOG_PATH = join(os.tmpdir(), "offline-pdf-editor.log");
      appendFileSync(LOG_PATH, stamped);
    } catch { /* give up quietly */ }
  }
}

// electron-main.js lives next to public/ and vendor/ both in dev (repo root)
// and when packaged (resources/app/), so __dirname is the correct base in both.
const ROOT = __dirname;
const PORT = 0;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".bin": "application/octet-stream",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".pdf": "application/pdf",
  ".ttf": "font/ttf",
  ".otf": "font/otf",
  ".ttc": "font/collection",
};

// `/sdkjs/` aliases the vendored ONLYOFFICE sdkjs root so the editor core's
// hardcoded `../../../../sdkjs/…` asset paths (font engine, cursors, spell,
// stamps) resolve against our offline bundle. `/fonts/` is a safety net for
// CGlobalFontLoader's hardcoded fontFilesPath (patched at runtime too).
const MOUNTS = [
  { prefix: "/sdkjs/", dir: join(ROOT, "vendor", "onlyoffice", "sdkjs") },
  { prefix: "/fonts/", dir: join(ROOT, "vendor", "fonts") },
  { prefix: "/vendor/", dir: join(ROOT, "vendor") },
  { prefix: "/", dir: join(ROOT, "public") },
];

function resolvePath(urlPath) {
  // Use posix.normalize so URL slashes stay "/" on Windows (path.normalize
  // would rewrite them to "\\" and break the prefix matching below).
  const clean = posix.normalize(decodeURIComponent(urlPath.split("?")[0]));
  if (clean.includes("..")) return null;
  for (const m of MOUNTS) {
    if (clean.startsWith(m.prefix)) {
      const rel = clean.slice(m.prefix.length) || "index.html";
      return join(m.dir, ...rel.split("/"));
    }
  }
  return null;
}

function startServer() {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      let filePath = resolvePath(req.url || "/");
      if (!filePath) { res.writeHead(400).end("Bad request"); return; }
      try {
        let info = await stat(filePath);
        if (info.isDirectory()) filePath = join(filePath, "index.html");
        const body = await readFile(filePath);
        res.writeHead(200, {
          "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
          "Cross-Origin-Opener-Policy": "same-origin",
          "Cross-Origin-Embedder-Policy": "require-corp",
        });
        res.end(body);
      } catch {
        res.writeHead(404, { "Content-Type": "text/plain" }).end("Not found");
      }
    });
    server.listen(PORT, "127.0.0.1", () => resolve(server.address().port));
  });
}

let mainWindow;
let rendererReady = false;
let pendingOpenPath = null;

// "Öffnen mit" / double-click: a PDF path may arrive on the command line
// (first launch) or from a second instance (single-instance lock below).
function pdfPathFromArgv(argv) {
  // packaged: [exe, ...args]; dev: [electron, ., ...args]
  const args = argv.slice(app.isPackaged ? 1 : 2);
  return args.find((a) => /\.pdf$/i.test(a) && !a.startsWith("-")) || null;
}

async function sendOpenFile(filePath) {
  if (!filePath) return;
  if (!mainWindow || !rendererReady) {
    pendingOpenPath = filePath; // delivered once the renderer says it's ready
    return;
  }
  try {
    const data = await readFile(filePath);
    const name = filePath.replace(/^.*[\\/]/, "");
    mainWindow.webContents.send("open-file", { name, data });
    logLine(`[open-with] sent ${filePath} (${data.length} bytes)`);
  } catch (e) {
    logLine(`[open-with] FAILED to read ${filePath}: ${e.message}`);
  }
}

// Single instance: double-clicking another PDF focuses the existing window
// and opens the file there instead of spawning a second app.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
    sendOpenFile(pdfPathFromArgv(argv));
  });
}

// Renderer announces it can accept files (editor initialised).
ipcMain.on("renderer-ready", () => {
  rendererReady = true;
  if (pendingOpenPath) {
    const p = pendingOpenPath;
    pendingOpenPath = null;
    sendOpenFile(p);
  }
});

// Drucken: write the serialized PDF to a temp file and open it with the
// system's default PDF application, which owns the actual print dialog.
ipcMain.handle("print-pdf", async (_event, bytes, name) => {
  try {
    const safe = String(name || "dokument.pdf").replace(/[^\w.\-äöüÄÖÜß ]/g, "_");
    const tmp = join(os.tmpdir(), `print-${Date.now()}-${safe}`);
    await writeFile(tmp, Buffer.from(bytes));
    const err = await shell.openPath(tmp);
    if (err) throw new Error(err);
    logLine(`[print] opened ${tmp} in default PDF app`);
    return { ok: true };
  } catch (e) {
    logLine(`[print] FAILED: ${e.message}`);
    return { ok: false, error: e.message };
  }
});

// Native "Speichern": save dialog + direct file write. The renderer sends the
// serialized PDF bytes (structured-clone keeps them a Uint8Array).
ipcMain.handle("save-pdf", async (_event, bytes, suggestedName) => {
  try {
    const { canceled, filePath } = await dialog.showSaveDialog(mainWindow, {
      title: "PDF speichern",
      defaultPath: suggestedName || "dokument.pdf",
      filters: [{ name: "PDF-Dokument", extensions: ["pdf"] }],
    });
    if (canceled || !filePath) return { saved: false };
    await writeFile(filePath, Buffer.from(bytes));
    logLine(`[save] wrote ${filePath} (${bytes.length} bytes)`);
    return { saved: true, path: filePath };
  } catch (e) {
    logLine(`[save] FAILED: ${e.message}`);
    return { saved: false, error: e.message };
  }
});

app.on("ready", async () => {
  const port = await startServer();

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    title: "Offline PDF Editor",
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: join(__dirname, "preload.js"),
    },
  });

  // ── Diagnostics ────────────────────────────────────────────────────────
  logLine(`=== App start (packaged=${app.isPackaged}, electron=${process.versions.electron}) ===`);
  const wc = mainWindow.webContents;
  wc.on("console-message", (_e, level, message, line, sourceId) => {
    const tag = ["log", "warn", "error", "info"][level] || level;
    logLine(`[renderer:${tag}] ${message} (${sourceId}:${line})`);
  });
  wc.on("did-fail-load", (_e, code, desc, url) => {
    logLine(`[did-fail-load] ${code} ${desc} ${url}`);
  });
  wc.on("render-process-gone", (_e, details) => {
    logLine(`[render-process-gone] ${JSON.stringify(details)}`);
  });
  wc.on("unresponsive", () => logLine("[unresponsive]"));
  wc.session.webRequest.onCompleted((d) => {
    if (d.statusCode >= 400) logLine(`[http ${d.statusCode}] ${d.url}`);
  });
  // F12 toggles DevTools (kept for troubleshooting; not opened automatically).
  wc.on("before-input-event", (_e, input) => {
    if (input.type === "keyDown" && input.key === "F12") {
      wc.isDevToolsOpened() ? wc.closeDevTools() : wc.openDevTools({ mode: "detach" });
    }
  });

  mainWindow.loadURL(`http://127.0.0.1:${port}`);
  mainWindow.on("closed", () => { mainWindow = null; });

  // PDF passed on the command line ("Öffnen mit" / double-click)
  const argvPdf = pdfPathFromArgv(process.argv);
  if (argvPdf) {
    logLine(`[open-with] argv file: ${argvPdf}`);
    pendingOpenPath = argvPdf; // delivered on renderer-ready
  }
});

app.on("window-all-closed", () => app.quit());
