/**
 * Builds the `__fonts_ranges` table for AllFonts.js: a flat array of
 * [startCodepoint, endCodepoint, fontInfoIndex] triples, ascending and
 * non-overlapping, mapping Unicode ranges onto bundled font families.
 *
 * Two consumers read this table (via AscFonts.FontPickerByCharacter, which
 * `common/Drawings/Externals.js` initialises from window["__fonts_ranges"]):
 *   1. The JS-side font picker — substitutes the run font when typed
 *      characters aren't coverable by the current font.
 *   2. The WASM render engine — `drawingfile.js` serialises the ranges into
 *      `_InitializeFontsRanges`, which the engine uses to map codepoints to
 *      font files whenever the (subset-)embedded PDF font lacks a glyph.
 *
 * Upstream generates this data with the AllFontsGen tool; without it every
 * character missing from an embedded subset font renders as a .notdef box —
 * which is exactly what newly *typed* text in a PDF hits, since PDF subset
 * fonts only embed the glyphs the original document used.
 *
 * Family names are resolved against the actually-registered families, with
 * per-class fallbacks, so the emitted table never references a missing font.
 */
export function buildFontRanges(familyNames) {
  const index = new Map(familyNames.map((name, i) => [name, i]));
  const resolve = (prefs) => {
    for (const p of prefs) {
      if (index.has(p)) return index.get(p);
    }
    return -1;
  };

  const SANS  = ["Liberation Sans", "DejaVu Sans", "Open Sans", "FreeSans"];
  const WIDE  = ["DejaVu Sans", "FreeSans", "Liberation Sans"];   // broadest general coverage
  const INTL  = ["FreeSans", "DejaVu Sans"];                      // Armenian/Hebrew/Arabic
  const INDIC = ["FreeSerif", "FreeSans", "DejaVu Sans"];         // Indic scripts, Thai, Lao
  const SYM   = ["Symbola", "DejaVu Sans"];                       // symbols, dingbats, emoji

  // [start, end, preference list] — ascending, non-overlapping.
  const spec = [
    [0x0020, 0x04FF, SANS],   // Latin, Latin-1/Ext-A/B, IPA, Greek, Cyrillic
    [0x0500, 0x052F, WIDE],   // Cyrillic Supplement
    [0x0530, 0x058F, INTL],   // Armenian
    [0x0590, 0x05FF, INTL],   // Hebrew
    [0x0600, 0x08FF, INTL],   // Arabic + supplements
    [0x0900, 0x0DFF, INDIC],  // Devanagari … Sinhala
    [0x0E00, 0x0E7F, INDIC],  // Thai
    [0x0E80, 0x0FFF, INDIC],  // Lao, Tibetan (partial coverage)
    [0x10A0, 0x10FF, WIDE],   // Georgian
    [0x1D00, 0x1FFF, WIDE],   // Phonetic ext, Latin Ext Additional, Greek Ext
    [0x2000, 0x25FF, WIDE],   // Punctuation, currency, arrows, math, box drawing
    [0x2600, 0x27BF, SYM],    // Misc symbols + dingbats
    [0x27C0, 0x2BFF, WIDE],   // Supplemental arrows/math
    [0x2C60, 0x2C7F, WIDE],   // Latin Extended-C
    [0x2E00, 0x2E7F, WIDE],   // Supplemental punctuation
    [0xA720, 0xA7FF, WIDE],   // Latin Extended-D
    [0xFB00, 0xFB4F, WIDE],   // Alphabetic presentation forms (ligatures, Hebrew)
    [0xFE70, 0xFEFF, INTL],   // Arabic presentation forms-B
    [0x1D400, 0x1D7FF, SYM],  // Mathematical alphanumeric symbols
    [0x1F300, 0x1FAFF, SYM],  // Misc pictographs / emoji
  ];

  const out = [];
  for (const [start, end, prefs] of spec) {
    const i = resolve(prefs);
    if (i >= 0) out.push(start, end, i);
  }
  return out;
}
