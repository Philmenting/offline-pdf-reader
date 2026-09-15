import { test } from "node:test"
import assert from "node:assert/strict"
import { fitImage, isPageImage } from "../public/js/modules/image-page.js"

test("landscape image fits portrait page without cropping or distortion", () => {
  const box = fitImage(1200, 600, 600, 800)
  assert.deepEqual(box, { x: 0, y: 250, width: 600, height: 300 })
})
test("portrait image fits landscape page", () => {
  assert.deepEqual(fitImage(600, 1200, 800, 600), { x: 250, y: 0, width: 300, height: 600 })
})
test("invalid dimensions are rejected", () => {
  for (const value of [0, -1, NaN, Infinity]) assert.throws(() => fitImage(value, 100, 600, 800))
})
test("supported image types work with missing MIME metadata", () => {
  assert.equal(isPageImage({ name: "scan.JPG", type: "" }), true)
  assert.equal(isPageImage({ name: "scan", type: "image/webp" }), true)
  assert.equal(isPageImage({ name: "scan.svg", type: "image/svg+xml" }), false)
})
