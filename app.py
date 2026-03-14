import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from pytesseract import TesseractError
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
)


@dataclass
class ParsedDocInfo:
    date: str = ""
    vendor: str = ""
    doc_type: str = "Dokument"
    number: str = ""


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name)
    return name[:140] or "Dokument"


def parse_doc_info(text: str) -> ParsedDocInfo:
    lower = text.lower()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Doc type
    doc_type = "Dokument"
    if "rechnung" in lower or "invoice" in lower:
        doc_type = "Rechnung"
    elif "angebot" in lower or "quote" in lower:
        doc_type = "Angebot"
    elif "vertrag" in lower or "contract" in lower:
        doc_type = "Vertrag"
    elif "lieferschein" in lower:
        doc_type = "Lieferschein"

    # Date
    date_patterns = [
        r"\b(\d{2}\.\d{2}\.\d{4})\b",
        r"\b(\d{2}\.\d{2}\.\d{2})\b",
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b(\d{2}-\d{2}-\d{4})\b",
        r"\b(\d{2}-\d{2}-\d{2})\b",
        r"\b(\d{2}/\d{2}/\d{4})\b",
        r"\b(\d{2}/\d{2}/\d{2})\b",
    ]
    date = ""
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1)
            for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%d/%m/%Y", "%d/%m/%y"): 
                try:
                    date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass
            if date:
                break

    # Number (supports common separators like / and _, trims trailing punctuation)
    number = ""
    number_patterns = [
        r"(?i)(?:rechnungs(?:nr|nummer)\.?|invoice\s*(?:no|number)\.?|belegnr\.?|vorgangs(?:nr|nummer)\.?|nr\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
        r"(?i)\b(?:inv|doc)\s*[-_]?\s*([A-Z0-9][A-Z0-9/_-]{2,})\b",
    ]
    for pat in number_patterns:
        m_num = re.search(pat, text)
        if m_num:
            number = m_num.group(1).strip(".,;:)")
            break

    # Vendor heuristic: first non-empty line that isn't too numeric
    vendor = ""
    for ln in lines[:12]:
        if len(ln) < 3:
            continue
        if re.search(r"\d{2,}", ln) and len(ln) < 8:
            continue
        if any(k in ln.lower() for k in ["rechnung", "invoice", "seite", "page"]):
            continue
        vendor = ln
        break

    return ParsedDocInfo(date=date, vendor=vendor, doc_type=doc_type, number=number)


def suggest_filename_from_text(text: str) -> str:
    info = parse_doc_info(text)
    parts = []
    if info.date:
        parts.append(info.date)
    parts.append(info.doc_type)
    if info.vendor:
        parts.append(info.vendor)
    if info.number:
        parts.append(info.number)
    return sanitize_filename("_".join(parts)) + ".pdf"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Offline PDF Reader")
        self.resize(1100, 800)

        self.pdf_path: Path | None = None
        self.doc: fitz.Document | None = None
        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations: dict[int, int] = {}

        self.preview = QLabel("Kein PDF geladen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(420)

        self.text_output = QTextEdit()
        self.text_output.setReadOnly(True)
        self.text_output.setPlaceholderText("Extrahierter Text erscheint hier …")

        self.suggested_name = QLineEdit()
        self.suggested_name.setPlaceholderText("Vorgeschlagener Dateiname")

        self.ocr_lang_input = QLineEdit("deu+eng")
        self.ocr_lang_input.setPlaceholderText("z.B. deu+eng")

        self.page_info = QLabel("Seite: -/- | Zoom: 100%")
        self.page_info.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_open = QPushButton("PDF öffnen")
        btn_prev = QPushButton("◀ Vorherige")
        btn_next = QPushButton("Nächste ▶")
        btn_zoom_out = QPushButton("− Zoom")
        btn_zoom_in = QPushButton("+ Zoom")
        btn_zoom_reset = QPushButton("100%")
        btn_rotate_left = QPushButton("↺ Drehen")
        btn_rotate_right = QPushButton("↻ Drehen")
        btn_extract = QPushButton("Text/OCR extrahieren")
        btn_extract_all = QPushButton("Alle Seiten extrahieren")
        btn_saveas = QPushButton("Speichern als …")
        btn_merge = QPushButton("PDFs mergen")
        btn_split = QPushButton("Seiten extrahieren")
        btn_reorder = QPushButton("Seiten neu anordnen")

        btn_open.clicked.connect(self.open_pdf)
        btn_prev.clicked.connect(self.prev_page)
        btn_next.clicked.connect(self.next_page)
        btn_zoom_out.clicked.connect(self.zoom_out)
        btn_zoom_in.clicked.connect(self.zoom_in)
        btn_zoom_reset.clicked.connect(self.reset_zoom)
        btn_rotate_left.clicked.connect(self.rotate_left)
        btn_rotate_right.clicked.connect(self.rotate_right)
        btn_extract.clicked.connect(self.extract_text_and_suggest)
        btn_extract_all.clicked.connect(self.extract_text_all_pages_and_suggest)
        btn_saveas.clicked.connect(self.save_as_suggested)
        btn_merge.clicked.connect(self.merge_pdfs)
        btn_split.clicked.connect(self.extract_pages_to_new_pdf)
        btn_reorder.clicked.connect(self.reorder_pages_to_new_pdf)

        row = QHBoxLayout()
        row.addWidget(btn_open)
        row.addWidget(btn_prev)
        row.addWidget(btn_next)
        row.addWidget(btn_zoom_out)
        row.addWidget(btn_zoom_in)
        row.addWidget(btn_zoom_reset)
        row.addWidget(btn_rotate_left)
        row.addWidget(btn_rotate_right)
        row.addWidget(btn_extract)
        row.addWidget(btn_extract_all)
        row.addWidget(btn_saveas)
        row.addWidget(btn_merge)
        row.addWidget(btn_split)
        row.addWidget(btn_reorder)

        layout = QVBoxLayout()
        layout.addLayout(row)
        layout.addWidget(self.page_info)
        layout.addWidget(self.preview)
        layout.addWidget(self.text_output)

        ocr_row = QHBoxLayout()
        ocr_row.addWidget(QLabel("OCR-Sprachen (Tesseract):"))
        ocr_row.addWidget(self.ocr_lang_input)
        layout.addLayout(ocr_row)

        layout.addWidget(self.suggested_name)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        menu = self.menuBar().addMenu("Datei")
        act_open = QAction("Öffnen", self)
        act_open.triggered.connect(self.open_pdf)
        menu.addAction(act_open)

    def open_pdf(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "PDF auswählen", "", "PDF files (*.pdf)")
        if not file_name:
            return

        self.pdf_path = Path(file_name)
        try:
            self.doc = fitz.open(file_name)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"PDF konnte nicht geöffnet werden:\n{e}")
            self.doc = None
            return

        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations.clear()
        self.render_current_page()
        self.text_output.clear()
        self.suggested_name.clear()

    def render_current_page(self) -> None:
        if not self.doc or len(self.doc) == 0:
            self.preview.setText("Kein PDF geladen")
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
        self.preview.setPixmap(qpix.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.page_info.setText(
            f"Seite: {self.current_page + 1}/{total} | Zoom: {int(self.zoom_factor * 100)}% | Drehung: {rotation}°"
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.doc:
            self.render_current_page()

    def keyPressEvent(self, event) -> None:
        key = event.key()
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
        if key == Qt.Key.Key_Minus:
            self.zoom_out()
            return
        if key == Qt.Key.Key_0:
            self.reset_zoom()
            return
        if key == Qt.Key.Key_R:
            self.rotate_right()
            return
        if key == Qt.Key.Key_L:
            self.rotate_left()
            return
        super().keyPressEvent(event)

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
        current = self.page_rotations.get(self.current_page, 0)
        self.page_rotations[self.current_page] = (current - 90) % 360
        self.render_current_page()

    def rotate_right(self) -> None:
        if not self.doc:
            return
        current = self.page_rotations.get(self.current_page, 0)
        self.page_rotations[self.current_page] = (current + 90) % 360
        self.render_current_page()

    def extract_text_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page = self.doc[self.current_page]
        text = page.get_text("text").strip()

        if len(text) < 40:
            # OCR fallback on current page image (respect UI rotation for better OCR)
            rotation = self.page_rotations.get(self.current_page, 0)
            matrix = fitz.Matrix(2.0, 2.0).prerotate(rotation)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            mode = "RGB"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            text, _ = self._ocr_image(img)

        if not text:
            text = "(Kein Text erkannt)"

        self.text_output.setPlainText(text)
        self.suggested_name.setText(suggest_filename_from_text(text))

    def extract_text_all_pages_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        total = len(self.doc)
        if total == 0:
            QMessageBox.information(self, "Hinweis", "Das PDF enthält keine Seiten.")
            return

        progress = QProgressDialog("Extrahiere Text aus allen Seiten …", "Abbrechen", 0, total, self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        all_text_parts: list[str] = []
        ocr_failed_pages: list[int] = []
        ocr_error_preview: str = ""

        for idx in range(total):
            progress.setValue(idx)
            progress.setLabelText(f"Seite {idx + 1}/{total} wird verarbeitet …")
            QApplication.processEvents()
            if progress.wasCanceled():
                QMessageBox.information(self, "Abgebrochen", "Extraktion wurde abgebrochen.")
                return

            page = self.doc[idx]
            text = page.get_text("text").strip()

            if len(text) < 40:
                rotation = self.page_rotations.get(idx, 0)
                matrix = fitz.Matrix(2.0, 2.0).prerotate(rotation)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                text, ocr_error = self._ocr_image(img, show_error=False)
                if ocr_error:
                    ocr_failed_pages.append(idx + 1)
                    if not ocr_error_preview:
                        ocr_error_preview = ocr_error
                    text = ""

            if text:
                all_text_parts.append(text)

        progress.setValue(total)

        combined_text = "\n\n".join(all_text_parts).strip() or "(Kein Text erkannt)"
        self.text_output.setPlainText(combined_text)
        self.suggested_name.setText(suggest_filename_from_text(combined_text))

        if ocr_failed_pages:
            pages = ", ".join(str(p) for p in ocr_failed_pages[:10])
            if len(ocr_failed_pages) > 10:
                pages += ", …"
            QMessageBox.warning(
                self,
                "OCR teilweise fehlgeschlagen",
                "Die Extraktion wurde fortgesetzt, aber OCR schlug auf einigen Seiten fehl."
                f"\n\nSeiten: {pages}"
                f"\nFehleranzahl: {len(ocr_failed_pages)}"
                f"\n\nErster Fehler:\n{ocr_error_preview}",
            )

    def _ocr_lang(self) -> str:
        lang = self.ocr_lang_input.text().strip()
        return lang or "deu+eng"

    def _ocr_image(self, img: Image.Image, show_error: bool = True) -> tuple[str, str | None]:
        try:
            return pytesseract.image_to_string(img, lang=self._ocr_lang()).strip(), None
        except FileNotFoundError as e:
            msg = (
                "Tesseract wurde nicht gefunden. Bitte Tesseract installieren und sicherstellen, "
                "dass der Befehl 'tesseract' im PATH verfügbar ist."
            )
            if show_error:
                QMessageBox.warning(self, "OCR-Fehler", f"{msg}\n\nDetails:\n{e}")
            return "", f"{msg} Details: {e}"
        except TesseractError as e:
            if show_error:
                QMessageBox.warning(
                    self,
                    "OCR-Fehler",
                    "OCR konnte nicht ausgeführt werden. Bitte Tesseract/Sprachdaten prüfen."
                    f"\n\nDetails:\n{e}",
                )
            return "", str(e)

    def save_as_suggested(self) -> None:
        if not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Kein PDF geladen.")
            return

        default_name = self.suggested_name.text().strip() or self.pdf_path.name
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "PDF speichern als",
            str(self.pdf_path.with_name(default_name)),
            "PDF files (*.pdf)",
        )
        if not out_path:
            return

        try:
            has_rotations = any(rot % 360 != 0 for rot in self.page_rotations.values())
            if has_rotations:
                out_doc = fitz.open(str(self.pdf_path))
                try:
                    for idx, rot in self.page_rotations.items():
                        if 0 <= idx < len(out_doc) and rot % 360 != 0:
                            out_doc[idx].set_rotation(rot % 360)
                    out_doc.save(out_path)
                finally:
                    out_doc.close()
            else:
                with open(self.pdf_path, "rb") as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
            QMessageBox.information(self, "Gespeichert", f"Datei gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht speichern:\n{e}")

    def merge_pdfs(self) -> None:
        file_names, _ = QFileDialog.getOpenFileNames(self, "PDFs zum Mergen auswählen", "", "PDF files (*.pdf)")
        if not file_names or len(file_names) < 2:
            QMessageBox.information(self, "Hinweis", "Bitte mindestens zwei PDFs auswählen.")
            return

        out_path, _ = QFileDialog.getSaveFileName(self, "Gemergte PDF speichern", "merged.pdf", "PDF files (*.pdf)")
        if not out_path:
            return

        merged = fitz.open()
        try:
            for path in file_names:
                src = fitz.open(path)
                merged.insert_pdf(src)
                src.close()
            merged.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Gemergte PDF gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Merge fehlgeschlagen:\n{e}")
        finally:
            merged.close()

    def extract_pages_to_new_pdf(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page_spec, ok = QInputDialog.getText(
            self,
            "Seiten extrahieren",
            "Seitenbereich eingeben (z.B. 1,3,5-8, odd, even, all):",
        )
        if not ok or not page_spec.strip():
            return

        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return

        default_name = f"{self.pdf_path.stem}_extract.pdf"
        out_path, _ = QFileDialog.getSaveFileName(self, "Extrakt speichern", str(self.pdf_path.with_name(default_name)), "PDF files (*.pdf)")
        if not out_path:
            return

        out_doc = fitz.open()
        try:
            for idx in page_indices:
                out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                rot = self.page_rotations.get(idx, 0) % 360
                if rot:
                    out_doc[-1].set_rotation(rot)
            out_doc.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Extrakt gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Seitenextraktion fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    def reorder_pages_to_new_pdf(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        order_spec, ok = QInputDialog.getText(
            self,
            "Seiten neu anordnen",
            "Neue Seitenreihenfolge (z.B. 3,1,2,5-7,last,reverse):",
        )
        if not ok or not order_spec.strip():
            return

        ordered_pages = self._parse_order_spec(order_spec, len(self.doc))
        if not ordered_pages:
            QMessageBox.warning(self, "Ungültig", "Keine gültige Reihenfolge erkannt.")
            return

        default_name = f"{self.pdf_path.stem}_reordered.pdf"
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Neu angeordnete PDF speichern",
            str(self.pdf_path.with_name(default_name)),
            "PDF files (*.pdf)",
        )
        if not out_path:
            return

        out_doc = fitz.open()
        try:
            for idx in ordered_pages:
                out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                rot = self.page_rotations.get(idx, 0) % 360
                if rot:
                    out_doc[-1].set_rotation(rot)
            out_doc.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Neu angeordnete PDF gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Neu-Anordnung fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    @staticmethod
    def _parse_page_spec(spec: str, total_pages: int) -> list[int]:
        pages: set[int] = set()

        def parse_bound(raw: str, default: int) -> int:
            token = raw.strip().lower()
            if not token:
                return default
            if token in {"last", "end"}:
                return total_pages
            if token.isdigit():
                return int(token)
            return default

        for part in spec.split(","):
            token = part.strip().lower()
            if not token:
                continue

            if token in {"all", "*"}:
                pages.update(range(total_pages))
                continue
            if token == "odd":
                pages.update(range(0, total_pages, 2))
                continue
            if token == "even":
                pages.update(range(1, total_pages, 2))
                continue
            if token in {"last", "end"}:
                pages.add(total_pages - 1)
                continue

            if "-" in token:
                a, b = token.split("-", 1)
                start = parse_bound(a, 1)
                end = parse_bound(b, total_pages)
                if start > end:
                    start, end = end, start
                for p in range(start, end + 1):
                    if 1 <= p <= total_pages:
                        pages.add(p - 1)
            elif token.isdigit():
                p = int(token)
                if 1 <= p <= total_pages:
                    pages.add(p - 1)
        return sorted(pages)

    @staticmethod
    def _parse_order_spec(spec: str, total_pages: int) -> list[int]:
        ordered: list[int] = []

        def parse_single(token: str) -> int | None:
            tk = token.strip().lower()
            if tk in {"last", "end"}:
                return total_pages
            if tk.isdigit():
                return int(tk)
            return None

        for part in spec.split(","):
            token = part.strip().lower()
            if not token:
                continue

            if token == "all":
                ordered.extend(range(total_pages))
                continue
            if token in {"reverse", "rev"}:
                ordered.extend(reversed(range(total_pages)))
                continue

            if "-" in token:
                a, b = token.split("-", 1)
                start = parse_single(a)
                end = parse_single(b)
                if start is None or end is None:
                    continue
                step = 1 if start <= end else -1
                for p in range(start, end + step, step):
                    if 1 <= p <= total_pages:
                        ordered.append(p - 1)
                continue

            page = parse_single(token)
            if page is not None and 1 <= page <= total_pages:
                ordered.append(page - 1)

        return ordered


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
