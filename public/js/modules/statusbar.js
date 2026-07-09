// Status bar controls: current page / page count with jump-to-page input,
// and a zoom percent selector. State flows in from the engine's
// asc_onCurrentPage / asc_onCountPages / asc_onZoomChange callbacks (wired in
// main.js); user input flows back through the deps the host passes in.
import { el } from "./dom.js";

let deps = null; // { getRenderer, isDocOpen, setZoomPercent, refocusEditor }
let pageCount = 0;

export function updatePageCount(n) {
  pageCount = n | 0;
  el("page-count").textContent = `/ ${pageCount}`;
  const input = el("page-input");
  input.max = String(Math.max(1, pageCount));
}

export function updateCurrentPage(nZeroBased) {
  const input = el("page-input");
  if (document.activeElement !== input) input.value = String((nZeroBased | 0) + 1);
}

export function updateZoomDisplay(percent) {
  const select = el("zoom-select");
  if (!select || document.activeElement === select) return;
  const value = String(Math.round(percent));
  // keep the list tidy: show arbitrary engine zoom values via a transient option
  let transient = select.querySelector("option[data-transient]");
  if ([...select.options].some((o) => !o.dataset.transient && o.value === value)) {
    if (transient) transient.remove();
    select.value = value;
    return;
  }
  if (!transient) {
    transient = document.createElement("option");
    transient.dataset.transient = "1";
    select.appendChild(transient);
  }
  transient.value = value;
  transient.textContent = `${value} %`;
  select.value = value;
}

export function setStatusControlsVisible(on) {
  el("status-controls").hidden = !on;
}

export function wireStatusBar(dependencies) {
  deps = dependencies;
  const input = el("page-input");

  const jump = () => {
    if (!deps.isDocOpen()) return;
    const target = Math.max(1, Math.min(pageCount || 1, parseInt(input.value, 10) || 1));
    input.value = String(target);
    try {
      const r = deps.getRenderer();
      if (r && typeof r.navigateToPage === "function") r.navigateToPage(target - 1);
    } catch (e) { console.warn("Seitensprung fehlgeschlagen:", e); }
    deps.refocusEditor();
  };
  input.addEventListener("change", jump);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); jump(); }
    e.stopPropagation(); // keep keystrokes away from the editor
  });

  el("zoom-select").addEventListener("change", () => {
    if (!deps.isDocOpen()) return;
    deps.setZoomPercent(parseInt(el("zoom-select").value, 10) || 100);
    deps.refocusEditor();
  });
}
