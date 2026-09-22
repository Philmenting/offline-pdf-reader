// Regression for non-embedded Word/Arial fonts, real editing and Save.
// Optional local fixture: node test/pdf-fonts.e2e.mjs /path/to/input.pdf 15
// Never commit user documents. Requires build-engine, generate-fonts, Playwright.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
const PDFLib = createRequire(import.meta.url)('../public/js/vendor/pdf-lib.min.js');
const { PDFDocument, PDFName, StandardFonts } = PDFLib;
const root = new URL('../', import.meta.url);
const port = process.env.SMOKE_PORT || '3997';
const server = spawn(process.execPath, ['server.mjs'], { cwd: root, env: {...process.env, PORT:port} });
let browser;
try {
  let chromium;
  try { ({chromium} = await import('playwright')); }
  catch { ({chromium} = await import('playwright-core')); }
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args:['--disable-gpu'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  let bytes;
  const index = Number(process.argv[3] || 0);
  if (process.argv[2]) bytes = await readFile(process.argv[2]);
  else {
    const pdf = await PDFDocument.create();
    const p = pdf.addPage([595,842]);
    for (const [standard, name, y, text] of [
      [StandardFonts.Helvetica,'ArialMT',700,'Gebühren für Über- und Unterdeckungen: 3,16 €/m³'],
      [StandardFonts.HelveticaBold,'Arial-BoldMT',740,'Gebührenobergrenzen'],
      [StandardFonts.HelveticaBoldOblique,'Arial-BoldItalicMT',660,'Änderung: 0,42 €/m²'],
    ]) {
      const font = await pdf.embedFont(standard);
      p.drawText(text,{font,size:12,x:50,y});
      await pdf.flush();
      const dict = pdf.context.lookup(font.ref);
      dict.set(PDFName.of('Subtype'),PDFName.of('TrueType'));
      dict.set(PDFName.of('BaseFont'),PDFName.of(name));
      dict.set(PDFName.of('FirstChar'),pdf.context.obj(32));
      dict.set(PDFName.of('LastChar'),pdf.context.obj(255));
      const decoder = new TextDecoder('windows-1252');
      dict.set(PDFName.of('Widths'),pdf.context.obj(Array.from({length:224},(_,i)=>{
        try { return font.widthOfTextAtSize(decoder.decode(new Uint8Array([i+32])),1000); }
        catch { return 0; }
      })));
      dict.set(PDFName.of('FontDescriptor'),pdf.context.register(pdf.context.obj({Type:'FontDescriptor',FontName:name,Flags:32,FontBBox:[-665,-210,2000,1000],Ascent:905,Descent:-210,CapHeight:728,ItalicAngle:name.includes('Italic')?-12:0,StemV:80})));
    }
    const image = await pdf.embedPng('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=');
    p.drawImage(image,{x:50,y:30,width:20,height:20});
    bytes = Buffer.from(await pdf.save());
  }
  await page.goto(`http://127.0.0.1:${port}`);
  await page.waitForFunction(()=>window.__pdfEditorReady, null, {timeout:90000});
  await page.locator('#file-input').setInputFiles({name:'font-regression.pdf',mimeType:'application/pdf',buffer:bytes});
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('bereit zum Bearbeiten'),null,{timeout:90000});
  const scanned = await page.evaluate(i=>window.__pdfEditor.getDocumentRenderer().file.nativeFile.scanPage(i,1).join(''),index);
  assert.match(scanned,/b="1"/); assert.match(scanned,/i="1"/);
  assert.match(scanned,/Gebührenobergrenzen/); assert.match(scanned,/LiberationSans-Bold/);
  assert.doesNotMatch(scanned, /fontsubstitution value="1"/);
  await page.evaluate(i=>window.__pdfEditor.goToPage(i),index);
  await page.click('[data-tool="edit-text"]');
  await page.waitForFunction(i=>window.__pdfEditor.getDocumentRenderer().file.pages[i].isRecognized,index);
  await page.waitForFunction(()=>!window.AscCommon.g_font_loader.isWorking());
  await page.evaluate(i=>{
    const e=window.__pdfEditor,d=e.getPDFDoc();let edits=0;
    d.DoAction(()=>{
      for(const shape of d.GetPageInfo(i).drawings) for(const para of shape.GetDocContent?.()?.GetAllParagraphs({All:true})||[]) {
        para.CheckRunContent(run=>{
          const text=run.Content.map(c=>String.fromCodePoint(c.GetCodePoint?.()||32)).join('');
          for(const [before,after] of [['3,16','4,89'],['0,42','0,37']]) {
            const pos=text.indexOf(before);if(pos<0)continue;
            run.RemoveFromContent(pos,before.length,true);run.AddText(after,pos);edits++;
          }
        });
        shape.SetNeedRecalc(true);
      }
    },window.AscDFH.historydescription_Document_AddLetter,d);
    d.Recalculate();e.sendEvent('asc_onCanUndo',true);
    if(edits!==2)throw Error(`Expected 2 replacements, got ${edits}`);
  },index);
  await page.waitForFunction(async i=>{
    const {editableTextLines}=await import('/js/modules/editable-text.js');
    return !!editableTextLines(window.__pdfEditor.getPDFDoc(),i,1200);
  },index,{timeout:30000});
  const lines=await page.evaluate(async i=>{
    const e=window.__pdfEditor;
    const d=e.getPDFDoc();
    e.getDocumentRenderer().GetPrintPage(i,1200,1698,window.AscPDF.PRINT_CONTENT_TYPES.doc);
    const {editableTextLines}=await import('/js/modules/editable-text.js');
    return editableTextLines(window.__pdfEditor.getPDFDoc(),i,1200)?.map(l=>l.text).join('\n');
  },index);
  for(const text of ['Gebühren','für','Über-','4,89 €/m³','0,37 €/m²']) assert.ok(lines?.includes(text),text);
  const pending=page.waitForEvent('download',{timeout:60000});
  await page.click('#btn-save');
  const download=await pending;
  const chunks=[];for await(const chunk of await download.createReadStream())chunks.push(chunk);
  const saved=Buffer.concat(chunks);
  const reopened=await PDFDocument.load(saved);
  assert.equal(reopened.getPageCount(),(await PDFDocument.load(bytes)).getPageCount());
  assert.ok(reopened.getPage(index).node.Resources().get(PDFName.of('XObject')));
  const contents=reopened.getPage(index).node.Contents();
  const streams=contents.asArray ? contents.asArray().map(ref=>reopened.context.lookup(ref)) : [contents];
  const commands=streams.map(stream=>new TextDecoder().decode(PDFLib.decodePDFRawStream(stream).decode())).join('\n').toUpperCase();
  const textFont=await reopened.embedFont(StandardFonts.Helvetica);
  for(const text of ['Gebühren','4,89 €/m³','0,37 €/m²']) {
    assert.ok(commands.includes(textFont.encodeText(text).toString().slice(1,-1).toUpperCase()),`Saved text: ${text}`);
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: styles, exact Unicode, picture + text, two amount edits, real Save and PDF reopen');
} finally {
  await browser?.close();server.kill();
}
