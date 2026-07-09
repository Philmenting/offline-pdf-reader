// Search bar (Strg+F): thin UI over the engine's own search. asc_findText
// fills the PDF search engine (returns the match count, ids 0..count-1),
// asc_SelectSearchElement jumps to a match (scroll + highlight),
// asc_endFindText clears everything.
import { el } from "./dom.js";

const search = { query: "", count: 0, current: -1, debounce: 0 };
let deps = null; // { getEditor, canSearch, refocusEditor }

function updateSearchCount() {
  el("search-count").textContent =
    search.count > 0 ? `${search.current + 1}/${search.count}` : "0/0";
}

function runSearch(query) {
  const editor = deps.getEditor();
  search.query = query;
  if (!query) {
    try { editor.asc_endFindText(); } catch { /* no active search */ }
    search.count = 0;
    search.current = -1;
    updateSearchCount();
    return;
  }
  try {
    const props = new window.AscCommon.CSearchSettings();
    props.put_Text(query);
    props.put_MatchCase(false);
    search.count = editor.asc_findText(props, true) | 0;
    search.current = search.count > 0 ? 0 : -1;
    if (search.count > 0) {
      // highlight ALL matches on the page, then select the first
      try { editor._selectSearchingResults(true); } catch { /* optional */ }
      editor.asc_SelectSearchElement(0);
    }
  } catch (e) {
    console.warn("Suche fehlgeschlagen:", e);
    search.count = 0;
    search.current = -1;
  }
  updateSearchCount();
}

export function searchStep(dir) {
  if (search.count <= 0) return;
  search.current = (search.current + dir + search.count) % search.count;
  try { deps.getEditor().asc_SelectSearchElement(search.current); }
  catch (e) { console.warn("Treffer-Navigation fehlgeschlagen:", e); }
  updateSearchCount();
}

export function openSearchBar() {
  if (!deps.canSearch()) return;
  el("search-bar").hidden = false;
  const input = el("search-input");
  input.focus();
  input.select();
}

export function closeSearchBar() {
  el("search-bar").hidden = true;
  runSearch("");
  deps.refocusEditor();
}

export function wireSearchBar(dependencies) {
  deps = dependencies;
  const input = el("search-input");
  input.addEventListener("input", () => {
    clearTimeout(search.debounce);
    search.debounce = setTimeout(() => runSearch(input.value.trim()), 300);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      // fresh query typed without waiting for the debounce → search now
      if (input.value.trim() !== search.query) runSearch(input.value.trim());
      else searchStep(e.shiftKey ? -1 : 1);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeSearchBar();
    }
    e.stopPropagation(); // keep typed characters away from the editor
  });
  el("search-next").addEventListener("click", () => searchStep(1));
  el("search-prev").addEventListener("click", () => searchStep(-1));
  el("search-close").addEventListener("click", closeSearchBar);
}
