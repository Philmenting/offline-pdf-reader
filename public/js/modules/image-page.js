export const IMAGE_PAGE_ACCEPT = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"

export function isPageImage(file) {
  return /\.(png|jpe?g|webp)$/i.test(file.name) || /^image\/(png|jpeg|webp)$/.test(file.type)
}

export function fitImage(width, height, pageWidth, pageHeight) {
  if (![width, height, pageWidth, pageHeight].every(v => Number.isFinite(v) && v > 0)) {
    throw new Error("Ungültige Bild- oder Seitengröße.")
  }
  const scale = Math.min(pageWidth / width, pageHeight / height)
  const w = width * scale
  const h = height * scale
  return { x: (pageWidth - w) / 2, y: (pageHeight - h) / 2, width: w, height: h }
}

export async function imagePagePdf(file, PDFLib, pageSize) {
  if (!isPageImage(file)) throw new Error("Bitte PNG, JPEG oder WebP auswählen.")
  if (file.size > 40 * 1024 * 1024) throw new Error("Das Bild darf höchstens 40 MB groß sein.")
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" })
  try {
    if (bitmap.width * bitmap.height > 40000000) throw new Error("Das Bild darf höchstens 40 Megapixel haben.")
    const canvas = document.createElement("canvas")
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const ctx = canvas.getContext("2d")
    ctx.fillStyle = "#ffffff"
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(bitmap, 0, 0)
    const png = await new Promise((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error("Bild konnte nicht verarbeitet werden.")), "image/png"))
    const pdf = await PDFLib.PDFDocument.create()
    const image = await pdf.embedPng(await png.arrayBuffer())
    const page = pdf.addPage(pageSize)
    page.drawImage(image, fitImage(image.width, image.height, ...pageSize))
    return pdf.save()
  } finally {
    bitmap.close()
  }
}
