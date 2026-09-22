import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { replacementFont, embedMissingTrueTypeFonts } from '../public/js/modules/pdf-fonts.js';
import { editableTextLines } from '../public/js/modules/editable-text.js';
const PDFLib = createRequire(import.meta.url)('../public/js/vendor/pdf-lib.min.js');
const { PDFDocument, PDFName } = PDFLib;
const key = PDFName.of;

test('Windows PostScript names preserve family and bold/italic variants', () => {
  for (const [name, expected] of [
    ['ArialMT', 'LiberationSans-Regular'], ['Arial-BoldMT', 'LiberationSans-Bold'],
    ['Arial-BoldItalicMT', 'LiberationSans-BoldItalic'], ['Arial-ItalicMT', 'LiberationSans-Italic'],
    ['TimesNewRomanPS-BoldMT', 'LiberationSerif-Bold'], ['CourierNewPSMT', 'LiberationMono-Regular'],
  ]) assert.equal(replacementFont(name), expected);
  for (const name of ['ABCDEF+ArialMT', 'ArialUnicodeMS', 'Symbol', 'CustomFont']) assert.equal(replacementFont(name), null);
});

test('embed only known unembedded WinAnsi TrueType fonts; reuse streams; round-trip is idempotent', async () => {
  const pdf = await PDFDocument.create();
  pdf.addPage();
  const descriptor = pdf.context.obj({ Type: 'FontDescriptor', FontName: 'ArialMT', Flags: 32 });
  const descriptorRef = pdf.context.register(descriptor);
  const add = (extra = {}) => {
    const font = pdf.context.obj({ Type: 'Font', Subtype: 'TrueType', BaseFont: 'ArialMT', Encoding: 'WinAnsiEncoding', FontDescriptor: descriptorRef, ...extra });
    pdf.context.register(font); return font;
  };
  const regular = add(), shared = add(), bold = add({ BaseFont: 'Arial-BoldMT' });
  const embeddedDescriptor = pdf.context.obj({ FontFile2: pdf.context.register(pdf.context.stream(new Uint8Array([1]))) });
  const untouched = [add({ BaseFont: 'ABCDEF+ArialMT' }), add({ Subtype: 'Type0' }), add({ Subtype: 'Type1' }), add({ Encoding: 'MacRomanEncoding' }), add({ FontDescriptor: pdf.context.register(embeddedDescriptor) }), add({ BaseFont: 'CustomFont' })];
  const before = untouched.map(f => f.toString());
  const loads = [];
  const load = async name => {
    loads.push(name);
    const bytes = new Uint8Array(40);
    bytes.set([0, 1, 0, 0]);
    const { odttfToggle } = await import('../scripts/fonts-lib.mjs');
    return odttfToggle(bytes).buffer;
  };
  assert.equal(await embedMissingTrueTypeFonts(pdf, PDFLib, load), 3);
  assert.deepEqual(loads, ['LiberationSans-Regular.ttf', 'LiberationSans-Bold.ttf']);
  assert.deepEqual(untouched.map(f => f.toString()), before);
  assert.equal(descriptor.has(key('FontFile2')), false, 'shared original descriptor was not mutated');
  const fontStream = f => pdf.context.lookup(f.get(key('FontDescriptor'))).get(key('FontFile2'));
  assert.equal(fontStream(regular), fontStream(shared));
  assert.notEqual(fontStream(regular), fontStream(bold));
  const reopened = await PDFDocument.load(await pdf.save());
  assert.equal(await embedMissingTrueTypeFonts(reopened, PDFLib, load), 0);
});

test('pictures do not send exact editable Unicode text through OCR', () => {
  const paragraph = { IsRecalculated: () => true, Pages: [{Y:0}], Lines: [{Ranges:[{XVisible:0,XEndVisible:50}],Y:10,Metrics:{Ascent:8,Descent:2}}], GetTextOnLine: () => 'Gebühren: 4,89 €/m³, 0,37 €/m²\r\n' };
  const shape = { IsShape:()=>true, GetDocContent:()=>({GetAllParagraphs:()=>[paragraph]}), transformText:{TransformPointX:x=>x,TransformPointY:(_,y)=>y} };
  const doc = {GetPageWidthMM:()=>100,GetPageInfo:()=>({drawings:[{IsImage:()=>true},shape]})};
  assert.equal(editableTextLines(doc,0,100)[0].text, 'Gebühren: 4,89 €/m³, 0,37 €/m²');
  shape.transformText.shx=1;
  assert.equal(editableTextLines(doc,0,100),null,'rotated text still needs another extraction path');
});
