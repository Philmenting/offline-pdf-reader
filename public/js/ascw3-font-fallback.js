// Runtime guard for ONLYOFFICE's ASCW3 mini-font.
//
// ASCW3 is useful for a tiny set of checkbox/bullet glyphs, but in the offline
// editor it can also be selected for ordinary typed text after PDF subset-font
// retargeting. The face then reports a glyph and prevents ONLYOFFICE's normal
// character fallback from running, which renders letters such as "Bayern" as
// .notdef boxes. Treat ASCW3 as unusable for normal text codepoints and ask the
// bundled fonts for a real glyph instead.
(function installAscw3TextFallback() {
  const PATCH_KEY = "__offlinePdfAscw3FallbackPatched";
  const logged = new Set();

  function fontFamily(font) {
    if (!font) return "";
    if (typeof font.GetFamilyName === "function") return font.GetFamilyName() || "";
    if (font.m_pFaceInfo && font.m_pFaceInfo.family_name) return font.m_pFaceInfo.family_name;
    return font.m_wsFontName || "";
  }

  function isNormalTextCodePoint(codePoint) {
    if (typeof codePoint !== "number") return false;
    if (codePoint < 0x20) return false;
    if (codePoint >= 0xE000 && codePoint <= 0xF8FF) return false; // private-use symbols

    return codePoint <= 0x024F      // basic Latin + Latin extensions
      || (codePoint >= 0x0370 && codePoint <= 0x052F)  // Greek + Cyrillic
      || (codePoint >= 0x1E00 && codePoint <= 0x1EFF)  // Latin extended additional
      || (codePoint >= 0x2000 && codePoint <= 0x206F)  // punctuation
      || (codePoint >= 0x20A0 && codePoint <= 0x20CF); // currency symbols
  }

  function preferredFamilyFor(codePoint) {
    const picker = window.AscFonts && window.AscFonts.FontPickerByCharacter;
    if (picker && typeof picker.getFontBySymbol === "function") {
      try {
        const picked = picker.getFontBySymbol(codePoint);
        if (picked && picked !== "ASCW3") return picked;
      } catch { /* fall through */ }
    }
    return "Liberation Sans";
  }

  function currentStyle(tm) {
    const setup = tm && tm.m_oLastFont;
    const style = setup && (setup.SetUpStyle ?? setup.Style ?? setup.FontStyle);
    return typeof style === "number" ? style : 0;
  }

  function loadFallbackFont(tm, codePoint) {
    if (!tm || typeof tm.SetFontInternal !== "function") return null;

    const size = (window.AscFonts && window.AscFonts.MEASURE_FONTSIZE) || 10;
    const style = currentStyle(tm);
    const candidates = [
      preferredFamilyFor(codePoint),
      "Liberation Sans",
      "DejaVu Sans",
      "FreeSans",
      "Liberation Serif",
      "DejaVu Serif",
      "FreeSerif",
    ];
    const seen = new Set();

    for (const family of candidates) {
      if (!family || family === "ASCW3" || seen.has(family)) continue;
      seen.add(family);

      let font = null;
      try { font = tm.SetFontInternal(family, size, style, 72); } catch { font = null; }
      if (font && typeof font.GetGIDByUnicode === "function" && font.GetGIDByUnicode(codePoint)) {
        return { Font: font, CodePoint: codePoint };
      }
    }
    return null;
  }

  function install() {
    const tm = window.AscCommon && window.AscCommon.g_oTextMeasurer;
    if (!tm || tm[PATCH_KEY] || typeof tm.GetFontBySymbol !== "function") return false;

    const origGetFontBySymbol = tm.GetFontBySymbol;
    tm.GetFontBySymbol = function (codePoint, oPreferredFont, isForcePreferred) {
      const result = origGetFontBySymbol.call(this, codePoint, oPreferredFont, isForcePreferred);
      const family = fontFamily(result && result.Font);

      if (family === "ASCW3" && isNormalTextCodePoint(codePoint)) {
        const replacement = loadFallbackFont(this, codePoint);
        if (replacement) {
          const replacementFamily = fontFamily(replacement.Font);
          const key = `${codePoint}:${replacementFamily}`;
          if (!logged.has(key)) {
            logged.add(key);
            console.warn(`[fonts] ASCW3 text fallback: U+${codePoint.toString(16)} -> "${replacementFamily}"`);
          }
          return replacement;
        }
      }
      return result;
    };

    if (typeof tm.CheckUnicodeInCurrentFont === "function") {
      const origCheckUnicode = tm.CheckUnicodeInCurrentFont;
      tm.CheckUnicodeInCurrentFont = function (codePoint) {
        const current = this.m_oManager && this.m_oManager.m_pFont;
        if (fontFamily(current) === "ASCW3" && isNormalTextCodePoint(codePoint)) {
          return false;
        }
        return origCheckUnicode.call(this, codePoint);
      };
    }

    tm[PATCH_KEY] = true;
    console.log("[fonts] ASCW3 text fallback patch installed");
    return true;
  }

  if (install()) return;
  const timer = setInterval(() => {
    if (install()) clearInterval(timer);
  }, 250);
  setTimeout(() => clearInterval(timer), 30000);
})();
