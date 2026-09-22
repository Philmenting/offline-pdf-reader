// Resolve only known, non-embedded Windows TrueType families to bundled,
// metrically compatible fonts. Embedded/subset and custom encodings stay intact.
const ODTTF_KEY = [0xA0, 0x66, 0xD6, 0x20, 0x14, 0x96, 0x47, 0xFA, 0x95, 0x69, 0xB8, 0x50, 0xB0, 0x41, 0x49, 0x48];

export function replacementFont(name) {
  const match = /^(Arial|TimesNewRoman|CourierNew)(?:PS)?(?:-(BoldItalic|Bold|Italic))?(?:MT)?$/i.exec(name);
  if (!match) return null;
  const family = { arial: 'LiberationSans', timesnewroman: 'LiberationSerif', couriernew: 'LiberationMono' }[match[1].toLowerCase()];
  const style = { bolditalic: 'BoldItalic', bold: 'Bold', italic: 'Italic' }[match[2]?.toLowerCase()] || 'Regular';
  return `${family}-${style}`;
}

export async function embedMissingTrueTypeFonts(pdf, PDFLib, loadFont) {
  const { PDFName, PDFDict } = PDFLib;
  const key = name => PDFName.of(name);
  const streams = new Map();
  let count = 0;
  for (const [, font] of pdf.context.enumerateIndirectObjects()) {
    if (!(font instanceof PDFDict) || font.get(key('Subtype')) !== key('TrueType')
        || font.get(key('Encoding')) !== key('WinAnsiEncoding')) continue;
    const descriptor = pdf.context.lookup(font.get(key('FontDescriptor')));
    if (!(descriptor instanceof PDFDict)
        || ['FontFile', 'FontFile2', 'FontFile3'].some(name => descriptor.has(key(name)))) continue;
    const baseFont = font.get(key('BaseFont'));
    if (!(baseFont instanceof PDFName)) continue;
    const replacement = replacementFont(baseFont.decodeText());
    if (!replacement) continue;
    if (!streams.has(replacement)) {
      const bytes = new Uint8Array(await loadFont(`${replacement}.ttf`));
      // Offline font assets are obfuscated for ONLYOFFICE's font loader.
      for (let i = 0; i < Math.min(32, bytes.length); i++) bytes[i] ^= ODTTF_KEY[i % 16];
      if (bytes.length < 32 || bytes[0] !== 0 || bytes[1] !== 1 || bytes[2] !== 0 || bytes[3] !== 0) {
        throw new Error(`Ungültige Ersatzschrift: ${replacement}`);
      }
      streams.set(replacement, pdf.context.register(pdf.context.flateStream(bytes, { Length1: bytes.length })));
    }
    // A descriptor may be shared: never alter another font's descriptor.
    const embedded = descriptor.clone(pdf.context);
    embedded.set(key('FontFile2'), streams.get(replacement));
    embedded.set(key('FontName'), key(replacement));
    font.set(key('FontDescriptor'), pdf.context.register(embedded));
    font.set(key('BaseFont'), key(replacement));
    count++;
  }
  return count;
}
