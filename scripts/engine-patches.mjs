export function guardTextShaper(source) {
  const src = source.replaceAll("\r\n", "\n");
  const needle = "\t\tlet oFontInfo = this.GetFontInfo(this.FontSlot);\n"
    + "\t\tlet nFontId   = AscCommon.FontNameMap.GetId(this.FontId.m_pFaceInfo.family_name);";
  if (src.split(needle).length !== 2) throw new Error("patchTextShaper: expected exactly one FlushWord signature");
  return src.replace(needle, "\t\tif (!this.FontId || !this.FontId.m_pFaceInfo)\n"
    + "\t\t\treturn this.ClearBuffer(); // font not loaded, reshape on repaint\n" + needle);
}

export function keepPageContents(src) {
  const needle = "let bClearPage = !!oFile.pages[curIndex].isRecognized;";
  const count = src.split(needle).length - 1;
  if (count !== 2) throw new Error(`patchSaveNoPageClear: expected 2 occurrences, found ${count}`);
  return src.replaceAll(needle, "let bClearPage = false; // standalone writer cannot serialize drawings");
}
