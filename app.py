import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
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
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b(\d{2}/\d{2}/\d{4})\b",
    ]
    date = ""
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1)
            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass
            if date:
                break

    # Number
    number = ""
    m_num = re.search(r"(?i)(rechnungsnr\.?|invoice\s*no\.?|belegnr\.?|nr\.?)[\s:#-]*([A-Z0-9-]{4,})", text)
    if m_num:
        number = m_num.group(2)

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

        self.preview = QLabel("Kein PDF geladen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(420)

        self.text_output = QTextEdit()
        self.text_output.setReadOnly(True)
        self.text_output.setPlaceholderText("Extrahierter Text erscheint hier …")

        self.suggested_name = QLineEdit()
        self.suggested_name.setPlaceholderText("Vorgeschlagener Dateiname")

        btn_open = QPushButton("PDF öffnen")
        btn_extract = QPushButton("Text/OCR extrahieren")
        btn_saveas = QPushButton("Speichern als …")

        btn_open.clicked.connect(self.open_pdf)
        btn_extract.clicked.connect(self.extract_text_and_suggest)
        btn_saveas.clicked.connect(self.save_as_suggested)

        row = QHBoxLayout()
        row.addWidget(btn_open)
        row.addWidget(btn_extract)
        row.addWidget(btn_saveas)

        layout = QVBoxLayout()
        layout.addLayout(row)
        layout.addWidget(self.preview)
        layout.addWidget(self.text_output)
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

        self.render_first_page()
        self.text_output.clear()
        self.suggested_name.clear()

    def render_first_page(self) -> None:
        if not self.doc or len(self.doc) == 0:
            return
        page = self.doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        fmt = QImage.Format.Format_RGB888
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
        qpix = QPixmap.fromImage(img)
        self.preview.setPixmap(qpix.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.doc:
            self.render_first_page()

    def extract_text_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page = self.doc[0]
        text = page.get_text("text").strip()

        if len(text) < 40:
            # OCR fallback on first page image
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            mode = "RGB"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            text = pytesseract.image_to_string(img, lang="deu+eng").strip()

        if not text:
            text = "(Kein Text erkannt)"

        self.text_output.setPlainText(text)
        self.suggested_name.setText(suggest_filename_from_text(text))

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
            with open(self.pdf_path, "rb") as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            QMessageBox.information(self, "Gespeichert", f"Datei gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht speichern:\n{e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
