// "Zuletzt geöffnet" list on the empty-state card. Desktop only: the web
// build has no file paths to reopen, so the section simply stays hidden there.
import { el, setStatus } from "./dom.js";
import { listRecentFiles, openRecentFile } from "./storage.js";

export async function renderRecentFiles() {
  const section = el("recent-files");
  if (!section || !window.desktop) return;
  const entries = await listRecentFiles();
  const list = el("recent-list");
  list.textContent = "";
  section.hidden = entries.length === 0;
  for (const entry of entries.slice(0, 6)) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "recent-item";
    btn.title = entry.path;
    const name = document.createElement("span");
    name.className = "recent-name";
    name.textContent = entry.name;
    const date = document.createElement("span");
    date.className = "recent-date";
    date.textContent = new Date(entry.ts).toLocaleDateString("de-DE");
    btn.append(name, date);
    btn.addEventListener("click", async () => {
      setStatus(`Öffne „${entry.name}" …`);
      const res = await openRecentFile(entry.path);
      if (res && res.ok === false) {
        setStatus(`„${entry.name}" konnte nicht geöffnet werden${res.error ? `: ${res.error}` : "."}`);
        renderRecentFiles(); // prune vanished files
      }
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}
