// Preserve known text, using the SDK's own line layout and transforms. Return
// null for picture-bearing/rotated content rather than guessing its geometry.
export function editableTextLines(doc, pageIndex, widthPx) {
  const drawings = doc.GetPageInfo(pageIndex).drawings || [];
  const scale = widthPx / doc.GetPageWidthMM(pageIndex);
  const words = [];
  for (const drawing of drawings) {
    if (drawing.IsEditFieldShape?.()) continue;
    if (!drawing.IsShape?.()) return null;
    const content = drawing.GetDocContent?.();
    if (!content) continue;
    const transform = drawing.transformText;
    if (!transform || Math.abs(transform.shx || 0) > .0001 || Math.abs(transform.shy || 0) > .0001) return null;
    const paragraphs = content.GetAllParagraphs({ All: true });
    for (const paragraph of paragraphs) {
      if (!paragraph.IsRecalculated() || paragraph.Pages.length !== 1) return null;
      for (let i = 0; i < paragraph.Lines.length; i++) {
        const line = paragraph.Lines[i];
        if (line.Ranges.length !== 1) return null;
        const range = line.Ranges[0];
        const text = paragraph.GetTextOnLine(i).replace(/[\r\n]+$/g, "");
        if (!text.trim()) continue;
        const x0 = range.XVisible;
        const x1 = range.XEndVisible;
        const y = paragraph.Pages[0].Y + line.Y;
        const y0 = y - line.Metrics.Ascent;
        const y1 = y + line.Metrics.Descent;
        const bbox = {
          x0: transform.TransformPointX(x0, y0) * scale,
          x1: transform.TransformPointX(x1, y1) * scale,
          y0: transform.TransformPointY(x0, y0) * scale,
          y1: transform.TransformPointY(x1, y1) * scale,
        };
        if (!Object.values(bbox).every(Number.isFinite) || bbox.x1 <= bbox.x0 || bbox.y1 <= bbox.y0) return null;
        words.push({ text, bbox, _ocrLine: words.length });
      }
    }
  }
  return words.length ? words : null;
}
