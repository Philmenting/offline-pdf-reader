// Fix the PDF comment toolbar action for editor bundles that expose the
// comment payload as asc_CCommentDataWord instead of asc_CCommentData.
(function () {
  function getEditor() {
    return window.__pdfEditor || null;
  }

  function createCommentData(text) {
    const asc = window.Asc || {};
    const Ctor = asc.asc_CCommentDataWord || asc.asc_CCommentData;
    if (typeof Ctor !== "function") {
      throw new Error("Kommentar-Datenklasse ist in diesem ONLYOFFICE-Build nicht verfügbar.");
    }

    const data = new Ctor(null);
    if (typeof data.asc_putText === "function") data.asc_putText(text);
    else data.m_sText = text;

    const editor = getEditor();
    const userName = editor && editor.User && typeof editor.User.asc_getUserName === "function"
      ? editor.User.asc_getUserName()
      : "Offline";
    const userId = editor && editor.documentUserId ? editor.documentUserId : "offline-user";

    if (typeof data.asc_putUserName === "function") data.asc_putUserName(userName);
    else data.m_sUserName = userName;
    if (typeof data.asc_putUserId === "function") data.asc_putUserId(userId);
    else data.m_sUserId = userId;
    if (typeof data.asc_putOnlyOfficeTime === "function") data.asc_putOnlyOfficeTime(String(Date.now()));

    return data;
  }

  function setStatus(message) {
    const status = document.getElementById("status");
    if (status) status.textContent = message;
  }

  function addComment() {
    const editor = getEditor();
    if (!editor || typeof editor.asc_addComment !== "function") {
      setStatus("Kommentar konnte nicht hinzugefügt werden: Editor ist noch nicht bereit.");
      return;
    }

    const text = window.prompt("Kommentartext:");
    if (!text) return;

    try {
      editor.asc_addComment(createCommentData(text));
      setStatus("Kommentar hinzugefügt.");
    } catch (error) {
      console.error("Kommentar fehlgeschlagen:", error);
      setStatus("Kommentar konnte nicht hinzugefügt werden (Text markieren oder Position wählen).");
    }
  }

  document.addEventListener("click", function (event) {
    const button = event.target && event.target.closest && event.target.closest('[data-tool="comment"]');
    if (!button || button.disabled) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    addComment();
  }, true);
})();
