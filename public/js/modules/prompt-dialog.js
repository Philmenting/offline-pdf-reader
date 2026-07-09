// Reusable in-page prompt dialog. Replaces window.prompt(), which Electron's
// BrowserWindow does not implement (it throws "prompt() is not supported").
import { el } from "./dom.js";

let promptResolve = null;
let onClose = null; // e.g. hand focus back to the editor

export function showPromptDialog(message, defaultValue) {
  return new Promise((resolve) => {
    promptResolve = resolve;
    el("prompt-message").textContent = message;
    const input = el("prompt-input");
    input.value = defaultValue != null ? defaultValue : "";
    el("prompt-dialog").hidden = false;
    input.focus();
    input.select();
  });
}

function closePromptDialog(result) {
  el("prompt-dialog").hidden = true;
  if (onClose) onClose();
  const resolve = promptResolve;
  promptResolve = null;
  if (resolve) resolve(result);
}

export function wirePromptDialog(onCloseCallback) {
  onClose = onCloseCallback;
  const input = el("prompt-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); closePromptDialog(input.value); }
    else if (e.key === "Escape") { e.preventDefault(); closePromptDialog(null); }
    e.stopPropagation(); // keep typed characters away from the editor
  });
  el("prompt-ok").addEventListener("click", () => closePromptDialog(input.value));
  el("prompt-cancel").addEventListener("click", () => closePromptDialog(null));
  el("prompt-dialog").addEventListener("click", (e) => {
    if (e.target === el("prompt-dialog")) closePromptDialog(null); // backdrop click cancels
  });
}
