const { app, BrowserWindow } = require("electron");
const { createServer } = require("http");
const { readFile, stat } = require("fs/promises");
const { join, posix, extname } = require("path");

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

const MOUNTS = [
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
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${port}`);
  mainWindow.on("closed", () => { mainWindow = null; });
});

app.on("window-all-closed", () => app.quit());
