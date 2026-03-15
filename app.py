import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from pytesseract import Output, TesseractError, TesseractNotFoundError
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
    QSplitter,
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
    if "gutschrift" in lower or "credit note" in lower or "credit memo" in lower:
        doc_type = "Gutschrift"
    elif "rechnung" in lower or "invoice" in lower:
        doc_type = "Rechnung"
    elif "angebot" in lower or "quote" in lower:
        doc_type = "Angebot"
    elif "vertrag" in lower or "contract" in lower:
        doc_type = "Vertrag"
    elif "lieferschein" in lower:
        doc_type = "Lieferschein"

    # Date
    date_patterns = [
        r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b",
        r"\b(\d{1,2}\.\d{1,2}\.\d{2})\b",
        r"\b(\d{4}-\d{1,2}-\d{1,2})\b",
        r"\b(\d{1,2}-\d{1,2}-\d{4})\b",
        r"\b(\d{1,2}-\d{1,2}-\d{2})\b",
        r"\b(\d{4}/\d{1,2}/\d{1,2})\b",
        r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
        r"\b(\d{1,2}/\d{1,2}/\d{2})\b",
    ]
    date = ""
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1)
            for fmt in (
                "%d.%m.%Y",
                "%d.%m.%y",
                "%Y-%m-%d",
                "%d-%m-%Y",
                "%d-%m-%y",
                "%m-%d-%Y",
                "%m-%d-%y",
                "%Y/%m/%d",
                "%d/%m/%Y",
                "%d/%m/%y",
                "%m/%d/%Y",
                "%m/%d/%y",
            ):
                try:
                    date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass
            if date:
                break

    if not date:
        m_compact = re.search(r"(?i)\b(?:datum|date)\s*[:\-]?\s*(\d{8}|\d{6})\b", text)
        if m_compact:
            raw = m_compact.group(1)
            try:
                if len(raw) == 8:
                    if raw.startswith(("19", "20")):
                        parsed = datetime.strptime(raw, "%Y%m%d")
                    else:
                        parsed = datetime.strptime(raw, "%d%m%Y")
                else:
                    parsed = datetime.strptime(raw, "%d%m%y")
                date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass

    if not date:
        month_map = {
            "januar": "01",
            "january": "01",
            "jan": "01",
            "februar": "02",
            "february": "02",
            "feb": "02",
            "märz": "03",
            "maerz": "03",
            "march": "03",
            "mar": "03",
            "april": "04",
            "apr": "04",
            "mai": "05",
            "may": "05",
            "juni": "06",
            "june": "06",
            "jun": "06",
            "juli": "07",
            "july": "07",
            "jul": "07",
            "august": "08",
            "aug": "08",
            "september": "09",
            "sep": "09",
            "sept": "09",
            "oktober": "10",
            "october": "10",
            "okt": "10",
            "oct": "10",
            "november": "11",
            "nov": "11",
            "dezember": "12",
            "december": "12",
            "dez": "12",
            "dec": "12",
        }
        m_textual = re.search(r"\b(\d{1,2})[.\s-]+([A-Za-zÄÖÜäöü]+)[,\s-]+(\d{2}|\d{4})\b", text)
        if m_textual:
            day, month_raw, year_raw = m_textual.groups()
            month_key = month_raw.strip(".").lower()
            month = month_map.get(month_key)
            if month:
                try:
                    if len(year_raw) == 2:
                        parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%y")
                    else:
                        parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%Y")
                    date = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    pass

        if not date:
            m_textual_month_first = re.search(
                r"\b([A-Za-zÄÖÜäöü]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{2}|\d{4})\b",
                text,
                re.IGNORECASE,
            )
            if m_textual_month_first:
                month_raw, day, year_raw = m_textual_month_first.groups()
                month_key = month_raw.strip(".").lower()
                month = month_map.get(month_key)
                if month:
                    try:
                        if len(year_raw) == 2:
                            parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%y")
                        else:
                            parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%Y")
                        date = parsed.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

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

    amount, currency = extract_total_amount_info(text)
    if amount and info.doc_type in {"Rechnung", "Gutschrift"}:
        amount_tag = amount.replace(",", "-")
        parts.append(f"{amount_tag}{currency or 'EUR'}")

    return sanitize_filename("_".join(parts)) + ".pdf"


def _normalize_amount_token(raw: str) -> str:
    token = re.sub(r"[\s\u00A0\u202F]", "", raw)
    token = token.replace("'", "").replace("’", "")
    if not token:
        return ""

    has_comma = "," in token
    has_dot = "." in token

    if has_comma and has_dot:
        # Last separator is assumed to be decimal separator
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousand_sep = "." if decimal_sep == "," else ","
        token = token.replace(thousand_sep, "")
        integer_part, frac_part = token.rsplit(decimal_sep, 1)
        if frac_part.isdigit() and len(frac_part) == 1:
            frac_part += "0"
        return f"{integer_part},{frac_part}"

    if has_dot and not has_comma:
        if re.search(r"\.\d{1,2}$", token):
            integer_part, frac_part = token.rsplit(".", 1)
            if len(frac_part) == 1:
                frac_part += "0"
            return f"{integer_part},{frac_part}"
        return token.replace(".", "")

    if has_comma and not has_dot:
        if re.search(r",\d{1,2}$", token):
            integer_part, frac_part = token.rsplit(",", 1)
            if len(frac_part) == 1:
                frac_part += "0"
            return f"{integer_part},{frac_part}"
        return token.replace(",", "")

    return token


def _normalize_currency_token(raw: str) -> str:
    token = (raw or "").strip().lower()
    if token in {"€", "eur"}:
        return "EUR"
    if token in {"chf"}:
        return "CHF"
    if token in {"$", "usd"}:
        return "USD"
    if token in {"£", "gbp"}:
        return "GBP"
    return ""


def extract_total_amount_info(text: str) -> tuple[str, str]:
    amount_expr = r"\d{1,3}(?:[\.,'’\s\u00A0\u202F]\d{3})*(?:[\.,]\d{1,2})?|\d+(?:[\.,]\d{1,2})?"
    currency_expr = r"€|eur|chf|\$|usd|£|gbp"
    patterns = [
        rf"(?i)\b(?:gesamt(?:betrag)?|rechnungsbetrag|endbetrag|summe|zu\s+zahlen|zu\s+überweisen|brutto(?:betrag)?|fälliger\s+betrag|total(?:\s+due)?|grand\s+total|amount\s+due|amount\s+payable)\b[^\dA-Z]{{0,16}}(?:(?P<curr_before>{currency_expr})\s*)?(?P<amount>{amount_expr})\s*(?P<curr_after>{currency_expr})?",
        rf"(?i)(?P<curr_before>{currency_expr})\s*(?P<amount>{amount_expr})\b",
        rf"(?i)(?P<amount>{amount_expr})\s*(?P<curr_after>{currency_expr})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        amount = _normalize_amount_token(m.group("amount"))
        currency = _normalize_currency_token(m.groupdict().get("curr_before") or m.groupdict().get("curr_after") or "")
        return amount, currency
    return "", ""


def extract_total_amount(text: str) -> str:
    amount, _ = extract_total_amount_info(text)
    return amount


def candidate_variants(value: str) -> list[str]:
    # common OCR confusions for invoice-like identifiers
    confusion_map = {
        "0": ["O", "Q"],
        "O": ["0"],
        "1": ["I", "l"],
        "I": ["1", "l"],
        "l": ["1", "I"],
        "5": ["S"],
        "S": ["5"],
        "8": ["B"],
        "B": ["8"],
        "2": ["Z"],
        "Z": ["2"],
    }
    variants = {value}
    for idx, ch in enumerate(value):
        for repl in confusion_map.get(ch, []):
            variants.add(value[:idx] + repl + value[idx + 1 :])
    variants.discard(value)
    return sorted(variants)[:6]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Offline PDF Reader — MVP")
        self.resize(1220, 860)

        self.pdf_path: Path | None = None
        self.doc: fitz.Document | None = None
        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations: dict[int, int] = {}
        self.learning_rules_path = Path(__file__).with_name("learning_rules.json")
        self.learning_rules = self._load_learning_rules()

        self.preview = QLabel("Kein PDF geladen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(460)

        self.text_output = QTextEdit()
        self.text_output.setReadOnly(True)
        self.text_output.setPlaceholderText("Extrahierter Text erscheint hier …")

        self.suggested_name = QLineEdit()
        self.suggested_name.setPlaceholderText("Vorgeschlagener Dateiname")

        self.ocr_lang_input = QLineEdit("deu+eng")
        self.ocr_lang_input.setPlaceholderText("z.B. deu+eng")

        self.page_info = QLabel("Seite: -/- | Zoom: 100%")
        self.page_info.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.ocr_feedback = QLabel("OCR-Hinweise: -")
        self.ocr_feedback.setWordWrap(True)

        btn_open = QPushButton("PDF öffnen")
        btn_first = QPushButton("⏮ Erste")
        btn_prev = QPushButton("◀ Vorherige")
        btn_next = QPushButton("Nächste ▶")
        btn_last = QPushButton("Letzte ⏭")
        btn_zoom_out = QPushButton("− Zoom")
        btn_zoom_in = QPushButton("+ Zoom")
        btn_zoom_reset = QPushButton("100%")
        btn_goto = QPushButton("Gehe zu Seite")
        btn_rotate_left = QPushButton("↺ Drehen")
        btn_rotate_right = QPushButton("↻ Drehen")
        btn_rotate_reset = QPushButton("⟲ Reset-Drehung")
        btn_extract = QPushButton("Text/OCR extrahieren")
        btn_extract_all = QPushButton("Alle Seiten extrahieren")
        btn_saveas = QPushButton("Speichern als …")
        btn_merge = QPushButton("PDFs mergen")
        btn_split = QPushButton("Seiten extrahieren")
        btn_reorder = QPushButton("Seiten neu anordnen")

        for b in [btn_open, btn_first, btn_prev, btn_next, btn_last, btn_zoom_out, btn_zoom_in, btn_zoom_reset, btn_goto, btn_rotate_left, btn_rotate_right, btn_rotate_reset, btn_extract, btn_extract_all, btn_saveas, btn_merge, btn_split, btn_reorder]:
            b.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_open.clicked.connect(self.open_pdf)
        btn_first.clicked.connect(self.first_page)
        btn_prev.clicked.connect(self.prev_page)
        btn_next.clicked.connect(self.next_page)
        btn_last.clicked.connect(self.last_page)
        btn_zoom_out.clicked.connect(self.zoom_out)
        btn_zoom_in.clicked.connect(self.zoom_in)
        btn_zoom_reset.clicked.connect(self.reset_zoom)
        btn_goto.clicked.connect(self.go_to_page)
        btn_rotate_left.clicked.connect(self.rotate_left)
        btn_rotate_right.clicked.connect(self.rotate_right)
        btn_rotate_reset.clicked.connect(self.reset_rotation)
        btn_extract.clicked.connect(self.extract_text_and_suggest)
        btn_extract_all.clicked.connect(self.extract_text_all_pages_and_suggest)
        btn_saveas.clicked.connect(self.save_as_suggested)
        btn_merge.clicked.connect(self.merge_pdfs)
        btn_split.clicked.connect(self.extract_pages_to_new_pdf)
        btn_reorder.clicked.connect(self.reorder_pages_to_new_pdf)

        toolbar_top = QHBoxLayout()
        toolbar_top.addWidget(btn_open)
        toolbar_top.addSpacing(8)
        toolbar_top.addWidget(btn_first)
        toolbar_top.addWidget(btn_prev)
        toolbar_top.addWidget(btn_next)
        toolbar_top.addWidget(btn_last)
        toolbar_top.addSpacing(8)
        toolbar_top.addWidget(btn_goto)
        toolbar_top.addSpacing(8)
        toolbar_top.addWidget(btn_zoom_out)
        toolbar_top.addWidget(btn_zoom_in)
        toolbar_top.addWidget(btn_zoom_reset)
        toolbar_top.addSpacing(8)
        toolbar_top.addWidget(btn_rotate_left)
        toolbar_top.addWidget(btn_rotate_right)
        toolbar_top.addWidget(btn_rotate_reset)
        toolbar_top.addStretch(1)

        toolbar_bottom = QHBoxLayout()
        toolbar_bottom.addWidget(btn_extract)
        toolbar_bottom.addWidget(btn_extract_all)
        toolbar_bottom.addWidget(btn_saveas)
        toolbar_bottom.addSpacing(10)
        toolbar_bottom.addWidget(btn_split)
        toolbar_bottom.addWidget(btn_reorder)
        toolbar_bottom.addWidget(btn_merge)
        toolbar_bottom.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.preview)
        splitter.addWidget(self.text_output)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        meta_row = QHBoxLayout()
        meta_row.addWidget(QLabel("OCR-Sprachen (Tesseract):"))
        meta_row.addWidget(self.ocr_lang_input)
        meta_row.addSpacing(10)
        meta_row.addWidget(QLabel("Dateiname:"))
        meta_row.addWidget(self.suggested_name)

        layout = QVBoxLayout()
        layout.addLayout(toolbar_top)
        layout.addLayout(toolbar_bottom)
        layout.addWidget(self.page_info)
        layout.addWidget(splitter)
        layout.addLayout(meta_row)
        layout.addWidget(self.ocr_feedback)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        menu = self.menuBar().addMenu("Datei")
        act_open = QAction("Öffnen", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self.open_pdf)
        menu.addAction(act_open)

        act_close_pdf = QAction("PDF schließen", self)
        act_close_pdf.setShortcut("Ctrl+W")
        act_close_pdf.triggered.connect(self.close_pdf)
        menu.addAction(act_close_pdf)

        menu.addSeparator()

        act_save_as = QAction("Speichern als …", self)
        act_save_as.setShortcut("Ctrl+S")
        act_save_as.triggered.connect(self.save_as_suggested)
        menu.addAction(act_save_as)

        act_extract = QAction("Alle Seiten extrahieren", self)
        act_extract.setShortcut("Ctrl+Shift+E")
        act_extract.triggered.connect(self.extract_text_all_pages_and_suggest)
        menu.addAction(act_extract)

        self.statusBar().showMessage("Bereit. Öffne ein PDF, um zu starten.")
        self._apply_styles()

    def _load_learning_rules(self) -> dict:
        if not self.learning_rules_path.exists():
            return {"replacements": {}}
        try:
            data = json.loads(self.learning_rules_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("replacements", {}), dict):
                return data
        except Exception:
            pass
        return {"replacements": {}}

    def _save_learning_rules(self) -> None:
        try:
            self.learning_rules_path.write_text(json.dumps(self.learning_rules, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _apply_learning_rules(self, text: str) -> str:
        replacements = self.learning_rules.get("replacements", {})
        if not replacements:
            return text
        out = text
        for src, dst in replacements.items():
            out = re.sub(rf"\b{re.escape(src)}\b", dst, out)
        return out

    def _ocr_image_with_confidence(self, img: Image.Image, show_error: bool = True) -> tuple[str, str | None, list[str]]:
        text, err = self._ocr_image(img, show_error=show_error)
        if err:
            return text, err, []
        low_conf_tokens: list[str] = []
        try:
            data = pytesseract.image_to_data(img, lang=self._ocr_lang(), output_type=Output.DICT)
            for token, conf in zip(data.get("text", []), data.get("conf", [])):
                tk = (token or "").strip()
                if not tk:
                    continue
                try:
                    score = float(conf)
                except Exception:
                    continue
                if 0 <= score < 55 and len(tk) >= 2:
                    low_conf_tokens.append(tk)
        except Exception:
            pass
        return text, None, low_conf_tokens[:20]

    def _build_ocr_feedback(self, text: str, low_conf_tokens: list[str]) -> str:
        info = parse_doc_info(text)
        amount, currency = extract_total_amount_info(text)
        lines = []
        if low_conf_tokens:
            unique_tokens = []
            for t in low_conf_tokens:
                if t not in unique_tokens:
                    unique_tokens.append(t)
            lines.append("Unsichere OCR-Tokens: " + ", ".join(unique_tokens[:8]))

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
                if ok and choice and choice != number:
                    self.learning_rules.setdefault("replacements", {})[number] = choice
                    self._save_learning_rules()
                    lines.append(f"Lernregel gespeichert: {number} → {choice}")

        if amount:
            lines.append(f"Erkannter Gesamtbetrag: {amount} {currency or 'EUR'}")

        return "OCR-Hinweise: " + (" | ".join(lines) if lines else "keine Auffälligkeiten")

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f4f6fb; }
            QPushButton {
                background: #ffffff;
                border: 1px solid #d8deea;
                border-radius: 8px;
                padding: 6px 10px;
            }
            QPushButton:hover { background: #eef3ff; }
            QPushButton:pressed { background: #e2ebff; }
            QTextEdit, QLineEdit {
                background: #ffffff;
                border: 1px solid #d8deea;
                border-radius: 8px;
                padding: 6px;
            }
            QLabel { color: #1f2a44; }
            """
        )

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
        self.preview.setText("Kein PDF geladen")
        self.page_info.setText("Seite: -/- | Zoom: 100%")
        self.text_output.clear()
        self.suggested_name.clear()
        self.ocr_feedback.setText("OCR-Hinweise: -")
        self.statusBar().showMessage("PDF geschlossen.")

    def closeEvent(self, event) -> None:
        self._close_open_document()
        super().closeEvent(event)

    def open_pdf(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "PDF auswählen", "", "PDF files (*.pdf)")
        if not file_name:
            return

        self._close_open_document()
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
        self.ocr_feedback.setText("OCR-Hinweise: -")
        self.statusBar().showMessage(f"Geladen: {self.pdf_path.name} ({len(self.doc)} Seiten)")

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
        if self.pdf_path:
            self.statusBar().showMessage(f"{self.pdf_path.name} — Seite {self.current_page + 1}/{total}")

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
        if key == Qt.Key.Key_G and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.go_to_page()
            return
        if key == Qt.Key.Key_Minus:
            self.zoom_out()
            return
        if key == Qt.Key.Key_0:
            self.reset_zoom()
            return
        if key == Qt.Key.Key_R and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
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
        current = self.page_rotations.get(self.current_page, 0)
        self.page_rotations[self.current_page] = (current - 90) % 360
        self.render_current_page()

    def rotate_right(self) -> None:
        if not self.doc:
            return
        current = self.page_rotations.get(self.current_page, 0)
        self.page_rotations[self.current_page] = (current + 90) % 360
        self.render_current_page()

    def reset_rotation(self) -> None:
        if not self.doc:
            return
        self.page_rotations[self.current_page] = 0
        self.render_current_page()

    def extract_text_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page = self.doc[self.current_page]
        text = page.get_text("text").strip()
        low_conf_tokens: list[str] = []

        if len(text) < 40:
            # OCR fallback on current page image (respect UI rotation for better OCR)
            rotation = self.page_rotations.get(self.current_page, 0)
            matrix = fitz.Matrix(2.0, 2.0).prerotate(rotation)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            mode = "RGB"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            text, _, low_conf_tokens = self._ocr_image_with_confidence(img)

        if not text:
            text = "(Kein Text erkannt)"

        text = self._apply_learning_rules(text)
        self.text_output.setPlainText(text)
        self.suggested_name.setText(suggest_filename_from_text(text))
        self.ocr_feedback.setText(self._build_ocr_feedback(text, low_conf_tokens))
        self.statusBar().showMessage("Text aus aktueller Seite extrahiert.")

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
        all_low_conf_tokens: list[str] = []
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
                text, ocr_error, low_conf_tokens = self._ocr_image_with_confidence(img, show_error=False)
                all_low_conf_tokens.extend(low_conf_tokens)
                if ocr_error:
                    ocr_failed_pages.append(idx + 1)
                    if not ocr_error_preview:
                        ocr_error_preview = ocr_error
                    text = ""

            if text:
                all_text_parts.append(text)

        progress.setValue(total)

        combined_text = "\n\n".join(all_text_parts).strip() or "(Kein Text erkannt)"
        combined_text = self._apply_learning_rules(combined_text)
        self.text_output.setPlainText(combined_text)
        self.suggested_name.setText(suggest_filename_from_text(combined_text))
        self.ocr_feedback.setText(self._build_ocr_feedback(combined_text, all_low_conf_tokens))
        self.statusBar().showMessage(f"Text aus {total} Seiten extrahiert.")

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
        lang = self._ocr_lang()
        try:
            return pytesseract.image_to_string(img, lang=lang).strip(), None
        except (FileNotFoundError, TesseractNotFoundError) as e:
            msg = (
                "Tesseract wurde nicht gefunden. Bitte Tesseract installieren und sicherstellen, "
                "dass der Befehl 'tesseract' im PATH verfügbar ist."
            )
            if show_error:
                QMessageBox.warning(self, "OCR-Fehler", f"{msg}\n\nDetails:\n{e}")
            return "", f"{msg} Details: {e}"
        except TesseractError as e:
            err_text = str(e)
            # fallback if custom language pack is missing/misconfigured
            if lang != "deu+eng" and ("Failed loading language" in err_text or "Error opening data file" in err_text):
                try:
                    return pytesseract.image_to_string(img, lang="deu+eng").strip(), None
                except TesseractError:
                    pass
            if show_error:
                QMessageBox.warning(
                    self,
                    "OCR-Fehler",
                    "OCR konnte nicht ausgeführt werden. Bitte Tesseract/Sprachdaten prüfen."
                    f"\n\nDetails:\n{e}",
                )
            return "", err_text

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
        out_path = self._ensure_pdf_suffix(out_path)

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
            self.statusBar().showMessage(f"Gespeichert: {Path(out_path).name}")
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
        out_path = self._ensure_pdf_suffix(out_path)

        merged = fitz.open()
        try:
            for path in file_names:
                src = None
                try:
                    src = fitz.open(path)
                    merged.insert_pdf(src)
                finally:
                    if src is not None:
                        src.close()
            merged.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Gemergte PDF gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Merge erstellt: {Path(out_path).name}")
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
            "Seitenbereich eingeben (z.B. 1,3,5-8,current, odd, even, all):",
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
        out_path = self._ensure_pdf_suffix(out_path)

        out_doc = fitz.open()
        try:
            for idx in page_indices:
                out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                rot = self.page_rotations.get(idx, 0) % 360
                if rot:
                    out_doc[-1].set_rotation(rot)
            out_doc.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Extrakt gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Seitenextrakt erstellt: {Path(out_path).name}")
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
            "Neue Seitenreihenfolge (z.B. 3,1,current,2,5-7,last,reverse):",
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
        out_path = self._ensure_pdf_suffix(out_path)

        out_doc = fitz.open()
        try:
            for idx in ordered_pages:
                out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                rot = self.page_rotations.get(idx, 0) % 360
                if rot:
                    out_doc[-1].set_rotation(rot)
            out_doc.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Neu angeordnete PDF gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Neu angeordnete PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Neu-Anordnung fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    def _parse_page_spec(self, spec: str, total_pages: int) -> list[int]:
        ordered: list[int] = []
        seen: set[int] = set()

        def add_page(idx: int) -> None:
            if idx not in seen:
                seen.add(idx)
                ordered.append(idx)

        def parse_bound(raw: str, default: int) -> int:
            token = raw.strip().lower()
            if not token:
                return default
            if token in {"first", "start", "begin"}:
                return 1
            if token in {"last", "end"}:
                return total_pages
            if token in {"current", "cur", "here"}:
                return self.current_page + 1
            if token.isdigit():
                return int(token)
            return default

        for part in spec.split(","):
            token = part.strip().lower()
            if not token:
                continue

            if token in {"all", "*"}:
                for idx in range(total_pages):
                    add_page(idx)
                continue
            if token == "odd":
                for idx in range(0, total_pages, 2):
                    add_page(idx)
                continue
            if token == "even":
                for idx in range(1, total_pages, 2):
                    add_page(idx)
                continue
            if token in {"first", "start", "begin"}:
                add_page(0)
                continue
            if token in {"last", "end"}:
                add_page(total_pages - 1)
                continue
            if token in {"current", "cur", "here"}:
                if 0 <= self.current_page < total_pages:
                    add_page(self.current_page)
                continue

            if "-" in token:
                a, b = token.split("-", 1)
                start = parse_bound(a, 1)
                end = parse_bound(b, total_pages)
                step = 1 if start <= end else -1
                for p in range(start, end + step, step):
                    if 1 <= p <= total_pages:
                        add_page(p - 1)
            elif token.isdigit():
                p = int(token)
                if 1 <= p <= total_pages:
                    add_page(p - 1)
        return ordered

    def _parse_order_spec(self, spec: str, total_pages: int) -> list[int]:
        ordered: list[int] = []

        def parse_single(token: str) -> int | None:
            tk = token.strip().lower()
            if tk in {"first", "start", "begin"}:
                return 1
            if tk in {"last", "end"}:
                return total_pages
            if tk in {"current", "cur", "here"}:
                return self.current_page + 1
            if tk.isdigit():
                return int(tk)
            return None

        for part in spec.split(","):
            token = part.strip().lower()
            if not token:
                continue

            if token in {"all", "*"}:
                ordered.extend(range(total_pages))
                continue
            if token == "odd":
                ordered.extend(range(0, total_pages, 2))
                continue
            if token == "even":
                ordered.extend(range(1, total_pages, 2))
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
