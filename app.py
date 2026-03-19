import csv
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import fitz  # PyMuPDF
import pytesseract
from pytesseract import Output, TesseractError, TesseractNotFoundError
from PIL import Image, ImageOps
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QScrollArea,
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
    subject: str = ""


@dataclass
class ExportRecord:
    datei: str
    datum: str
    typ: str
    absender: str
    nummer: str
    betreff: str
    betrag: str
    waehrung: str
    text_laenge: int
    text_auszug: str


def _normalize_filename_part(value: str, max_len: int = 48) -> str:
    token = (value or "").strip()
    if not token:
        return ""
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
    }
    for src, dst in replacements.items():
        token = token.replace(src, dst)
    token = unicodedata.normalize("NFKD", token)
    token = token.encode("ascii", "ignore").decode("ascii")
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", token)
    token = re.sub(r"_+", "_", token).strip("._-")
    return token[:max_len]


class ReorderPagesDialog(QDialog):
    def __init__(self, doc: fitz.Document, page_rotations: dict[int, int], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Seiten neu anordnen (Drag & Drop)")
        self.resize(920, 640)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Ziehe die Miniaturen per Drag & Drop in die gewünschte Reihenfolge."))

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setWrapping(True)
        self.list_widget.setFlow(QListWidget.Flow.LeftToRight)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDragDropOverwriteMode(False)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.viewport().setAcceptDrops(True)
        self.list_widget.setDropIndicatorShown(True)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setSpacing(10)
        self.list_widget.setIconSize(QSize(140, 200))
        self.list_widget.setGridSize(QSize(160, 245))

        for i in range(len(doc)):
            page = doc[i]
            rotation = page_rotations.get(i, 0)
            pix = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35).prerotate(rotation), alpha=False)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            item = QListWidgetItem(QIcon(QPixmap.fromImage(img)), f"Seite {i + 1}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.list_widget.addItem(item)

        layout.addWidget(self.list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def ordered_pages(self) -> list[int]:
        order: list[int] = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            order.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return order


def sanitize_filename(name: str) -> str:
    name = _normalize_filename_part(name, max_len=140)
    return name or "Dokument"


def _looks_like_subject_line(line: str) -> bool:
    ln = (line or "").strip()
    if len(ln) < 10:
        return False
    if re.fullmatch(r"[\W_\d]+", ln):
        return False

    low = ln.lower()
    if any(k in low for k in ["straße", "str.", "strasse", "street", "telefon", "phone", "fax", "www.", "mail", "e-mail", "deutschland", "germany"]):
        return False

    words = re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", ln)
    if len(words) < 3:
        return False

    short_words = [w for w in words if len(w) <= 2]
    if len(short_words) > max(2, len(words) // 2):
        return False

    return True


def parse_doc_info(text: str) -> ParsedDocInfo:
    lower = text.lower()
    raw_lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in raw_lines if ln]

    # Doc type
    doc_type = "Dokument"
    has_order_confirmation = (
        "auftragsbestätigung" in lower
        or "auftragsbestaetigung" in lower
        or "order confirmation" in lower
    )
    has_delivery_note = (
        "lieferschein" in lower
        or "delivery note" in lower
        or "dispatch note" in lower
        or "despatch note" in lower
        or "packing slip" in lower
    )

    if "gutschrift" in lower or "credit note" in lower or "credit memo" in lower:
        doc_type = "Gutschrift"
    elif "mahnung" in lower or "zahlungserinnerung" in lower or "payment reminder" in lower:
        doc_type = "Mahnung"
    elif has_order_confirmation:
        doc_type = "Auftragsbestaetigung"
    elif has_delivery_note:
        doc_type = "Lieferschein"
    elif "rechnung" in lower or "invoice" in lower:
        doc_type = "Rechnung"
    elif "angebot" in lower or "quote" in lower:
        doc_type = "Angebot"
    elif (
        "bestellung" in lower
        or "purchase order" in lower
        or re.search(r"\border\b", lower)
    ):
        doc_type = "Bestellung"
    elif "vertrag" in lower or "contract" in lower:
        doc_type = "Vertrag"

    # Date
    date_patterns = [
        r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b",
        r"\b(\d{1,2}\.\d{1,2}\.\d{2})\b",
        r"\b(\d{4}-\d{1,2}-\d{1,2})\b",
        r"\b(\d{4}\.\d{1,2}\.\d{1,2})\b",
        r"\b(\d{1,2}-\d{1,2}-\d{4})\b",
        r"\b(\d{1,2}-\d{1,2}-\d{2})\b",
        r"\b(\d{4}/\d{1,2}/\d{1,2})\b",
        r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
        r"\b(\d{1,2}/\d{1,2}/\d{2})\b",
        r"\b(\d{1,2}\s+\d{1,2}\s+\d{4})\b",
        r"\b(\d{1,2}\s+\d{1,2}\s+\d{2})\b",
        r"\b(\d{4}\s+\d{1,2}\s+\d{1,2})\b",
    ]
    date = ""
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1)
            for fmt in (
                "%d.%m.%Y",
                "%d.%m.%y",
                "%m.%d.%Y",
                "%m.%d.%y",
                "%Y-%m-%d",
                "%Y.%m.%d",
                "%d-%m-%Y",
                "%d-%m-%y",
                "%m-%d-%Y",
                "%m-%d-%y",
                "%Y/%m/%d",
                "%d/%m/%Y",
                "%d/%m/%y",
                "%m/%d/%Y",
                "%m/%d/%y",
                "%d %m %Y",
                "%d %m %y",
                "%Y %m %d",
            ):
                try:
                    date = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass
            if date:
                break

    if not date:
        m_compact = re.search(
            r"(?i)\b(?:datum|date|rechnungsdatum|belegdatum|invoice\s+date)\s*[:\-]?\s*(\d{8}|\d{6})\b",
            text,
        )
        if m_compact:
            raw = m_compact.group(1)
            try:
                if len(raw) == 8:
                    if raw.startswith(("19", "20")):
                        parsed = datetime.strptime(raw, "%Y%m%d")
                    else:
                        parsed = None
                        for fmt in ("%d%m%Y", "%m%d%Y"):
                            try:
                                parsed = datetime.strptime(raw, fmt)
                                break
                            except ValueError:
                                continue
                        if not parsed:
                            raise ValueError
                else:
                    parsed = None
                    for fmt in ("%d%m%y", "%m%d%y", "%y%m%d"):
                        try:
                            parsed = datetime.strptime(raw, fmt)
                            break
                        except ValueError:
                            continue
                    if not parsed:
                        raise ValueError
                date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass

    if not date:
        # Fallback for standalone compact dates like 20260317, 17032026, or 170326.
        compact_candidates = re.findall(r"\b\d{8}\b", text) + re.findall(r"\b\d{6}\b", text)
        for raw in compact_candidates:
            parsed = None
            if len(raw) == 8:
                try:
                    if raw.startswith(("19", "20")):
                        parsed = datetime.strptime(raw, "%Y%m%d")
                    else:
                        parsed = None
                        for fmt in ("%d%m%Y", "%m%d%Y"):
                            try:
                                parsed = datetime.strptime(raw, fmt)
                                break
                            except ValueError:
                                continue
                        if not parsed:
                            raise ValueError
                except ValueError:
                    continue
            else:
                for fmt in ("%d%m%y", "%m%d%y", "%y%m%d"):
                    try:
                        parsed = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
                if not parsed:
                    continue

            if 1990 <= parsed.year <= 2100:
                date = parsed.strftime("%Y-%m-%d")
                break

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
            "marz": "03",
            "mär": "03",
            "mrz": "03",
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
        m_textual = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?[.\s-]+([A-Za-zÄÖÜäöü]+)[.,\s-]+(\d{2}|\d{4})\b",
            text,
            re.IGNORECASE,
        )
        if m_textual:
            day, month_raw, year_raw = m_textual.groups()
            month_key = month_raw.strip(".,").lower()
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
                r"\b([A-Za-zÄÖÜäöü]+)[.,]?[-/\s]+(\d{1,2})(?:st|nd|rd|th)?[,\.]?[-/\s]+(\d{2}|\d{4})\b",
                text,
                re.IGNORECASE,
            )
            if m_textual_month_first:
                month_raw, day, year_raw = m_textual_month_first.groups()
                month_key = month_raw.strip(".,").lower()
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
        r"(?i)(?:rechnungs(?:nr|nummer)\.?|rechn\.?\s*[-/]?\s*nr\.?|re\.?\s*[-/]?\s*nr\.?|rg\.?\s*[-/]?\s*nr\.?|invoice\s*(?:no|number|nr)\.?|invoice\s*#|belegnr\.?|vorgangs(?:nr|nummer)\.?|bestell(?:nr|nummer)\.?|order\s*(?:no|number)\.?|purchase\s*order\s*(?:no|number)\.?|lieferschein(?:nr|nummer)\.?|delivery\s*note\s*(?:no|number)\.?|nr\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
        r"(?i)\b(?:inv|doc|po|dn)\s*[-_]?\s*([A-Z0-9][A-Z0-9/_-]{2,})\b",
    ]
    for pat in number_patterns:
        m_num = re.search(pat, text)
        if m_num:
            number = m_num.group(1).strip(".,;:)")
            break

    def is_address_like(line: str) -> bool:
        l = line.lower()
        if re.search(r"\b\d{5}\b", l):
            return True
        if re.search(r"\b\d{1,4}[a-z]?\b", l) and any(
            token in l
            for token in ["straße", "str.", "strasse", "street", "st.", "weg", "allee", "avenue", "road", "rd."]
        ):
            return True
        if any(token in l for token in ["deutschland", "germany", "telefon", "phone", "fax", "mail", "e-mail", "www."]):
            return True
        return False

    # Subject heuristic: prefer explicit Betreff/Subject; else best title-like line
    subject = ""
    m_subject = re.search(r"(?im)^(?:betreff|subject)\s*(?::|-)?\s*(.+)$", text)
    if m_subject:
        subject = m_subject.group(1).strip()
    else:
        preferred_keywords = ["verkauf", "teilgrundstück", "grundstück", "betreff", "antrag", "kündigung", "vertrag", "rechnung", "invoice", "angebot", "gutschrift"]
        weighted: list[tuple[int, str]] = []
        for idx, ln in enumerate(lines[:60]):
            if not _looks_like_subject_line(ln):
                continue
            ll = ln.lower()
            score = 0
            if any(k in ll for k in preferred_keywords):
                score += 8
            score += min(6, len(re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", ln)))
            if idx < 25:
                score += 2
            weighted.append((score, ln))
        if weighted:
            weighted.sort(key=lambda x: x[0], reverse=True)
            subject = weighted[0][1]

    # Sender/vendor heuristic:
    # top-left often recipient; skip address-like and subject/doc lines and prefer company-like names
    company_tokens = ["gmbh", "ag", "ug", "kg", "ohg", "inc", "llc", "ltd", "corp", "s.a.", "sarl", "e.k."]
    candidates: list[str] = []
    for ln in lines[:30]:
        ll = ln.lower()
        if len(ln) < 3:
            continue
        if is_address_like(ln):
            continue
        if any(k in ll for k in ["rechnung", "invoice", "seite", "page", "betreff", "subject", "datum", "date"]):
            continue
        if re.fullmatch(r"[\d\W_]+", ln):
            continue
        candidates.append(ln)

    vendor = ""
    for ln in candidates:
        if any(tok in ln.lower() for tok in company_tokens):
            vendor = ln
            break
    if not vendor and candidates:
        vendor = candidates[0]

    return ParsedDocInfo(date=date, vendor=vendor, doc_type=doc_type, number=number, subject=subject)


def suggest_filename_from_text(text: str) -> str:
    info = parse_doc_info(text)
    parts = [
        _normalize_filename_part(info.date, max_len=16),
        _normalize_filename_part(info.doc_type, max_len=28),
        _normalize_filename_part(info.vendor, max_len=40),
        _normalize_filename_part(info.number, max_len=28),
    ]
    prioritized = [p for p in parts if p]
    if prioritized:
        return "_".join(prioritized)[:140] + ".pdf"

    fallback = _normalize_filename_part(info.subject, max_len=90)
    if fallback:
        return fallback + ".pdf"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:40]:
        if _looks_like_subject_line(ln):
            cleaned = _normalize_filename_part(ln, max_len=90)
            if cleaned:
                return cleaned + ".pdf"

    return "Dokument.pdf"


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
    label_expr = (
        r"gesamt(?:betrag)?|rechnungsbetrag|endbetrag|summe|zu\s+zahlen|zu\s+überweisen|"
        r"brutto(?:betrag)?|fälliger\s+betrag|offener\s+betrag|restbetrag|saldo|"
        r"zahlbar(?:er\s+betrag)?|total(?:\s+due)?|grand\s+total|amount\s+due|"
        r"amount\s+payable|balance\s+due"
    )

    keyword_pattern = re.compile(
        rf"(?i)\b(?P<label>{label_expr})\b[^\dA-Z]{{0,16}}(?:(?P<curr_before>{currency_expr})\s*)?(?P<amount>{amount_expr})\s*(?P<curr_after>{currency_expr})?"
    )
    fallback_patterns = [
        re.compile(rf"(?i)(?P<curr_before>{currency_expr})\s*(?P<amount>{amount_expr})\b"),
        re.compile(rf"(?i)(?P<amount>{amount_expr})\s*(?P<curr_after>{currency_expr})\b"),
    ]

    label_weights = {
        "grand total": 100,
        "amount due": 95,
        "amount payable": 95,
        "balance due": 95,
        "gesamtbetrag": 90,
        "endbetrag": 90,
        "rechnungsbetrag": 88,
        "zu zahlen": 86,
        "zu überweisen": 86,
        "falliger betrag": 84,
        "bruttobetrag": 82,
        "brutto": 80,
        "total": 76,
        "summe": 70,
        "saldo": 68,
        "offener betrag": 66,
        "restbetrag": 65,
    }

    best: tuple[int, int, str, str] | None = None
    for m in keyword_pattern.finditer(text):
        amount = _normalize_amount_token(m.group("amount"))
        if not amount:
            continue

        currency = _normalize_currency_token(m.groupdict().get("curr_before") or m.groupdict().get("curr_after") or "")
        raw_label = (m.group("label") or "").lower()
        normalized_label = unicodedata.normalize("NFKD", raw_label).encode("ascii", "ignore").decode("ascii")
        weight = label_weights.get(normalized_label, 60)

        candidate = (weight, m.start(), amount, currency)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    if best is not None:
        return best[2], best[3]

    for pat in fallback_patterns:
        m = pat.search(text)
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


def build_export_record(file_name: str, text: str) -> ExportRecord:
    info = parse_doc_info(text)
    amount, currency = extract_total_amount_info(text)
    excerpt = re.sub(r"\s+", " ", text).strip()[:220]
    return ExportRecord(
        datei=file_name,
        datum=info.date,
        typ=info.doc_type,
        absender=info.vendor,
        nummer=info.number,
        betreff=info.subject,
        betrag=amount,
        waehrung=currency,
        text_laenge=len(text),
        text_auszug=excerpt,
    )


def export_records_as_txt(records: list[ExportRecord]) -> str:
    parts: list[str] = []
    for rec in records:
        parts.append(
            "\n".join(
                [
                    f"Datei: {rec.datei}",
                    f"Datum: {rec.datum or '-'}",
                    f"Typ: {rec.typ or '-'}",
                    f"Absender: {rec.absender or '-'}",
                    f"Nummer: {rec.nummer or '-'}",
                    f"Betreff: {rec.betreff or '-'}",
                    f"Betrag: {(rec.betrag + ' ' + rec.waehrung).strip() or '-'}",
                    f"Textlänge: {rec.text_laenge}",
                    f"Auszug: {rec.text_auszug or '-'}",
                ]
            )
        )
    return "\n\n".join(parts)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Offline PDF Reader — MVP")
        self.resize(1220, 860)
        self.setAcceptDrops(True)

        self.pdf_path: Path | None = None
        self.doc: fitz.Document | None = None
        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations: dict[int, int] = {}
        self.learning_rules_path = Path(__file__).with_name("learning_rules.json")
        self.learning_rules = self._load_learning_rules()
        self._configure_tesseract_runtime()

        self.undo_stack: list[tuple[bytes, dict[int, int], int]] = []
        self.redo_stack: list[tuple[bytes, dict[int, int], int]] = []
        self.is_dirty = False

        self.preview = QLabel("Kein PDF geladen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(460)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidget(self.preview)
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.thumb_list = QListWidget()
        self.thumb_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.thumb_list.setFlow(QListWidget.Flow.TopToBottom)
        self.thumb_list.setMovement(QListWidget.Movement.Static)
        self.thumb_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.thumb_list.setIconSize(QSize(90, 130))
        self.thumb_list.setSpacing(6)
        self.thumb_list.setMinimumWidth(120)
        self.thumb_list.setMaximumWidth(520)
        self.thumb_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.thumb_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thumb_list.customContextMenuRequested.connect(self._show_thumbnail_context_menu)
        self.thumb_list.itemClicked.connect(self._on_thumbnail_clicked)

        self.extracted_text = ""
        self.text_dialog: QDialog | None = None
        self.text_output_view: QTextEdit | None = None

        self.suggested_name = QLineEdit()
        self.suggested_name.setPlaceholderText("Vorgeschlagener Dateiname")

        self.ocr_lang = "deu+eng"
        self.ocr_cancel_requested = False
        self.ocr_cache: dict[str, tuple[str, str | None, list[str]]] = {}
        self.ocr_cache_order: list[str] = []
        self.ocr_cache_max_entries = 80
        self.doc_revision = 0

        self.search_query = QLineEdit()
        self.search_query.setPlaceholderText("Suche in allen Seiten …")
        self.search_results_list = QListWidget()
        self.search_results_list.setMinimumHeight(140)
        self.search_results_list.setVisible(False)
        self.search_results_list.itemClicked.connect(self._on_search_result_clicked)
        self.search_hits: list[dict] = []
        self.current_search_hit = -1

        self.page_info = QLabel("Seite: -/- | Zoom: 100%")
        self.page_info.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.ocr_feedback = QLabel("OCR-Hinweise: -")
        self.ocr_feedback.setWordWrap(True)

        btn_open = QPushButton("Öffnen")
        btn_first = QPushButton("⏮")
        btn_prev = QPushButton("◀")
        btn_next = QPushButton("▶")
        btn_last = QPushButton("⏭")
        btn_zoom_out = QPushButton("−")
        btn_zoom_in = QPushButton("+")
        btn_zoom_reset = QPushButton("100%")
        btn_goto = QPushButton("#")
        btn_rotate_left = QPushButton("↺")
        btn_rotate_right = QPushButton("↻")
        btn_rotate_reset = QPushButton("⟲")
        self.btn_undo = QPushButton("↶")
        self.btn_redo = QPushButton("↷")
        btn_extract = QPushButton("OCR")
        btn_extract_all = QPushButton("Text alle")
        btn_ocr_all = QPushButton("OCR alle")
        btn_saveas = QPushButton("Speichern")
        btn_merge = QPushButton("Merge")
        btn_split = QPushButton("Extrakt")
        btn_reorder = QPushButton("Sortieren")
        btn_remove_empty = QPushButton("Leer entfernen")
        btn_search = QPushButton("Suchen")
        btn_hit_prev = QPushButton("Treffer ◀")
        btn_hit_next = QPushButton("Treffer ▶")
        self.search_counter = QLabel("Treffer: 0/0")
        self.btn_cancel_ocr = QPushButton("OCR stoppen")
        self.btn_cancel_ocr.setEnabled(False)

        for b in [btn_open, btn_first, btn_prev, btn_next, btn_last, btn_zoom_out, btn_zoom_in, btn_zoom_reset, btn_goto, btn_rotate_left, btn_rotate_right, btn_rotate_reset, self.btn_undo, self.btn_redo, btn_extract, btn_extract_all, btn_ocr_all, btn_saveas, btn_merge, btn_split, btn_reorder, btn_remove_empty, btn_search, btn_hit_prev, btn_hit_next, self.btn_cancel_ocr]:
            b.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_open.setToolTip("PDF öffnen")
        btn_first.setToolTip("Erste Seite")
        btn_prev.setToolTip("Vorherige Seite")
        btn_next.setToolTip("Nächste Seite")
        btn_last.setToolTip("Letzte Seite")
        btn_zoom_out.setToolTip("Zoom verkleinern")
        btn_zoom_in.setToolTip("Zoom vergrößern")
        btn_zoom_reset.setToolTip("Zoom auf 100%")
        btn_goto.setToolTip("Gehe zu Seite")
        btn_rotate_left.setToolTip("Nach links drehen")
        btn_rotate_right.setToolTip("Nach rechts drehen")
        btn_rotate_reset.setToolTip("Drehung zurücksetzen")
        self.btn_undo.setToolTip("Rückgängig (Ctrl+Z)")
        self.btn_redo.setToolTip("Wiederholen (Ctrl+Y)")
        btn_search.setToolTip("Text in allen Seiten suchen")
        btn_hit_prev.setToolTip("Vorherigen Treffer")
        btn_hit_next.setToolTip("Nächsten Treffer")
        self.btn_cancel_ocr.setToolTip("Laufenden OCR-Vorgang abbrechen")

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
        self.btn_undo.clicked.connect(self.undo_last_change)
        self.btn_redo.clicked.connect(self.redo_last_change)
        btn_extract.clicked.connect(self.extract_text_and_suggest)
        btn_extract_all.clicked.connect(self.extract_text_all_pages_and_suggest)
        btn_ocr_all.clicked.connect(self.ocr_all_pages_and_suggest)
        btn_saveas.clicked.connect(self.save_as_suggested)
        btn_merge.clicked.connect(self.merge_pdfs)
        btn_split.clicked.connect(self.extract_pages_to_new_pdf)
        btn_reorder.clicked.connect(self.reorder_pages_to_new_pdf)
        btn_remove_empty.clicked.connect(self.remove_empty_pages_to_new_pdf)
        btn_search.clicked.connect(self.open_search_and_run)
        self.search_query.returnPressed.connect(self.search_all_pages)
        btn_hit_prev.clicked.connect(self.prev_search_hit)
        btn_hit_next.clicked.connect(self.next_search_hit)
        self.btn_cancel_ocr.clicked.connect(self.cancel_ocr)

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
        toolbar_top.addSpacing(8)
        toolbar_top.addWidget(self.btn_undo)
        toolbar_top.addWidget(self.btn_redo)
        toolbar_top.addStretch(1)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Dateiname:"))
        name_row.addWidget(self.suggested_name)
        name_row.addStretch(1)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Suche:"))
        search_row.addWidget(self.search_query, 1)
        search_row.addWidget(btn_search)
        search_row.addWidget(btn_hit_prev)
        search_row.addWidget(btn_hit_next)
        search_row.addWidget(self.search_counter)

        ocr_row = QHBoxLayout()
        ocr_row.addWidget(self.ocr_feedback, 1)
        ocr_row.addWidget(self.btn_cancel_ocr)

        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(8)
        self.content_splitter.addWidget(self.thumb_list)
        self.content_splitter.addWidget(self.preview_scroll)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([220, 940])

        content_row = QHBoxLayout()
        content_row.addWidget(self.content_splitter, 1)

        layout = QVBoxLayout()
        layout.addLayout(name_row)
        layout.addLayout(toolbar_top)
        layout.addLayout(search_row)
        layout.addWidget(self.search_results_list)
        layout.addWidget(self.page_info)
        layout.addLayout(content_row, 1)
        layout.addLayout(ocr_row)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        menu_file = self.menuBar().addMenu("Datei")
        act_open = QAction("Öffnen", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self.open_pdf)
        menu_file.addAction(act_open)

        act_close_pdf = QAction("PDF schließen", self)
        act_close_pdf.setShortcut("Ctrl+W")
        act_close_pdf.triggered.connect(self.close_pdf)
        menu_file.addAction(act_close_pdf)

        menu_file.addSeparator()

        act_save_as = QAction("Speichern als …", self)
        act_save_as.setShortcut("Ctrl+S")
        act_save_as.triggered.connect(self.save_as_suggested)
        menu_file.addAction(act_save_as)

        menu_file.addSeparator()

        act_undo = QAction("Rückgängig", self)
        act_undo.setShortcut("Ctrl+Z")
        act_undo.triggered.connect(self.undo_last_change)
        menu_file.addAction(act_undo)

        act_redo = QAction("Wiederholen", self)
        act_redo.setShortcuts(["Ctrl+Y", "Ctrl+Shift+Z"])
        act_redo.triggered.connect(self.redo_last_change)
        menu_file.addAction(act_redo)

        menu_ocr = self.menuBar().addMenu("OCR & Text")
        act_extract_current = QAction("Aktuelle Seite extrahieren", self)
        act_extract_current.setShortcut("Ctrl+E")
        act_extract_current.triggered.connect(self.extract_text_and_suggest)
        menu_ocr.addAction(act_extract_current)

        act_extract = QAction("Alle Seiten extrahieren", self)
        act_extract.setShortcut("Ctrl+Shift+E")
        act_extract.triggered.connect(self.extract_text_all_pages_and_suggest)
        menu_ocr.addAction(act_extract)

        act_ocr_all = QAction("OCR alle Seiten", self)
        act_ocr_all.triggered.connect(self.ocr_all_pages_and_suggest)
        menu_ocr.addAction(act_ocr_all)

        act_ocr_lang = QAction("OCR-Sprache wählen …", self)
        act_ocr_lang.triggered.connect(self.choose_ocr_language)
        menu_ocr.addAction(act_ocr_lang)

        act_show_text = QAction("Extrahierten Text anzeigen", self)
        act_show_text.setShortcut("Ctrl+T")
        act_show_text.triggered.connect(self.show_extracted_text_window)
        menu_ocr.addAction(act_show_text)

        act_search = QAction("In allen Seiten suchen", self)
        act_search.setShortcut("Ctrl+F")
        act_search.triggered.connect(self.open_search)
        menu_ocr.addAction(act_search)

        menu_tools = self.menuBar().addMenu("PDF-Werkzeuge")
        act_split = QAction("Seiten extrahieren", self)
        act_split.triggered.connect(self.extract_pages_to_new_pdf)
        menu_tools.addAction(act_split)

        act_reorder = QAction("Seiten neu anordnen", self)
        act_reorder.triggered.connect(self.reorder_pages_to_new_pdf)
        menu_tools.addAction(act_reorder)

        act_merge = QAction("PDFs mergen", self)
        act_merge.triggered.connect(self.merge_pdfs)
        menu_tools.addAction(act_merge)

        act_split_chunks = QAction("PDF in Blöcke teilen …", self)
        act_split_chunks.triggered.connect(self.split_pdf_into_chunks)
        menu_tools.addAction(act_split_chunks)

        act_remove_empty = QAction("Leere Seiten entfernen", self)
        act_remove_empty.triggered.connect(self.remove_empty_pages_to_new_pdf)
        menu_tools.addAction(act_remove_empty)

        act_delete_pages = QAction("Ausgewählte Seiten löschen", self)
        act_delete_pages.setShortcut("Delete")
        act_delete_pages.triggered.connect(self.delete_selected_pages)
        menu_tools.addAction(act_delete_pages)

        menu_export = self.menuBar().addMenu("Export")
        act_export_current = QAction("Aktuelle Datei exportieren …", self)
        act_export_current.triggered.connect(self.export_current_file)
        menu_export.addAction(act_export_current)

        act_export_folder = QAction("Ordner aggregiert exportieren …", self)
        act_export_folder.triggered.connect(self.export_folder_aggregate)
        menu_export.addAction(act_export_folder)

        act_batch_rename = QAction("Ordner Batch-Rename …", self)
        act_batch_rename.triggered.connect(self.batch_rename_folder)
        menu_export.addAction(act_batch_rename)

        self.statusBar().showMessage("Bereit. Öffne ein PDF, um zu starten.")
        self._update_undo_redo_buttons()
        self._apply_styles()

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

    def _set_extracted_text(self, text: str) -> None:
        self.extracted_text = text
        if self.text_output_view is not None:
            self.text_output_view.setPlainText(text)

    def show_extracted_text_window(self) -> None:
        if self.text_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Extrahierter OCR/Text")
            dlg.resize(880, 620)
            lay = QVBoxLayout(dlg)
            view = QTextEdit()
            view.setReadOnly(True)
            view.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            view.setPlaceholderText("Noch kein Text extrahiert.")
            view.setPlainText(self.extracted_text or "")
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(self._show_ocr_text_context_menu)
            lay.addWidget(view)

            btn_pick_name = QPushButton("Markierung als Dateiname")
            btn_pick_name.clicked.connect(self.use_selected_text_as_filename)
            lay.addWidget(btn_pick_name)

            self.text_dialog = dlg
            self.text_output_view = view
        self.text_output_view.setPlainText(self.extracted_text or "")
        self.text_dialog.show()
        self.text_dialog.raise_()
        self.text_dialog.activateWindow()

    def _show_ocr_text_context_menu(self, pos) -> None:
        if self.text_output_view is None:
            return
        menu = self.text_output_view.createStandardContextMenu()
        menu.addSeparator()
        act_take_name = QAction("Als Dateiname übernehmen", self)
        act_take_name.triggered.connect(self.use_selected_text_as_filename)
        menu.addAction(act_take_name)
        menu.exec(self.text_output_view.mapToGlobal(pos))

    def use_selected_text_as_filename(self) -> None:
        if self.text_output_view is None:
            return
        cursor = self.text_output_view.textCursor()
        selected = (cursor.selectedText() or "").replace("\u2029", " ").strip()
        if not selected:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst eine Zeile oder einen Textbereich im OCR-Fenster markieren.")
            return

        candidate = sanitize_filename(selected)
        if not candidate or len(candidate) < 2:
            QMessageBox.information(self, "Hinweis", "Die Markierung ergibt keinen gültigen Dateinamen.")
            return

        if not candidate.lower().endswith(".pdf"):
            candidate = self._ensure_pdf_suffix(candidate)

        self.suggested_name.setText(candidate)
        self.statusBar().showMessage("Dateiname aus Markierung übernommen.")

    def _suggest_name_from_extracted_text_or_first_page(self, text: str) -> str:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        for ln in lines:
            if len(ln) < 4:
                continue
            if re.fullmatch(r"[\W_]+", ln):
                continue
            candidate = sanitize_filename(ln)
            if candidate and candidate.lower() != "dokument":
                return self._ensure_pdf_suffix(candidate)
        return self._suggest_name_from_first_page()

    def _set_dirty(self, dirty: bool) -> None:
        if dirty:
            self.doc_revision += 1
            self._clear_ocr_cache()
        self.is_dirty = dirty
        title = "Offline PDF Reader — MVP"
        if self.pdf_path:
            title += f" | {self.pdf_path.name}"
        if self.is_dirty:
            title += " *"
        self.setWindowTitle(title)

    def _snapshot_state(self) -> tuple[bytes, dict[int, int], int] | None:
        if not self.doc:
            return None
        try:
            payload = self.doc.tobytes(garbage=3, deflate=True)
            return payload, dict(self.page_rotations), self.current_page
        except Exception:
            return None

    def _restore_state(self, snap: tuple[bytes, dict[int, int], int]) -> bool:
        try:
            payload, rotations, page_idx = snap
            new_doc = fitz.open(stream=payload, filetype="pdf")
            old = self.doc
            self.doc = new_doc
            if old is not None:
                old.close()
            self.page_rotations = dict(rotations)
            self.current_page = max(0, min(page_idx, len(self.doc) - 1)) if len(self.doc) else 0
            self._refresh_thumbnails()
            self.render_current_page()
            return True
        except Exception:
            return False

    def _update_undo_redo_buttons(self) -> None:
        if hasattr(self, "btn_undo") and self.btn_undo is not None:
            self.btn_undo.setEnabled(bool(self.undo_stack))
        if hasattr(self, "btn_redo") and self.btn_redo is not None:
            self.btn_redo.setEnabled(bool(self.redo_stack))

    def _push_undo_state(self) -> None:
        snap = self._snapshot_state()
        if snap is None:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > 30:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self._update_undo_redo_buttons()

    def undo_last_change(self) -> None:
        if not self.undo_stack:
            self.statusBar().showMessage("Nichts zum Rückgängig machen.")
            self._update_undo_redo_buttons()
            return
        current = self._snapshot_state()
        snap = self.undo_stack.pop()
        if current is not None:
            self.redo_stack.append(current)
        if self._restore_state(snap):
            self._set_dirty(True)
            self.statusBar().showMessage("Änderung rückgängig gemacht.")
        self._update_undo_redo_buttons()

    def redo_last_change(self) -> None:
        if not self.redo_stack:
            self.statusBar().showMessage("Nichts zum Wiederholen.")
            self._update_undo_redo_buttons()
            return
        current = self._snapshot_state()
        snap = self.redo_stack.pop()
        if current is not None:
            self.undo_stack.append(current)
        if self._restore_state(snap):
            self._set_dirty(True)
            self.statusBar().showMessage("Änderung wiederholt.")
        self._update_undo_redo_buttons()

    def _refresh_thumbnails(self) -> None:
        self.thumb_list.blockSignals(True)
        self.thumb_list.clear()
        if self.doc:
            for i in range(len(self.doc)):
                page = self.doc[i]
                rotation = self.page_rotations.get(i, 0)
                pix = page.get_pixmap(matrix=fitz.Matrix(0.18, 0.18).prerotate(rotation), alpha=False)
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                item = QListWidgetItem(QIcon(QPixmap.fromImage(img)), f"{i + 1}")
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.thumb_list.addItem(item)
        self.thumb_list.blockSignals(False)

    def _sync_thumbnail_selection(self) -> None:
        if not self.doc:
            return
        if 0 <= self.current_page < self.thumb_list.count():
            self.thumb_list.blockSignals(True)
            self.thumb_list.setCurrentRow(self.current_page)
            self.thumb_list.scrollToItem(self.thumb_list.item(self.current_page))
            self.thumb_list.blockSignals(False)

    def _on_thumbnail_clicked(self, item: QListWidgetItem) -> None:
        idx = int(item.data(Qt.ItemDataRole.UserRole))
        if self.doc and 0 <= idx < len(self.doc):
            self.current_page = idx
            self.render_current_page()

    def _show_thumbnail_context_menu(self, pos) -> None:
        item = self.thumb_list.itemAt(pos)
        if item and not item.isSelected():
            self.thumb_list.setCurrentItem(item)
        menu = self.thumb_list.createStandardContextMenu()
        delete_action = QAction("Ausgewählte Seite(n) löschen", self)
        delete_action.triggered.connect(self.delete_selected_pages)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.exec(self.thumb_list.mapToGlobal(pos))

    def delete_selected_pages(self) -> None:
        if not self.doc or len(self.doc) == 0:
            return
        selected = sorted({int(it.data(Qt.ItemDataRole.UserRole)) for it in self.thumb_list.selectedItems()})
        if not selected:
            selected = [self.current_page]

        if len(selected) >= len(self.doc):
            QMessageBox.warning(self, "Nicht möglich", "Mindestens eine Seite muss erhalten bleiben.")
            return

        if len(selected) > 1:
            pages_preview = ", ".join(str(p + 1) for p in selected[:12])
            if len(selected) > 12:
                pages_preview += ", …"
            choice = QMessageBox.question(
                self,
                "Mehrere Seiten löschen",
                f"{len(selected)} Seiten wirklich löschen?\n\nSeiten: {pages_preview}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return

        self._push_undo_state()
        selected_set = set(selected)
        old_total = len(self.doc)
        for idx in reversed(selected):
            self.doc.delete_page(idx)

        # rotations remap
        mapping: dict[int, int] = {}
        new_idx = 0
        for old_idx in range(old_total):
            if old_idx in selected_set:
                continue
            mapping[old_idx] = new_idx
            new_idx += 1
        self.page_rotations = {
            mapping[old_idx]: rot
            for old_idx, rot in self.page_rotations.items()
            if old_idx in mapping and rot % 360 != 0
        }

        removed_before = sum(1 for s in selected if s < self.current_page)
        if self.current_page in selected_set:
            self.current_page = max(0, self.current_page - removed_before)
        else:
            self.current_page = max(0, self.current_page - removed_before)
        if self.doc:
            self.current_page = min(self.current_page, len(self.doc) - 1)

        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self._update_search_counter()
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()
        self.statusBar().showMessage(f"{len(selected)} Seite(n) gelöscht (noch nicht gespeichert)")

    def open_search(self) -> None:
        self.search_results_list.setVisible(True)
        self.search_query.setFocus()
        self.search_query.selectAll()

    def open_search_and_run(self) -> None:
        self.search_results_list.setVisible(True)
        self.search_all_pages()

    def search_all_pages(self) -> None:
        if not self.doc:
            return
        self.search_results_list.setVisible(True)
        query = self.search_query.text().strip()
        if len(query) < 2:
            self.search_hits = []
            self.current_search_hit = -1
            self.search_results_list.clear()
            self._update_search_counter()
            QMessageBox.information(self, "Suche", "Bitte mindestens 2 Zeichen eingeben.")
            return

        self.search_hits = []
        self.search_results_list.clear()
        needle = query.casefold()

        for idx in range(len(self.doc)):
            text = self.doc[idx].get_text("text").strip()
            if len(text) < 20:
                rotation = self.page_rotations.get(idx, 0)
                ocr_text, _, _ = self._ocr_page_with_retry_cached(idx, rotation, retries=1)
                if ocr_text:
                    text = f"{text}\n{ocr_text}".strip()

            for line in text.splitlines():
                if needle in line.casefold():
                    hit = {"page": idx, "snippet": line.strip()[:180]}
                    self.search_hits.append(hit)

        for hit_idx, hit in enumerate(self.search_hits, start=1):
            item = QListWidgetItem(f"{hit_idx}. S.{hit['page'] + 1}: {hit['snippet']}")
            item.setData(Qt.ItemDataRole.UserRole, hit_idx - 1)
            self.search_results_list.addItem(item)

        self.current_search_hit = 0 if self.search_hits else -1
        self._update_search_counter()
        if self.search_hits:
            self.jump_to_search_hit(0)
        else:
            self.statusBar().showMessage("Keine Treffer gefunden.")

    def _update_search_counter(self) -> None:
        total = len(self.search_hits)
        current = self.current_search_hit + 1 if 0 <= self.current_search_hit < total else 0
        self.search_counter.setText(f"Treffer: {current}/{total}")

    def _on_search_result_clicked(self, item: QListWidgetItem) -> None:
        hit_idx = int(item.data(Qt.ItemDataRole.UserRole))
        self.jump_to_search_hit(hit_idx)

    def jump_to_search_hit(self, hit_idx: int) -> None:
        if not (0 <= hit_idx < len(self.search_hits)):
            return
        self.current_search_hit = hit_idx
        hit = self.search_hits[hit_idx]
        self.current_page = hit["page"]
        self.render_current_page()
        self.search_results_list.blockSignals(True)
        self.search_results_list.setCurrentRow(hit_idx)
        self.search_results_list.blockSignals(False)
        self._update_search_counter()

    def next_search_hit(self) -> None:
        if not self.search_hits:
            return
        self.jump_to_search_hit((self.current_search_hit + 1) % len(self.search_hits))

    def prev_search_hit(self) -> None:
        if not self.search_hits:
            return
        self.jump_to_search_hit((self.current_search_hit - 1) % len(self.search_hits))

    def cancel_ocr(self) -> None:
        self.ocr_cancel_requested = True
        self.statusBar().showMessage("OCR-Abbruch angefordert …")

    def _set_ocr_running(self, running: bool) -> None:
        self.btn_cancel_ocr.setEnabled(running)
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

    def _ocr_cache_get(self, key: str) -> tuple[str, str | None, list[str]] | None:
        value = self.ocr_cache.get(key)
        if value is None:
            return None
        if key in self.ocr_cache_order:
            self.ocr_cache_order.remove(key)
        self.ocr_cache_order.append(key)
        return value

    def _ocr_cache_put(self, key: str, value: tuple[str, str | None, list[str]]) -> None:
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
    ) -> tuple[str, str | None, list[str]]:
        if not self.doc or not (0 <= page_index < len(self.doc)):
            return "", "Ungültige Seite", []

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

    def _ocr_with_retry(self, img: Image.Image, retries: int = 1) -> tuple[str, str | None, list[str]]:
        last_err: str | None = None
        for _ in range(retries + 1):
            text, err, tokens = self._ocr_image_with_confidence(img, show_error=False)
            if not err:
                return text, None, tokens
            last_err = err
        return "", last_err, []

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
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.doc_revision = 0
        self._clear_ocr_cache()
        self.preview.setText("Kein PDF geladen")
        self.page_info.setText("Seite: -/- | Zoom: 100%")
        self._set_extracted_text("")
        self.suggested_name.clear()
        self.ocr_feedback.setText("OCR-Hinweise: -")
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
            choice = QMessageBox.question(
                self,
                "Ungespeicherte Änderungen",
                "Es gibt ungespeicherte Änderungen. Wirklich beenden?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
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

        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.doc_revision = 0
        self._clear_ocr_cache()
        self._refresh_thumbnails()
        self.render_current_page()
        self._set_extracted_text("")
        self.suggested_name.clear()
        self.ocr_feedback.setText("OCR-Hinweise: -")
        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self._update_search_counter()
        self._set_dirty(False)
        self._update_undo_redo_buttons()
        self.statusBar().showMessage(f"Geladen: {self.pdf_path.name} ({len(self.doc)} Seiten)")

    def open_pdf(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "PDF auswählen", "", "PDF files (*.pdf)")
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
                    rects = page.search_for(self.search_query.text().strip())
                    if rects:
                        painter = QPainter(qpix)
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                        pen = QPen(QColor(255, 196, 0))
                        pen.setWidth(3)
                        painter.setPen(pen)
                        for r in rects[:6]:
                            box = self._map_search_rect_to_view(
                                rect=r,
                                page_width=float(page.rect.width),
                                page_height=float(page.rect.height),
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

        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QTextEdit)):
            super().keyPressEvent(event)
            return

        if key == Qt.Key.Key_Delete and self.thumb_list.hasFocus():
            self.delete_selected_pages()
            return
        if key == Qt.Key.Key_F and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.search_results_list.setVisible(True)
            self.search_query.setFocus()
            self.search_query.selectAll()
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

        if len(text) < 40:
            # OCR fallback on current page image (respect UI rotation for better OCR)
            rotation = self.page_rotations.get(self.current_page, 0)
            text, _, low_conf_tokens = self._ocr_page_with_retry_cached(self.current_page, rotation, retries=1)

        if not text:
            text = "(Kein Text erkannt)"

        text = self._apply_learning_rules(text)
        self._set_extracted_text(text)
        self.show_extracted_text_window()
        self.suggested_name.setText(self._suggest_name_from_extracted_text_or_first_page(text))
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

        self._set_ocr_running(True)
        try:
            for idx in range(total):
                progress.setValue(idx)
                progress.setLabelText(f"Seite {idx + 1}/{total} wird verarbeitet …")
                QApplication.processEvents()
                if progress.wasCanceled() or self.ocr_cancel_requested:
                    QMessageBox.information(self, "Abgebrochen", "Extraktion wurde abgebrochen.")
                    return

                page = self.doc[idx]
                text = page.get_text("text").strip()

                if len(text) < 40:
                    rotation = self.page_rotations.get(idx, 0)
                    text, ocr_error, low_conf_tokens = self._ocr_page_with_retry_cached(idx, rotation, retries=1)
                    all_low_conf_tokens.extend(low_conf_tokens)
                    if ocr_error:
                        ocr_failed_pages.append(idx + 1)
                        if not ocr_error_preview:
                            ocr_error_preview = ocr_error
                        text = ""

                if text:
                    all_text_parts.append(text)
        finally:
            self._set_ocr_running(False)

        progress.setValue(total)

        combined_text = "\n\n".join(all_text_parts).strip() or "(Kein Text erkannt)"
        combined_text = self._apply_learning_rules(combined_text)
        self._set_extracted_text(combined_text)
        self.show_extracted_text_window()
        self.suggested_name.setText(self._suggest_name_from_extracted_text_or_first_page(combined_text))
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

    def ocr_all_pages_and_suggest(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        total = len(self.doc)
        if total == 0:
            QMessageBox.information(self, "Hinweis", "Das PDF enthält keine Seiten.")
            return

        progress = QProgressDialog("Führe OCR auf allen Seiten aus …", "Abbrechen", 0, total, self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        all_text_parts: list[str] = []
        all_low_conf_tokens: list[str] = []
        ocr_failed_pages: list[int] = []
        ocr_error_preview: str = ""

        self._set_ocr_running(True)
        try:
            for idx in range(total):
                progress.setValue(idx)
                progress.setLabelText(f"OCR Seite {idx + 1}/{total} …")
                QApplication.processEvents()
                if progress.wasCanceled() or self.ocr_cancel_requested:
                    QMessageBox.information(self, "Abgebrochen", "OCR wurde abgebrochen.")
                    return

                rotation = self.page_rotations.get(idx, 0)
                text, ocr_error, low_conf_tokens = self._ocr_page_with_retry_cached(idx, rotation, retries=1)
                all_low_conf_tokens.extend(low_conf_tokens)
                if ocr_error:
                    ocr_failed_pages.append(idx + 1)
                    if not ocr_error_preview:
                        ocr_error_preview = ocr_error
                    continue

                text = text.strip()
                if text:
                    all_text_parts.append(text)
        finally:
            self._set_ocr_running(False)

        progress.setValue(total)

        combined_text = "\n\n".join(all_text_parts).strip() or "(Kein Text erkannt)"
        combined_text = self._apply_learning_rules(combined_text)
        self._set_extracted_text(combined_text)
        self.show_extracted_text_window()
        self.suggested_name.setText(self._suggest_name_from_extracted_text_or_first_page(combined_text))
        self.ocr_feedback.setText(self._build_ocr_feedback(combined_text, all_low_conf_tokens))
        self.statusBar().showMessage(f"OCR für {total} Seiten abgeschlossen.")

        if ocr_failed_pages:
            pages = ", ".join(str(p) for p in ocr_failed_pages[:10])
            if len(ocr_failed_pages) > 10:
                pages += ", …"
            QMessageBox.warning(
                self,
                "OCR teilweise fehlgeschlagen",
                "OCR wurde fortgesetzt, aber auf einigen Seiten ist ein Fehler aufgetreten."
                f"\n\nSeiten: {pages}"
                f"\nFehleranzahl: {len(ocr_failed_pages)}"
                f"\n\nErster Fehler:\n{ocr_error_preview}",
            )

    def _is_page_likely_empty(self, page: fitz.Page) -> bool:
        text = page.get_text("text")
        if re.search(r"\w", text):
            return False

        pix = page.get_pixmap(matrix=fitz.Matrix(0.7, 0.7), colorspace=fitz.csGRAY, alpha=False)
        if not pix.samples:
            return True

        samples = pix.samples
        dark_pixels = sum(1 for val in samples if val < 245)
        dark_ratio = dark_pixels / len(samples)

        return dark_ratio < 0.0025

    def remove_empty_pages_to_new_pdf(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        total = len(self.doc)
        if total == 0:
            QMessageBox.information(self, "Hinweis", "Das PDF enthält keine Seiten.")
            return

        progress = QProgressDialog("Prüfe Seiten auf Leere …", "Abbrechen", 0, total, self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        keep_indices: list[int] = []
        removed_pages: list[int] = []

        for idx in range(total):
            progress.setValue(idx)
            progress.setLabelText(f"Seite {idx + 1}/{total} wird analysiert …")
            QApplication.processEvents()
            if progress.wasCanceled():
                QMessageBox.information(self, "Abgebrochen", "Analyse wurde abgebrochen.")
                return

            page = self.doc[idx]
            if self._is_page_likely_empty(page):
                removed_pages.append(idx + 1)
            else:
                keep_indices.append(idx)

        progress.setValue(total)

        if not removed_pages:
            QMessageBox.information(self, "Fertig", "Keine leeren Seiten gefunden.")
            return

        if not keep_indices:
            QMessageBox.warning(self, "Hinweis", "Alle Seiten wurden als leer erkannt. Das Dokument bleibt unverändert.")
            return

        try:
            self._push_undo_state()
            # delete in reverse order so original indices remain valid during deletion
            for idx in reversed([p - 1 for p in removed_pages]):
                self.doc.delete_page(idx)

            # remap rotations to new page indices
            old_rotations = dict(self.page_rotations)
            mapping = {old_idx: new_idx for new_idx, old_idx in enumerate(keep_indices)}
            self.page_rotations = {
                mapping[old_idx]: rot
                for old_idx, rot in old_rotations.items()
                if old_idx in mapping and rot % 360 != 0
            }

            if self.current_page >= len(self.doc):
                self.current_page = max(0, len(self.doc) - 1)

            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()

            preview = ", ".join(str(p) for p in removed_pages[:12])
            if len(removed_pages) > 12:
                preview += ", …"
            QMessageBox.information(
                self,
                "Erfolg",
                f"Leere Seiten wurden im aktuellen Dokument entfernt.\n\n"
                f"Entfernte Seiten: {len(removed_pages)}\n"
                f"Seiten: {preview}\n\n"
                "Wenn alles passt, kannst du anschließend über 'Speichern als …' sichern.",
            )
            self.statusBar().showMessage(f"Leere Seiten entfernt: {len(removed_pages)} (nicht gespeichert)")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Leere Seiten konnten nicht entfernt werden:\n{e}")

    def _suggest_name_from_first_page(self) -> str:
        if not self.doc or len(self.doc) == 0:
            return "Dokument.pdf"

        page = self.doc[0]

        # Prefer content from lower 2/3 of the first page (skip letterhead/address zone at top)
        try:
            h = float(page.rect.height)
            cutoff = h / 3.0
            blocks = page.get_text("blocks") or []
            lower_text_parts: list[str] = []
            for blk in blocks:
                if len(blk) < 5:
                    continue
                x0, y0, x1, y1, txt = blk[:5]
                if y0 < cutoff:
                    continue
                t = (txt or "").strip()
                if t:
                    lower_text_parts.append(t)
            lower_text = "\n".join(lower_text_parts).strip()
        except Exception:
            lower_text = ""

        if lower_text:
            suggestion = suggest_filename_from_text(lower_text)
            if suggestion != "Dokument.pdf":
                return suggestion

        native_text = page.get_text("text").strip()
        suggestion = suggest_filename_from_text(native_text)
        if suggestion != "Dokument.pdf":
            return suggestion

        # Fallback to OCR for scanned/low-quality first pages
        rotation = self.page_rotations.get(0, 0)
        ocr_text, _, _ = self._ocr_page_with_retry_cached(0, rotation, retries=1)
        if ocr_text:
            suggestion = suggest_filename_from_text(ocr_text)
            if suggestion != "Dokument.pdf":
                return suggestion

        return suggestion

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
            self.ocr_lang = normalized
        else:
            self.ocr_lang = dict(options)[choice]

        self._clear_ocr_cache()
        self.statusBar().showMessage(f"OCR-Sprache gesetzt: {self._ocr_lang()}", 4000)

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

    def _ocr_lang(self) -> str:
        normalized = self._normalize_ocr_language_code(self.ocr_lang)
        return normalized or "deu+eng"

    def _prepare_image_for_ocr(self, img: Image.Image) -> Image.Image:
        prepared = img.convert("L")
        prepared = ImageOps.autocontrast(prepared)
        width, height = prepared.size
        upscale = 1.5
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        prepared = prepared.resize((max(1, int(width * upscale)), max(1, int(height * upscale))), resampling)
        return prepared

    def _postprocess_ocr_text(self, text: str) -> str:
        out = text
        if "deu" in self._ocr_lang():
            # Frequently observed confusions in German OCR runs.
            out = re.sub(r"(?<=\w)é(?=\w)", "ö", out)
            out = re.sub(r"(?i)\bfiir\b", "für", out)
            out = re.sub(r"(?i)\biiber\b", "über", out)
        return out

    def _ocr_image(self, img: Image.Image, show_error: bool = True) -> tuple[str, str | None]:
        lang = self._ocr_lang()
        prepared = self._prepare_image_for_ocr(img)
        config = "--oem 1 --psm 6"
        try:
            raw = pytesseract.image_to_string(prepared, lang=lang, config=config).strip()
            return self._postprocess_ocr_text(raw), None
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
                    raw = pytesseract.image_to_string(prepared, lang="deu+eng", config=config).strip()
                    return self._postprocess_ocr_text(raw), None
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
            source_path = self.pdf_path.resolve()
            dest_path = Path(out_path).resolve()
            same_target = source_path == dest_path

            out_doc = fitz.open()
            try:
                out_doc.insert_pdf(self.doc)
                for idx, rot in self.page_rotations.items():
                    if 0 <= idx < len(out_doc) and rot % 360 != 0:
                        out_doc[idx].set_rotation(rot % 360)

                if same_target:
                    tmp_out = dest_path.with_name(f"{dest_path.stem}.tmp{dest_path.suffix}")
                    out_doc.save(str(tmp_out))

                    # Replace can fail on Windows while the original file is still open.
                    self._close_open_document()
                    tmp_out.replace(dest_path)
                    self.doc = fitz.open(str(dest_path))
                    # Rotations are now baked into the PDF. Keep in-memory overrides clean
                    # to avoid applying them a second time in the preview.
                    self.page_rotations.clear()
                else:
                    out_doc.save(str(dest_path))
            finally:
                out_doc.close()

            self._refresh_thumbnails()
            self.render_current_page()
            self._set_dirty(False)
            QMessageBox.information(self, "Gespeichert", f"Datei gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Gespeichert: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht speichern:\n{e}")

    def export_current_file(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        text = self.extracted_text.strip()
        if not text:
            text = "\n\n".join(self.doc[i].get_text("text") for i in range(len(self.doc))).strip()
        record = build_export_record(self.pdf_path.name, text)

        fmt, ok = QInputDialog.getItem(self, "Exportformat", "Format:", ["TXT", "JSON", "CSV"], 0, False)
        if not ok:
            return
        suffix = fmt.lower()
        out_path, _ = QFileDialog.getSaveFileName(self, "Export speichern", str(self.pdf_path.with_suffix(f".{suffix}")))
        if not out_path:
            return

        data = [record]
        self._write_export_data(Path(out_path), fmt, data)
        self.statusBar().showMessage(f"Export erstellt: {Path(out_path).name}")

    def export_folder_aggregate(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "PDF-Ordner auswählen")
        if not folder:
            return
        pdfs = sorted(Path(folder).glob("*.pdf"))
        if not pdfs:
            QMessageBox.information(self, "Hinweis", "Keine PDFs im Ordner gefunden.")
            return

        records: list[ExportRecord] = []
        for pdf in pdfs:
            try:
                with fitz.open(str(pdf)) as doc:
                    text = self._extract_text_with_ocr_fallback(doc)
                records.append(build_export_record(pdf.name, text))
            except Exception:
                records.append(build_export_record(pdf.name, ""))

        fmt, ok = QInputDialog.getItem(self, "Exportformat", "Format:", ["TXT", "JSON", "CSV"], 1, False)
        if not ok:
            return
        out_path, _ = QFileDialog.getSaveFileName(self, "Aggregat speichern", str(Path(folder) / f"export_gesamt.{fmt.lower()}"))
        if not out_path:
            return
        self._write_export_data(Path(out_path), fmt, records)
        self.statusBar().showMessage(f"Ordner-Export erstellt: {Path(out_path).name}")

    def _extract_text_with_ocr_fallback(self, doc: fitz.Document) -> str:
        parts: list[str] = []
        for idx in range(len(doc)):
            page = doc[idx]
            text = page.get_text("text").strip()
            if len(text) < 40:
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                ocr_text, _ = self._ocr_image(img, show_error=False)
                if ocr_text:
                    text = f"{text}\n{ocr_text}".strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts).strip()

    def _write_export_data(self, out_path: Path, fmt: str, records: list[ExportRecord]) -> None:
        rows = [asdict(r) for r in records]
        if fmt == "TXT":
            out_path.write_text(export_records_as_txt(records), encoding="utf-8")
            return
        if fmt == "JSON":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else list(asdict(ExportRecord("", "", "", "", "", "", "", "", 0, "")).keys()))
            writer.writeheader()
            writer.writerows(rows)

    def batch_rename_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Ordner für Batch-Rename auswählen")
        if not folder:
            return
        folder_path = Path(folder)
        files = sorted(folder_path.glob("*.pdf"))
        if not files:
            QMessageBox.information(self, "Hinweis", "Keine PDFs gefunden.")
            return

        proposals: list[tuple[Path, Path]] = []
        used_targets: set[str] = set()
        for src in files:
            try:
                with fitz.open(str(src)) as doc:
                    first_page_text = doc[0].get_text("text") if len(doc) else ""
                base_name = suggest_filename_from_text(first_page_text)
            except Exception:
                base_name = "Dokument.pdf"
            stem = Path(base_name).stem
            candidate = f"{stem}.pdf"
            n = 1
            while candidate.lower() in used_targets or (folder_path / candidate).exists() and (folder_path / candidate) != src:
                candidate = f"{stem}({n}).pdf"
                n += 1
            used_targets.add(candidate.lower())
            proposals.append((src, folder_path / candidate))

        preview = "\n".join([f"{src.name} -> {dst.name}" for src, dst in proposals[:30]])
        if len(proposals) > 30:
            preview += "\n…"

        confirm = QMessageBox.question(
            self,
            "Batch-Rename Vorschau (Dry-Run)",
            "Vorschau (es wurde noch nichts umbenannt):\n\n"
            f"{preview}\n\n"
            "Jetzt wirklich umbenennen?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        renamed = 0
        for src, dst in proposals:
            if src == dst:
                continue
            src.rename(dst)
            renamed += 1
        QMessageBox.information(self, "Fertig", f"Batch-Rename abgeschlossen: {renamed} Datei(en) umbenannt.")

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

    def split_pdf_into_chunks(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        total_pages = len(self.doc)
        if total_pages < 2:
            QMessageBox.information(self, "Hinweis", "Das PDF hat nur eine Seite und muss nicht geteilt werden.")
            return

        pages_per_chunk, ok = QInputDialog.getInt(
            self,
            "PDF in Blöcke teilen",
            f"Nach wie vielen Seiten teilen? (1-{total_pages})",
            5,
            1,
            total_pages,
            1,
        )
        if not ok:
            return

        parent_folder = QFileDialog.getExistingDirectory(self, "Zielordner für den Ausgabeordner auswählen")
        if not parent_folder:
            return

        base_name_raw = self.pdf_path.stem if self.pdf_path and self.pdf_path.stem else "PDFs geteilt"
        base_name = sanitize_filename(base_name_raw) or "PDFs geteilt"
        folder_name = sanitize_filename(f"{base_name} geteilt") or "PDFs geteilt"
        out_dir = Path(parent_folder) / folder_name
        suffix = 2
        while out_dir.exists():
            out_dir = Path(parent_folder) / f"{folder_name} ({suffix})"
            suffix += 1
        out_dir.mkdir(parents=True, exist_ok=False)

        created_files: list[Path] = []
        try:
            part_no = 1
            for start in range(0, total_pages, pages_per_chunk):
                end = min(start + pages_per_chunk, total_pages)
                part_doc = fitz.open()
                try:
                    for idx in range(start, end):
                        part_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                        rot = self.page_rotations.get(idx, 0) % 360
                        if rot:
                            part_doc[-1].set_rotation(rot)
                    part_name_raw = f"{base_name or 'PDF'}_Teil_{part_no:03d}_S{start+1:03d}-S{end:03d}.pdf"
                    part_name = sanitize_filename(part_name_raw)
                    if not part_name.lower().endswith(".pdf"):
                        part_name = self._ensure_pdf_suffix(part_name)
                    part_path = out_dir / part_name
                    part_doc.save(str(part_path))
                    created_files.append(part_path)
                    part_no += 1
                finally:
                    part_doc.close()

            QMessageBox.information(
                self,
                "Erfolg",
                f"PDF wurde in {len(created_files)} Datei(en) geteilt:\n{out_dir}",
            )
            self.statusBar().showMessage(f"PDF geteilt: {len(created_files)} Datei(en) in {out_dir.name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Teilen fehlgeschlagen:\n{e}")

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

        dlg = ReorderPagesDialog(self.doc, self.page_rotations, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        ordered_pages = dlg.ordered_pages()
        if not ordered_pages:
            QMessageBox.warning(self, "Ungültig", "Keine gültige Reihenfolge erkannt.")
            return

        if ordered_pages == list(range(len(self.doc))):
            QMessageBox.information(self, "Hinweis", "Die Reihenfolge ist unverändert.")
            return

        out_doc = fitz.open()
        try:
            self._push_undo_state()
            for idx in ordered_pages:
                out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                rot = self.page_rotations.get(idx, 0) % 360
                if rot:
                    out_doc[-1].set_rotation(rot)

            old_doc = self.doc
            self.doc = out_doc
            out_doc = None
            old_doc.close()

            self.page_rotations = {
                new_idx: (self.page_rotations.get(old_idx, 0) % 360)
                for new_idx, old_idx in enumerate(ordered_pages)
                if self.page_rotations.get(old_idx, 0) % 360 != 0
            }

            self.current_page = 0
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()

            QMessageBox.information(
                self,
                "Erfolg",
                "Neue Seitenreihenfolge wurde im aktuellen Dokument übernommen.\n\n"
                "Wenn alles passt, speichere anschließend über 'Speichern als …'.",
            )
            self.statusBar().showMessage("Seitenreihenfolge übernommen (nicht gespeichert)")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Neu-Anordnung fehlgeschlagen:\n{e}")
        finally:
            if out_doc is not None:
                out_doc.close()

    def _parse_page_spec(self, spec: str, total_pages: int) -> list[int]:
        ordered: list[int] = []
        seen: set[int] = set()

        def add_page(idx: int) -> None:
            if not (0 <= idx < total_pages):
                return
            if idx not in seen:
                seen.add(idx)
                ordered.append(idx)

        def parse_single(token: str) -> int | None:
            tk = token.strip().lower()
            alias_with_offset = re.fullmatch(
                r"(first|start|begin|last|end|current|cur|here)\s*([+-]\s*\d+)?",
                tk,
            )
            if alias_with_offset:
                base_name, offset_raw = alias_with_offset.groups()
                if base_name in {"first", "start", "begin"}:
                    base = 1
                elif base_name in {"last", "end"}:
                    base = total_pages
                else:
                    base = self.current_page + 1
                if offset_raw:
                    base += int(offset_raw.replace(" ", ""))
                return base
            if tk.isdigit():
                return int(tk)
            # Relative shorthand to current page, e.g. "+2" or "-1".
            if re.fullmatch(r"[+-]\s*\d+", tk):
                return (self.current_page + 1) + int(tk.replace(" ", ""))
            return None

        def parse_bound(raw: str, default: int) -> int:
            page = parse_single(raw)
            if page is None:
                return default
            return page

        def parse_range_with_step(token: str) -> tuple[str, str, int] | None:
            body = token
            step = 1
            if "/" in token:
                body, step_raw = token.rsplit("/", 1)
                step_raw = step_raw.strip()
                if not step_raw.isdigit() or int(step_raw) <= 0:
                    return None
                step = int(step_raw)

            # Range bounds can themselves contain signed alias offsets like
            # "last-1" or "current+2". Use a bound-aware regex instead of a
            # naive split("-", 1), otherwise "last-1-last" is parsed wrongly.
            bound = r"(?:\d+|first|start|begin|last|end|current|cur|here)(?:\s*[+-]\s*\d+)?"
            m = re.fullmatch(rf"\s*({bound})\s*-\s*({bound})\s*", body)
            if not m:
                return None
            start_raw, end_raw = m.groups()
            return start_raw, end_raw, step

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
            if token in {"reverse", "rev"}:
                for idx in reversed(range(total_pages)):
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

            page = parse_single(token)
            if page is not None:
                if 1 <= page <= total_pages:
                    add_page(page - 1)
                continue

            range_info = parse_range_with_step(token)
            if range_info is not None:
                a, b, step = range_info
                start = parse_bound(a, 1)
                end = parse_bound(b, total_pages)
                signed_step = step if start <= end else -step
                for p in range(start, end + signed_step, signed_step):
                    if 1 <= p <= total_pages:
                        add_page(p - 1)
        return ordered

    def _parse_order_spec(self, spec: str, total_pages: int) -> list[int]:
        ordered: list[int] = []

        def parse_single(token: str) -> int | None:
            tk = token.strip().lower()
            alias_with_offset = re.fullmatch(
                r"(first|start|begin|last|end|current|cur|here)\s*([+-]\s*\d+)?",
                tk,
            )
            if alias_with_offset:
                base_name, offset_raw = alias_with_offset.groups()
                if base_name in {"first", "start", "begin"}:
                    base = 1
                elif base_name in {"last", "end"}:
                    base = total_pages
                else:
                    base = self.current_page + 1
                if offset_raw:
                    base += int(offset_raw.replace(" ", ""))
                return base
            if tk.isdigit():
                return int(tk)
            # Relative shorthand to current page, e.g. "+2" or "-1".
            if re.fullmatch(r"[+-]\s*\d+", tk):
                return (self.current_page + 1) + int(tk.replace(" ", ""))
            return None

        def parse_range_with_step(token: str) -> tuple[str, str, int] | None:
            body = token
            step = 1
            if "/" in token:
                body, step_raw = token.rsplit("/", 1)
                step_raw = step_raw.strip()
                if not step_raw.isdigit() or int(step_raw) <= 0:
                    return None
                step = int(step_raw)

            # Range bounds can themselves contain signed alias offsets like
            # "last-1" or "current+2". Use a bound-aware regex instead of a
            # naive split("-", 1), otherwise "last-1-last" is parsed wrongly.
            bound = r"(?:\d+|first|start|begin|last|end|current|cur|here)(?:\s*[+-]\s*\d+)?"
            m = re.fullmatch(rf"\s*({bound})\s*-\s*({bound})\s*", body)
            if not m:
                return None
            start_raw, end_raw = m.groups()
            return start_raw, end_raw, step

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

            page = parse_single(token)
            if page is not None:
                if 1 <= page <= total_pages:
                    ordered.append(page - 1)
                continue

            range_info = parse_range_with_step(token)
            if range_info is not None:
                a, b, step = range_info
                start = parse_single(a)
                end = parse_single(b)
                if start is None or end is None:
                    continue
                signed_step = step if start <= end else -step
                for p in range(start, end + signed_step, signed_step):
                    if 1 <= p <= total_pages:
                        ordered.append(p - 1)
                continue

        return ordered


STARTUP_PDF_OPTION_NAMES = {
    "--file",
    "--open",
    "--document",
    "--pdf",
    "--filename",
    "--input",
    "--path",
    "-f",
}


def resolve_startup_pdf_argument(raw_arg: str) -> Path | None:
    value = (raw_arg or "").strip()
    if not value:
        return None

    # Some launchers wrap file paths in explicit option forms.
    # Accept common variants like --file=/tmp/doc.pdf, --file:/tmp/doc.pdf,
    # -f=/tmp/doc.pdf, -f/tmp/doc.pdf and --open="...".
    for option in STARTUP_PDF_OPTION_NAMES:
        lower_value = value.lower()

        # Accept option+path concatenation used by some wrappers,
        # e.g. -f/tmp/doc.pdf, -fC:\\docs\\file.pdf,
        # --file/tmp/doc.pdf or --open~/docs/file.pdf.
        if len(value) > len(option) and lower_value.startswith(option):
            remainder = value[len(option):]
            # Only treat concatenated short-option payloads as file paths when
            # the remainder actually looks like a path/URI. This avoids
            # mis-parsing unrelated flags like "-fullscreen" as "-f" + "...".
            looks_like_path = (
                remainder[:1] in {"/", "\\", "~", ".", '"', "'"}
                or re.match(r"^[A-Za-z]:", remainder)
                or remainder.lower().startswith(("file://", "./", "../"))
                or remainder.lower().endswith(".pdf")
            )
            if looks_like_path:
                value = remainder.strip()
                if not value:
                    return None
                break

        for separator in ("=", ":"):
            prefix = f"{option}{separator}"
            if lower_value.startswith(prefix):
                value = value[len(prefix):].strip()
                if not value:
                    return None
                break
        else:
            continue
        break

    # Some launch wrappers pass the argument with surrounding quotes intact.
    # Repeatedly unwrap matching quote pairs so doubly-wrapped values like
    # '"/tmp/doc.pdf"' still resolve as file paths.
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
        if not value:
            return None

    # Chat/launcher wrappers sometimes include surrounding delimiters around
    # links, e.g. <file:///tmp/doc.pdf> or (file:///tmp/doc.pdf).
    # Repeatedly unwrap matching outer pairs so doubly-wrapped values still
    # resolve as file paths.
    wrapper_pairs = {"<": ">", "(": ")", "[": "]", "{": "}"}
    while len(value) >= 2 and value[0] in wrapper_pairs and value[-1] == wrapper_pairs[value[0]]:
        value = value[1:-1].strip()
        if not value:
            return None

    # Some chat surfaces append sentence punctuation to pasted links like
    # "<file:///tmp/doc.pdf#page=2>.". Trim obvious trailing punctuation so
    # startup parsing still resolves the PDF path.
    while value.endswith((".", ",", ";", ":", "!", "?")):
        trimmed = value.rstrip(".,;:!?").strip()
        if (
            trimmed
            and (
                trimmed.lower().startswith("file://")
                or ".pdf" in trimmed.lower()
            )
        ):
            value = trimmed
            continue
        break

    # On Windows, urlparse treats drive-letter paths like C:\foo.pdf as URL schemes
    # (scheme="c"). Detect and normalize those paths before URL parsing.
    if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/].*", value):
        value = re.split(r"[?#]", value, maxsplit=1)[0]
        candidate = Path(unquote(value)).expanduser()
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() == ".pdf":
            return candidate

    parsed = urlparse(value)
    if parsed.scheme == "file":
        file_path = unquote(parsed.path or "")
        if parsed.netloc:
            netloc = parsed.netloc.lower()
            # RFC 8089: file://localhost/... targets the local machine.
            # Treat it like an empty authority instead of a UNC host.
            if netloc == "localhost":
                pass
            # Windows may emit file://C:/path where the drive letter lands
            # in netloc. Normalize to C:/path instead of //C:/path.
            elif os.name == "nt" and re.match(r"^[A-Za-z]:$", parsed.netloc):
                file_path = f"{parsed.netloc}{file_path}"
            else:
                file_path = f"//{parsed.netloc}{file_path}"

        # Windows file URIs are often shaped like /C:/path/to/file.pdf.
        # Strip the leading slash so Path resolves correctly on Windows.
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", file_path):
            file_path = file_path[1:]

        # Older producers may encode drive letters as C| instead of C:.
        # Accept both /C|/path and C|/path legacy forms.
        if os.name == "nt" and re.match(r"^/?[A-Za-z]\|/", file_path):
            if file_path.startswith("/"):
                file_path = file_path[1:]
            file_path = f"{file_path[0]}:{file_path[2:]}"

        candidate = Path(file_path).expanduser()
    elif parsed.scheme == "" and (parsed.query or parsed.fragment):
        # Desktop launchers occasionally append URL-style query/fragment parts
        # (e.g. "/path/doc.pdf#page=3"). Strip those while keeping file paths.
        candidate = Path(unquote(parsed.path or "")).expanduser()
    else:
        # Some launchers pass plain local paths with percent-encoding
        # (e.g. "/tmp/My%20Doc.pdf"). Decode those before existence checks.
        candidate = Path(unquote(value)).expanduser()

    if candidate.exists() and candidate.is_file() and candidate.suffix.lower() == ".pdf":
        return candidate
    return None


def find_startup_pdf_argument(argv: list[str]) -> Path | None:
    i = 0
    while i < len(argv):
        raw = argv[i]
        if raw in STARTUP_PDF_OPTION_NAMES:
            if i + 1 < len(argv):
                candidate = resolve_startup_pdf_argument(argv[i + 1])
                if candidate is not None:
                    return candidate
            # If the option has no usable value, keep scanning from the next
            # token instead of skipping it. This allows chains like
            # "--file --open=/tmp/doc.pdf" to still resolve the later option.
            i += 1
            continue

        candidate = resolve_startup_pdf_argument(raw)
        if candidate is not None:
            return candidate
        i += 1

    return None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()

    startup_candidate = find_startup_pdf_argument(sys.argv[1:])

    if startup_candidate is not None:
        win._open_pdf_path(str(startup_candidate))

    win.show()
    sys.exit(app.exec())
