from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import fitz
import pytesseract
from pytesseract import Output, TesseractError, TesseractNotFoundError
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QTextEdit,
)

try:
    import cv2  # type: ignore[import-not-found]
except Exception:
    cv2 = None

try:
    import numpy as np  # type: ignore[import-not-found]
except Exception:
    np = None


class OcrMixin:
    def _configure_tesseract_runtime(self) -> None:
        if os.name != "nt":
            return

        candidates: list[Path] = []

        # PyInstaller onefile extraction dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "tesseract" / "tesseract.exe")

        # Next to packaged app
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "tesseract" / "tesseract.exe")

        # Dev/build env fallback
        candidates.append(Path("C:/Program Files/Tesseract-OCR/tesseract.exe"))

        for candidate in candidates:
            if candidate.exists():
                pytesseract.pytesseract.tesseract_cmd = str(candidate)
                tessdata = candidate.parent / "tessdata"
                if tessdata.exists():
                    os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))
                return

        system_tesseract = shutil.which("tesseract")
        if system_tesseract:
            pytesseract.pytesseract.tesseract_cmd = system_tesseract

    def _load_learning_rules(self) -> dict:
        if not self.learning_rules_path.exists():
            return {"replacements": {}}
        try:
            data = json.loads(self.learning_rules_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("replacements", {}), dict):
                sanitized: dict[str, str] = {}
                seen_keys: set[str] = set()
                for src, dst in data.get("replacements", {}).items():
                    if isinstance(src, str) and isinstance(dst, str):
                        clean_src = src.strip()
                        clean_dst = dst.strip()
                        if not clean_src or not clean_dst:
                            continue
                        casefold_key = clean_src.casefold()
                        if casefold_key in seen_keys:
                            continue
                        seen_keys.add(casefold_key)
                        sanitized[clean_src] = clean_dst
                return {"replacements": sanitized}
        except Exception:
            pass
        return {"replacements": {}}

    def _load_app_settings(self) -> dict:
        defaults = {"ocrCorrectionMode": "konservativ", "ocrLang": "deu+eng"}
        if not self.app_settings_path.exists():
            return dict(defaults)
        try:
            data = json.loads(self.app_settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                mode = data.get("ocrCorrectionMode", defaults["ocrCorrectionMode"])
                lang = data.get("ocrLang", defaults["ocrLang"])
                loaded = dict(defaults)
                if isinstance(mode, str) and mode in {"konservativ", "aggressiv"}:
                    loaded["ocrCorrectionMode"] = mode
                if isinstance(lang, str) and self._normalize_ocr_language_code(lang):
                    loaded["ocrLang"] = self._normalize_ocr_language_code(lang)
                return loaded
        except Exception:
            pass
        return dict(defaults)

    def _save_app_settings(self) -> None:
        try:
            payload = json.dumps(
                {
                    "ocrCorrectionMode": self.ocr_correction_mode
                    if self.ocr_correction_mode in {"konservativ", "aggressiv"}
                    else "konservativ",
                    "ocrLang": self._ocr_lang(),
                },
                indent=2,
                ensure_ascii=False,
            )
            tmp_path = self.app_settings_path.with_suffix(self.app_settings_path.suffix + ".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self.app_settings_path)
        except Exception:
            pass

    def _save_learning_rules(self) -> None:
        try:
            replacements = self.learning_rules.get("replacements", {})
            if not isinstance(replacements, dict):
                replacements = {}

            normalized_replacements: dict[str, str] = {}
            seen_keys: set[str] = set()
            for key in sorted(replacements.keys(), key=lambda s: s.casefold() if isinstance(s, str) else str(s)):
                if not isinstance(key, str):
                    continue
                value = replacements.get(key)
                if not isinstance(value, str):
                    continue
                clean_key = key.strip()
                clean_value = value.strip()
                if not clean_key or not clean_value:
                    continue
                casefold_key = clean_key.casefold()
                if casefold_key in seen_keys:
                    continue
                seen_keys.add(casefold_key)
                normalized_replacements[clean_key] = clean_value

            payload = json.dumps({"replacements": normalized_replacements}, indent=2, ensure_ascii=False)
            tmp_path = self.learning_rules_path.with_suffix(self.learning_rules_path.suffix + ".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self.learning_rules_path)
        except Exception:
            pass

    def _set_learning_replacement(self, src: str, dst: str) -> bool:
        clean_src = (src or "").strip()
        clean_dst = (dst or "").strip()
        if not clean_src or not clean_dst or clean_src.casefold() == clean_dst.casefold():
            return False

        replacements = self.learning_rules.setdefault("replacements", {})
        if not isinstance(replacements, dict):
            replacements = {}
            self.learning_rules["replacements"] = replacements

        # Keep only one canonical key per token regardless of case.
        for existing in list(replacements.keys()):
            if isinstance(existing, str) and existing.casefold() == clean_src.casefold() and existing != clean_src:
                replacements.pop(existing, None)

        replacements[clean_src] = clean_dst
        return True

    def _apply_learning_rules(self, text: str) -> str:
        replacements = self.learning_rules.get("replacements", {})
        if not isinstance(replacements, dict) or not replacements:
            return text
        out = text
        # Apply longer keys first (e.g. RE-100 before RE-10) and match case-insensitively.
        valid_sources = [src for src, dst in replacements.items() if isinstance(src, str) and isinstance(dst, str)]
        for src in sorted(valid_sources, key=len, reverse=True):
            dst = replacements[src]
            out = re.sub(rf"\b{re.escape(src)}\b", dst, out, flags=re.IGNORECASE)
        return out

    def cancel_ocr(self) -> None:
        self.ocr_cancel_requested = True
        self.statusBar().showMessage("OCR-Abbruch angefordert …")

    def _set_ocr_running(self, running: bool) -> None:
        self.btn_cancel_ocr.setEnabled(running)
        self.btn_retry_failed_ocr.setEnabled((not running) and bool(self.last_ocr_failed_pages))
        if running:
            self.ocr_cancel_requested = False

    def _clear_ocr_cache(self) -> None:
        self.ocr_cache.clear()
        self.ocr_cache_order.clear()

    def _ocr_cache_key(self, page_index: int, rotation: int, mode: str) -> str:
        path_part = "doc"
        if self.pdf_path and self.pdf_path.exists():
            try:
                st = self.pdf_path.stat()
                path_part = f"{self.pdf_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
            except Exception:
                path_part = str(self.pdf_path)
        return f"{path_part}|rev:{self.doc_revision}|p{page_index}|r{rotation % 360}|lang:{self._ocr_lang()}|mode:{mode}"

    def _ocr_cache_get(self, key: str) -> tuple[str, str | None, list[str], list[str]] | None:
        value = self.ocr_cache.get(key)
        if value is None:
            return None
        if key in self.ocr_cache_order:
            self.ocr_cache_order.remove(key)
        self.ocr_cache_order.append(key)
        return value

    def _ocr_cache_put(self, key: str, value: tuple[str, str | None, list[str], list[str]]) -> None:
        self.ocr_cache[key] = value
        if key in self.ocr_cache_order:
            self.ocr_cache_order.remove(key)
        self.ocr_cache_order.append(key)
        while len(self.ocr_cache_order) > self.ocr_cache_max_entries:
            oldest = self.ocr_cache_order.pop(0)
            self.ocr_cache.pop(oldest, None)

    def _ocr_page_with_retry_cached(
        self,
        page_index: int,
        rotation: int,
        retries: int = 1,
    ) -> tuple[str, str | None, list[str], list[str]]:
        if not self.doc or not (0 <= page_index < len(self.doc)):
            return "", "Ungültige Seite", [], []

        key = self._ocr_cache_key(page_index, rotation, mode="confidence")
        cached = self._ocr_cache_get(key)
        if cached is not None:
            return cached

        page = self.doc[page_index]
        matrix = fitz.Matrix(2.0, 2.0).prerotate(rotation)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        result = self._ocr_with_retry(img, retries=retries)
        self._ocr_cache_put(key, result)
        return result

    def _ocr_with_retry(self, img: Image.Image, retries: int = 1) -> tuple[str, str | None, list[str], list[str]]:
        last_err: str | None = None
        last_tokens: list[str] = []
        last_lines: list[str] = []
        for _ in range(retries + 1):
            text, err, tokens, low_conf_lines = self._ocr_image_with_confidence(img, show_error=False)
            if not err:
                return text, None, tokens, low_conf_lines
            last_err = err
            last_tokens = tokens
            last_lines = low_conf_lines
        return "", last_err, last_tokens, last_lines

    @staticmethod
    def _restore_word_case(source: str, replacement: str) -> str:
        if source.isupper():
            return replacement.upper()
        if len(source) > 1 and source[0].isupper() and source[1:].islower():
            return replacement.capitalize()
        return replacement

    @staticmethod
    def _fix_german_umlaut_confusions(text: str) -> str:
        if "ii" not in (text or "").casefold():
            return text

        consonants = "bcdfghjklmnpqrstvwxyz"
        patterns = (
            rf"(?i)^ii(?=[{consonants}])",
            rf"(?i)(?<=[{consonants}])ii(?=[{consonants}])",
            rf"(?i)(?<=[{consonants}])ii$",
        )

        def replace_word(match: re.Match[str]) -> str:
            word = match.group(0)
            fixed = word.casefold()
            for pattern in patterns:
                fixed = re.sub(pattern, "ü", fixed)
            if fixed == word.casefold():
                return word
            return OcrMixin._restore_word_case(word, fixed)

        # Restrict replacements to alphabetic words so IDs like RE-2026-II remain untouched.
        return re.sub(r"\b[^\W\d_]{3,}\b", replace_word, text, flags=re.UNICODE)

    @staticmethod
    def _compute_otsu_threshold(img: Image.Image) -> int:
        hist = img.histogram()
        total = sum(hist)
        if total <= 0:
            return 127

        sum_total = sum(idx * count for idx, count in enumerate(hist))
        sum_back = 0.0
        weight_back = 0
        max_variance = -1.0
        threshold = 127

        for idx, count in enumerate(hist):
            weight_back += count
            if weight_back == 0:
                continue
            weight_fore = total - weight_back
            if weight_fore == 0:
                break
            sum_back += idx * count
            mean_back = sum_back / weight_back
            mean_fore = (sum_total - sum_back) / weight_fore
            variance = weight_back * weight_fore * (mean_back - mean_fore) ** 2
            if variance > max_variance:
                max_variance = variance
                threshold = idx
        return threshold

    def _prepare_image_for_ocr_pillow(self, gray: Image.Image, variant: str) -> Image.Image:
        denoised = gray.filter(ImageFilter.MedianFilter(size=3))
        if variant == "otsu":
            threshold = self._compute_otsu_threshold(denoised)
            return denoised.point(lambda px: 255 if px > threshold else 0, mode="L")

        # Approximate adaptive thresholding by comparing against a blurred local baseline.
        baseline = denoised.filter(ImageFilter.GaussianBlur(radius=8))
        adaptive_pixels = [
            255 if src > max(0, local - 12) else 0
            for src, local in zip(denoised.getdata(), baseline.getdata())
        ]
        adaptive = Image.new("L", denoised.size)
        adaptive.putdata(adaptive_pixels)
        return adaptive

    def _prepare_image_for_ocr_cv2(self, gray: Image.Image, variant: str) -> Image.Image:
        if cv2 is None or np is None:
            return self._prepare_image_for_ocr_pillow(gray, variant)

        arr = np.array(gray)
        denoised = cv2.fastNlMeansDenoising(arr, None, 12, 7, 21)
        if variant == "otsu":
            _, thresh = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            thresh = cv2.adaptiveThreshold(
                denoised,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                12,
            )
        return Image.fromarray(thresh)

    def _prepare_image_for_ocr(self, img: Image.Image, variant: str = "adaptive") -> Image.Image:
        prepared = ImageOps.autocontrast(img.convert("L"))
        width, height = prepared.size
        max_dim = max(width, height)
        upscale = 1.75 if max_dim < 1400 else 1.25 if max_dim < 2200 else 1.0
        if upscale != 1.0:
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            prepared = prepared.resize((max(1, int(width * upscale)), max(1, int(height * upscale))), resampling)
        if cv2 is not None and np is not None:
            return self._prepare_image_for_ocr_cv2(prepared, variant)
        return self._prepare_image_for_ocr_pillow(prepared, variant)

    def _extract_confidence_signals(self, data: dict) -> tuple[list[str], list[str], float]:
        low_conf_tokens: list[str] = []
        line_scores: dict[tuple[int, int, int], list[float]] = {}
        line_tokens: dict[tuple[int, int, int], list[str]] = {}
        all_scores: list[float] = []

        rows = zip(
            data.get("text", []),
            data.get("conf", []),
            data.get("block_num", []),
            data.get("par_num", []),
            data.get("line_num", []),
        )
        for token, conf, block_num, par_num, line_num in rows:
            tk = (token or "").strip()
            if not tk:
                continue
            try:
                score = float(conf)
            except Exception:
                continue
            if score < 0:
                continue

            all_scores.append(score)
            line_key = (int(block_num), int(par_num), int(line_num))
            line_scores.setdefault(line_key, []).append(score)
            line_tokens.setdefault(line_key, []).append(tk)
            if score < 55 and len(tk) >= 2:
                low_conf_tokens.append(tk)

        low_conf_lines: list[str] = []
        for line_key, scores in line_scores.items():
            avg_score = sum(scores) / len(scores)
            snippet = " ".join(line_tokens.get(line_key, [])).strip()
            if snippet and avg_score < 60:
                low_conf_lines.append(snippet[:120])

        mean_conf = sum(all_scores) / len(all_scores) if all_scores else 0.0
        return low_conf_tokens[:20], low_conf_lines[:6], mean_conf

    def _run_ocr_pass(self, img: Image.Image, lang: str, psm: int, variant: str) -> OCRPassResult:
        prepared = self._prepare_image_for_ocr(img, variant=variant)
        config = f"--oem 1 --psm {psm}"
        raw = pytesseract.image_to_string(prepared, lang=lang, config=config).strip()
        data = pytesseract.image_to_data(prepared, lang=lang, config=config, output_type=Output.DICT)
        low_conf_tokens, low_conf_lines, mean_conf = self._extract_confidence_signals(data)
        return OCRPassResult(
            text=self._postprocess_ocr_text(raw),
            mean_confidence=mean_conf,
            low_conf_tokens=low_conf_tokens,
            low_conf_lines=low_conf_lines,
        )

    @staticmethod
    def _ocr_result_score(result: OCRPassResult) -> tuple[float, int, int]:
        return (
            round(result.mean_confidence - (len(result.low_conf_lines) * 5.0) - (len(result.low_conf_tokens) * 1.2), 3),
            len(result.text),
            -len(result.low_conf_tokens),
        )

    @staticmethod
    def _should_retry_ocr_pass(result: OCRPassResult) -> bool:
        if not result.text.strip():
            return True
        if result.mean_confidence < 72:
            return True
        if len(result.low_conf_lines) >= 2:
            return True
        return len(result.low_conf_tokens) >= 8

    def _run_ocr_passes(self, img: Image.Image, lang: str) -> OCRPassResult:
        primary = self._run_ocr_pass(img, lang=lang, psm=6, variant="adaptive")
        best = primary
        if self._should_retry_ocr_pass(primary):
            fallback = self._run_ocr_pass(img, lang=lang, psm=4, variant="otsu")
            if self._ocr_result_score(fallback) > self._ocr_result_score(best):
                best = fallback

            # Third pass for noisy scans: sparse text mode can recover fragmented glyphs
            # (helps with umlauts / ß confusions on low-quality pages).
            sparse = self._run_ocr_pass(img, lang=lang, psm=11, variant="adaptive")
            if self._ocr_result_score(sparse) > self._ocr_result_score(best):
                best = sparse
        return best

    def _ocr_image_with_confidence(
        self,
        img: Image.Image,
        show_error: bool = True,
    ) -> tuple[str, str | None, list[str], list[str]]:
        lang = self._ocr_lang()
        try:
            result = self._run_ocr_passes(img, lang=lang)
            return result.text, None, result.low_conf_tokens, result.low_conf_lines
        except (FileNotFoundError, TesseractNotFoundError) as e:
            msg = (
                "Tesseract wurde nicht gefunden. Bitte Tesseract installieren und sicherstellen, "
                "dass der Befehl 'tesseract' im PATH verfügbar ist."
            )
            if show_error:
                QMessageBox.warning(self, "OCR-Fehler", f"{msg}\n\nDetails:\n{e}")
            return "", f"{msg} Details: {e}", [], []
        except TesseractError as e:
            err_text = str(e)
            if lang != "deu+eng" and ("Failed loading language" in err_text or "Error opening data file" in err_text):
                try:
                    result = self._run_ocr_passes(img, lang="deu+eng")
                    return result.text, None, result.low_conf_tokens, result.low_conf_lines
                except TesseractError:
                    pass
            if show_error:
                QMessageBox.warning(
                    self,
                    "OCR-Fehler",
                    "OCR konnte nicht ausgeführt werden. Bitte Tesseract/Sprachdaten prüfen."
                    f"\n\nDetails:\n{e}",
                )
            return "", err_text, [], []

    def _build_ocr_feedback(self, text: str, low_conf_tokens: list[str], low_conf_lines: list[str]) -> str:
        info = parse_doc_info(text)
        amount, currency = extract_total_amount_info(text)
        lines = []
        if low_conf_tokens:
            unique_tokens = []
            for t in low_conf_tokens:
                if t not in unique_tokens:
                    unique_tokens.append(t)
            lines.append("Unsichere OCR-Tokens: " + ", ".join(unique_tokens[:8]))
        if low_conf_lines:
            unique_lines = []
            for line in low_conf_lines:
                if line not in unique_lines:
                    unique_lines.append(line)
            lines.append("Unsichere Zeilen: " + " | ".join(unique_lines[:3]))

        number = info.number
        if number:
            variants = candidate_variants(number)
            if variants:
                lines.append("Mögliche Nummer-Varianten: " + ", ".join(variants[:4]))
                choice, ok = QInputDialog.getItem(
                    self,
                    "OCR-Korrektur",
                    "Erkannte Dokument-/Rechnungsnummer prüfen:",
                    [number] + variants[:4],
                    0,
                    False,
                )
                if ok and choice and choice != number and self._set_learning_replacement(number, choice):
                    self._save_learning_rules()
                    lines.append(f"Lernregel gespeichert: {number} → {choice}")

        if amount:
            lines.append(f"Erkannter Gesamtbetrag: {amount} {currency or 'EUR'}")

        return "OCR: " + (" · ".join(lines) if lines else "keine Auffälligkeiten")

    def _apply_styles(self) -> None:
        # ── Design-Tokens ────────────────────────────────────────────────────
        # Primärfarbe: tiefes Indigo
        # Akzent: warmes Blau
        # Hintergrund: sehr helles Grau mit minimalem Blaustich
        # Text: Dunkelgrau (kein hartes Schwarz)
        self.setStyleSheet("""
            /* ── Fenster & Container ──────────────────────────────────── */
            QMainWindow, QWidget {
                background: #f7f8fa;
                color: #1e2432;
                font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                font-size: 13px;
            }

            /* ── Toolbar-Hintergrund ───────────────────────────────────── */
            QWidget[role="toolbar"] {
                background: #ffffff;
                border-bottom: 1px solid #e4e7ef;
            }

            /* ── Namens-Zeile ─────────────────────────────────────────── */
            QWidget[role="namebar"] {
                background: #f0f2f7;
                border-bottom: 1px solid #e4e7ef;
            }

            /* ── Suchleiste ───────────────────────────────────────────── */
            QWidget[role="searchbar"] {
                background: #fff8e7;
                border-bottom: 1px solid #f0d580;
            }

            /* ── OCR-Statusleiste ─────────────────────────────────────── */
            QWidget[role="ocrbar"] {
                background: #f0f2f7;
                border-top: 1px solid #e4e7ef;
            }

            /* ── Trennlinie in der Toolbar ────────────────────────────── */
            QFrame[role="toolsep"] {
                color: #dde0ea;
                max-height: 22px;
                margin: 6px 2px;
            }

            /* ── Standard-Button ──────────────────────────────────────── */
            QPushButton {
                background: #f0f2f7;
                color: #1e2432;
                border: 1px solid #dde0ea;
                border-radius: 7px;
                padding: 5px 11px;
                font-size: 13px;
            }
            QPushButton:hover {
                background: #e6e9f4;
                border-color: #b8bfd4;
            }
            QPushButton:pressed {
                background: #d8dcee;
                border-color: #9ba5c4;
            }
            QPushButton:disabled {
                background: #f5f6f9;
                color: #aab0c4;
                border-color: #e8eaf0;
            }

            /* ── Icon-Button (kompakt, quadratisch) ───────────────────── */
            QPushButton[btnRole="icon"] {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 7px;
                padding: 5px 8px;
                font-size: 14px;
                min-width: 28px;
                max-width: 36px;
            }
            QPushButton[btnRole="icon"]:hover {
                background: #e8ebf5;
                border-color: #cdd2e8;
            }
            QPushButton[btnRole="icon"]:pressed {
                background: #d4d9ee;
                border-color: #9ba5c4;
            }
            QPushButton[btnRole="icon"]:disabled {
                color: #b8bfd4;
            }

            /* ── Primär-Button (Akzent) ───────────────────────────────── */
            QPushButton[btnRole="primary"] {
                background: #3d5afe;
                color: #ffffff;
                border: 1px solid #2a45e8;
                border-radius: 7px;
                padding: 5px 14px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton[btnRole="primary"]:hover {
                background: #5472ff;
                border-color: #3d5afe;
            }
            QPushButton[btnRole="primary"]:pressed {
                background: #2a45e8;
                border-color: #1a35d8;
            }
            QPushButton[btnRole="primary"]:disabled {
                background: #9daaf5;
                border-color: #8899e8;
                color: #dce2ff;
            }

            /* ── Aktions-Button (subtil, aber klarer als Standard) ────── */
            QPushButton[btnRole="action"] {
                background: #ffffff;
                color: #2d3a5e;
                border: 1px solid #cdd2e8;
                border-radius: 7px;
                padding: 5px 11px;
                font-size: 13px;
            }
            QPushButton[btnRole="action"]:hover {
                background: #eef0fb;
                border-color: #9ba5c4;
            }
            QPushButton[btnRole="action"]:pressed {
                background: #dce0f5;
            }
            QPushButton[btnRole="action"]:disabled {
                color: #aab0c4;
                border-color: #e4e7ef;
            }

            /* ── Eingabefelder ────────────────────────────────────────── */
            QLineEdit {
                background: #ffffff;
                color: #1e2432;
                border: 1px solid #cdd2e8;
                border-radius: 7px;
                padding: 5px 10px;
                font-size: 13px;
                selection-background-color: #c2ccff;
            }
            QLineEdit:focus {
                border-color: #3d5afe;
                background: #ffffff;
            }
            QLineEdit:hover {
                border-color: #9ba5c4;
            }

            QTextEdit {
                background: #ffffff;
                color: #1e2432;
                border: 1px solid #cdd2e8;
                border-radius: 7px;
                padding: 6px;
                font-size: 13px;
                selection-background-color: #c2ccff;
            }
            QTextEdit:focus {
                border-color: #3d5afe;
            }

            /* ── Labels ───────────────────────────────────────────────── */
            QLabel {
                color: #1e2432;
                background: transparent;
            }
            QLabel[role="fieldlabel"] {
                color: #6b748a;
                font-size: 11px;
                font-weight: 500;
                letter-spacing: 0.5px;
            }
            QLabel[role="pageinfo"] {
                color: #6b748a;
                font-size: 12px;
                padding: 0 8px;
            }
            QLabel[role="counter"] {
                color: #6b748a;
                font-size: 12px;
                min-width: 48px;
                text-align: center;
            }

            /* ── Listen (Suchtreffer, Thumbnails) ─────────────────────── */
            QListWidget {
                background: #ffffff;
                border: none;
                border-right: 1px solid #e4e7ef;
                outline: none;
            }
            QListWidget::item {
                padding: 5px 10px;
                border-bottom: 1px solid #f0f2f7;
                color: #1e2432;
            }
            QListWidget::item:selected {
                background: #e8ecff;
                color: #1e2432;
            }
            QListWidget::item:hover {
                background: #f0f2fb;
            }

            /* ── Vorschau-Scroll ───────────────────────────────────────── */
            QScrollArea {
                background: #ebedf5;
                border: none;
            }
            QScrollBar:vertical {
                background: #f0f2f7;
                width: 8px;
                border-radius: 4px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #c0c6db;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover { background: #9ba5c4; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: #f0f2f7;
                height: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #c0c6db;
                border-radius: 4px;
                min-width: 30px;
            }
            QScrollBar::handle:horizontal:hover { background: #9ba5c4; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

            /* ── Splitter ─────────────────────────────────────────────── */
            QSplitter::handle {
                background: #e4e7ef;
                width: 1px;
            }
            QSplitter::handle:hover {
                background: #9ba5c4;
            }

            /* ── Menüleiste ───────────────────────────────────────────── */
            QMenuBar {
                background: #ffffff;
                color: #1e2432;
                border-bottom: 1px solid #e4e7ef;
                padding: 2px 4px;
            }
            QMenuBar::item {
                padding: 4px 10px;
                border-radius: 5px;
            }
            QMenuBar::item:selected {
                background: #eef0fb;
            }
            QMenu {
                background: #ffffff;
                border: 1px solid #dde0ea;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 24px 6px 12px;
                border-radius: 5px;
                color: #1e2432;
            }
            QMenu::item:selected {
                background: #eef0fb;
                color: #3d5afe;
            }
            QMenu::separator {
                height: 1px;
                background: #e4e7ef;
                margin: 4px 8px;
            }

            /* ── Statusleiste ─────────────────────────────────────────── */
            QStatusBar {
                background: #f0f2f7;
                color: #6b748a;
                border-top: 1px solid #e4e7ef;
                font-size: 12px;
                padding: 2px 8px;
            }

            /* ── Fortschrittsbalken ───────────────────────────────────── */
            QProgressDialog {
                background: #ffffff;
                border: 1px solid #dde0ea;
                border-radius: 10px;
            }
            QProgressBar {
                background: #e8eaf0;
                border: none;
                border-radius: 4px;
                height: 6px;
                text-align: center;
            }
            QProgressBar::chunk {
                background: #3d5afe;
                border-radius: 4px;
            }

            /* ── Tabellen ─────────────────────────────────────────────── */
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f7f8fa;
                border: 1px solid #dde0ea;
                border-radius: 8px;
                gridline-color: #ebedf5;
                selection-background-color: #e8ecff;
                selection-color: #1e2432;
            }
            QHeaderView::section {
                background: #f0f2f7;
                color: #4a5272;
                border: none;
                border-bottom: 1px solid #dde0ea;
                border-right: 1px solid #ebedf5;
                padding: 6px 10px;
                font-size: 12px;
                font-weight: 500;
            }

            /* ── Dialog-Buttons ───────────────────────────────────────── */
            QDialogButtonBox QPushButton {
                min-width: 80px;
            }
        """)

    @staticmethod
    def _ensure_pdf_suffix(path: str) -> str:
        return path if path.lower().endswith(".pdf") else f"{path}.pdf"

    def _close_open_document(self) -> None:
        if self.doc is None:
            return
        try:
            self.doc.close()
        except Exception:
            pass
        self.doc = None

    def close_pdf(self) -> None:
        self._close_open_document()
        self.pdf_path = None
        self.current_page = 0
        self.page_rotations.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.doc_revision = 0
        self._clear_ocr_cache()
        self.last_ocr_failed_pages = []
        self.last_recognized_page_texts = {}
        self.btn_retry_failed_ocr.setEnabled(False)
        self.preview.setText("Kein PDF geladen")
        self.page_info.setText("Seite: -/- | Zoom: 100%")
        self._set_extracted_text("")
        self.suggested_name.clear()
        self.ocr_feedback.setText("OCR bereit")
        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self._update_search_counter()
        self._refresh_thumbnails()
        self._set_dirty(False)
        self._update_undo_redo_buttons()
        self.statusBar().showMessage("PDF geschlossen.")

    def closeEvent(self, event) -> None:
        if self.doc and self.is_dirty:
            msg = QMessageBox(self)
            msg.setWindowTitle("Ungespeicherte Änderungen")
            msg.setText("Es gibt ungespeicherte Änderungen.")
            msg.setInformativeText("Möchtest du die Änderungen vor dem Beenden speichern?")
            btn_save = msg.addButton("Speichern", QMessageBox.ButtonRole.AcceptRole)
            btn_discard = msg.addButton("Verwerfen", QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel = msg.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(btn_save)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == btn_cancel:
                event.ignore()
                return
            if clicked == btn_save:
                self.save_in_place()
        self._close_open_document()
        super().closeEvent(event)

    def _open_pdf_path(self, file_name: str) -> None:
        self._close_open_document()
        self.pdf_path = Path(file_name)
        try:
            self.doc = fitz.open(file_name)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"PDF konnte nicht geöffnet werden:\n{e}")
            self.doc = None
            return

        if self.doc.needs_pass:
            pw, ok = QInputDialog.getText(
                self, "Passwort erforderlich", "Dieses PDF ist passwortgeschützt:", QLineEdit.EchoMode.Password
            )
            if not ok or not self.doc.authenticate(pw):
                QMessageBox.critical(self, "Fehler", "Falsches Passwort oder Abgebrochen. PDF wird nicht geöffnet.")
                self.doc.close()
                self.doc = None
                self.pdf_path = None
                return

        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.doc_revision = 0
        self._clear_ocr_cache()
        self.last_ocr_failed_pages = []
        self.last_recognized_page_texts = {}
        self.btn_retry_failed_ocr.setEnabled(False)
        self._refresh_thumbnails()
        self.render_current_page()
        self._set_extracted_text("")
        self.suggested_name.clear()
        self.ocr_feedback.setText("OCR bereit")
        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self._update_search_counter()
        self._set_dirty(False)
        self._update_undo_redo_buttons()
        self.statusBar().showMessage(f"Geladen: {self.pdf_path.name} ({len(self.doc)} Seiten)")

    def open_pdf(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "PDF auswählen", "", "PDF-Dateien (*.pdf)")
        if not file_name:
            return
        self._open_pdf_path(file_name)

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if not mime.hasUrls():
            event.ignore()
            return
        for url in mime.urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if not mime.hasUrls():
            event.ignore()
            return
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            file_name = url.toLocalFile()
            if file_name.lower().endswith(".pdf"):
                self._open_pdf_path(file_name)
                event.acceptProposedAction()
                return
        event.ignore()

    def _map_search_rect_to_view(
        self,
        rect: fitz.Rect,
        page_width: float,
        page_height: float,
        rotation: int,
        scale: float,
        view_width: int,
        view_height: int,
    ) -> tuple[int, int, int, int] | None:
        x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
        rot = rotation % 360

        def _as_int_box(a0: float, b0: float, a1: float, b1: float) -> tuple[int, int, int, int] | None:
            lx, rx = sorted((a0, a1))
            ty, by = sorted((b0, b1))
            ix = max(0, int(round(lx * scale)))
            iy = max(0, int(round(ty * scale)))
            iw = int(round((rx - lx) * scale))
            ih = int(round((by - ty) * scale))
            if iw <= 0 or ih <= 0:
                return None
            if ix >= view_width or iy >= view_height:
                return None
            return ix, iy, iw, ih

        # Primary mapping: clockwise view transform.
        if rot == 0:
            mapped = _as_int_box(x0, y0, x1, y1)
        elif rot == 90:
            mapped = _as_int_box(page_height - y1, x0, page_height - y0, x1)
        elif rot == 180:
            mapped = _as_int_box(page_width - x1, page_height - y1, page_width - x0, page_height - y0)
        elif rot == 270:
            mapped = _as_int_box(y0, page_width - x1, y1, page_width - x0)
        else:
            mapped = _as_int_box(x0, y0, x1, y1)

        if mapped is not None:
            return mapped

        # Fallback mapping for environments where +90 is rendered CCW.
        if rot == 90:
            return _as_int_box(y0, page_width - x1, y1, page_width - x0)
        if rot == 270:
            return _as_int_box(page_height - y1, x0, page_height - y0, x1)
        return None

    def render_current_page(self) -> None:
        if not self.doc or len(self.doc) == 0:
            self.preview.clear()
            self.preview.setText("Kein PDF geladen")
            self.preview.adjustSize()
            self.page_info.setText("Seite: -/- | Zoom: 100%")
            return

        total = len(self.doc)
        self.current_page = max(0, min(self.current_page, total - 1))

        page = self.doc[self.current_page]
        rotation = self.page_rotations.get(self.current_page, 0)
        matrix = fitz.Matrix(self.zoom_factor, self.zoom_factor).prerotate(rotation)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        fmt = QImage.Format.Format_RGB888
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
        qpix = QPixmap.fromImage(img)

        if 0 <= self.current_search_hit < len(self.search_hits):
            hit = self.search_hits[self.current_search_hit]
            if hit.get("page") == self.current_page:
                try:
                    page_width = float(page.rect.width)
                    page_height = float(page.rect.height)
                    query = self.search_query.text().strip()

                    # Draw all native matches on the current page as lightweight context,
                    # then emphasize the currently selected hit.
                    context_rects: list[fitz.Rect] = []
                    if query:
                        context_rects = page.search_for(query)[:200]

                    selected_rects: list[fitz.Rect] = []
                    if hit.get("rect"):
                        x0, y0, x1, y1 = hit["rect"]
                        selected_rects = [fitz.Rect(x0, y0, x1, y1)]
                    elif context_rects:
                        selected_rects = [context_rects[0]]

                    if context_rects or selected_rects:
                        painter = QPainter(qpix)
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

                        if context_rects:
                            ctx_pen = QPen(QColor(255, 224, 130, 220))
                            ctx_pen.setWidth(1)
                            painter.setPen(ctx_pen)
                            painter.setBrush(QColor(255, 236, 179, 80))
                            for r in context_rects:
                                box = self._map_search_rect_to_view(
                                    rect=r,
                                    page_width=page_width,
                                    page_height=page_height,
                                    rotation=rotation,
                                    scale=self.zoom_factor,
                                    view_width=qpix.width(),
                                    view_height=qpix.height(),
                                )
                                if box is None:
                                    continue
                                x, y, w, h = box
                                painter.drawRect(x, y, w, h)

                        if selected_rects:
                            sel_pen = QPen(QColor(255, 170, 0))
                            sel_pen.setWidth(3)
                            painter.setPen(sel_pen)
                            painter.setBrush(QColor(255, 196, 0, 60))
                            for r in selected_rects:
                                box = self._map_search_rect_to_view(
                                    rect=r,
                                    page_width=page_width,
                                    page_height=page_height,
                                    rotation=rotation,
                                    scale=self.zoom_factor,
                                    view_width=qpix.width(),
                                    view_height=qpix.height(),
                                )
                                if box is None:
                                    continue
                                x, y, w, h = box
                                painter.drawRect(x, y, w, h)
                        painter.end()
                except Exception:
                    pass

        self.preview.setText("")
        self.preview.setPixmap(qpix)
        self.preview.resize(qpix.size())
        self.page_info.setText(
            f"Seite: {self.current_page + 1}/{total} | Zoom: {int(self.zoom_factor * 100)}% | Drehung: {rotation}°"
        )
        self._sync_thumbnail_selection()
        if self.pdf_path:
            self.statusBar().showMessage(f"{self.pdf_path.name} — Seite {self.current_page + 1}/{total}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.doc:
            self.render_current_page()

    def keyPressEvent(self, event) -> None:
        key = event.key()

        if key == Qt.Key.Key_F3:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prev_search_hit()
            else:
                self.next_search_hit()
            return

        if key == Qt.Key.Key_Escape and (
            self.search_results_list.isVisible()
            or self.search_query.text().strip()
            or self.search_query.hasFocus()
        ):
            self.close_search_panel()
            self.preview.setFocus()
            return

        if key == Qt.Key.Key_F and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.open_search()
            return

        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QTextEdit)):
            super().keyPressEvent(event)
            return

        if key == Qt.Key.Key_Delete and self.thumb_list.hasFocus():
            self.delete_selected_pages()
            return

        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self.next_page()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.prev_page()
            return
        if key == Qt.Key.Key_Home:
            self.first_page()
            return
        if key == Qt.Key.Key_End:
            self.last_page()
            return
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
            return
        if key == Qt.Key.Key_G and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.go_to_page()
            return
        if key == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.save_as_suggested()
            return
        if key == Qt.Key.Key_W and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.close_pdf()
            return
        if key == Qt.Key.Key_Minus:
            self.zoom_out()
            return
        if key == Qt.Key.Key_0:
            self.reset_zoom()
            return
        if key == Qt.Key.Key_R and event.modifiers() & Qt.KeyboardModifier.ControlModifier and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.reset_rotation()
            return
        if key == Qt.Key.Key_R:
            self.rotate_right()
            return
        if key == Qt.Key.Key_L:
            self.rotate_left()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if not self.doc:
            super().wheelEvent(event)
            return

        angle = event.angleDelta().y()
        if angle == 0:
            super().wheelEvent(event)
            return

        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if angle > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
            return

        if angle > 0:
            self.prev_page()
        else:
            self.next_page()
        event.accept()

    def next_page(self) -> None:
        if not self.doc:
            return
        if self.current_page < len(self.doc) - 1:
            self.current_page += 1
            self.render_current_page()

    def prev_page(self) -> None:
        if not self.doc:
            return
        if self.current_page > 0:
            self.current_page -= 1
            self.render_current_page()

    def first_page(self) -> None:
        if not self.doc:
            return
        if self.current_page != 0:
            self.current_page = 0
            self.render_current_page()

    def last_page(self) -> None:
        if not self.doc:
            return
        last_idx = len(self.doc) - 1
        if self.current_page != last_idx:
            self.current_page = last_idx
            self.render_current_page()

    def go_to_page(self) -> None:
        if not self.doc:
            return
        page, ok = QInputDialog.getInt(
            self,
            "Gehe zu Seite",
            f"Seitenzahl (1-{len(self.doc)}):",
            self.current_page + 1,
            1,
            len(self.doc),
            1,
        )
        if not ok:
            return
        self.current_page = page - 1
        self.render_current_page()

    def zoom_in(self) -> None:
        if not self.doc:
            return
        self.zoom_factor = min(3.0, self.zoom_factor + 0.15)
        self.render_current_page()

    def zoom_out(self) -> None:
        if not self.doc:
            return
        self.zoom_factor = max(0.6, self.zoom_factor - 0.15)
        self.render_current_page()

    def reset_zoom(self) -> None:
        if not self.doc:
            return
        self.zoom_factor = 1.0
        self.render_current_page()

    def rotate_left(self) -> None:
        if not self.doc:
            return
        self._push_undo_state()
        current = self.page_rotations.get(self.current_page, 0)
        self.page_rotations[self.current_page] = (current - 90) % 360
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()

    def rotate_right(self) -> None:
        if not self.doc:
            return
        self._push_undo_state()
        current = self.page_rotations.get(self.current_page, 0)
        self.page_rotations[self.current_page] = (current + 90) % 360
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()

    def reset_rotation(self) -> None:
        if not self.doc:
            return
        current = self.page_rotations.get(self.current_page, 0)
        if current == 0:
            return
        self._push_undo_state()
        self.page_rotations[self.current_page] = 0
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()

    def extract_text_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page = self.doc[self.current_page]
        text = page.get_text("text").strip()
        low_conf_tokens: list[str] = []
        low_conf_lines: list[str] = []

        if len(text) < 40:
            # OCR fallback on current page image (respect UI rotation for better OCR)
            rotation = self.page_rotations.get(self.current_page, 0)
            text, _, low_conf_tokens, low_conf_lines = self._ocr_page_with_retry_cached(self.current_page, rotation, retries=1)

        if not text:
            text = "(Kein Text erkannt)"

        text = self._apply_learning_rules(text)
        self._set_extracted_text(text)
        self.show_extracted_text_window()
        self.suggested_name.setText(self._suggest_name_from_extracted_text_or_first_page(text))
        self.ocr_feedback.setText(self._build_ocr_feedback(text, low_conf_tokens, low_conf_lines))
        self.statusBar().showMessage("Text der aktuellen Seite erkannt.")

    def extract_text_all_pages_and_suggest(self) -> None:
        self.recognize_text_all_pages_and_suggest()

    def ocr_all_pages_and_suggest(self) -> None:
        self.recognize_text_all_pages_and_suggest()

    def ocr_and_suggest_filename(self) -> None:
        self.recognize_text_all_pages_and_suggest()

    @staticmethod
    def _format_page_list_preview(page_numbers: list[int], limit: int = 10) -> str:
        if not page_numbers:
            return "-"
        preview = ", ".join(str(p) for p in page_numbers[:limit])
        if len(page_numbers) > limit:
            preview += ", …"
        return preview

    @staticmethod
    def _normalize_ocr_error_signature(err: str) -> str:
        normalized = (err or "").strip()
        if not normalized:
            return "Unbekannter OCR-Fehler"
        lowered = normalized.casefold()
        if "tesseract wurde nicht gefunden" in lowered or "not found" in lowered:
            return "Tesseract nicht gefunden"
        if "failed loading language" in lowered or "error opening data file" in lowered:
            return "Sprachdaten fehlen"
        if "image" in lowered and "empty" in lowered:
            return "Leere/Beschädigte Bilddaten"
        return normalized.splitlines()[0][:120]

    def _summarize_ocr_errors(self, page_errors: dict[int, str], top: int = 3) -> str:
        if not page_errors:
            return ""

        grouped: dict[str, list[int]] = {}
        for page_no, err in sorted(page_errors.items()):
            key = self._normalize_ocr_error_signature(err)
            grouped.setdefault(key, []).append(page_no)

        lines = [f"Fehlerseiten gesamt: {len(page_errors)}"]
        ranked = sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True)
        for label, pages in ranked[:top]:
            lines.append(
                f"- {label}: {len(pages)} Seite(n) (z. B. {self._format_page_list_preview(pages, limit=6)})"
            )
        if len(ranked) > top:
            lines.append(f"- Weitere Fehlerarten: {len(ranked) - top}")
        return "\n".join(lines)

    @staticmethod
    def _build_ocr_run_summary(total: int, ok: int, failed: int, canceled: bool, processed: int) -> str:
        state = "abgebrochen" if canceled else "abgeschlossen"
        success_rate = (ok / processed * 100.0) if processed > 0 else 0.0
        return (
            f"OCR {state}: {ok}/{processed} Seiten erkannt"
            f" ({success_rate:.1f}%), Fehler: {failed}, Gesamtseiten: {total}."
        )

    @staticmethod
    def _build_ocr_followup_hint(failed: int, canceled: bool) -> str:
        if canceled:
            return "Hinweis: Du kannst den Lauf erneut starten oder nur fehlgeschlagene Seiten erneut versuchen."
        if failed > 0:
            return "Nächster Schritt: 'OCR-Fehler erneut' ausführen, um nur problematische Seiten zu wiederholen."
        return "Nächster Schritt: Dateinamen-Vorschlag prüfen und ggf. direkt speichern/exportieren."

    def recognize_text_all_pages_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        total = len(self.doc)
        if total == 0:
            QMessageBox.information(self, "Hinweis", "Das PDF enthält keine Seiten.")
            return

        progress = QProgressDialog("Erkenne Text auf allen Seiten …", "Abbrechen", 0, total, self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        all_text_parts: list[str] = []
        all_low_conf_tokens: list[str] = []
        all_low_conf_lines: list[str] = []
        ocr_failed_pages: list[int] = []
        page_ocr_errors: dict[int, str] = {}
        page_texts: dict[int, str] = {}

        processed_pages = 0
        canceled = False

        self._set_ocr_running(True)
        try:
            for idx in range(total):
                ok_count = len(all_text_parts)
                fail_count = len(ocr_failed_pages)
                progress.setValue(idx)
                progress.setLabelText(
                    f"Erkennung Seite {idx + 1}/{total} … (OK: {ok_count} | Fehler: {fail_count})"
                )
                QApplication.processEvents()
                if progress.wasCanceled() or self.ocr_cancel_requested:
                    canceled = True
                    break

                rotation = self.page_rotations.get(idx, 0)
                text, ocr_error, low_conf_tokens, low_conf_lines = self._ocr_page_with_retry_cached(idx, rotation, retries=1)
                processed_pages = idx + 1
                all_low_conf_tokens.extend(low_conf_tokens)
                all_low_conf_lines.extend(low_conf_lines)
                if ocr_error:
                    page_no = idx + 1
                    ocr_failed_pages.append(page_no)
                    page_ocr_errors[page_no] = ocr_error
                    continue

                text = text.strip()
                if text:
                    all_text_parts.append(text)
                    page_texts[idx] = text
        finally:
            self._set_ocr_running(False)

        progress.setValue(total)

        if canceled and not all_text_parts:
            QMessageBox.information(self, "Abgebrochen", "Texterkennung wurde abgebrochen (keine verwertbaren Ergebnisse).")
            return

        self.last_ocr_failed_pages = list(ocr_failed_pages)
        self.last_recognized_page_texts = dict(page_texts)
        self.btn_retry_failed_ocr.setEnabled(bool(self.last_ocr_failed_pages))

        combined_text = "\n\n".join(all_text_parts).strip() or "(Kein Text erkannt)"
        combined_text = self._apply_learning_rules(combined_text)
        self._set_extracted_text(combined_text)
        self.show_extracted_text_window()
        self.suggested_name.setText(self._suggest_name_from_extracted_text_or_first_page(combined_text))
        self.ocr_feedback.setText(self._build_ocr_feedback(combined_text, all_low_conf_tokens, all_low_conf_lines))
        run_summary = self._build_ocr_run_summary(
            total=total,
            ok=len(all_text_parts),
            failed=len(ocr_failed_pages),
            canceled=canceled,
            processed=processed_pages if canceled else total,
        )
        followup_hint = self._build_ocr_followup_hint(len(ocr_failed_pages), canceled)
        self.statusBar().showMessage(run_summary)

        if canceled:
            QMessageBox.information(
                self,
                "Abgebrochen (Teilergebnis)",
                "Texterkennung wurde abgebrochen. Das bisherige Teilergebnis wurde übernommen."
                f"\n\n{run_summary}"
                f"\n{followup_hint}",
            )

        if ocr_failed_pages:
            pages = self._format_page_list_preview(ocr_failed_pages)
            summary = self._summarize_ocr_errors(page_ocr_errors)
            QMessageBox.warning(
                self,
                "OCR teilweise fehlgeschlagen",
                "Texterkennung wurde fortgesetzt, aber auf einigen Seiten ist ein Fehler aufgetreten."
                f"\n\nSeiten: {pages}"
                f"\nFehleranzahl: {len(ocr_failed_pages)}"
                f"\n\nZusammenfassung:\n{summary}"
                f"\n\n{followup_hint}",
            )
        elif not canceled:
            QMessageBox.information(
                self,
                "OCR abgeschlossen",
                f"{run_summary}\n{followup_hint}",
            )

    def retry_failed_ocr_pages(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        if not self.last_ocr_failed_pages:
            QMessageBox.information(self, "Hinweis", "Es gibt keine fehlgeschlagenen OCR-Seiten zum Wiederholen.")
            return

        pages_to_retry = sorted({p for p in self.last_ocr_failed_pages if 1 <= p <= len(self.doc)})
        if not pages_to_retry:
            QMessageBox.information(self, "Hinweis", "Keine gültigen Seiten für erneuten OCR-Versuch gefunden.")
            return

        # Avoid stale failed OCR cache entries for retry.
        self._clear_ocr_cache()

        progress = QProgressDialog("Wiederhole OCR für fehlgeschlagene Seiten …", "Abbrechen", 0, len(pages_to_retry), self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        remaining_failed: list[int] = []
        retry_page_errors: dict[int, str] = {}
        processed_retry_pages = 0
        canceled = False

        self._set_ocr_running(True)
        try:
            for i, page_no in enumerate(pages_to_retry):
                progress.setValue(i)
                progress.setLabelText(f"OCR erneut: Seite {page_no} ({i + 1}/{len(pages_to_retry)}) …")
                QApplication.processEvents()
                if progress.wasCanceled() or self.ocr_cancel_requested:
                    remaining_failed.extend(pages_to_retry[i:])
                    canceled = True
                    break

                idx = page_no - 1
                rotation = self.page_rotations.get(idx, 0)
                text, err, _, _ = self._ocr_page_with_retry_cached(idx, rotation, retries=2)
                processed_retry_pages += 1
                if err or not text.strip():
                    remaining_failed.append(page_no)
                    retry_page_errors[page_no] = err or "Kein Text erkannt"
                    continue
                self.last_recognized_page_texts[idx] = text.strip()
        finally:
            self._set_ocr_running(False)
            progress.setValue(len(pages_to_retry))

        self.last_ocr_failed_pages = remaining_failed
        self.btn_retry_failed_ocr.setEnabled(bool(self.last_ocr_failed_pages))

        combined_parts = [self.last_recognized_page_texts[k] for k in sorted(self.last_recognized_page_texts.keys()) if self.last_recognized_page_texts.get(k)]
        if combined_parts:
            combined_text = self._apply_learning_rules("\n\n".join(combined_parts).strip())
            self._set_extracted_text(combined_text)
            self.show_extracted_text_window()
            self.suggested_name.setText(self._suggest_name_from_extracted_text_or_first_page(combined_text))

        retried_total = len(pages_to_retry)
        retried_ok = retried_total - len(remaining_failed)
        retry_summary = self._build_ocr_run_summary(
            total=retried_total,
            ok=retried_ok,
            failed=len(remaining_failed),
            canceled=canceled,
            processed=processed_retry_pages if canceled else retried_total,
        )
        followup_hint = self._build_ocr_followup_hint(len(remaining_failed), canceled)
        self.statusBar().showMessage(retry_summary)

        if canceled:
            preview = self._format_page_list_preview(remaining_failed)
            QMessageBox.information(
                self,
                "OCR erneut abgebrochen",
                "Erneuter OCR-Versuch wurde abgebrochen. Nicht verarbeitete Seiten bleiben als fehlgeschlagen markiert."
                f"\n\nOffene Seiten: {preview}"
                f"\nAnzahl: {len(remaining_failed)}"
                f"\n\n{retry_summary}"
                f"\n{followup_hint}",
            )
        elif remaining_failed:
            preview = self._format_page_list_preview(remaining_failed)
            summary = self._summarize_ocr_errors(retry_page_errors)
            QMessageBox.warning(
                self,
                "OCR erneut teilweise fehlgeschlagen",
                "Einige Seiten konnten weiterhin nicht erkannt werden."
                f"\n\nSeiten: {preview}"
                f"\nAnzahl: {len(remaining_failed)}"
                f"\n\nZusammenfassung:\n{summary}"
                f"\n\n{followup_hint}",
            )
        else:
            QMessageBox.information(self, "Fertig", f"{retry_summary}\n{followup_hint}")

    def choose_ocr_language(self) -> None:
        options = [
            ("Deutsch + Englisch (deu+eng)", "deu+eng"),
            ("Deutsch (deu)", "deu"),
            ("Englisch (eng)", "eng"),
            ("Französisch (fra)", "fra"),
            ("Spanisch (spa)", "spa"),
            ("Italienisch (ita)", "ita"),
        ]
        labels = [label for label, _ in options] + ["Benutzerdefiniert …"]

        current_label = next((label for label, code in options if code == self._ocr_lang()), labels[0])
        choice, ok = QInputDialog.getItem(
            self,
            "OCR-Sprache wählen",
            "Tesseract-Sprachcode:",
            labels,
            max(0, labels.index(current_label)),
            False,
        )
        if not ok:
            return

        selected_lang = ""
        if choice == "Benutzerdefiniert …":
            custom, ok_custom = QInputDialog.getText(
                self,
                "OCR-Sprache (benutzerdefiniert)",
                "Code (z. B. deu+eng, eng, deu):",
                QLineEdit.EchoMode.Normal,
                self._ocr_lang(),
            )
            if not ok_custom:
                return
            normalized = self._normalize_ocr_language_code(custom)
            if not normalized:
                QMessageBox.information(
                    self,
                    "Ungültiger Sprachcode",
                    "Bitte einen gültigen Tesseract-Sprachcode eingeben (z. B. deu, eng oder deu+eng).",
                )
                return
            selected_lang = normalized
        else:
            selected_lang = dict(options)[choice]

        missing_langs = self._missing_ocr_language_codes(selected_lang)
        if missing_langs:
            QMessageBox.warning(
                self,
                "OCR-Sprache nicht installiert",
                "Folgende Tesseract-Sprachdaten fehlen: "
                + ", ".join(missing_langs)
                + "\n\nBitte Sprachpakete installieren und erneut wählen.",
            )
            return

        self.ocr_lang = selected_lang
        self._clear_ocr_cache()
        self._save_app_settings()
        self._update_ocr_mode_label()
        self.statusBar().showMessage(f"OCR-Sprache gesetzt: {self._ocr_lang()}", 4000)

    def _update_ocr_mode_label(self) -> None:
        mode = self.ocr_correction_mode if self.ocr_correction_mode in {"konservativ", "aggressiv"} else "konservativ"
        label_text = f"OCR: {self._ocr_lang()} | Modus: {mode}"
        self.ocr_mode_label.setText(label_text)
        self.ocr_mode_label.setToolTip(label_text)
        self.setWindowTitle(self._compose_window_title())

    def choose_ocr_correction_mode(self) -> None:
        options = ["konservativ", "aggressiv"]
        current = self.ocr_correction_mode if self.ocr_correction_mode in options else "konservativ"
        choice, ok = QInputDialog.getItem(
            self,
            "OCR-Korrekturmodus",
            "Modus:",
            options,
            options.index(current),
            False,
        )
        if not ok:
            return
        self.ocr_correction_mode = choice
        self._save_app_settings()
        self._update_ocr_mode_label()
        self.statusBar().showMessage(f"OCR-Korrekturmodus gesetzt: {choice}", 4000)

    def reset_ocr_preferences(self) -> None:
        self.ocr_lang = "deu+eng"
        self.ocr_correction_mode = "konservativ"
        self._clear_ocr_cache()
        self._save_app_settings()
        self._update_ocr_mode_label()
        self.statusBar().showMessage("OCR-Einstellungen auf Standard zurückgesetzt.", 4000)

    def _normalize_ocr_language_code(self, code: str | None) -> str:
        normalized = (code or "").strip().lower().replace(",", "+")
        normalized = re.sub(r"\s*\+\s*", "+", normalized)
        normalized = re.sub(r"\+{2,}", "+", normalized)
        normalized = normalized.strip("+")
        if not normalized:
            return ""

        if not re.fullmatch(r"[a-z_]+(?:\+[a-z_]+)*", normalized):
            return ""
        return normalized

    def _get_installed_ocr_languages(self) -> set[str] | None:
        if self._installed_ocr_langs_cache is not None:
            return self._installed_ocr_langs_cache
        try:
            langs = pytesseract.get_languages(config="")
        except (TesseractNotFoundError, TesseractError, OSError):
            return None
        self._installed_ocr_langs_cache = {str(lang).strip().lower() for lang in langs if str(lang).strip()}
        return self._installed_ocr_langs_cache

    def _missing_ocr_language_codes(self, code: str | None) -> list[str]:
        normalized = self._normalize_ocr_language_code(code)
        if not normalized:
            return []

        requested = [part for part in normalized.split("+") if part]

        def compute_missing(installed_langs: set[str] | None) -> list[str]:
            if not installed_langs:
                return []
            return [part for part in requested if part not in installed_langs]

        installed = self._get_installed_ocr_languages()
        missing = compute_missing(installed)

        # Refresh once when languages appear missing. This helps when language
        # packs were installed while the app is already running.
        if missing and self._installed_ocr_langs_cache is not None:
            self._installed_ocr_langs_cache = None
            missing = compute_missing(self._get_installed_ocr_languages())

        return missing

    def _ocr_lang(self) -> str:
        normalized = self._normalize_ocr_language_code(self.ocr_lang)
        return normalized or "deu+eng"

    def _postprocess_ocr_text(self, text: str) -> str:
        out = text
        if "deu" in self._ocr_lang():
            # Keep this conservative so invoice numbers and IDs are not rewritten.
            out = re.sub(r"(?<=\w)é(?=\w)", "ö", out)
            out = self._fix_german_umlaut_confusions(out)
            # Common OCR confusion in German words: internal uppercase R -> ß (e.g. StraRe -> Straße).
            out = re.sub(r"(?<=[a-zäöü])R(?=[a-zäöü])", "ß", out)
            # Conservative token-level fixes for very frequent umlaut misses.
            out = re.sub(r"(?i)\bfur\b", lambda m: self._restore_word_case(m.group(0), "für"), out)
            out = re.sub(r"(?i)\buber\b", lambda m: self._restore_word_case(m.group(0), "über"), out)

            if self.ocr_correction_mode == "aggressiv":
                consonants = "bcdfghjklmnpqrstvwxyz"
                # Aggressive heuristic for scanned German text; avoid digits/IDs by restricting to words.
                out = re.sub(
                    rf"(?i)\b[^{consonants}\W\d_]*([{consonants}])o([{consonants}])[^{consonants}\W\d_]*\b",
                    lambda m: m.group(0).replace("o", "ö", 1).replace("O", "Ö", 1),
                    out,
                )
                out = re.sub(
                    rf"(?i)(?<=[{consonants}])i(?=[{consonants}])",
                    "ü",
                    out,
                )
        return out

    def _ocr_image(self, img: Image.Image, show_error: bool = True) -> tuple[str, str | None]:
        text, err, _, _ = self._ocr_image_with_confidence(img, show_error=show_error)
        return text, err

