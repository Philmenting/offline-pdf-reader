import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import fitz  # PyMuPDF
import pytesseract
from pytesseract import Output, TesseractError, TesseractNotFoundError
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
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
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
)

from pdf_text_utils import base14_fontcode, detected_fontcode, star_points

try:
    import cv2  # type: ignore[import-not-found]
except Exception:
    cv2 = None

try:
    import numpy as np  # type: ignore[import-not-found]
except Exception:
    np = None


APP_TITLE = "Offline PDF Leser — MVP"


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


@dataclass
class OCRPassResult:
    text: str
    mean_confidence: float
    low_conf_tokens: list[str]
    low_conf_lines: list[str]


@dataclass
class BatchRenameProposal:
    src: Path
    dst: Path
    reason: str
    source: str
    confidence: str


class SortableTableWidgetItem(QTableWidgetItem):
    def __init__(self, text: str, sort_key=None):
        super().__init__(text)
        if sort_key is not None:
            self.setData(Qt.ItemDataRole.UserRole, sort_key)

    def __lt__(self, other):
        if isinstance(other, QTableWidgetItem):
            left = self.data(Qt.ItemDataRole.UserRole)
            right = other.data(Qt.ItemDataRole.UserRole)
            if left is not None and right is not None:
                try:
                    return left < right
                except Exception:
                    pass
        return super().__lt__(other)


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
    if not name:
        return "Dokument"

    # Avoid Windows reserved device names (CON, PRN, AUX, NUL, COM1..9, LPT1..9).
    # Windows also rejects these names when used as the *stem* before an extension
    # (e.g. "CON.pdf", "LPT1.txt").
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    stem = name.split(".", 1)[0].upper()
    if stem in reserved:
        return f"{name}_"
    return name


def _list_pdf_files(folder: Path) -> list[Path]:
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])


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

    type_patterns: dict[str, list[str]] = {
        "Gutschrift": [r"\bgutschrift\b", r"\bcredit\s+note\b", r"\bcredit\s+memo\b"],
        "Mahnung": [r"\bmahnung\b", r"\bzahlungserinnerung\b", r"\bpayment\s+reminder\b"],
        "Auftragsbestaetigung": [r"\bauftragsbest[aä]tigung\b", r"\border\s+confirmation\b"],
        "Lieferschein": [r"\blieferschein\b", r"\bdelivery\s+note\b", r"\bdispatch\s+note\b", r"\bdespatch\s+note\b", r"\bpacking\s+slip\b"],
        "Rechnung": [r"\brechnung\b", r"\binvoice\b"],
        "Angebot": [r"\bangebot\b", r"\bquote\b", r"\bquotation\b"],
        "Bestellung": [r"\bbestellung\b", r"\bpurchase\s+order\b", r"\border\b"],
        "Vertrag": [r"\bvertrag\b", r"\bcontract\b"],
    }

    type_scores: dict[str, int] = {k: 0 for k in type_patterns}

    # Strong signal: explicit labels like "Dokumenttyp: ..." or "Type: ..."
    type_label_re = re.compile(r"(?i)^(?:dokumenttyp|typ|type|document\s+type)\s*[:#-]?\s*(.+)$")
    for idx, ln in enumerate(lines[:60]):
        m_type = type_label_re.match(ln)
        if not m_type:
            continue
        tail = m_type.group(1).lower()
        for t_name, patterns in type_patterns.items():
            if any(re.search(p, tail) for p in patterns):
                type_scores[t_name] += 10
        if idx + 1 < len(lines):
            nxt = lines[idx + 1].lower()
            for t_name, patterns in type_patterns.items():
                if any(re.search(p, nxt) for p in patterns):
                    type_scores[t_name] += 7

    # Global signal: score full text + top lines (headings weigh more)
    top_chunk = "\n".join(lines[:30]).lower()
    for t_name, patterns in type_patterns.items():
        for pat in patterns:
            if re.search(pat, lower):
                type_scores[t_name] += 2
            if re.search(pat, top_chunk):
                type_scores[t_name] += 2

    # Prefer specific types over generic "order" when same score.
    type_priority = {
        "Gutschrift": 8,
        "Mahnung": 7,
        "Auftragsbestaetigung": 6,
        "Lieferschein": 5,
        "Rechnung": 4,
        "Angebot": 3,
        "Bestellung": 2,
        "Vertrag": 1,
    }

    best_type = max(type_scores.items(), key=lambda kv: (kv[1], type_priority.get(kv[0], 0)))
    if best_type[1] > 0:
        doc_type = best_type[0]

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
    def _parse_date_token(raw: str) -> str:
        token = (raw or "").strip().strip(".,;:)")
        if not token:
            return ""

        # Compact numeric dates like 20260324 or 240326 are common in OCR output.
        compact = re.sub(r"\D", "", token)
        if compact:
            compact_formats: tuple[str, ...] = ()
            if len(compact) == 8:
                compact_formats = ("%Y%m%d", "%d%m%Y", "%m%d%Y")
            elif len(compact) == 6:
                compact_formats = ("%d%m%y", "%y%m%d", "%m%d%y")
            for fmt in compact_formats:
                try:
                    parsed = datetime.strptime(compact, fmt)
                    if 1990 <= parsed.year <= 2100:
                        return parsed.strftime("%Y-%m-%d")
                except ValueError:
                    continue

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
                parsed = datetime.strptime(token, fmt)
                if 1990 <= parsed.year <= 2100:
                    return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    date = ""

    # Prefer explicit date labels (often more accurate than first free date in OCR text).
    date_label_pattern = (
        r"(?:dat(?:um|urn)|date|"
        r"rechnungs\s*[-_]?\s*dat(?:um|urn)|beleg\s*[-_]?\s*dat(?:um|urn)|"
        r"ausstellungs\s*[-_]?\s*dat(?:um|urn)|leistungs\s*[-_]?\s*dat(?:um|urn)|"
        r"invoice\s+date|document\s+date|issue\s+date)"
    )
    date_label_re = re.compile(rf"(?i)^{date_label_pattern}\s*[:#-]?\s*(.*)$")
    inline_date_re = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{8}|\d{6})\b")
    for idx, ln in enumerate(lines[:80]):
        m_label = date_label_re.match(ln)
        if not m_label:
            continue
        tail = (m_label.group(1) or "").strip()
        m_inline = inline_date_re.search(tail)
        if m_inline:
            parsed_inline = _parse_date_token(m_inline.group(1))
            if parsed_inline:
                date = parsed_inline
                break
        if idx + 1 < len(lines):
            m_next = inline_date_re.search(lines[idx + 1])
            if m_next:
                parsed_next = _parse_date_token(m_next.group(1))
                if parsed_next:
                    date = parsed_next
                    break

    if not date:
        for pat in date_patterns:
            m = re.search(pat, text)
            if m:
                parsed_generic = _parse_date_token(m.group(1))
                if parsed_generic:
                    date = parsed_generic
                    break

    if not date:
        m_compact = re.search(
            rf"(?i)\b{date_label_pattern}\s*[:\-]?\s*(\d{{8}}|\d{{6}})\b",
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
    def _normalize_doc_number(raw: str) -> str:
        cleaned = (raw or "").strip().strip(".,;:)")
        cleaned = re.sub(r"^[#:\-\s]+", "", cleaned)

        # Remove common OCR label leftovers when capture groups are noisy.
        cleaned = re.sub(
            r"(?i)^(?:nr|nummer|no|number|invoice|rechnung|beleg|doc|id)\s*[:#\-/]*\s*",
            "",
            cleaned,
        )

        # Normalize OCR-confused separators and collapse whitespace around separators.
        cleaned = cleaned.replace("\\", "/")
        cleaned = re.sub(r"\s*([/_-])\s*", r"\1", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)

        # Document numbers are usually compact tokens; remove remaining spaces.
        cleaned = cleaned.replace(" ", "")

        # Trim trailing punctuation while preserving identifier separators.
        cleaned = re.sub(r"[.,;:]+$", "", cleaned)

        # OCR confusion fixes for typical document IDs.
        # Keep conservative: only in numeric context or adjacent to separators.
        cleaned = re.sub(r"(?<=\d)[Oo](?=\d)", "0", cleaned)
        cleaned = re.sub(r"(?<=\d)[Il](?=\d)", "1", cleaned)
        cleaned = re.sub(r"(?<=\d)S(?=\d)", "5", cleaned)
        cleaned = re.sub(r"(?<=\d)B(?=\d)", "8", cleaned)

        # Remove accidental duplicate separators from OCR.
        cleaned = re.sub(r"([/_-]){2,}", r"\1", cleaned)

        return cleaned.strip()

    number = ""
    value_re = re.compile(r"^[A-Z0-9][A-Z0-9/_-]{2,}$", re.IGNORECASE)

    number_candidates: dict[str, int] = {}

    def _add_number_candidate(raw_val: str, score: int) -> None:
        val = _normalize_doc_number(raw_val)
        if not val or not value_re.match(val):
            return
        number_candidates[val] = number_candidates.get(val, 0) + score

    invoice_like = doc_type in {"Rechnung", "Gutschrift", "Mahnung"}
    order_like = doc_type in {"Bestellung", "Auftragsbestaetigung"}
    delivery_like = doc_type == "Lieferschein"

    strict_patterns = [
        (
            r"(?i)(?:rechnungs\s*[-_]?\s*(?:nr|nummer)\.?|rechn\.?\s*[-/]?\s*nr\.?|re\.?\s*[-/]?\s*nr\.?|rg\.?\s*[-/]?\s*nr\.?|invoice\s*(?:no|number|nr)\.?|invoice\s*#|beleg\s*[-_]?\s*nr\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
            12 if invoice_like else 10,
        ),
        (
            r"(?i)(?:vorgangs\s*[-_]?\s*(?:nr|nummer)\.?|bestell\s*[-_]?\s*(?:nr|nummer)\.?|order\s*(?:no|number)\.?|purchase\s*order\s*(?:no|number)\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
            9 if order_like else 6,
        ),
        (
            r"(?i)(?:lieferschein\s*[-_]?\s*(?:nr|nummer)\.?|delivery\s*note\s*(?:no|number)\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
            10 if delivery_like else 7,
        ),
        (r"(?i)\b(?:inv|doc|po|dn)\s*[-_]?\s*([A-Z0-9][A-Z0-9/_-]{2,})\b", 4),
    ]
    for pat, pts in strict_patterns:
        for m_num in re.finditer(pat, text):
            _add_number_candidate(m_num.group(1), pts)

    label_re = re.compile(
        r"(?i)^(?:rechnungs\s*[-_]?\s*(?:nr|nummer)|rechn\.?\s*nr\.?|re\.?\s*nr\.?|rg\.?\s*nr\.?|invoice\s*(?:no|number|nr)|beleg\s*[-_]?\s*nr\.?|vorgangs\s*[-_]?\s*(?:nr|nummer)|bestell\s*[-_]?\s*(?:nr|nummer)|order\s*(?:no|number)|purchase\s*order\s*(?:no|number)|lieferschein\s*[-_]?\s*(?:nr|nummer)|delivery\s*note\s*(?:no|number)|nr\.?)\s*[:#-]?\s*(.*)$"
    )
    for idx, ln in enumerate(lines[:80]):
        m_label = label_re.match(ln.strip())
        if not m_label:
            continue
        ll = ln.lower()
        if any(k in ll for k in ["rechnung", "invoice", "beleg"]):
            base_score = 11 if invoice_like else 9
        elif any(k in ll for k in ["bestell", "order", "purchase"]):
            base_score = 8 if order_like else 6
        elif any(k in ll for k in ["lieferschein", "delivery note"]):
            base_score = 9 if delivery_like else 6
        else:
            base_score = 6
        inline_val = _normalize_doc_number(m_label.group(1))
        if inline_val:
            _add_number_candidate(inline_val, base_score)
        if idx + 1 < len(lines):
            _add_number_candidate(lines[idx + 1], base_score - 1)

    # Prefer candidates that look like realistic document IDs for current doc type.
    for cand in list(number_candidates.keys()):
        bonus = 0
        low = cand.lower()

        # Generic quality hints.
        if re.search(r"\d", cand) and re.search(r"[A-Z]", cand, re.IGNORECASE):
            bonus += 2
        if any(sep in cand for sep in ["/", "-", "_"]):
            bonus += 1
        if len(cand) < 4:
            bonus -= 3
        elif len(cand) < 6:
            bonus -= 1

        # Prefix/type hints.
        if re.match(r"(?i)^(re|rg|inv)", cand):
            bonus += 3 if invoice_like else 1
        if re.match(r"(?i)^(po|best|ord)", cand):
            bonus += 3 if order_like else 0
        if re.match(r"(?i)^(dn|ls|lief)", cand):
            bonus += 3 if delivery_like else 0

        # Penalize obviously generic tokens that often appear in headers.
        if low in {"invoice", "rechnung", "number", "nummer", "order", "po", "doc", "id", "total", "summe"}:
            bonus -= 6

        # Single-block pure digits are less reliable than mixed IDs.
        if re.fullmatch(r"\d{4,}", cand):
            bonus -= 2

        number_candidates[cand] += bonus

    if number_candidates:
        number = max(number_candidates.items(), key=lambda kv: (kv[1], len(kv[0])))[0]

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
    recipient_tokens = ["herr", "frau", "empfaenger", "empfänger", "kunde", "an:", "z. hd", "z.hd", "attn", "recipient"]

    def _clean_vendor_prefix(raw: str) -> str:
        cleaned = re.sub(r"(?i)^\s*(firma|company|vendor|lieferant|sender|absender)\s*[:\-]\s*", "", raw).strip()
        return cleaned

    sender_label_re = re.compile(
        r"(?i)^\s*(?:absender|sender|von|from|firma|company|vendor|lieferant)\s*[:\-]\s*(.+)$"
    )
    recipient_label_re = re.compile(
        r"(?i)^\s*(?:rechnung\s+an|invoice\s+to|kunde|empf[aä]nger|recipient|bill\s+to|ship\s+to)\s*[:\-]?.*$"
    )

    candidates: list[tuple[str, int]] = []

    # Strong signal: explicit sender/vendor labels.
    for ln in lines[:40]:
        m_sender = sender_label_re.match(ln)
        if not m_sender:
            continue
        labeled = _clean_vendor_prefix(m_sender.group(1))
        if labeled and not is_address_like(labeled):
            candidates.append((labeled, 14))

    # General fallback: title-like lines near the top.
    for ln in lines[:35]:
        ll = ln.lower()
        if len(ln) < 3:
            continue
        if is_address_like(ln):
            continue
        if recipient_label_re.match(ln):
            continue
        if any(k in ll for k in ["rechnung", "invoice", "seite", "page", "betreff", "subject", "datum", "date"]):
            continue
        if re.fullmatch(r"[\d\W_]+", ln):
            continue
        cleaned = _clean_vendor_prefix(ln)
        if cleaned:
            candidates.append((cleaned, 0))

    vendor = ""
    if candidates:
        scored: list[tuple[int, str]] = []
        for idx, (ln, base_score) in enumerate(candidates):
            ll = ln.lower()
            score = base_score
            if any(tok in ll for tok in company_tokens):
                score += 8
            if any(tok in ll for tok in recipient_tokens):
                score -= 6
            if re.search(r"\b(gbr|kg|gmbh|ag|inc|llc|ltd|corp|s\.?a\.?r\.?l\.?|s\.?a\.?)\b", ll):
                score += 3
            if idx < 10:
                score += 1
            word_count = len(re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", ln))
            score += min(4, word_count)
            if len(ln) > 64:
                score -= 2
            scored.append((score, ln))

        scored.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
        vendor = scored[0][1]

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


class ThumbnailListWidget(QListWidget):
    pagesReordered = Signal()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        super().dropEvent(event)
        self.pagesReordered.emit()


class PreviewLabel(QLabel):
    clicked = Signal(float, float)
    dragStarted = Signal(float, float)
    dragMoved = Signal(float, float)
    dragFinished = Signal(float, float)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._drag_origin: QPointF | None = None
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            self._drag_origin = pos
            self._dragging = True
            self.dragStarted.emit(pos.x(), pos.y())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            pos = event.position()
            self.dragMoved.emit(pos.x(), pos.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            pos = event.position()
            dx = pos.x() - self._drag_origin.x()
            dy = pos.y() - self._drag_origin.y()
            if abs(dx) < 4 and abs(dy) < 4:
                self.clicked.emit(pos.x(), pos.y())
            else:
                self.dragFinished.emit(pos.x(), pos.y())
        self._drag_origin = None
        self._dragging = False
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1220, 860)
        self.setAcceptDrops(True)

        self.pdf_path: Path | None = None
        self.doc: fitz.Document | None = None
        self.current_page = 0
        self.zoom_factor = 1.35
        self.page_rotations: dict[int, int] = {}
        self.learning_rules_path = Path(__file__).with_name("learning_rules.json")
        self.app_settings_path = Path(__file__).with_name("app_settings.json")
        self.learning_rules = self._load_learning_rules()
        self.app_settings = self._load_app_settings()
        self._configure_tesseract_runtime()

        self.undo_stack: list[tuple[bytes, dict[int, int], int]] = []
        self.redo_stack: list[tuple[bytes, dict[int, int], int]] = []
        self.is_dirty = False

        self.preview = PreviewLabel("Kein PDF geladen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(400, 460)
        self.preview.setCursor(Qt.CursorShape.ArrowCursor)
        self.preview.setProperty("role", "pagepreview")

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidget(self.preview)
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_scroll.setProperty("role", "previewarea")

        self.thumb_list = ThumbnailListWidget()
        self.thumb_list.setProperty("role", "thumbrail")
        self.thumb_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.thumb_list.setFlow(QListWidget.Flow.TopToBottom)
        self.thumb_list.setMovement(QListWidget.Movement.Snap)
        self.thumb_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.thumb_list.setIconSize(QSize(100, 140))
        self.thumb_list.setSpacing(8)
        self.thumb_list.setMinimumWidth(130)
        self.thumb_list.setMaximumWidth(520)
        self.thumb_list.setUniformItemSizes(True)
        self.thumb_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.thumb_list.setDragEnabled(True)
        self.thumb_list.setAcceptDrops(True)
        self.thumb_list.setDropIndicatorShown(True)
        self.thumb_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.thumb_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.thumb_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thumb_list.customContextMenuRequested.connect(self._show_thumbnail_context_menu)
        self.thumb_list.itemClicked.connect(self._on_thumbnail_clicked)
        self.thumb_list.pagesReordered.connect(self._reorder_pages_by_thumbnail_order)
        self.preview.clicked.connect(self._handle_preview_click)
        self.preview.dragStarted.connect(self._handle_preview_drag_start)
        self.preview.dragMoved.connect(self._handle_preview_drag_move)
        self.preview.dragFinished.connect(self._handle_preview_drag_finish)

        self.extracted_text = ""
        self.text_dialog: QDialog | None = None
        self.text_output_view: QTextEdit | None = None
        self.pending_annotation: dict | None = None
        self.preview_drag_start: tuple[float, float] | None = None
        self.preview_drag_current: tuple[float, float] | None = None
        self.preview_drag_points: list[tuple[float, float]] = []
        self.selected_annotation_xref: int | None = None
        self.selected_widget_xref: int | None = None
        self.annotation_drag_state: dict | None = None
        self._editing_text_block: dict | None = None
        self.annotation_image_path: Path | None = None
        self.annotation_image_preview: QPixmap | None = None

        self.suggested_name = QLineEdit()
        self.suggested_name.setPlaceholderText("Dateiname wird nach OCR vorgeschlagen …")
        self.suggested_name.setClearButtonEnabled(True)

        self.ocr_lang = str(self.app_settings.get("ocrLang", "deu+eng"))
        if not self._normalize_ocr_language_code(self.ocr_lang):
            self.ocr_lang = "deu+eng"
        self.ocr_correction_mode = str(self.app_settings.get("ocrCorrectionMode", "konservativ"))
        if self.ocr_correction_mode not in {"konservativ", "aggressiv"}:
            self.ocr_correction_mode = "konservativ"
        self.ocr_cancel_requested = False
        self.ocr_cache: dict[str, tuple[str, str | None, list[str], list[str]]] = {}
        self.ocr_cache_order: list[str] = []
        self.ocr_cache_max_entries = 80
        self.doc_revision = 0
        self._page_render_cache: dict[tuple, QPixmap] = {}
        self.last_ocr_failed_pages: list[int] = []
        self.last_recognized_page_texts: dict[int, str] = {}
        self._installed_ocr_langs_cache: set[str] | None = None

        self.search_query = QLineEdit()
        self.search_query.setPlaceholderText("Suche in allen Seiten …")
        self.search_query.setAccessibleName("Suchfeld")
        self.search_query.setAccessibleDescription("Suchbegriff eingeben, um alle Seiten einschließlich OCR-Texte zu durchsuchen")
        self.search_query.setClearButtonEnabled(True)
        self.search_results_list = QListWidget()
        self.search_results_list.setMinimumHeight(140)
        self.search_results_list.setVisible(False)
        self.search_results_list.setAccessibleName("Suchtrefferliste")
        self.search_results_list.setAccessibleDescription("Liste aller Suchtreffer über alle Seiten mit Seiten- und Zeilenangabe")
        self.search_results_list.itemClicked.connect(self._on_search_result_clicked)
        self.search_hits: list[dict] = []
        self.current_search_hit = -1

        self.page_info = QLabel("Seite: -/- | Zoom: 100%")

        self.thumb_panel = QWidget()
        self.thumb_panel.setProperty("role", "thumbpanel")
        thumb_panel_layout = QVBoxLayout(self.thumb_panel)
        thumb_panel_layout.setContentsMargins(10, 10, 10, 10)
        thumb_panel_layout.setSpacing(8)
        thumb_header = QWidget()
        thumb_header.setProperty("role", "sectioncard")
        thumb_header_layout = QVBoxLayout(thumb_header)
        thumb_header_layout.setContentsMargins(10, 10, 10, 10)
        thumb_header_layout.setSpacing(2)
        self.thumb_title_label = QLabel("Seiten")
        self.thumb_title_label.setProperty("role", "cardtitle")
        self.thumb_meta_label = QLabel("Noch kein PDF geladen")
        self.thumb_meta_label.setProperty("role", "panelsubtitle")
        thumb_header_layout.addWidget(self.thumb_title_label)
        thumb_header_layout.addWidget(self.thumb_meta_label)
        thumb_panel_layout.addWidget(thumb_header)

        # ── Lesezeichen / Inhaltsverzeichnis (PDF-Outline) ────────────────
        self.outline_card = QWidget()
        self.outline_card.setProperty("role", "sectioncard")
        outline_layout = QVBoxLayout(self.outline_card)
        outline_layout.setContentsMargins(10, 10, 10, 10)
        outline_layout.setSpacing(4)
        outline_title = QLabel("Lesezeichen")
        outline_title.setProperty("role", "cardtitle")
        outline_layout.addWidget(outline_title)
        self.outline_list = QListWidget()
        self.outline_list.setMaximumHeight(180)
        self.outline_list.setAccessibleName("Lesezeichen-Navigation")
        self.outline_list.setAccessibleDescription("Inhaltsverzeichnis / Lesezeichen des PDFs zum Anspringen")
        self.outline_list.itemClicked.connect(self._on_outline_item_clicked)
        outline_layout.addWidget(self.outline_list)
        self.outline_empty_label = QLabel("Keine Lesezeichen im Dokument.")
        self.outline_empty_label.setProperty("role", "panelsubtitle")
        self.outline_empty_label.setWordWrap(True)
        outline_layout.addWidget(self.outline_empty_label)
        thumb_panel_layout.addWidget(self.outline_card)

        thumb_panel_layout.addWidget(self.thumb_list, 1)

        self.preview.setAccessibleName("PDF-Seitenvorschau")
        self.preview.setAccessibleDescription("Zeigt die aktuell ausgewählte Seite als große Vorschau")
        self.preview_scroll.setAccessibleName("Vorschau-Scrollbereich")
        self.preview_scroll.setAccessibleDescription("Scrollbarer Bereich für die PDF-Seitenvorschau")
        self.thumb_list.setAccessibleName("Seiten-Miniaturen")
        self.thumb_list.setAccessibleDescription("Liste der Seiten-Miniaturen zum Auswählen und Umordnen")
        self.suggested_name.setAccessibleName("Vorgeschlagener Dateiname")
        self.suggested_name.setAccessibleDescription("Bearbeitbarer Dateiname für das Speichern")
        self.search_query.setAccessibleName("Suchfeld für Dokumentseiten")
        self.search_query.setAccessibleDescription("Suchbegriff eingeben und in allen Seiten suchen")
        self.search_results_list.setAccessibleName("Suchergebnisse")
        self.search_results_list.setAccessibleDescription("Trefferliste mit Seitenbezug")
        self.page_info.setAccessibleName("Seiten- und Zoomstatus")

        self.ocr_feedback = QLabel("OCR bereit")
        self.ocr_feedback.setWordWrap(True)
        self.ocr_feedback.setAccessibleName("OCR-Hinweise")
        self.ocr_feedback.setAccessibleDescription("Zeigt OCR-Qualitätshinweise und Auffälligkeiten")
        self.ocr_mode_label = QLabel()
        self.ocr_mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.ocr_mode_label.setAccessibleName("OCR-Status")

        # ── Hilfsfunktionen für Buttons ─────────────────────────────────────
        def _icon_btn(label: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            b.setProperty("btnRole", "icon")
            b.setMinimumHeight(32)
            return b

        def _primary_btn(label: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            b.setProperty("btnRole", "primary")
            b.setMinimumHeight(34)
            return b

        def _action_btn(label: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            b.setProperty("btnRole", "action")
            b.setMinimumHeight(34)
            return b

        def _std_icon(button: QPushButton, icon: QStyle.StandardPixmap, text: str = "") -> None:
            button.setIcon(self.style().standardIcon(icon))
            if text != button.text():
                button.setText(text)

        def _make_annotation_icon(kind: str) -> QIcon:
            pix = QPixmap(20, 20)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#35507c"))
            pen.setWidth(2)
            painter.setPen(pen)

            if kind == "text":
                font = QFont()
                font.setBold(True)
                font.setPointSize(11)
                painter.setFont(font)
                painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "T")
            elif kind == "rect":
                painter.drawRoundedRect(3, 4, 14, 11, 3, 3)
            elif kind == "highlight":
                painter.fillRect(3, 10, 14, 5, QColor("#ffeb3b"))
                painter.drawLine(4, 9, 16, 9)
            elif kind == "strikeout":
                font = QFont()
                font.setBold(True)
                font.setPointSize(11)
                painter.setFont(font)
                painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "S")
                painter.drawLine(3, 10, 17, 10)
            elif kind == "underline":
                font = QFont()
                font.setBold(True)
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, "U")
                painter.drawLine(4, 17, 16, 17)
            elif kind == "line":
                painter.drawLine(4, 15, 16, 5)
            elif kind == "arrow":
                painter.drawLine(4, 15, 15, 6)
                painter.drawLine(11, 6, 15, 6)
                painter.drawLine(15, 6, 15, 10)
            elif kind == "image":
                painter.drawRoundedRect(3, 4, 14, 12, 2, 2)
                painter.drawEllipse(6, 7, 2, 2)
                painter.drawLine(5, 14, 9, 10)
                painter.drawLine(9, 10, 12, 13)
                painter.drawLine(12, 13, 15, 9)
            elif kind == "redact":
                painter.fillRect(4, 6, 12, 8, QColor("#111111"))
                painter.drawLine(4, 15, 16, 15)
            elif kind == "note":
                painter.drawRoundedRect(4, 4, 12, 12, 2, 2)
                painter.drawLine(7, 8, 13, 8)
                painter.drawLine(7, 11, 12, 11)
            elif kind == "freehand":
                painter.drawLine(4, 14, 7, 10)
                painter.drawLine(7, 10, 11, 13)
                painter.drawLine(11, 13, 16, 6)
            elif kind == "text-replace":
                font = QFont()
                font.setBold(True)
                font.setPointSize(8)
                painter.setFont(font)
                painter.drawRoundedRect(3, 4, 14, 12, 2, 2)
                painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "Aa")
            elif kind == "text-edit":
                painter.drawLine(3, 6, 13, 6)
                painter.drawLine(3, 10, 11, 10)
                painter.drawLine(3, 14, 9, 14)
                # kleiner Stift oben rechts
                painter.drawLine(13, 13, 17, 9)
                painter.drawLine(13, 13, 14, 16)
            elif kind == "ellipse":
                painter.drawEllipse(3, 5, 14, 10)
            elif kind == "star":
                pts = []
                cx, cy = 10.0, 10.0
                for i in range(10):
                    ang = -math.pi / 2 + i * math.pi / 5
                    r = 8.0 if i % 2 == 0 else 3.2
                    pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
                painter.drawPolygon(pts)
            elif kind == "link":
                painter.drawArc(3, 7, 9, 6, 30 * 16, 180 * 16)
                painter.drawArc(8, 7, 9, 6, 210 * 16, 180 * 16)
                painter.drawLine(8, 10, 12, 10)

            painter.end()
            return QIcon(pix)

        def _make_toolbar_icon(kind: str) -> QIcon:
            pix = QPixmap(20, 20)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#35507c"))
            pen.setWidth(2)
            painter.setPen(pen)

            if kind == "extract":
                painter.drawRoundedRect(4, 3, 12, 14, 2, 2)
                painter.drawLine(7, 8, 13, 8)
                painter.drawLine(7, 11, 13, 11)
            elif kind == "extract-all":
                painter.drawRoundedRect(3, 4, 8, 11, 2, 2)
                painter.drawRoundedRect(9, 6, 8, 11, 2, 2)
            elif kind == "ocr-name":
                painter.drawRoundedRect(3, 3, 10, 14, 2, 2)
                painter.drawLine(6, 8, 10, 8)
                painter.drawLine(6, 11, 10, 11)
                painter.drawLine(13, 14, 17, 10)
            elif kind == "merge":
                painter.drawLine(4, 6, 10, 12)
                painter.drawLine(16, 6, 10, 12)
                painter.drawLine(10, 12, 10, 17)
            elif kind == "split":
                painter.drawLine(10, 4, 10, 10)
                painter.drawLine(10, 10, 5, 15)
                painter.drawLine(10, 10, 15, 15)
            elif kind == "crop":
                painter.drawLine(6, 4, 6, 14)
                painter.drawLine(6, 14, 16, 14)
                painter.drawLine(10, 4, 10, 10)
                painter.drawLine(10, 10, 16, 10)
            elif kind == "reorder":
                painter.drawLine(5, 6, 15, 6)
                painter.drawLine(5, 10, 13, 10)
                painter.drawLine(5, 14, 15, 14)
            elif kind == "remove-empty":
                painter.drawRoundedRect(5, 4, 10, 12, 2, 2)
                painter.drawLine(7, 8, 13, 8)
                painter.drawLine(8, 8, 12, 13)
            elif kind == "goto":
                font = QFont()
                font.setBold(True)
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "#")
            elif kind == "zoom-reset":
                painter.drawRoundedRect(4, 4, 12, 12, 3, 3)
                painter.drawLine(7, 10, 13, 10)
                painter.drawLine(10, 7, 10, 13)
            elif kind == "rotate-left":
                painter.drawArc(4, 4, 12, 12, 40 * 16, 240 * 16)
                painter.drawLine(4, 8, 4, 4)
                painter.drawLine(4, 4, 8, 4)
            elif kind == "rotate-right":
                painter.drawArc(4, 4, 12, 12, 220 * 16, 240 * 16)
                painter.drawLine(16, 8, 16, 4)
                painter.drawLine(12, 4, 16, 4)
            elif kind == "rotate-reset":
                painter.drawEllipse(5, 5, 10, 10)
                painter.drawLine(10, 7, 10, 10)
                painter.drawLine(10, 10, 13, 12)

            painter.end()
            return QIcon(pix)

        def _tool_btn(label: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            b.setProperty("btnRole", "tool")
            b.setMinimumHeight(34)
            return b

        def _separator() -> QFrame:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.VLine)
            sep.setProperty("role", "toolsep")
            return sep

        btn_open         = _primary_btn("PDF öffnen",     "PDF-Datei öffnen (Ctrl+O)")
        btn_first        = _icon_btn("⏮",                 "Erste Seite")
        btn_prev         = _icon_btn("◀",                 "Vorherige Seite")
        btn_next         = _icon_btn("▶",                 "Nächste Seite")
        btn_last         = _icon_btn("⏭",                 "Letzte Seite")
        btn_goto         = _icon_btn("#",                 "Zu Seite springen")
        btn_zoom_out     = _icon_btn("−",                 "Verkleinern")
        btn_zoom_in      = _icon_btn("+",                 "Vergrößern")
        btn_zoom_reset   = _icon_btn("⊡",                 "Zoom zurücksetzen")
        btn_rotate_left  = _icon_btn("↺",                 "Seite links drehen")
        btn_rotate_right = _icon_btn("↻",                 "Seite rechts drehen")
        btn_rotate_reset = _icon_btn("⟲",                 "Drehung zurücksetzen")
        self.btn_undo    = _icon_btn("↶",                 "Rückgängig (Ctrl+Z)")
        self.btn_redo    = _icon_btn("↷",                 "Wiederholen (Ctrl+Y)")
        btn_extract      = _tool_btn("Seite",             "Text der aktuellen Seite extrahieren")
        btn_extract_all  = _tool_btn("Alle",              "Text aller Seiten extrahieren")
        btn_auto_ocr_name = _primary_btn("OCR + Benennen", "OCR ausführen und Dateinamen vorschlagen")
        btn_save         = _primary_btn("Speichern",      "Direkt speichern (Ctrl+S)")
        btn_saveas       = _action_btn("Speichern als …", "Speichern unter (Ctrl+Shift+S)")
        btn_merge        = _tool_btn("Merge",             "PDFs zusammenführen")
        btn_split        = _tool_btn("Split",             "Seiten extrahieren")
        btn_crop         = _tool_btn("Crop",              "Seiten zuschneiden")
        btn_duplicate    = _tool_btn("Dupl.",             "Aktuelle oder ausgewählte Seiten duplizieren")
        btn_blank_page   = _tool_btn("Leer+",             "Leere Seite nach der aktuellen Seite einfügen")
        btn_form_fields  = _tool_btn("Form",              "Formularfelder auf der aktuellen Seite bearbeiten")
        btn_add_text     = _action_btn("Text",            "Text auf PDF hinzufügen")
        btn_add_rect     = _action_btn("Rechteck",        "Rechteck auf PDF hinzufügen")
        btn_add_ellipse  = _action_btn("Ellipse",         "Ellipse / Kreis auf PDF hinzufügen")
        btn_add_star     = _action_btn("Stern",           "Stern-Form auf PDF hinzufügen")
        btn_add_link     = _action_btn("Hyperlink",       "Klickbaren Web-Link auf PDF einfügen")
        btn_add_highlight = _action_btn("Marker",         "Markierung auf PDF hinzufügen")
        btn_add_strikeout = _action_btn("Durchstreichen", "Text durchstreichen")
        btn_add_underline = _action_btn("Unterstreichen", "Text unterstreichen")
        btn_add_line     = _action_btn("Linie",           "Linie auf PDF hinzufügen")
        btn_add_arrow    = _action_btn("Pfeil",           "Pfeil auf PDF hinzufügen")
        btn_add_image    = _action_btn("Bild",            "Bild, Signatur oder Stempel einfügen")
        btn_add_redact   = _action_btn("Schwärzen",       "Text oder Bereiche irreversibel schwärzen")
        btn_add_note     = _action_btn("Notiz",           "Haftnotiz / Kommentar einfügen")
        btn_add_freehand = _action_btn("Freihand",        "Freihand-Markierung zeichnen")
        btn_replace_text = _action_btn("Text ersetzen",   "Bereich schwärzen und durch neuen Text ersetzen")
        btn_edit_text    = _action_btn("Text bearbeiten", "Vorhandenen Textabschnitt anklicken und direkt im Block bearbeiten")
        btn_reorder      = _tool_btn("Sortieren",         "Seiten neu anordnen")
        btn_remove_empty = _tool_btn("Leer",              "Leere Seiten entfernen")
        btn_search       = _icon_btn("🔍",                 "Suche starten (Ctrl+F)")
        btn_search_close = _icon_btn("✕",                 "Suche schließen")
        btn_hit_prev     = _icon_btn("◀",                 "Vorheriger Treffer")
        btn_hit_next     = _icon_btn("▶",                 "Nächster Treffer")

        _std_icon(btn_open, QStyle.StandardPixmap.SP_DialogOpenButton)
        _std_icon(btn_save, QStyle.StandardPixmap.SP_DialogSaveButton)
        _std_icon(btn_saveas, QStyle.StandardPixmap.SP_DialogSaveButton)
        _std_icon(self.btn_undo, QStyle.StandardPixmap.SP_ArrowBack)
        _std_icon(self.btn_redo, QStyle.StandardPixmap.SP_ArrowForward)
        _std_icon(btn_search, QStyle.StandardPixmap.SP_FileDialogContentsView)
        _std_icon(btn_search_close, QStyle.StandardPixmap.SP_DialogCloseButton)
        btn_goto.setIcon(_make_toolbar_icon("goto"))
        btn_zoom_reset.setIcon(_make_toolbar_icon("zoom-reset"))
        btn_rotate_left.setIcon(_make_toolbar_icon("rotate-left"))
        btn_rotate_right.setIcon(_make_toolbar_icon("rotate-right"))
        btn_rotate_reset.setIcon(_make_toolbar_icon("rotate-reset"))
        btn_extract.setIcon(_make_toolbar_icon("extract"))
        btn_extract_all.setIcon(_make_toolbar_icon("extract-all"))
        btn_auto_ocr_name.setIcon(_make_toolbar_icon("ocr-name"))
        btn_merge.setIcon(_make_toolbar_icon("merge"))
        btn_split.setIcon(_make_toolbar_icon("split"))
        btn_crop.setIcon(_make_toolbar_icon("crop"))
        btn_duplicate.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        btn_blank_page.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView))
        btn_form_fields.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        btn_reorder.setIcon(_make_toolbar_icon("reorder"))
        btn_remove_empty.setIcon(_make_toolbar_icon("remove-empty"))
        btn_add_text.setIcon(_make_annotation_icon("text"))
        btn_add_rect.setIcon(_make_annotation_icon("rect"))
        btn_add_ellipse.setIcon(_make_annotation_icon("ellipse"))
        btn_add_star.setIcon(_make_annotation_icon("star"))
        btn_add_link.setIcon(_make_annotation_icon("link"))
        btn_add_highlight.setIcon(_make_annotation_icon("highlight"))
        btn_add_strikeout.setIcon(_make_annotation_icon("strikeout"))
        btn_add_underline.setIcon(_make_annotation_icon("underline"))
        btn_add_line.setIcon(_make_annotation_icon("line"))
        btn_add_arrow.setIcon(_make_annotation_icon("arrow"))
        btn_add_image.setIcon(_make_annotation_icon("image"))
        btn_add_redact.setIcon(_make_annotation_icon("redact"))
        btn_add_note.setIcon(_make_annotation_icon("note"))
        btn_add_freehand.setIcon(_make_annotation_icon("freehand"))
        btn_replace_text.setIcon(_make_annotation_icon("text-replace"))
        btn_edit_text.setIcon(_make_annotation_icon("text-edit"))

        self.search_counter = QLabel("0 / 0")
        self.search_counter.setAccessibleName("Suchtreffer-Zähler")
        self.search_counter.setProperty("role", "counter")

        self.annotation_hint = QLabel("Bereit")
        self.annotation_hint.setProperty("role", "hintchip")
        self.annotation_hint.setAccessibleName("Aktiver Werkzeughinweis")

        self.btn_cancel_ocr = _action_btn("OCR stoppen")
        self.btn_cancel_ocr.setEnabled(False)
        self.btn_retry_failed_ocr = _action_btn("Fehler wiederholen")
        self.btn_retry_failed_ocr.setEnabled(False)
        self.btn_reset_ocr_prefs = _action_btn("OCR zurücksetzen")

        btn_open.setToolTip("PDF öffnen")
        btn_open.setAccessibleName("PDF öffnen")
        btn_first.setToolTip("Erste Seite")
        btn_first.setAccessibleName("Erste Seite")
        btn_prev.setToolTip("Vorherige Seite")
        btn_prev.setAccessibleName("Vorherige Seite")
        btn_next.setToolTip("Nächste Seite")
        btn_next.setAccessibleName("Nächste Seite")
        btn_last.setToolTip("Letzte Seite")
        btn_last.setAccessibleName("Letzte Seite")
        btn_zoom_out.setToolTip("Zoom verkleinern")
        btn_zoom_out.setAccessibleName("Zoom verkleinern")
        btn_zoom_in.setToolTip("Zoom vergrößern")
        btn_zoom_in.setAccessibleName("Zoom vergrößern")
        btn_zoom_reset.setToolTip("Zoom auf 100%")
        btn_zoom_reset.setAccessibleName("Zoom auf 100 Prozent")
        btn_goto.setToolTip("Gehe zu Seite")
        btn_goto.setAccessibleName("Gehe zu Seite")
        btn_rotate_left.setToolTip("Nach links drehen")
        btn_rotate_left.setAccessibleName("Nach links drehen")
        btn_rotate_right.setToolTip("Nach rechts drehen")
        btn_rotate_right.setAccessibleName("Nach rechts drehen")
        btn_rotate_reset.setToolTip("Drehung zurücksetzen")
        btn_rotate_reset.setAccessibleName("Drehung zurücksetzen")
        self.btn_undo.setToolTip("Rückgängig (Ctrl+Z)")
        self.btn_undo.setAccessibleName("Rückgängig")
        self.btn_redo.setToolTip("Wiederholen (Ctrl+Y)")
        self.btn_redo.setAccessibleName("Wiederholen")
        btn_search.setToolTip("Text in allen Seiten suchen")
        btn_search.setAccessibleName("In allen Seiten suchen")
        btn_auto_ocr_name.setToolTip("OCR für alle Seiten starten und Dateinamen vorschlagen")
        btn_auto_ocr_name.setAccessibleName("OCR und Dateiname vorschlagen")
        btn_search_close.setToolTip("Suche schließen")
        btn_search_close.setAccessibleName("Suche schließen")
        btn_hit_prev.setToolTip("Vorherigen Treffer")
        btn_hit_prev.setAccessibleName("Vorheriger Suchtreffer")
        btn_hit_next.setToolTip("Nächsten Treffer")
        btn_hit_next.setAccessibleName("Nächster Suchtreffer")
        self.btn_cancel_ocr.setToolTip("Laufenden OCR-Vorgang abbrechen")
        self.btn_cancel_ocr.setAccessibleName("OCR-Vorgang abbrechen")
        self.btn_retry_failed_ocr.setToolTip("Nur fehlgeschlagene OCR-Seiten erneut versuchen")
        self.btn_retry_failed_ocr.setAccessibleName("Fehlgeschlagene OCR erneut versuchen")
        self.btn_reset_ocr_prefs.setToolTip("OCR-Sprache und Korrekturmodus zurücksetzen")
        self.btn_reset_ocr_prefs.setAccessibleName("OCR-Einstellungen zurücksetzen")

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
        btn_extract.setAccessibleName("Text auf aktueller Seite erkennen")
        btn_extract_all.setAccessibleName("Text auf allen Seiten erkennen")
        btn_save.setToolTip("PDF direkt speichern (Ctrl+S)")
        btn_save.setAccessibleName("PDF direkt speichern")
        btn_saveas.setAccessibleName("PDF speichern unter")
        btn_saveas.setToolTip("PDF speichern als … (Ctrl+Shift+S)")
        btn_merge.setAccessibleName("PDFs zusammenführen")
        btn_split.setAccessibleName("Seiten extrahieren")
        btn_crop.setAccessibleName("Seiten zuschneiden")
        btn_crop.setToolTip("Seiten zuschneiden")
        btn_duplicate.setAccessibleName("Seiten duplizieren")
        btn_blank_page.setAccessibleName("Leere Seite einfügen")
        btn_form_fields.setAccessibleName("Formularfelder bearbeiten")
        btn_add_text.setAccessibleName("Text auf PDF hinzufügen")
        btn_add_rect.setAccessibleName("Rechteck auf PDF hinzufügen")
        btn_add_ellipse.setAccessibleName("Ellipse auf PDF hinzufügen")
        btn_add_star.setAccessibleName("Stern auf PDF hinzufügen")
        btn_add_link.setAccessibleName("Hyperlink auf PDF einfügen")
        btn_add_highlight.setAccessibleName("Markierung auf PDF hinzufügen")
        btn_add_strikeout.setAccessibleName("Text im PDF durchstreichen")
        btn_add_underline.setAccessibleName("Text im PDF unterstreichen")
        btn_add_line.setAccessibleName("Linie auf PDF hinzufügen")
        btn_add_arrow.setAccessibleName("Pfeil auf PDF hinzufügen")
        btn_add_image.setAccessibleName("Bild auf PDF hinzufügen")
        btn_add_redact.setAccessibleName("PDF-Bereich schwärzen")
        btn_add_note.setAccessibleName("Notiz auf PDF hinzufügen")
        btn_add_freehand.setAccessibleName("Freihand auf PDF zeichnen")
        btn_replace_text.setAccessibleName("Text im PDF ersetzen")
        btn_edit_text.setAccessibleName("Vorhandenen Text im PDF bearbeiten")
        btn_reorder.setAccessibleName("Seiten sortieren")
        btn_remove_empty.setAccessibleName("Leere Seiten entfernen")

        btn_extract.clicked.connect(self.extract_text_and_suggest)
        btn_extract_all.clicked.connect(self.recognize_text_all_pages_and_suggest)
        btn_auto_ocr_name.clicked.connect(self.ocr_and_suggest_filename)
        btn_save.clicked.connect(self.save_in_place)
        btn_saveas.clicked.connect(self.save_as_suggested)
        btn_merge.clicked.connect(self.merge_pdfs)
        btn_split.clicked.connect(self.extract_pages_to_new_pdf)
        btn_crop.clicked.connect(self.start_visual_crop)
        btn_duplicate.clicked.connect(self.duplicate_selected_pages)
        btn_blank_page.clicked.connect(self.insert_blank_page_after_current)
        btn_form_fields.clicked.connect(self.edit_form_fields_on_current_page)
        btn_add_text.clicked.connect(self.add_text_annotation)
        btn_add_rect.clicked.connect(self.add_rectangle_annotation)
        btn_add_ellipse.clicked.connect(self.add_ellipse_annotation)
        btn_add_star.clicked.connect(self.add_star_annotation)
        btn_add_link.clicked.connect(self.add_link_annotation)
        btn_add_highlight.clicked.connect(self.add_highlight_annotation)
        btn_add_strikeout.clicked.connect(self.add_strikeout_annotation)
        btn_add_underline.clicked.connect(self.add_underline_annotation)
        btn_add_line.clicked.connect(self.add_line_annotation)
        btn_add_arrow.clicked.connect(self.add_arrow_annotation)
        btn_add_image.clicked.connect(self.add_image_annotation)
        btn_add_redact.clicked.connect(self.add_redaction_annotation)
        btn_add_note.clicked.connect(self.add_note_annotation)
        btn_add_freehand.clicked.connect(self.add_freehand_annotation)
        btn_replace_text.clicked.connect(self.replace_text_annotation)
        btn_edit_text.clicked.connect(self.edit_text_tool)
        btn_reorder.clicked.connect(self.reorder_pages_to_new_pdf)
        btn_remove_empty.clicked.connect(self.remove_empty_pages_to_new_pdf)
        btn_search.clicked.connect(self.open_search_and_run)
        btn_search_close.clicked.connect(self.close_search_panel)
        self.search_query.returnPressed.connect(self.search_all_pages)
        btn_hit_prev.clicked.connect(self.prev_search_hit)
        btn_hit_next.clicked.connect(self.next_search_hit)
        self.btn_cancel_ocr.clicked.connect(self.cancel_ocr)
        self.btn_retry_failed_ocr.clicked.connect(self.retry_failed_ocr_pages)
        self.btn_reset_ocr_prefs.clicked.connect(self.reset_ocr_preferences)

        self.annotation_tool_kind = "text"
        self.annotation_tool_buttons: dict[str, QPushButton] = {
            "text": btn_add_text,
            "rect": btn_add_rect,
            "ellipse": btn_add_ellipse,
            "star": btn_add_star,
            "link": btn_add_link,
            "highlight": btn_add_highlight,
            "strikeout": btn_add_strikeout,
            "underline": btn_add_underline,
            "line": btn_add_line,
            "arrow": btn_add_arrow,
            "image": btn_add_image,
            "redact": btn_add_redact,
            "note": btn_add_note,
            "freehand": btn_add_freehand,
            "text-replace": btn_replace_text,
            "text-edit": btn_edit_text,
        }

        self.annotation_panel = QWidget()
        self.annotation_panel.setProperty("role", "sidepanel")
        self.annotation_panel.setMinimumWidth(240)
        self.annotation_panel.setMaximumWidth(360)
        annotation_layout = QVBoxLayout(self.annotation_panel)
        annotation_layout.setContentsMargins(12, 12, 12, 12)
        annotation_layout.setSpacing(10)
        annotation_title = QLabel("Annotieren")
        annotation_title.setProperty("role", "paneltitle")
        annotation_layout.addWidget(annotation_title)
        annotation_subtitle = QLabel("Werkzeuge und Eigenschaften direkt in der Sidebar – ohne Dialog-Stapel.")
        annotation_subtitle.setWordWrap(True)
        annotation_subtitle.setProperty("role", "panelsubtitle")
        annotation_layout.addWidget(annotation_subtitle)

        tool_card = QWidget()
        tool_card.setProperty("role", "panelcard")
        tool_card_layout = QVBoxLayout(tool_card)
        tool_card_layout.setContentsMargins(12, 12, 12, 12)
        tool_card_layout.setSpacing(10)
        tool_card_title = QLabel("Werkzeuge")
        tool_card_title.setProperty("role", "cardtitle")
        tool_card_layout.addWidget(tool_card_title)
        tool_grid = QGridLayout()
        tool_grid.setHorizontalSpacing(8)
        tool_grid.setVerticalSpacing(8)
        tool_grid.addWidget(btn_add_text, 0, 0)
        tool_grid.addWidget(btn_add_rect, 0, 1)
        tool_grid.addWidget(btn_add_ellipse, 1, 0)
        tool_grid.addWidget(btn_add_line, 1, 1)
        tool_grid.addWidget(btn_add_arrow, 2, 0)
        tool_grid.addWidget(btn_add_freehand, 2, 1)
        tool_grid.addWidget(btn_add_highlight, 3, 0)
        tool_grid.addWidget(btn_add_strikeout, 3, 1)
        tool_grid.addWidget(btn_add_underline, 4, 0)
        tool_grid.addWidget(btn_add_link, 4, 1)
        tool_grid.addWidget(btn_add_image, 5, 0)
        tool_grid.addWidget(btn_add_note, 5, 1)
        tool_grid.addWidget(btn_add_redact, 6, 0)
        tool_grid.addWidget(btn_replace_text, 6, 1)
        tool_grid.addWidget(btn_edit_text, 7, 0)
        tool_grid.addWidget(btn_add_star, 7, 1)
        tool_card_layout.addLayout(tool_grid)
        annotation_layout.addWidget(tool_card)

        properties_card = QWidget()
        properties_card.setProperty("role", "panelcard")
        properties_layout = QVBoxLayout(properties_card)
        properties_layout.setContentsMargins(12, 12, 12, 12)
        properties_layout.setSpacing(8)
        properties_title = QLabel("Eigenschaften")
        properties_title.setProperty("role", "cardtitle")
        properties_layout.addWidget(properties_title)

        self.annotation_form_hint = QLabel("Text hinzufügen")
        self.annotation_form_hint.setProperty("role", "panelinfo")
        properties_layout.addWidget(self.annotation_form_hint)

        self.annotation_text_label = QLabel("Text")
        self.annotation_text_label.setProperty("role", "fieldlabel")
        properties_layout.addWidget(self.annotation_text_label)
        self.annotation_text_input = QTextEdit()
        self.annotation_text_input.setPlaceholderText("Text für die Annotation …")
        self.annotation_text_input.setFixedHeight(84)
        properties_layout.addWidget(self.annotation_text_input)

        self.annotation_image_label = QLabel("Bild / Signatur")
        self.annotation_image_label.setProperty("role", "fieldlabel")
        properties_layout.addWidget(self.annotation_image_label)
        self.annotation_image_path_label = QLabel("Keine Datei ausgewählt")
        self.annotation_image_path_label.setProperty("role", "panelinfo")
        properties_layout.addWidget(self.annotation_image_path_label)
        self.btn_annotation_pick_image = _action_btn("Datei wählen", "Bilddatei für Signatur, Stempel oder Overlay auswählen")
        self.btn_annotation_pick_image.clicked.connect(self.pick_annotation_image)
        properties_layout.addWidget(self.btn_annotation_pick_image)

        size_grid = QGridLayout()
        size_grid.setHorizontalSpacing(8)
        size_grid.setVerticalSpacing(8)

        self.annotation_width_label = QLabel("Breite %")
        self.annotation_width_label.setProperty("role", "fieldlabel")
        self.annotation_width_spin = QDoubleSpinBox()
        self.annotation_width_spin.setRange(1.0, 100.0)
        self.annotation_width_spin.setDecimals(1)
        self.annotation_width_spin.setSingleStep(1.0)
        self.annotation_width_spin.setSuffix(" %")
        self.annotation_width_spin.setValue(35.0)
        size_grid.addWidget(self.annotation_width_label, 0, 0)
        size_grid.addWidget(self.annotation_width_spin, 0, 1)

        self.annotation_height_label = QLabel("Höhe %")
        self.annotation_height_label.setProperty("role", "fieldlabel")
        self.annotation_height_spin = QDoubleSpinBox()
        self.annotation_height_spin.setRange(1.0, 100.0)
        self.annotation_height_spin.setDecimals(1)
        self.annotation_height_spin.setSingleStep(1.0)
        self.annotation_height_spin.setSuffix(" %")
        self.annotation_height_spin.setValue(12.0)
        size_grid.addWidget(self.annotation_height_label, 1, 0)
        size_grid.addWidget(self.annotation_height_spin, 1, 1)

        self.annotation_font_label = QLabel("Schriftgröße")
        self.annotation_font_label.setProperty("role", "fieldlabel")
        self.annotation_font_size_spin = QSpinBox()
        self.annotation_font_size_spin.setRange(6, 72)
        self.annotation_font_size_spin.setSuffix(" pt")
        self.annotation_font_size_spin.setValue(12)
        size_grid.addWidget(self.annotation_font_label, 2, 0)
        size_grid.addWidget(self.annotation_font_size_spin, 2, 1)

        self.annotation_line_width_label = QLabel("Linienstärke")
        self.annotation_line_width_label.setProperty("role", "fieldlabel")
        self.annotation_line_width_spin = QDoubleSpinBox()
        self.annotation_line_width_spin.setRange(0.5, 20.0)
        self.annotation_line_width_spin.setDecimals(1)
        self.annotation_line_width_spin.setSingleStep(0.5)
        self.annotation_line_width_spin.setSuffix(" pt")
        self.annotation_line_width_spin.setValue(2.0)
        size_grid.addWidget(self.annotation_line_width_label, 3, 0)
        size_grid.addWidget(self.annotation_line_width_spin, 3, 1)
        properties_layout.addLayout(size_grid)

        self.annotation_font_family_label = QLabel("Schriftart")
        self.annotation_font_family_label.setProperty("role", "fieldlabel")
        properties_layout.addWidget(self.annotation_font_family_label)
        self.annotation_font_family_combo = QComboBox()
        # Base-14-Fonts, die PyMuPDF ohne externe Schriftdatei einbetten kann.
        for label in ("Helvetica", "Times", "Courier"):
            self.annotation_font_family_combo.addItem(label)
        properties_layout.addWidget(self.annotation_font_family_combo)

        self.annotation_font_style_row = QHBoxLayout()
        self.annotation_font_style_row.setSpacing(6)
        self.annotation_bold_check = QCheckBox("Fett")
        self.annotation_italic_check = QCheckBox("Kursiv")
        self.annotation_font_style_row.addWidget(self.annotation_bold_check)
        self.annotation_font_style_row.addWidget(self.annotation_italic_check)
        self.annotation_font_style_row.addStretch(1)
        properties_layout.addLayout(self.annotation_font_style_row)

        self.annotation_color_label = QLabel("Farbe")
        self.annotation_color_label.setProperty("role", "fieldlabel")
        properties_layout.addWidget(self.annotation_color_label)
        self.annotation_color_input = QLineEdit()
        self.annotation_color_input.setPlaceholderText("z.B. 220, 20, 60 oder #dc143c")
        self.annotation_color_input.textChanged.connect(self._refresh_annotation_color_buttons)
        properties_layout.addWidget(self.annotation_color_input)

        self.annotation_color_row = QHBoxLayout()
        self.annotation_color_row.setSpacing(6)
        self.annotation_color_buttons: list[tuple[QPushButton, str]] = []
        for color_hex in ("#dc143c", "#0078d7", "#ffeb3b", "#10b981", "#111827", "#ffffff"):
            chip = QPushButton("")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFixedSize(28, 28)
            chip.setProperty("btnRole", "colorchip")
            chip.clicked.connect(lambda _checked=False, value=color_hex: self._set_annotation_color(value))
            self.annotation_color_buttons.append((chip, color_hex))
            self.annotation_color_row.addWidget(chip)
        self.annotation_color_row.addStretch(1)
        properties_layout.addLayout(self.annotation_color_row)

        self.btn_activate_annotation = _primary_btn("Werkzeug aktivieren", "Ausgewähltes Werkzeug mit diesen Eigenschaften starten")
        self.btn_activate_annotation.clicked.connect(self.activate_selected_annotation_tool)
        self.btn_cancel_annotation_mode = _action_btn("Modus verlassen", "Aktiven Annotationsmodus beenden")
        self.btn_cancel_annotation_mode.clicked.connect(self._clear_pending_annotation)
        action_row = QVBoxLayout()
        action_row.setSpacing(6)
        action_row.addWidget(self.btn_activate_annotation)
        action_row.addWidget(self.btn_cancel_annotation_mode)
        properties_layout.addLayout(action_row)
        annotation_layout.addWidget(properties_card)

        self.annotation_selection_label = QLabel("Keine Annotation ausgewählt")
        self.annotation_selection_label.setWordWrap(True)
        self.annotation_selection_label.setProperty("role", "panelinfo")
        annotation_layout.addWidget(self.annotation_selection_label)
        self.btn_edit_annotation_style = _action_btn("Stil bearbeiten", "Farbe und Linienstärke der ausgewählten Annotation anpassen")
        self.btn_edit_annotation_style.clicked.connect(self.edit_selected_annotation_style)
        self.btn_edit_annotation_style.setEnabled(False)
        annotation_layout.addWidget(self.btn_edit_annotation_style)
        self.btn_edit_annotation_comment = _action_btn("Kommentar bearbeiten", "Kommentar/Inhalt der ausgewählten Annotation bearbeiten")
        self.btn_edit_annotation_comment.clicked.connect(self.edit_selected_annotation_comment)
        self.btn_edit_annotation_comment.setEnabled(False)
        annotation_layout.addWidget(self.btn_edit_annotation_comment)
        self.btn_reply_annotation = _action_btn("Antwort hinzufügen", "Antwort-Notiz zur ausgewählten Annotation hinzufügen")
        self.btn_reply_annotation.clicked.connect(self.reply_to_selected_annotation)
        self.btn_reply_annotation.setEnabled(False)
        annotation_layout.addWidget(self.btn_reply_annotation)
        self.btn_delete_annotation = _action_btn("Auswahl löschen", "Ausgewählte Annotation entfernen")
        self.btn_delete_annotation.clicked.connect(self.delete_selected_annotation)
        self.btn_delete_annotation.setEnabled(False)
        annotation_layout.addWidget(self.btn_delete_annotation)
        self.btn_apply_redactions = _action_btn("Schwärzungen final anwenden", "Alle platzierten Schwärzungen endgültig in das PDF einbrennen")
        self.btn_apply_redactions.clicked.connect(self.apply_pending_redactions)
        annotation_layout.addWidget(self.btn_apply_redactions)

        # ── Kommentare / Annotationsübersicht ─────────────────────────────
        self.comment_card = QWidget()
        self.comment_card.setProperty("role", "panelcard")
        comment_layout = QVBoxLayout(self.comment_card)
        comment_layout.setContentsMargins(12, 12, 12, 12)
        comment_layout.setSpacing(8)
        comment_title = QLabel("Kommentare")
        comment_title.setProperty("role", "cardtitle")
        comment_layout.addWidget(comment_title)
        comment_hint = QLabel("Alle Annotationen des Dokuments – anklicken zum Anspringen.")
        comment_hint.setWordWrap(True)
        comment_hint.setProperty("role", "panelsubtitle")
        comment_layout.addWidget(comment_hint)
        self.comment_list = QListWidget()
        self.comment_list.setMinimumHeight(120)
        self.comment_list.itemClicked.connect(self._on_comment_item_clicked)
        comment_layout.addWidget(self.comment_list)
        comment_btn_row = QHBoxLayout()
        comment_btn_row.setSpacing(6)
        self.btn_toggle_resolved = _action_btn("Erledigt umschalten", "Ausgewählten Kommentar als erledigt markieren / wieder öffnen")
        self.btn_toggle_resolved.clicked.connect(self.toggle_selected_comment_resolved)
        self.btn_refresh_comments = _action_btn("Aktualisieren", "Kommentarliste neu aufbauen")
        self.btn_refresh_comments.clicked.connect(self._refresh_comment_list)
        comment_btn_row.addWidget(self.btn_toggle_resolved)
        comment_btn_row.addWidget(self.btn_refresh_comments)
        comment_layout.addLayout(comment_btn_row)
        annotation_layout.addWidget(self.comment_card)

        # ── Inline-Textbearbeitung ────────────────────────────────────────
        self.inline_edit_card = QWidget()
        self.inline_edit_card.setProperty("role", "panelcard")
        inline_edit_layout = QVBoxLayout(self.inline_edit_card)
        inline_edit_layout.setContentsMargins(12, 12, 12, 12)
        inline_edit_layout.setSpacing(8)
        inline_edit_title = QLabel("Text bearbeiten")
        inline_edit_title.setProperty("role", "cardtitle")
        inline_edit_layout.addWidget(inline_edit_title)
        self.inline_text_edit = QTextEdit()
        self.inline_text_edit.setPlaceholderText("Text der ausgewählten Annotation oder des Formularfelds …")
        self.inline_text_edit.setFixedHeight(100)
        inline_edit_layout.addWidget(self.inline_text_edit)
        self.btn_save_inline = _primary_btn("Änderung speichern", "Text direkt in der Annotation oder im Formularfeld speichern – kein Dialog")
        self.btn_save_inline.clicked.connect(self.save_inline_text_edit)
        inline_edit_layout.addWidget(self.btn_save_inline)
        self.inline_edit_card.setVisible(False)
        annotation_layout.addWidget(self.inline_edit_card)

        annotation_layout.addStretch(1)

        self.annotation_panel_scroll = QScrollArea()
        self.annotation_panel_scroll.setWidget(self.annotation_panel)
        self.annotation_panel_scroll.setWidgetResizable(True)
        self.annotation_panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.annotation_panel_scroll.setProperty("role", "previewarea")
        self.annotation_panel_scroll.setMinimumWidth(240)
        self.annotation_panel_scroll.setMaximumWidth(372)
        self.annotation_panel_scroll.setFrameShape(QFrame.Shape.NoFrame)

        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar_widget = QWidget()
        toolbar_widget.setProperty("role", "toolbar")
        toolbar_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar_widget.setMinimumHeight(104)

        def _ribbon_group(title: str, widgets: list[QWidget]) -> QWidget:
            group = QWidget()
            group.setProperty("role", "ribbongroup")
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(9, 7, 9, 7)
            group_layout.setSpacing(5)
            label = QLabel(title)
            label.setProperty("role", "ribbontitle")
            group_layout.addWidget(label)
            row = QHBoxLayout()
            row.setSpacing(5)
            for widget in widgets:
                row.addWidget(widget)
            row.addStretch(1)
            group_layout.addLayout(row)
            return group

        def _ribbon_tab(groups: list[QWidget]) -> QWidget:
            page = QWidget()
            page.setProperty("role", "ribbontab")
            page_layout = QHBoxLayout(page)
            page_layout.setContentsMargins(10, 6, 10, 6)
            page_layout.setSpacing(8)
            for group in groups:
                page_layout.addWidget(group)
            page_layout.addStretch(1)
            return page

        # Getabbte Ribbon-Leiste (OnlyOffice-artig) statt einzeiliger Toolbar.
        self.ribbon_tabs = QTabWidget()
        self.ribbon_tabs.setProperty("role", "ribbon")
        self.ribbon_tabs.addTab(
            _ribbon_tab([_ribbon_group("Datei", [btn_open, btn_save, btn_saveas])]),
            "Datei",
        )
        self.ribbon_tabs.addTab(
            _ribbon_tab([
                _ribbon_group("Navigation", [btn_first, btn_prev, btn_next, btn_last, btn_goto]),
                _ribbon_group("Bearbeiten", [self.btn_undo, self.btn_redo]),
                _ribbon_group("Suche", [btn_search]),
            ]),
            "Start",
        )
        self.ribbon_tabs.addTab(
            _ribbon_tab([
                _ribbon_group("Zoom", [btn_zoom_out, btn_zoom_in, btn_zoom_reset]),
                _ribbon_group("Drehen", [btn_rotate_left, btn_rotate_right, btn_rotate_reset]),
            ]),
            "Ansicht",
        )
        self.ribbon_tabs.addTab(
            _ribbon_tab([
                _ribbon_group("Seiten", [btn_duplicate, btn_blank_page, btn_reorder, btn_remove_empty]),
            ]),
            "Seiten",
        )
        self.ribbon_tabs.addTab(
            _ribbon_tab([
                _ribbon_group("OCR", [btn_extract, btn_extract_all, btn_auto_ocr_name]),
            ]),
            "OCR",
        )
        self.ribbon_tabs.addTab(
            _ribbon_tab([
                _ribbon_group("Werkzeuge", [btn_split, btn_crop, btn_form_fields, btn_merge]),
            ]),
            "Werkzeuge",
        )
        self.ribbon_tabs.setCurrentIndex(1)  # "Start" als Standard

        toolbar_outer = QVBoxLayout(toolbar_widget)
        toolbar_outer.setContentsMargins(0, 0, 0, 0)
        toolbar_outer.setSpacing(0)
        toolbar_outer.addWidget(self.ribbon_tabs)

        # ── Dateiname-Zeile ──────────────────────────────────────────────────
        name_widget = QWidget()
        name_widget.setProperty("role", "namebar")
        name_layout = QHBoxLayout(name_widget)
        name_layout.setContentsMargins(10, 8, 10, 8)
        name_layout.setSpacing(8)
        lbl_name = QLabel("Dateiname")
        lbl_name.setProperty("role", "fieldlabel")
        name_layout.addWidget(lbl_name)
        name_layout.addWidget(self.suggested_name, 1)
        self.page_info.setProperty("role", "pageinfo")
        name_layout.addWidget(self.page_info)

        # ── Suchleiste ───────────────────────────────────────────────────────
        self.search_bar_widget = QWidget()
        self.search_bar_widget.setProperty("role", "searchbar")
        self.search_bar_widget.setVisible(False)
        search_layout = QHBoxLayout(self.search_bar_widget)
        search_layout.setContentsMargins(10, 8, 10, 8)
        search_layout.setSpacing(6)
        lbl_search = QLabel("Suche")
        lbl_search.setProperty("role", "fieldlabel")
        search_layout.addWidget(lbl_search)
        search_layout.addWidget(self.search_query, 1)
        search_layout.addWidget(btn_search)
        search_layout.addWidget(btn_hit_prev)
        search_layout.addWidget(btn_hit_next)
        search_layout.addWidget(self.search_counter)
        search_layout.addWidget(btn_search_close)

        # ── OCR-Statusleiste ─────────────────────────────────────────────────
        ocr_bar = QWidget()
        ocr_bar.setProperty("role", "ocrbar")
        ocr_layout = QHBoxLayout(ocr_bar)
        ocr_layout.setContentsMargins(10, 8, 10, 8)
        ocr_layout.setSpacing(8)
        ocr_layout.addWidget(self.ocr_feedback, 1)
        ocr_layout.addWidget(self.annotation_hint)
        ocr_layout.addWidget(self.ocr_mode_label)
        ocr_layout.addWidget(self.btn_cancel_ocr)
        ocr_layout.addWidget(self.btn_retry_failed_ocr)
        ocr_layout.addWidget(self.btn_reset_ocr_prefs)

        # ── Splitter (Thumbnails + Vorschau) ─────────────────────────────────
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(6)
        self.content_splitter.addWidget(self.thumb_panel)
        self.content_splitter.addWidget(self.preview_scroll)
        self.content_splitter.addWidget(self.annotation_panel_scroll)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setStretchFactor(2, 0)
        self.content_splitter.setSizes([170, 840, 310])

        # ── Haupt-Layout ─────────────────────────────────────────────────────
        toolbar_scroll = QScrollArea()
        toolbar_scroll.setWidget(toolbar_widget)
        toolbar_scroll.setWidgetResizable(True)
        toolbar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        toolbar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        toolbar_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar_scroll.setMinimumHeight(112)
        toolbar_scroll.setMaximumHeight(140)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar_scroll)
        layout.addWidget(name_widget)
        layout.addWidget(self.search_bar_widget)
        layout.addWidget(self.search_results_list)
        preview_shell = QWidget()
        preview_shell.setProperty("role", "previewcard")
        preview_shell_layout = QVBoxLayout(preview_shell)
        preview_shell_layout.setContentsMargins(0, 0, 0, 0)
        preview_shell_layout.setSpacing(0)
        preview_shell_layout.addWidget(self.content_splitter, 1)
        layout.addWidget(preview_shell, 1)
        layout.addWidget(ocr_bar)

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

        act_import_office = QAction("Office-Dokument öffnen (→ PDF) …", self)
        act_import_office.triggered.connect(self.import_office_as_pdf)
        menu_file.addAction(act_import_office)

        menu_file.addSeparator()

        act_save = QAction("Speichern", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self.save_in_place)
        menu_file.addAction(act_save)

        act_save_as = QAction("Speichern als …", self)
        act_save_as.setShortcut("Ctrl+Shift+S")
        act_save_as.triggered.connect(self.save_as_suggested)
        menu_file.addAction(act_save_as)

        act_export_docx = QAction("Herunterladen als Word (DOCX) …", self)
        act_export_docx.triggered.connect(self.export_as_office)
        menu_file.addAction(act_export_docx)

        act_print = QAction("Drucken …", self)
        act_print.setShortcut("Ctrl+P")
        act_print.triggered.connect(self.print_document)
        menu_file.addAction(act_print)

        act_edit_metadata = QAction("Metadaten bearbeiten …", self)
        act_edit_metadata.triggered.connect(self.edit_pdf_metadata)
        menu_file.addAction(act_edit_metadata)

        act_scrub_metadata = QAction("Metadaten & versteckte Daten bereinigen …", self)
        act_scrub_metadata.triggered.connect(self.scrub_metadata)
        menu_file.addAction(act_scrub_metadata)

        act_encrypt_pdf = QAction("PDF mit Passwort schützen …", self)
        act_encrypt_pdf.triggered.connect(self.export_encrypted_pdf_copy)
        menu_file.addAction(act_encrypt_pdf)

        act_decrypt_pdf = QAction("PDF entschlüsselt speichern …", self)
        act_decrypt_pdf.triggered.connect(self.export_decrypted_pdf_copy)
        menu_file.addAction(act_decrypt_pdf)

        act_optimize_pdf = QAction("PDF optimieren / komprimieren …", self)
        act_optimize_pdf.triggered.connect(self.export_optimized_pdf_copy)
        menu_file.addAction(act_optimize_pdf)

        act_document_info = QAction("Dokumentinfos anzeigen …", self)
        act_document_info.triggered.connect(self.show_document_info)
        menu_file.addAction(act_document_info)

        act_apply_preset = QAction("Preset-Profil anwenden …", self)
        act_apply_preset.triggered.connect(self.apply_export_preset)
        menu_file.addAction(act_apply_preset)

        menu_file.addSeparator()

        act_undo = QAction("Rückgängig", self)
        act_undo.setShortcut("Ctrl+Z")
        act_undo.triggered.connect(self.undo_last_change)
        menu_file.addAction(act_undo)

        act_redo = QAction("Wiederholen", self)
        act_redo.setShortcuts(["Ctrl+Y", "Ctrl+Shift+Z"])
        act_redo.triggered.connect(self.redo_last_change)
        menu_file.addAction(act_redo)

        menu_view = self.menuBar().addMenu("Ansicht")
        act_fit_width = QAction("An Breite anpassen", self)
        act_fit_width.setShortcut("Ctrl+Shift+W")
        act_fit_width.triggered.connect(self.fit_to_width)
        menu_view.addAction(act_fit_width)

        act_fit_page = QAction("An Seite anpassen", self)
        act_fit_page.setShortcut("Ctrl+Shift+P")
        act_fit_page.triggered.connect(self.fit_to_page)
        menu_view.addAction(act_fit_page)

        act_zoom_reset_menu = QAction("Zoom zurücksetzen (100 %)", self)
        act_zoom_reset_menu.setShortcut("Ctrl+0")
        act_zoom_reset_menu.triggered.connect(self.reset_zoom)
        menu_view.addAction(act_zoom_reset_menu)

        menu_ocr = self.menuBar().addMenu("OCR und Text")
        act_extract_current = QAction("Text auf aktueller Seite erkennen", self)
        act_extract_current.setShortcut("Ctrl+E")
        act_extract_current.triggered.connect(self.extract_text_and_suggest)
        menu_ocr.addAction(act_extract_current)

        act_extract = QAction("Text aller Seiten erkennen", self)
        act_extract.setShortcut("Ctrl+Shift+E")
        act_extract.triggered.connect(self.recognize_text_all_pages_and_suggest)
        menu_ocr.addAction(act_extract)

        act_retry_failed_ocr = QAction("Nur fehlgeschlagene OCR-Seiten erneut versuchen", self)
        act_retry_failed_ocr.triggered.connect(self.retry_failed_ocr_pages)
        menu_ocr.addAction(act_retry_failed_ocr)

        act_ocr_and_name = QAction("OCR + Dateinamen vorschlagen", self)
        act_ocr_and_name.setShortcut("Ctrl+Shift+R")
        act_ocr_and_name.triggered.connect(self.ocr_and_suggest_filename)
        menu_ocr.addAction(act_ocr_and_name)

        act_ocr_lang = QAction("OCR-Sprache wählen …", self)
        act_ocr_lang.triggered.connect(self.choose_ocr_language)
        menu_ocr.addAction(act_ocr_lang)

        act_ocr_correction_mode = QAction("OCR-Korrekturmodus wählen …", self)
        act_ocr_correction_mode.triggered.connect(self.choose_ocr_correction_mode)
        menu_ocr.addAction(act_ocr_correction_mode)

        act_ocr_reset = QAction("OCR-Einstellungen zurücksetzen", self)
        act_ocr_reset.triggered.connect(self.reset_ocr_preferences)
        menu_ocr.addAction(act_ocr_reset)

        act_show_text = QAction("Erkannten Text anzeigen", self)
        act_show_text.setShortcut("Ctrl+T")
        act_show_text.triggered.connect(self.show_extracted_text_window)
        menu_ocr.addAction(act_show_text)

        act_search = QAction("In allen Seiten suchen", self)
        act_search.setShortcut("Ctrl+F")
        act_search.triggered.connect(self.open_search)
        menu_ocr.addAction(act_search)

        act_search_next = QAction("Nächsten Suchtreffer", self)
        act_search_next.setShortcut("F3")
        act_search_next.triggered.connect(self.next_search_hit)
        menu_ocr.addAction(act_search_next)

        act_search_prev = QAction("Vorherigen Suchtreffer", self)
        act_search_prev.setShortcut("Shift+F3")
        act_search_prev.triggered.connect(self.prev_search_hit)
        menu_ocr.addAction(act_search_prev)

        act_searchable_pdf = QAction("Durchsuchbare PDF-Kopie erstellen …", self)
        act_searchable_pdf.triggered.connect(self.export_searchable_pdf_copy)
        menu_ocr.addAction(act_searchable_pdf)

        menu_tools = self.menuBar().addMenu("PDF-Werkzeuge")
        act_split = QAction("Seiten extrahieren", self)
        act_split.triggered.connect(self.extract_pages_to_new_pdf)
        menu_tools.addAction(act_split)

        act_reorder = QAction("Seiten neu anordnen", self)
        act_reorder.triggered.connect(self.reorder_pages_to_new_pdf)
        menu_tools.addAction(act_reorder)

        act_crop = QAction("Seite visuell zuschneiden", self)
        act_crop.triggered.connect(self.start_visual_crop)
        menu_tools.addAction(act_crop)
        act_crop_dialog = QAction("Seiten zuschneiden …", self)
        act_crop_dialog.triggered.connect(self.crop_pages_to_new_pdf)
        menu_tools.addAction(act_crop_dialog)

        act_add_text = QAction("Text hinzufügen …", self)
        act_add_text.triggered.connect(self.add_text_annotation)
        menu_tools.addAction(act_add_text)

        act_edit_text = QAction("Vorhandenen Text bearbeiten …", self)
        act_edit_text.triggered.connect(self.edit_text_tool)
        menu_tools.addAction(act_edit_text)

        act_add_rect = QAction("Rechteck hinzufügen …", self)
        act_add_rect.triggered.connect(self.add_rectangle_annotation)
        menu_tools.addAction(act_add_rect)

        act_add_ellipse = QAction("Ellipse / Kreis hinzufügen …", self)
        act_add_ellipse.triggered.connect(self.add_ellipse_annotation)
        menu_tools.addAction(act_add_ellipse)

        act_add_star = QAction("Stern hinzufügen …", self)
        act_add_star.triggered.connect(self.add_star_annotation)
        menu_tools.addAction(act_add_star)

        act_insert_symbol = QAction("Symbol / Sonderzeichen einfügen …", self)
        act_insert_symbol.triggered.connect(self.insert_symbol)
        menu_tools.addAction(act_insert_symbol)

        act_insert_textart = QAction("TextArt (stilisierter Text) einfügen …", self)
        act_insert_textart.triggered.connect(self.insert_textart)
        menu_tools.addAction(act_insert_textart)

        act_add_link = QAction("Hyperlink einfügen …", self)
        act_add_link.triggered.connect(self.add_link_annotation)
        menu_tools.addAction(act_add_link)

        act_add_highlight = QAction("Markierung hinzufügen …", self)
        act_add_highlight.triggered.connect(self.add_highlight_annotation)
        menu_tools.addAction(act_add_highlight)

        act_add_strikeout = QAction("Text durchstreichen …", self)
        act_add_strikeout.triggered.connect(self.add_strikeout_annotation)
        menu_tools.addAction(act_add_strikeout)

        act_add_underline = QAction("Text unterstreichen …", self)
        act_add_underline.triggered.connect(self.add_underline_annotation)
        menu_tools.addAction(act_add_underline)

        act_add_line = QAction("Linie hinzufügen …", self)
        act_add_line.triggered.connect(self.add_line_annotation)
        menu_tools.addAction(act_add_line)

        act_add_arrow = QAction("Pfeil hinzufügen …", self)
        act_add_arrow.triggered.connect(self.add_arrow_annotation)
        menu_tools.addAction(act_add_arrow)

        act_apply_redactions = QAction("Schwärzungen final anwenden", self)
        act_apply_redactions.triggered.connect(self.apply_pending_redactions)
        menu_tools.addAction(act_apply_redactions)

        act_merge = QAction("PDFs zusammenführen", self)
        act_merge.triggered.connect(self.merge_pdfs)
        menu_tools.addAction(act_merge)

        act_images_to_pdf = QAction("Bilder zu PDF umwandeln …", self)
        act_images_to_pdf.triggered.connect(self.images_to_pdf)
        menu_tools.addAction(act_images_to_pdf)

        act_split_chunks = QAction("PDF in Blöcke teilen …", self)
        act_split_chunks.triggered.connect(self.split_pdf_into_chunks)
        menu_tools.addAction(act_split_chunks)

        act_remove_empty = QAction("Leere Seiten entfernen", self)
        act_remove_empty.triggered.connect(self.remove_empty_pages_to_new_pdf)
        menu_tools.addAction(act_remove_empty)

        act_rotate_left = QAction("Seite links drehen", self)
        act_rotate_left.setShortcut("Ctrl+Alt+Left")
        act_rotate_left.triggered.connect(self.rotate_left)
        menu_tools.addAction(act_rotate_left)

        act_rotate_right = QAction("Seite rechts drehen", self)
        act_rotate_right.setShortcut("Ctrl+Alt+Right")
        act_rotate_right.triggered.connect(self.rotate_right)
        menu_tools.addAction(act_rotate_right)

        act_delete_pages = QAction("Ausgewählte Seiten löschen", self)
        act_delete_pages.setShortcut("Delete")
        act_delete_pages.triggered.connect(self.delete_selected_pages)
        menu_tools.addAction(act_delete_pages)

        act_duplicate_pages = QAction("Ausgewählte Seiten duplizieren", self)
        act_duplicate_pages.triggered.connect(self.duplicate_selected_pages)
        menu_tools.addAction(act_duplicate_pages)

        act_insert_blank_page = QAction("Leere Seite einfügen", self)
        act_insert_blank_page.triggered.connect(self.insert_blank_page_after_current)
        menu_tools.addAction(act_insert_blank_page)

        act_edit_form_fields = QAction("Formularfelder bearbeiten …", self)
        act_edit_form_fields.triggered.connect(self.edit_form_fields_on_current_page)
        menu_tools.addAction(act_edit_form_fields)

        act_add_page_numbers = QAction("Seitennummern einfügen …", self)
        act_add_page_numbers.triggered.connect(self.insert_page_numbers)
        menu_tools.addAction(act_add_page_numbers)

        act_add_header_footer = QAction("Kopf-/Fußzeile einfügen …", self)
        act_add_header_footer.triggered.connect(self.insert_header_footer)
        menu_tools.addAction(act_add_header_footer)

        act_insert_table = QAction("Tabelle einfügen …", self)
        act_insert_table.triggered.connect(self.insert_table)
        menu_tools.addAction(act_insert_table)

        act_add_watermark = QAction("Wasserzeichen einfügen …", self)
        act_add_watermark.triggered.connect(self.add_text_watermark)
        menu_tools.addAction(act_add_watermark)

        act_delete_annotation = QAction("Ausgewählte Annotation löschen", self)
        act_delete_annotation.setShortcut("Backspace")
        act_delete_annotation.triggered.connect(self.delete_selected_annotation)
        menu_tools.addAction(act_delete_annotation)

        menu_export = self.menuBar().addMenu("Exportieren")
        act_export_current = QAction("Aktuelle Datei exportieren …", self)
        act_export_current.triggered.connect(self.export_current_file)
        menu_export.addAction(act_export_current)

        act_export_pages_images = QAction("Seiten als Bilder exportieren …", self)
        act_export_pages_images.triggered.connect(self.export_pages_as_images)
        menu_export.addAction(act_export_pages_images)

        act_export_grayscale = QAction("PDF als Graustufen-Kopie exportieren …", self)
        act_export_grayscale.triggered.connect(self.export_grayscale_pdf_copy)
        menu_export.addAction(act_export_grayscale)

        act_extract_images = QAction("Bilder aus PDF extrahieren …", self)
        act_extract_images.triggered.connect(self.extract_images_from_pdf)
        menu_export.addAction(act_extract_images)

        act_page_overview = QAction("Seitenübersicht anzeigen …", self)
        act_page_overview.triggered.connect(self.show_page_overview)
        menu_export.addAction(act_page_overview)

        act_export_folder = QAction("Ordner aggregiert exportieren …", self)
        act_export_folder.triggered.connect(self.export_folder_aggregate)
        menu_export.addAction(act_export_folder)

        act_batch_rename = QAction("Ordner stapelweise umbenennen …", self)
        act_batch_rename.triggered.connect(self.batch_rename_folder)
        menu_export.addAction(act_batch_rename)

        self._set_annotation_defaults("text")
        self._sync_annotation_tool_buttons()
        self._refresh_annotation_color_buttons()

        # Improve menu accessibility/discoverability (screen readers + status hints).
        all_actions = [
            act_open,
            act_close_pdf,
            act_import_office,
            act_export_docx,
            act_save,
            act_save_as,
            act_print,
            act_undo,
            act_redo,
            act_fit_width,
            act_fit_page,
            act_zoom_reset_menu,
            act_extract_current,
            act_extract,
            act_retry_failed_ocr,
            act_ocr_and_name,
            act_ocr_lang,
            act_ocr_correction_mode,
            act_ocr_reset,
            act_show_text,
            act_search,
            act_search_next,
            act_search_prev,
            act_searchable_pdf,
            act_split,
            act_reorder,
            act_crop,
            act_add_text,
            act_edit_text,
            act_add_rect,
            act_add_ellipse,
            act_add_star,
            act_insert_symbol,
            act_insert_textart,
            act_add_link,
            act_add_highlight,
            act_add_strikeout,
            act_add_underline,
            act_add_line,
            act_add_arrow,
            act_merge,
            act_split_chunks,
            act_remove_empty,
            act_rotate_left,
            act_rotate_right,
            act_delete_pages,
            act_delete_annotation,
            act_export_current,
            act_export_folder,
            act_batch_rename,
        ]
        for action in all_actions:
            text = action.text().replace("&", "")
            action.setStatusTip(text)
            action.setToolTip(text)
            action.setWhatsThis(text)

        self.statusBar().showMessage("Bereit. Öffne ein PDF, um zu starten.")
        self._update_undo_redo_buttons()
        self._update_ocr_mode_label()
        self._apply_styles()

        # ── Autosave / Crash-Recovery ─────────────────────────────────────
        self._recovery_dir = Path.home() / ".offline-pdf-reader" / "recovery"
        try:
            self._recovery_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._recovery_dir = Path(tempfile.gettempdir())
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(60_000)  # alle 60 s
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start()

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

    def _set_extracted_text(self, text: str) -> None:
        self.extracted_text = text
        if self.text_output_view is not None:
            self.text_output_view.setPlainText(text)

    def show_extracted_text_window(self) -> None:
        if self.text_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Erkannter Text")
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

    def _compose_window_title(self) -> str:
        mode = self.ocr_correction_mode if self.ocr_correction_mode in {"konservativ", "aggressiv"} else "konservativ"
        base = f"{APP_TITLE} (OCR: {self._ocr_lang()} | Modus: {mode})"
        if self.pdf_path:
            base += f" | {self.pdf_path.name}"
        if self.is_dirty:
            base += " *"
        return base

    def _set_dirty(self, dirty: bool) -> None:
        if dirty:
            self.doc_revision += 1
            self._clear_ocr_cache()
            if hasattr(self, "_page_render_cache"):
                self._page_render_cache.clear()
        self.is_dirty = dirty
        self.setWindowTitle(self._compose_window_title())

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
        self._update_thumbnail_meta()

    def _sync_thumbnail_selection(self) -> None:
        if not self.doc:
            return
        if 0 <= self.current_page < self.thumb_list.count():
            self.thumb_list.blockSignals(True)
            self.thumb_list.setCurrentRow(self.current_page)
            self.thumb_list.scrollToItem(self.thumb_list.item(self.current_page))
            self.thumb_list.blockSignals(False)
        self._update_thumbnail_meta()

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
        duplicate_action = QAction("Ausgewählte Seite(n) duplizieren", self)
        duplicate_action.triggered.connect(self.duplicate_selected_pages)
        menu.addSeparator()
        menu.addAction(duplicate_action)
        menu.addAction(delete_action)
        menu.exec(self.thumb_list.mapToGlobal(pos))

    def _reorder_pages_by_thumbnail_order(self) -> None:
        if not self.doc or self.thumb_list.count() != len(self.doc):
            return

        new_order = [int(self.thumb_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.thumb_list.count())]
        if sorted(new_order) != list(range(len(self.doc))):
            return
        if new_order == list(range(len(self.doc))):
            return

        self._push_undo_state()
        old_rotations = dict(self.page_rotations)
        old_current_page = self.current_page

        self.doc.select(new_order)
        old_to_new = {old_idx: new_idx for new_idx, old_idx in enumerate(new_order)}
        self.page_rotations = {
            old_to_new[old_idx]: rot
            for old_idx, rot in old_rotations.items()
            if old_idx in old_to_new and rot % 360 != 0
        }
        self.current_page = old_to_new.get(old_current_page, 0)

        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self._update_search_counter()

        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()
        self.statusBar().showMessage("Seitenreihenfolge per Drag & Drop geändert (noch nicht gespeichert)")

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
        self.selected_annotation_xref = None
        self.selected_widget_xref = None
        self._update_selected_annotation_ui()
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()
        self.statusBar().showMessage(f"{len(selected)} Seite(n) gelöscht (noch nicht gespeichert)")

    def duplicate_selected_pages(self) -> None:
        if not self.doc or len(self.doc) == 0:
            return
        selected = sorted({int(it.data(Qt.ItemDataRole.UserRole)) for it in self.thumb_list.selectedItems()})
        if not selected:
            selected = [self.current_page]

        self._push_undo_state()
        old_total = len(self.doc)
        old_rotations = dict(self.page_rotations)
        selected_set = set(selected)
        source_doc = fitz.open(stream=self.doc.tobytes(), filetype="pdf")
        try:
            offset = 0
            for idx in selected:
                insert_at = idx + 1 + offset
                self.doc.insert_pdf(source_doc, from_page=idx, to_page=idx, start_at=insert_at)
                rot = old_rotations.get(idx, 0) % 360
                if rot:
                    self.doc[insert_at].set_rotation(rot)
                offset += 1
        finally:
            source_doc.close()

        original_positions: dict[int, int] = {}
        duplicate_positions: dict[int, int] = {}
        new_rotations: dict[int, int] = {}
        new_idx = 0
        for old_idx in range(old_total):
            original_positions[old_idx] = new_idx
            rot = old_rotations.get(old_idx, 0) % 360
            if rot:
                new_rotations[new_idx] = rot
            new_idx += 1
            if old_idx in selected_set:
                duplicate_positions[old_idx] = new_idx
                if rot:
                    new_rotations[new_idx] = rot
                new_idx += 1
        self.page_rotations = new_rotations

        if self.current_page in duplicate_positions:
            self.current_page = duplicate_positions[self.current_page]
        else:
            self.current_page = original_positions.get(self.current_page, self.current_page)

        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self._update_search_counter()
        self.selected_annotation_xref = None
        self.selected_widget_xref = None
        self._update_selected_annotation_ui()
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()
        self.statusBar().showMessage(f"{len(selected)} Seite(n) dupliziert (noch nicht gespeichert)")

    def insert_blank_page_after_current(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        width, ok = QInputDialog.getDouble(self, "Leere Seite einfügen", "Breite in pt:", 595.0, 100.0, 5000.0, 1)
        if not ok:
            return
        height, ok = QInputDialog.getDouble(self, "Leere Seite einfügen", "Höhe in pt:", 842.0, 100.0, 5000.0, 1)
        if not ok:
            return
        try:
            self._push_undo_state()
            insert_at = self.current_page + 1
            self.doc.new_page(pno=insert_at, width=width, height=height)
            old_rotations = dict(self.page_rotations)
            self.page_rotations = {
                (idx if idx < insert_at else idx + 1): rot
                for idx, rot in old_rotations.items()
                if rot % 360 != 0
            }
            self.current_page = insert_at
            self.search_hits = []
            self.current_search_hit = -1
            self.search_results_list.clear()
            self._update_search_counter()
            self.selected_annotation_xref = None
            self.selected_widget_xref = None
            self._update_selected_annotation_ui()
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            self.statusBar().showMessage("Leere Seite eingefügt")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Leere Seite konnte nicht eingefügt werden:\n{e}")

    @staticmethod
    def _annotation_subtype_name(annot) -> str:
        try:
            annot_type = getattr(annot, "type", None)
            if isinstance(annot_type, tuple) and len(annot_type) > 1:
                return str(annot_type[1] or "")
        except Exception:
            pass
        return ""

    def _pending_redaction_summary(self) -> tuple[int, list[int]]:
        if not self.doc:
            return 0, []
        total = 0
        pages: list[int] = []
        for idx in range(len(self.doc)):
            count = 0
            for annot in self.doc[idx].annots() or []:
                subtype = self._annotation_subtype_name(annot).strip().casefold()
                if subtype in {"redact", "redaction"}:
                    count += 1
            if count:
                total += count
                pages.append(idx + 1)
        return total, pages

    def apply_pending_redactions(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        total, pages = self._pending_redaction_summary()
        if total <= 0:
            QMessageBox.information(self, "Hinweis", "Es sind aktuell keine platzierten Schwärzungen zum finalen Anwenden vorhanden.")
            return
        page_preview = ", ".join(str(p) for p in pages[:8])
        if len(pages) > 8:
            page_preview += ", …"
        answer = QMessageBox.question(
            self,
            "Schwärzungen final anwenden",
            f"{total} Schwärzung(en) auf {len(pages)} Seite(n) endgültig anwenden?\n\n"
            "Danach kann der geschwärzte Inhalt nicht mehr verschoben oder bearbeitet werden."
            + (f"\n\nBetroffene Seiten: {page_preview}" if page_preview else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._push_undo_state()
            for idx in range(len(self.doc)):
                page = self.doc[idx]
                has_redactions = False
                for annot in page.annots() or []:
                    subtype = self._annotation_subtype_name(annot).strip().casefold()
                    if subtype in {"redact", "redaction"}:
                        has_redactions = True
                        break
                if has_redactions:
                    page.apply_redactions()
            self.selected_annotation_xref = None
            self.selected_widget_xref = None
            self._update_selected_annotation_ui()
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            self.statusBar().showMessage(f"{total} Schwärzung(en) final angewendet")
            QMessageBox.information(self, "Erfolg", f"{total} Schwärzung(en) wurden endgültig angewendet.")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Schwärzungen konnten nicht final angewendet werden:\n{e}")

    def delete_selected_annotation(self) -> None:
        if self.selected_annotation_xref is None or not self.doc or not (0 <= self.current_page < len(self.doc)):
            QMessageBox.information(self, "Hinweis", "Bitte zuerst eine Annotation auswählen.")
            return
        page = self.doc[self.current_page]
        try:
            annot = page.load_annot(self.selected_annotation_xref)
            if annot is None:
                raise ValueError("Annotation nicht gefunden.")
            self._push_undo_state()
            page.delete_annot(annot)
            self.selected_annotation_xref = None
            self.selected_widget_xref = None
            self._update_selected_annotation_ui()
            self._set_dirty(True)
            self._refresh_thumbnails()
            self._refresh_comment_list()
            self.render_current_page()
            self.statusBar().showMessage("Annotation gelöscht")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Annotation konnte nicht gelöscht werden:\n{e}")

    def edit_selected_annotation_comment(self) -> None:
        if self.selected_annotation_xref is None or not self.doc or not (0 <= self.current_page < len(self.doc)):
            QMessageBox.information(self, "Hinweis", "Bitte zuerst eine Annotation auswählen.")
            return
        page = self.doc[self.current_page]
        try:
            annot = page.load_annot(self.selected_annotation_xref)
            if annot is None:
                raise ValueError("Annotation nicht gefunden.")
            current_text = ""
            try:
                info = annot.info or {}
                current_text = str(info.get("content") or info.get("subject") or "")
            except Exception:
                current_text = ""
            text, ok = QInputDialog.getMultiLineText(
                self,
                "Kommentar bearbeiten",
                "Kommentar / Inhalt der Annotation:",
                text=current_text,
            )
            if not ok:
                return
            self._push_undo_state()
            try:
                annot.set_info(content=text)
            except Exception:
                annot.set_info(info={"content": text})
            annot.update()
            self._set_dirty(True)
            self._update_selected_annotation_ui()
            self._refresh_comment_list()
            self.render_current_page()
            self.statusBar().showMessage("Annotation-Kommentar aktualisiert")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Kommentar konnte nicht bearbeitet werden:\n{e}")

    def save_inline_text_edit(self) -> None:
        if not self.doc or not (0 <= self.current_page < len(self.doc)):
            return
        page = self.doc[self.current_page]
        new_text = self.inline_text_edit.toPlainText()

        # Direktes Bearbeiten eines vorhandenen Textblocks hat Vorrang.
        if self._editing_text_block is not None:
            self._apply_text_block_edit(new_text)
            return

        if self.selected_widget_xref is not None:
            for w in page.widgets() or []:
                if w.xref == self.selected_widget_xref:
                    try:
                        self._push_undo_state()
                        w.field_value = new_text
                        w.update()
                        self._set_dirty(True)
                        self._refresh_thumbnails()
                        self.render_current_page()
                        self.statusBar().showMessage("Formularfeld gespeichert.")
                    except Exception as e:
                        QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen:\n{e}")
                    return
            return

        if self.selected_annotation_xref is not None:
            try:
                annot = page.load_annot(self.selected_annotation_xref)
                if annot is None:
                    raise ValueError("Annotation nicht gefunden.")
                self._push_undo_state()
                try:
                    annot.set_info(content=new_text)
                except Exception:
                    annot.set_info(info={"content": new_text})
                annot.update()
                self._set_dirty(True)
                self._update_selected_annotation_ui()
                self._refresh_thumbnails()
                self.render_current_page()
                self.statusBar().showMessage("Annotation gespeichert.")
            except Exception as e:
                QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen:\n{e}")

    def edit_selected_annotation_style(self) -> None:
        if self.selected_annotation_xref is None or not self.doc or not (0 <= self.current_page < len(self.doc)):
            QMessageBox.information(self, "Hinweis", "Bitte zuerst eine Annotation auswählen.")
            return
        page = self.doc[self.current_page]
        try:
            annot = page.load_annot(self.selected_annotation_xref)
            if annot is None:
                raise ValueError("Annotation nicht gefunden.")
            color_raw, ok = QInputDialog.getText(
                self,
                "Stil bearbeiten",
                "Neue Farbe (RGB oder Hex):",
                text="0, 120, 215",
            )
            if not ok:
                return
            color = self._parse_rgb_color(color_raw, (0.0, 120 / 255.0, 215 / 255.0))
            width, ok = QInputDialog.getDouble(self, "Stil bearbeiten", "Linienstärke in pt:", 2.0, 0.1, 20.0, 1)
            if not ok:
                return
            self._push_undo_state()
            try:
                annot.set_colors(stroke=color)
            except Exception:
                pass
            try:
                annot.set_border(width=width)
            except Exception:
                pass
            annot.update()
            self._set_dirty(True)
            self._update_selected_annotation_ui()
            self.render_current_page()
            self.statusBar().showMessage("Annotationsstil aktualisiert")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Annotationsstil konnte nicht geändert werden:\n{e}")

    def reply_to_selected_annotation(self) -> None:
        if self.selected_annotation_xref is None or not self.doc or not (0 <= self.current_page < len(self.doc)):
            QMessageBox.information(self, "Hinweis", "Bitte zuerst eine Annotation auswählen.")
            return
        page = self.doc[self.current_page]
        try:
            annot = page.load_annot(self.selected_annotation_xref)
            if annot is None:
                raise ValueError("Annotation nicht gefunden.")
            text, ok = QInputDialog.getMultiLineText(
                self,
                "Antwort hinzufügen",
                "Antwort / Review-Notiz:",
            )
            if not ok or not text.strip():
                return
            self._push_undo_state()
            rect = annot.rect
            point = fitz.Point(rect.x1 + 12, rect.y0)
            reply = page.add_text_annot(point, text.strip())
            try:
                reply.set_info(title="Antwort", subject="Review-Antwort")
            except Exception:
                pass
            reply.update()
            self.selected_annotation_xref = getattr(reply, "xref", None)
            self._set_dirty(True)
            self._update_selected_annotation_ui()
            self.render_current_page()
            self.statusBar().showMessage("Antwort-Notiz hinzugefügt")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Antwort konnte nicht hinzugefügt werden:\n{e}")

    def edit_pdf_metadata(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        metadata = dict(self.doc.metadata or {})
        fields = [
            ("title", "Titel"),
            ("author", "Autor"),
            ("subject", "Betreff"),
            ("keywords", "Schlüsselwörter"),
        ]
        updated = dict(metadata)
        for key, label in fields:
            value, ok = QInputDialog.getText(
                self,
                "PDF-Metadaten bearbeiten",
                f"{label}:",
                text=str(updated.get(key, "") or ""),
            )
            if not ok:
                return
            updated[key] = value
        try:
            self._push_undo_state()
            self.doc.set_metadata(updated)
            self._set_dirty(True)
            self.statusBar().showMessage("PDF-Metadaten aktualisiert")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Metadaten konnten nicht aktualisiert werden:\n{e}")

    def scrub_metadata(self) -> None:
        """Entfernt Dokument-Metadaten und versteckte Daten (Datenschutz).

        Leert die Standard-Metadaten (Autor/Titel/…) und das XML-Metadaten-
        Paket. Relevant für internen Gebrauch, bevor Dateien weitergegeben
        werden.
        """
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        answer = QMessageBox.question(
            self,
            "Metadaten bereinigen",
            "Alle Dokument-Metadaten (Autor, Titel, Betreff, Schlüsselwörter, "
            "Erstell-/Änderungsprogramm) und das XML-Metadatenpaket entfernen?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._push_undo_state()
            cleared = {key: "" for key in (self.doc.metadata or {}).keys()}
            self.doc.set_metadata(cleared)
            try:
                self.doc.del_xml_metadata()
            except Exception:
                pass
            self._set_dirty(True)
            self.statusBar().showMessage("Metadaten bereinigt – zum Speichern nicht vergessen.")
            QMessageBox.information(self, "Fertig", "Metadaten und XML-Paket wurden entfernt.")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Metadaten konnten nicht bereinigt werden:\n{e}")

    def open_search(self) -> None:
        self.search_bar_widget.setVisible(True)
        self.search_results_list.setVisible(True)
        self.search_query.setFocus()
        self.search_query.selectAll()

    def open_search_and_run(self) -> None:
        self.search_bar_widget.setVisible(True)
        self.search_results_list.setVisible(True)
        self.search_all_pages()

    def close_search_panel(self) -> None:
        self.search_query.clear()
        self.search_hits = []
        self.current_search_hit = -1
        self.search_results_list.clear()
        self.search_results_list.setVisible(False)
        self.search_bar_widget.setVisible(False)
        self._update_search_counter()

    @staticmethod
    def _normalize_search_text(value: str) -> str:
        text = (value or "").casefold()
        # Keep common OCR confusions searchable, but only inside word-like tokens.
        text = text.replace("é", "ö")
        text = MainWindow._fix_german_umlaut_confusions(text)
        # Normalize umlauts to base vowels for tolerant matching.
        text = text.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        # Treat punctuation/separators as spaces so searches like
        # "RE 2026 001" match lines containing "RE-2026/001".
        text = re.sub(r"[^a-z0-9]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _search_variants(normalized_text: str) -> set[str]:
        base = (normalized_text or "").strip()
        if not base:
            return set()

        variants = {base}
        # OCR confusion: ß can become R/r
        variants.add(base.replace("ss", "r"))
        # OCR confusion: ü can become i (normalize u<->i between consonants)
        variants.add(re.sub(r"(?i)(?<=[bcdfghjklmnpqrstvwxyz])i(?=[bcdfghjklmnpqrstvwxyz])", "u", base))
        variants.add(re.sub(r"(?i)(?<=[bcdfghjklmnpqrstvwxyz])u(?=[bcdfghjklmnpqrstvwxyz])", "i", base))
        return {v for v in variants if v}

    def search_all_pages(self) -> None:
        if not self.doc:
            return
        self.search_bar_widget.setVisible(True)
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
        needle = self._normalize_search_text(query)
        needle_variants = self._search_variants(needle)

        for idx in range(len(self.doc)):
            page = self.doc[idx]
            page_hits_before = len(self.search_hits)

            # Native PDF hit detection via exact text search to allow precise jump/highlight.
            rect_hits: list[fitz.Rect] = []
            try:
                rect_hits = page.search_for(query)
            except Exception:
                rect_hits = []

            for rect_idx, rect in enumerate(rect_hits, start=1):
                snippet = (page.get_textbox(rect) or "").strip() or query
                hit = {
                    "page": idx,
                    "line": rect_idx,
                    "snippet": snippet[:180],
                    "source": "native",
                    "rect": (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
                }
                self.search_hits.append(hit)

            def scan_text_block(text_block: str, source: str) -> None:
                for line_no, line in enumerate(text_block.splitlines(), start=1):
                    snippet = line.strip()
                    if not snippet:
                        continue
                    norm_line = self._normalize_search_text(snippet)
                    line_variants = self._search_variants(norm_line)
                    if needle_variants and any(nv in lv for lv in line_variants for nv in needle_variants):
                        hit = {"page": idx, "line": line_no, "snippet": snippet[:180], "source": source}
                        self.search_hits.append(hit)

            # OCR fallback only if no native rect hit was found on this page.
            if len(self.search_hits) == page_hits_before:
                native_text = page.get_text("text").strip()
                scan_text_block(native_text, "native")
            if len(self.search_hits) == page_hits_before:
                rotation = self.page_rotations.get(idx, 0)
                ocr_text, _, _, _ = self._ocr_page_with_retry_cached(idx, rotation, retries=1)
                if ocr_text:
                    scan_text_block(ocr_text, "ocr")

        for hit_idx, hit in enumerate(self.search_hits, start=1):
            src = "OCR" if hit.get("source") == "ocr" else "PDF"
            item = QListWidgetItem(
                f"{hit_idx}. S.{hit['page'] + 1}/Z.{hit.get('line', '-')}: {hit['snippet']} ({src})"
            )
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
        self.search_counter.setText(f"{current} / {total}")

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
        src = "OCR" if hit.get("source") == "ocr" else "PDF"
        self.statusBar().showMessage(
            f"Treffer {hit_idx + 1}/{len(self.search_hits)}: Seite {hit['page'] + 1}, Zeile {hit.get('line', '-') } ({src})"
        )

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
            return MainWindow._restore_word_case(word, fixed)

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
        base_styles = """
            /* ── Fenster & Container ──────────────────────────────────── */
            QMainWindow, QWidget {
                background: #f7f8fa;
                color: #1e2432;
                font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                font-size: 13px;
            }

            /* ── Toolbar-Hintergrund ───────────────────────────────────── */
            QWidget[role="toolbar"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ffffff, stop:0.55 #f7f9ff, stop:1 #eef3ff);
                border-bottom: 1px solid #e4e7ef;
            }

            /* ── Namens-Zeile ─────────────────────────────────────────── */
            QWidget[role="namebar"] {
                background: #f5f7fc;
                border-bottom: 1px solid #e4e7ef;
            }

            /* ── Suchleiste ───────────────────────────────────────────── */
            QWidget[role="searchbar"] {
                background: #f7faff;
                border-bottom: 1px solid #e4eaf6;
            }

            /* ── OCR-Statusleiste ─────────────────────────────────────── */
            QWidget[role="ocrbar"] {
                background: #f7f9fd;
                border-top: 1px solid #e4e7ef;
            }
            QWidget[role="sidepanel"] {
                background: #f9fbff;
                border-left: 1px solid #e4e7ef;
            }
            QWidget[role="thumbpanel"] {
                background: #f5f8ff;
                border-right: 1px solid #e4eaf6;
            }
            QWidget[role="sectioncard"] {
                background: rgba(255, 255, 255, 0.92);
                border: 1px solid #e5eaf6;
                border-radius: 12px;
            }
            QWidget[role="previewcard"] {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fdfefe, stop:1 #f6f8fc);
                border-left: 1px solid #edf1f8;
                border-right: 1px solid #edf1f8;
            }
            QWidget[role="ribbongroup"] {
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid #e4e8f4;
                border-radius: 14px;
            }
            QLabel[role="ribbontitle"] {
                color: #5f6c8c;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.08em;
                text-transform: uppercase;
            }
            QWidget[role="panelcard"] {
                background: #ffffff;
                border: 1px solid #e5eaf6;
                border-radius: 14px;
            }
            QWidget[role="namebar"], QWidget[role="searchbar"], QWidget[role="ocrbar"] {
                border-bottom: 1px solid #e8edf7;
            }
            QLabel[role="cardtitle"] {
                color: #1e2432;
                font-size: 14px;
                font-weight: 700;
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
                border-radius: 9px;
                padding: 6px 12px;
                font-size: 13px;
                font-weight: 500;
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
                border-radius: 10px;
                padding: 6px 14px;
                font-size: 13px;
                font-weight: 700;
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
                border-radius: 10px;
                padding: 6px 11px;
                font-size: 13px;
            }
            QPushButton[btnRole="action"]::icon {
                padding-right: 4px;
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
            QPushButton[btnRole="tool"] {
                background: #ffffff;
                color: #2d3a5e;
                border: 1px solid #d7dcef;
                border-radius: 10px;
                padding: 5px 8px;
                font-size: 12px;
                font-weight: 600;
                min-width: 64px;
            }
            QPushButton[btnRole="tool"]:hover {
                background: #eef3ff;
                border-color: #9fb2ea;
            }
            QPushButton[btnRole="tool"]:pressed {
                background: #dde7ff;
                border-color: #7f98e2;
            }
            QPushButton[btnRole="tool"]:disabled {
                color: #aab0c4;
                border-color: #e4e7ef;
            }
            QPushButton[toolActive="true"] {
                background: #e9efff;
                border-color: #7c93ff;
                color: #20337d;
                font-weight: 600;
            }

            /* ── Eingabefelder ────────────────────────────────────────── */
            QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit {
                background: #ffffff;
                color: #1e2432;
                border: 1px solid #cdd2e8;
                border-radius: 7px;
                padding: 5px 10px;
                font-size: 13px;
                selection-background-color: #c2ccff;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
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
                font-weight: 700;
                letter-spacing: 0.7px;
            }
            QLabel[role="pageinfo"] {
                color: #52607a;
                font-size: 12px;
                padding: 6px 10px;
                background: #ffffff;
                border: 1px solid #dfe5f2;
                border-radius: 10px;
            }
            QLabel[role="counter"] {
                color: #6b748a;
                font-size: 12px;
                min-width: 48px;
                text-align: center;
            }
            QLabel[role="hintchip"] {
                background: #eef2ff;
                color: #40508b;
                border: 1px solid #d7defc;
                border-radius: 11px;
                padding: 4px 10px;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel[role="hintchip"][active="true"] {
                background: #fff4d6;
                color: #8a5a00;
                border-color: #f2d27a;
            }
            QLabel[role="paneltitle"] {
                color: #1e2432;
                font-size: 19px;
                font-weight: 700;
            }
            QLabel[role="panelsubtitle"] {
                color: #6b748a;
                font-size: 12px;
                line-height: 1.4;
            }
            QLabel[role="panelinfo"] {
                background: #f7f9fd;
                border: 1px solid #e4e7ef;
                border-radius: 10px;
                color: #47516b;
                padding: 10px;
                font-size: 12px;
            }

            /* ── Listen (Suchtreffer, Thumbnails) ─────────────────────── */
            QListWidget {
                background: #ffffff;
                border: none;
                border-right: 1px solid #e4e7ef;
                outline: none;
            }
            QListWidget[role="thumbrail"] {
                background: transparent;
                border: 1px solid #e3e8f5;
                border-radius: 14px;
            }
            QListWidget::item {
                padding: 7px 10px;
                border-bottom: 1px solid #edf1f8;
                color: #1e2432;
                margin: 2px 4px;
                border-radius: 10px;
            }
            QListWidget::item:selected {
                background: #e8ecff;
                border: 1px solid #cbd6ff;
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
            QScrollArea[role="previewarea"] {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #edf1fb, stop:1 #e7ebf6);
                border-left: 1px solid #edf1f8;
                border-right: 1px solid #edf1f8;
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
            QLabel[role="pagepreview"] {
                background: #ffffff;
                border: 1px solid #d9deeb;
                border-radius: 18px;
                padding: 14px;
            }

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
        """
        dark_overrides = """
            QMainWindow, QWidget {
                background: #0f1724;
                color: #e5ecf6;
            }
            QWidget[role="toolbar"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #131c2b, stop:0.55 #162235, stop:1 #1a2840);
                border-bottom: 1px solid #22304a;
            }
            QWidget[role="namebar"], QWidget[role="ocrbar"], QWidget[role="sidepanel"] {
                background: #111a29;
                border-color: #22304a;
            }
            QWidget[role="thumbpanel"] {
                background: #0f1724;
                border-right: 1px solid #22304a;
            }
            QWidget[role="previewcard"] {
                background: #0f1724;
                border-left: 1px solid #22304a;
                border-right: 1px solid #22304a;
            }
            QWidget[role="searchbar"] {
                background: #111a29;
                border-bottom: 1px solid #22304a;
            }
            QWidget[role="ribbongroup"], QWidget[role="panelcard"], QWidget[role="sectioncard"], QLabel[role="panelinfo"], QLabel[role="hintchip"] {
                background: #162235;
                border-color: #253654;
                color: #dbe6f7;
            }
            QLabel[role="ribbontitle"], QLabel[role="fieldlabel"], QLabel[role="pageinfo"], QLabel[role="counter"], QLabel[role="panelsubtitle"] {
                color: #98a8c4;
            }
            QLabel[role="paneltitle"], QLabel[role="cardtitle"], QLabel {
                color: #e5ecf6;
            }
            QLabel[role="hintchip"][active="true"] {
                background: #3b2c10;
                color: #ffd98a;
                border-color: #7b5b1f;
            }
            QPushButton {
                background: #1a2840;
                color: #e5ecf6;
                border-color: #2a3c5f;
            }
            QPushButton:hover {
                background: #22324f;
                border-color: #4b67a3;
            }
            QPushButton:pressed {
                background: #293a5d;
            }
            QPushButton:disabled {
                background: #121b2a;
                color: #72819a;
                border-color: #1d293d;
            }
            QPushButton[btnRole="icon"] {
                background: transparent;
                color: #dbe6f7;
            }
            QPushButton[btnRole="icon"]:hover {
                background: #1e2d47;
                border-color: #35507c;
            }
            QPushButton[btnRole="primary"] {
                background: #4b6bff;
                border-color: #6782ff;
                color: #ffffff;
            }
            QPushButton[btnRole="primary"]:hover {
                background: #6481ff;
            }
            QPushButton[btnRole="action"] {
                background: #142033;
                color: #dbe6f7;
                border-color: #29405f;
            }
            QPushButton[btnRole="tool"] {
                background: #142033;
                color: #dbe6f7;
                border-color: #29405f;
            }
            QPushButton[toolActive="true"] {
                background: #243858;
                border-color: #7d9cff;
                color: #eef4ff;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QListWidget, QTableWidget, QMenuBar, QMenu, QScrollArea, QLabel[role="pagepreview"] {
                background: #0f1724;
                color: #e5ecf6;
                border-color: #243654;
            }
            QLabel[role="pageinfo"] {
                background: #162235;
                border-color: #253654;
            }
            QListWidget[role="thumbrail"] {
                background: transparent;
                border: 1px solid #22304a;
            }
            QListWidget::item {
                color: #dbe6f7;
                border-bottom: 1px solid #1a2740;
            }
            QListWidget::item:selected {
                background: #20314d;
                border: 1px solid #4669a2;
            }
            QListWidget::item:hover {
                background: #1a2840;
            }
            QScrollArea[role="previewarea"] {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #111a29, stop:1 #0d1522);
            }
            QScrollBar:vertical, QScrollBar:horizontal, QStatusBar, QHeaderView::section {
                background: #162235;
                color: #c9d5ea;
                border-color: #22304a;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal, QProgressBar::chunk {
                background: #4b6bff;
            }
            QMenuBar::item:selected, QMenu::item:selected {
                background: #1d2d47;
                color: #eef4ff;
            }
            QSplitter::handle {
                background: #22304a;
            }
        """
        self.setStyleSheet(base_styles + (dark_overrides if self._is_dark_mode() else ""))

    def _is_dark_mode(self) -> bool:
        return self.palette().window().color().lightness() < 128

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
        self._page_render_cache.clear()
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
        self._update_thumbnail_meta()
        self._set_dirty(False)
        self._update_undo_redo_buttons()
        self._refresh_comment_list()
        self._refresh_outline()
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
            if self.doc.needs_pass:
                password = ""
                authenticated = 0
                for _ in range(3):
                    password, ok = QInputDialog.getText(
                        self,
                        "Passwort erforderlich",
                        f"Passwort für {self.pdf_path.name} eingeben:",
                        QLineEdit.EchoMode.Password,
                    )
                    if not ok:
                        self.doc.close()
                        self.doc = None
                        return
                    authenticated = self.doc.authenticate(password)
                    if authenticated:
                        break
                if not authenticated:
                    raise ValueError("Passwort war falsch oder PDF konnte nicht entschlüsselt werden.")
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
        self._page_render_cache.clear()
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
        self._update_thumbnail_meta()
        self._update_undo_redo_buttons()
        self._refresh_comment_list()
        self._refresh_outline()
        self.statusBar().showMessage(f"Geladen: {self.pdf_path.name} ({len(self.doc)} Seiten)")
        self._maybe_offer_recovery()

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
            self._clear_pending_annotation()
            self.preview.clear()
            self.preview.setText("Kein PDF geladen")
            self.preview.adjustSize()
            self.page_info.setText("Seite: -/- | Zoom: 100%")
            return

        total = len(self.doc)
        self.current_page = max(0, min(self.current_page, total - 1))

        page = self.doc[self.current_page]
        rotation = self.page_rotations.get(self.current_page, 0)
        # Basis-Render (teures get_pixmap) cachen; bei jeder Änderung wird der
        # Cache in _set_dirty geleert, daher ist der Schlüssel kollisionsfrei.
        cache_key = (self.current_page, round(self.zoom_factor, 4), rotation % 360, self.doc_revision)
        cached_base = self._page_render_cache.get(cache_key)
        if cached_base is not None:
            qpix = cached_base.copy()
        else:
            matrix = fitz.Matrix(self.zoom_factor, self.zoom_factor).prerotate(rotation)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            fmt = QImage.Format.Format_RGB888
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
            base = QPixmap.fromImage(img)
            if len(self._page_render_cache) > 12:
                self._page_render_cache.clear()
            self._page_render_cache[cache_key] = base
            qpix = base.copy()

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

        if self.selected_annotation_xref is not None:
            try:
                annot = page.load_annot(self.selected_annotation_xref)
                if annot is not None:
                    box = self._map_search_rect_to_view(
                        rect=annot.rect,
                        page_width=float(page.rect.width),
                        page_height=float(page.rect.height),
                        rotation=rotation,
                        scale=self.zoom_factor,
                        view_width=qpix.width(),
                        view_height=qpix.height(),
                    )
                    if box is not None:
                        painter = QPainter(qpix)
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                        pen = QPen(QColor(61, 90, 254, 235))
                        pen.setWidth(3)
                        painter.setPen(pen)
                        painter.setBrush(QColor(61, 90, 254, 40))
                        x, y, w, h = box
                        painter.drawRoundedRect(x, y, w, h, 8, 8)
                        painter.end()
            except Exception:
                self.selected_annotation_xref = None
                self.selected_widget_xref = None
                self._update_selected_annotation_ui()

        if self.pending_annotation and self.preview_drag_start and self.preview_drag_current:
            try:
                kind = self.pending_annotation.get("kind")
                rect_spec = self._rect_percent_from_drag(
                    self.preview_drag_start[0],
                    self.preview_drag_start[1],
                    self.preview_drag_current[0],
                    self.preview_drag_current[1],
                )
                if rect_spec is not None:
                    box = self._rect_percent_to_view_box(rect_spec, qpix.width(), qpix.height())
                    if box is not None:
                        painter = QPainter(qpix)
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                        x, y, w, h = box
                        if kind == "crop":
                            overlay = QColor(15, 23, 36, 105 if not self._is_dark_mode() else 150)
                            painter.fillRect(0, 0, qpix.width(), y, overlay)
                            painter.fillRect(0, y, x, h, overlay)
                            painter.fillRect(x + w, y, max(0, qpix.width() - (x + w)), h, overlay)
                            painter.fillRect(0, y + h, qpix.width(), max(0, qpix.height() - (y + h)), overlay)
                            pen = QPen(QColor(56, 189, 248, 235))
                            pen.setWidth(3)
                            painter.setPen(pen)
                            painter.setBrush(QColor(56, 189, 248, 28))
                            painter.drawRoundedRect(x, y, w, h, 10, 10)
                        elif kind == "image":
                            if self.annotation_image_preview is not None and not self.annotation_image_preview.isNull():
                                scaled = self.annotation_image_preview.scaled(
                                    w,
                                    h,
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation,
                                )
                                draw_x = x + max(0, (w - scaled.width()) // 2)
                                draw_y = y + max(0, (h - scaled.height()) // 2)
                                painter.setOpacity(0.78)
                                painter.drawPixmap(draw_x, draw_y, scaled)
                                painter.setOpacity(1.0)
                            pen = QPen(QColor(34, 197, 94, 235))
                            pen.setWidth(3)
                            painter.setPen(pen)
                            painter.setBrush(QColor(34, 197, 94, 24))
                            painter.drawRoundedRect(x, y, w, h, 10, 10)
                        elif kind == "redact":
                            pen = QPen(QColor(239, 68, 68, 235))
                            pen.setWidth(3)
                            painter.setPen(pen)
                            painter.setBrush(QColor(17, 17, 17, 150))
                            painter.drawRoundedRect(x, y, w, h, 8, 8)
                        else:
                            pen = QPen(QColor(61, 90, 254, 220))
                            pen.setWidth(2)
                            painter.setPen(pen)
                            painter.setBrush(QColor(61, 90, 254, 24))
                            painter.drawRoundedRect(x, y, w, h, 8, 8)
                        painter.end()
                elif kind == "freehand" and len(self.preview_drag_points) >= 2:
                    painter = QPainter(qpix)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    pen = QPen(QColor(220, 20, 60, 220))
                    try:
                        pen.setWidthF(float(self.pending_annotation.get("line_width", 2.5)))
                    except Exception:
                        pen.setWidth(3)
                    painter.setPen(pen)
                    last = None
                    for px, py in self.preview_drag_points:
                        cx = int(round((px / 100.0) * qpix.width()))
                        cy = int(round((py / 100.0) * qpix.height()))
                        if last is not None:
                            painter.drawLine(last[0], last[1], cx, cy)
                        last = (cx, cy)
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
        if key == Qt.Key.Key_Backspace and self.selected_annotation_xref is not None:
            self.delete_selected_annotation()
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

    def _current_page_view_size(self) -> tuple[float, float] | None:
        if not self.doc or not (0 <= self.current_page < len(self.doc)):
            return None
        page = self.doc[self.current_page]
        width = float(page.rect.width)
        height = float(page.rect.height)
        if self.page_rotations.get(self.current_page, 0) % 360 in (90, 270):
            width, height = height, width
        if width <= 0 or height <= 0:
            return None
        return width, height

    def fit_to_width(self) -> None:
        size = self._current_page_view_size()
        if size is None:
            return
        width, _ = size
        avail = self.preview_scroll.viewport().width() - 28
        if avail <= 0:
            return
        self.zoom_factor = max(0.1, min(6.0, avail / width))
        self.render_current_page()

    def fit_to_page(self) -> None:
        size = self._current_page_view_size()
        if size is None:
            return
        width, height = size
        avail_w = self.preview_scroll.viewport().width() - 28
        avail_h = self.preview_scroll.viewport().height() - 28
        if avail_w <= 0 or avail_h <= 0:
            return
        self.zoom_factor = max(0.1, min(6.0, min(avail_w / width, avail_h / height)))
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
        ocr_text, _, _, _ = self._ocr_page_with_retry_cached(0, rotation, retries=1)
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

    def export_searchable_pdf_copy(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        total = len(self.doc)
        if total == 0:
            QMessageBox.information(self, "Hinweis", "Das PDF enthält keine Seiten.")
            return

        default_out = self.pdf_path.with_name(f"{self.pdf_path.stem}_searchable.pdf")
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Durchsuchbare PDF speichern",
            str(default_out),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)

        progress = QProgressDialog("Erzeuge durchsuchbare PDF …", "Abbrechen", 0, total, self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        out_doc = fitz.open()
        failed_pages: list[int] = []
        failed_reason = ""

        self._set_ocr_running(True)
        try:
            for idx in range(total):
                progress.setValue(idx)
                progress.setLabelText(f"OCR-Seite {idx + 1}/{total} …")
                QApplication.processEvents()

                if progress.wasCanceled() or self.ocr_cancel_requested:
                    QMessageBox.information(self, "Abgebrochen", "Export wurde abgebrochen.")
                    return

                try:
                    rotation = self.page_rotations.get(idx, 0)
                    page = self.doc[idx]
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0).prerotate(rotation), alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

                    pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                        img,
                        extension="pdf",
                        lang=self._ocr_lang(),
                        config="--psm 6",
                    )
                    ocr_page_doc = fitz.open("pdf", pdf_bytes)
                    try:
                        out_doc.insert_pdf(ocr_page_doc)
                    finally:
                        ocr_page_doc.close()
                except (TesseractError, TesseractNotFoundError, RuntimeError, ValueError) as e:
                    failed_pages.append(idx + 1)
                    if not failed_reason:
                        failed_reason = str(e)
                except Exception as e:
                    failed_pages.append(idx + 1)
                    if not failed_reason:
                        failed_reason = str(e)
        finally:
            self._set_ocr_running(False)
            progress.setValue(total)

        if len(out_doc) == 0:
            out_doc.close()
            QMessageBox.critical(
                self,
                "Fehler",
                "Es konnte keine durchsuchbare PDF erstellt werden.\n\n"
                f"Fehler: {failed_reason or 'Unbekannter OCR-Fehler'}",
            )
            return

        try:
            out_doc.save(out_path)
            self.statusBar().showMessage(f"Durchsuchbare PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konnte durchsuchbare PDF nicht speichern:\n{e}")
            out_doc.close()
            return
        finally:
            out_doc.close()

        if failed_pages:
            preview = ", ".join(str(p) for p in failed_pages[:12])
            if len(failed_pages) > 12:
                preview += ", …"
            QMessageBox.warning(
                self,
                "Teilweise OCR-Fehler",
                "Die durchsuchbare PDF wurde erstellt, aber einige Seiten konnten nicht verarbeitet werden."
                f"\n\nFehlerseiten: {preview}"
                f"\nAnzahl: {len(failed_pages)}"
                f"\n\nErster Fehler:\n{failed_reason or '-'}",
            )
        else:
            QMessageBox.information(self, "Fertig", f"Durchsuchbare PDF erstellt:\n{out_path}")

    @staticmethod
    def _find_soffice() -> str | None:
        """Sucht das LibreOffice-Headless-Binary (offline-Konvertierung).

        Reihenfolge: **mitgebündeltes** LibreOffice neben der Anwendung
        (für eine eigenständige Installation auf dem Arbeitsrechner) →
        PATH → bekannte System-Installationspfade.
        """
        # 1) Mitgebündelt: relativ zur EXE bzw. zum PyInstaller-Bundle.
        bundle_roots: list[Path] = []
        try:
            bundle_roots.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundle_roots.append(Path(meipass))
        bundle_roots.append(Path(__file__).resolve().parent)
        bundle_rel = [
            Path("libreoffice") / "program" / ("soffice.exe" if os.name == "nt" else "soffice"),
            Path("libreoffice") / "program" / "soffice.bin",
        ]
        for root in bundle_roots:
            for rel in bundle_rel:
                candidate = root / rel
                if candidate.exists():
                    return str(candidate)

        # 2) PATH.
        for name in ("soffice", "libreoffice"):
            found = shutil.which(name)
            if found:
                return found

        # 3) Bekannte System-Installationspfade.
        candidates = [
            "/usr/bin/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        return None

    def _run_soffice_convert(self, soffice: str, src: Path, out_dir: Path, convert_to: str) -> Path:
        """Konvertiert eine Datei via LibreOffice-Headless und gibt den Zielpfad zurück.

        `convert_to` ist das LibreOffice-Filterziel, z. B. "pdf" oder
        "docx:MS Word 2007 XML". Wirft bei Fehlschlag eine Exception.
        """
        target_ext = convert_to.split(":", 1)[0]
        # Eigenes, isoliertes Benutzerprofil pro Aufruf: verhindert das
        # "Profil gesperrt"-Problem, falls auf dem Zielrechner bereits eine
        # LibreOffice-Instanz läuft.
        with tempfile.TemporaryDirectory() as profile_dir:
            profile_uri = Path(profile_dir).as_uri()
            cmd = [
                soffice,
                f"-env:UserInstallation={profile_uri}",
                "--headless",
                "--norestore",
                "--convert-to",
                convert_to,
                "--outdir",
                str(out_dir),
                str(src),
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("Konvertierung hat das Zeitlimit überschritten (180 s).")
        out_path = out_dir / f"{src.stem}.{target_ext}"
        if not out_path.exists():
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"LibreOffice hat keine Ausgabedatei erzeugt.\n{detail}")
        return out_path

    def import_office_as_pdf(self) -> None:
        """Öffnet ein Office-Dokument (DOCX/XLSX/PPTX/ODT …) als PDF.

        Konvertiert offline via LibreOffice-Headless und lädt das Ergebnis.
        """
        soffice = self._find_soffice()
        if not soffice:
            QMessageBox.warning(
                self,
                "LibreOffice nicht gefunden",
                "Für die Office-Konvertierung wird LibreOffice (soffice) benötigt.\n"
                "Bitte LibreOffice installieren oder im Build bündeln.",
            )
            return
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Office-Dokument öffnen",
            "",
            "Office-Dokumente (*.docx *.doc *.odt *.rtf *.xlsx *.xls *.ods *.pptx *.ppt *.odp);;Alle Dateien (*.*)",
        )
        if not file_name:
            return
        src = Path(file_name)
        out_pdf = src.with_suffix(".pdf")
        try:
            if out_pdf.exists():
                answer = QMessageBox.question(
                    self,
                    "Datei existiert",
                    f"{out_pdf.name} existiert bereits. Überschreiben?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.statusBar().showMessage(f"Konvertiere {src.name} → PDF …")
            QApplication.processEvents()
            produced = self._run_soffice_convert(soffice, src, src.parent, "pdf")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konvertierung fehlgeschlagen:\n{e}")
            return
        self._open_pdf_path(str(produced))
        self.statusBar().showMessage(f"Office-Dokument als PDF geöffnet: {produced.name}")

    def export_as_office(self) -> None:
        """Exportiert das aktuelle PDF als bearbeitbares Word-Dokument (DOCX).

        Bevorzugt `pdf2docx` (bessere Textwiedergabe), sonst LibreOffice.
        """
        if not self.doc or len(self.doc) == 0:
            QMessageBox.information(self, "Hinweis", "Kein PDF geladen.")
            return
        default_name = (self.pdf_path.with_suffix(".docx").name if self.pdf_path else "dokument.docx")
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Als Word-Dokument exportieren",
            default_name,
            "Word-Dokument (*.docx)",
        )
        if not out_path:
            return
        out_path = str(Path(out_path).with_suffix(".docx"))

        # Aktuellen (ggf. geänderten) Stand in eine temporäre PDF schreiben.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_pdf = Path(tmp) / "source.pdf"
            try:
                work = fitz.open()
                try:
                    work.insert_pdf(self.doc)
                    for idx, rot in self.page_rotations.items():
                        if 0 <= idx < len(work) and rot % 360 != 0:
                            work[idx].set_rotation(rot % 360)
                    work.save(str(tmp_pdf))
                finally:
                    work.close()
            except Exception as e:
                QMessageBox.critical(self, "Fehler", f"Zwischen-PDF konnte nicht erstellt werden:\n{e}")
                return

            self.statusBar().showMessage("Exportiere nach DOCX …")
            QApplication.processEvents()

            # 1) pdf2docx (falls verfügbar) – beste Textwiedergabe.
            try:
                from pdf2docx import Converter  # type: ignore[import-not-found]

                cv = Converter(str(tmp_pdf))
                try:
                    cv.convert(out_path)
                finally:
                    cv.close()
                self.statusBar().showMessage(f"Als DOCX exportiert (pdf2docx): {Path(out_path).name}")
                QMessageBox.information(self, "Fertig", f"Word-Dokument erstellt:\n{out_path}")
                return
            except ImportError:
                pass
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "Hinweis",
                    f"pdf2docx-Konvertierung fehlgeschlagen, versuche LibreOffice …\n{e}",
                )

            # 2) Fallback: LibreOffice-Headless.
            soffice = self._find_soffice()
            if not soffice:
                QMessageBox.warning(
                    self,
                    "Konvertierung nicht möglich",
                    "Weder pdf2docx noch LibreOffice verfügbar.\n"
                    "Bitte `pip install pdf2docx` ausführen oder LibreOffice installieren.",
                )
                return
            try:
                produced = self._run_soffice_convert(
                    soffice, tmp_pdf, Path(tmp), "docx:MS Word 2007 XML"
                )
                shutil.move(str(produced), out_path)
            except Exception as e:
                QMessageBox.critical(self, "Fehler", f"DOCX-Export fehlgeschlagen:\n{e}")
                return
        self.statusBar().showMessage(f"Als DOCX exportiert (LibreOffice): {Path(out_path).name}")
        QMessageBox.information(self, "Fertig", f"Word-Dokument erstellt:\n{out_path}")

    def _recovery_path_for(self, source: Path) -> Path:
        digest = hashlib.sha1(str(source.resolve()).encode("utf-8", "replace")).hexdigest()[:16]
        return self._recovery_dir / f"{digest}.recovery.pdf"

    def _autosave_tick(self) -> None:
        """Schreibt periodisch eine Wiederherstellungskopie, solange es
        ungespeicherte Änderungen gibt."""
        if not self.doc or not self.pdf_path or not self.is_dirty:
            return
        try:
            recovery = self._recovery_path_for(self.pdf_path)
            work = fitz.open()
            try:
                work.insert_pdf(self.doc)
                for idx, rot in self.page_rotations.items():
                    if 0 <= idx < len(work) and rot % 360 != 0:
                        work[idx].set_rotation(rot % 360)
                work.save(str(recovery))
            finally:
                work.close()
            self.statusBar().showMessage("Automatische Sicherung erstellt", 2000)
        except Exception:
            # Autosave darf den Nutzer nie stören.
            pass

    def _clear_recovery(self) -> None:
        if not self.pdf_path:
            return
        try:
            recovery = self._recovery_path_for(self.pdf_path)
            if recovery.exists():
                recovery.unlink()
        except Exception:
            pass

    def _maybe_offer_recovery(self) -> None:
        """Prüft beim Laden, ob eine neuere Wiederherstellungskopie existiert,
        und bietet an, deren Stand zu übernehmen."""
        if not self.pdf_path:
            return
        try:
            recovery = self._recovery_path_for(self.pdf_path)
            if not recovery.exists():
                return
            if recovery.stat().st_mtime <= self.pdf_path.stat().st_mtime:
                self._clear_recovery()
                return
        except Exception:
            return
        answer = QMessageBox.question(
            self,
            "Wiederherstellung gefunden",
            "Für diese Datei existiert eine neuere automatische Sicherung "
            "(z. B. nach einem Absturz). Möchtest du deren Stand laden?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            recovered = fitz.open(str(recovery))
            self.doc = recovered
            self.page_rotations.clear()
            self.doc_revision = 0
            self._page_render_cache.clear()
            self._refresh_thumbnails()
            self.render_current_page()
            self._set_dirty(True)
            self._refresh_comment_list()
            self._refresh_outline()
            self.statusBar().showMessage("Automatische Sicherung geladen – bitte prüfen und speichern.")
        except Exception as e:
            QMessageBox.warning(self, "Hinweis", f"Wiederherstellung konnte nicht geladen werden:\n{e}")

    def print_document(self) -> None:
        """Druckt das aktuelle PDF über den System-Druckdialog.

        Jede (ggf. rotierte) Seite wird zur Druckauflösung gerendert und
        seitenfüllend auf das Druckmedium gezeichnet. Der gewählte
        Seitenbereich des Dialogs wird berücksichtigt.
        """
        if not self.doc or len(self.doc) == 0:
            QMessageBox.information(self, "Hinweis", "Kein PDF geladen.")
            return

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(self.pdf_path.name if self.pdf_path else "Dokument")
        printer.setFromTo(1, len(self.doc))

        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("PDF drucken")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return

        from_page = printer.fromPage()
        to_page = printer.toPage()
        if from_page == 0 and to_page == 0:
            page_indices = list(range(len(self.doc)))
        else:
            page_indices = list(range(from_page - 1, to_page))

        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.critical(self, "Fehler", "Der Drucker konnte nicht initialisiert werden.")
            return

        try:
            page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
            dpi = max(printer.resolution(), 72)
            zoom = dpi / 72.0
            first = True
            for idx in page_indices:
                if not (0 <= idx < len(self.doc)):
                    continue
                if not first:
                    printer.newPage()
                first = False
                page = self.doc[idx]
                rotation = self.page_rotations.get(idx, 0)
                matrix = fitz.Matrix(zoom, zoom).prerotate(rotation)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                scaled = img.scaled(
                    int(page_rect.width()),
                    int(page_rect.height()),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = page_rect.x() + (page_rect.width() - scaled.width()) / 2
                y = page_rect.y() + (page_rect.height() - scaled.height()) / 2
                painter.drawImage(QPointF(x, y), scaled)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Drucken fehlgeschlagen:\n{e}")
            return
        finally:
            painter.end()

        self.statusBar().showMessage(f"Druckauftrag gesendet: {len(page_indices)} Seite(n)")

    def save_in_place(self) -> None:
        """Speichert das aktuelle PDF direkt an seinem Speicherort.

        Rotationen und alle anderen Änderungen (Seitenlöschungen, Neuanordnung)
        werden dauerhaft in die Originaldatei geschrieben.
        """
        if not self.pdf_path or not self.doc:
            QMessageBox.information(self, "Hinweis", "Kein PDF geladen.")
            return

        if not self.is_dirty:
            self.statusBar().showMessage("Keine ungespeicherten Änderungen.")
            return

        try:
            source_path = self.pdf_path.resolve()
            tmp_out = source_path.with_name(f"{source_path.stem}.tmp{source_path.suffix}")

            out_doc = fitz.open()
            try:
                out_doc.insert_pdf(self.doc)
                for idx, rot in self.page_rotations.items():
                    if 0 <= idx < len(out_doc) and rot % 360 != 0:
                        out_doc[idx].set_rotation(rot % 360)
                out_doc.save(str(tmp_out))
            finally:
                out_doc.close()

            # Original ersetzen (atomar: erst tmp schreiben, dann ersetzen)
            self._close_open_document()
            tmp_out.replace(source_path)
            self.doc = fitz.open(str(source_path))
            # Rotationen sind jetzt im PDF eingebrannt – In-Memory-Overrides leeren
            self.page_rotations.clear()

            self._refresh_thumbnails()
            self.render_current_page()
            self._set_dirty(False)
            self._clear_recovery()
            self.statusBar().showMessage(f"Gespeichert: {source_path.name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht speichern:\n{e}")

    def save_as_suggested(self) -> None:
        if not self.pdf_path or not self.doc:
            QMessageBox.information(self, "Hinweis", "Kein PDF geladen.")
            return

        default_name = self.suggested_name.text().strip() or self.pdf_path.name
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "PDF speichern als",
            str(self.pdf_path.with_name(default_name)),
            "PDF-Dateien (*.pdf)",
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
            self._clear_recovery()
            QMessageBox.information(self, "Gespeichert", f"Datei gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Gespeichert: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht speichern:\n{e}")

    def _clone_document_with_current_state(self) -> fitz.Document:
        if not self.doc:
            raise ValueError("Kein PDF geladen.")
        out_doc = fitz.open()
        out_doc.insert_pdf(self.doc)
        try:
            out_doc.set_metadata(self.doc.metadata or {})
        except Exception:
            pass
        for idx, rot in self.page_rotations.items():
            if 0 <= idx < len(out_doc) and rot % 360 != 0:
                out_doc[idx].set_rotation(rot % 360)
        return out_doc

    def apply_export_preset(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        profiles = [
            "Archivmodus — starke Optimierung bei unveränderten Farben",
            "Druckmodus — komplette PDF in Graustufen mit Komprimierung",
            "Entwurfsmodus — Wasserzeichen 'ENTWURF' plus Seitennummern",
        ]
        profile, ok = QInputDialog.getItem(self, "Preset-Profil", "Profil auswählen:", profiles, 0, False)
        if not ok:
            return

        if profile.startswith("Archivmodus"):
            default_name = self.pdf_path.with_name(f"{self.pdf_path.stem}_archive.pdf")
        elif profile.startswith("Druckmodus"):
            default_name = self.pdf_path.with_name(f"{self.pdf_path.stem}_print.pdf")
        else:
            default_name = self.pdf_path.with_name(f"{self.pdf_path.stem}_draft.pdf")

        out_path, _ = QFileDialog.getSaveFileName(self, "Preset-Export speichern", str(default_name), "PDF-Dateien (*.pdf)")
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)

        try:
            if profile.startswith("Archivmodus"):
                out_doc = self._clone_document_with_current_state()
                try:
                    out_doc.save(out_path, garbage=4, deflate=True, deflate_images=True, use_objstms=1)
                finally:
                    out_doc.close()
            elif profile.startswith("Druckmodus"):
                out_doc = fitz.open()
                try:
                    for idx in range(len(self.doc)):
                        page = self.doc[idx]
                        rotation = self.page_rotations.get(idx, 0)
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2).prerotate(rotation), alpha=False)
                        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                        gray = ImageOps.grayscale(img)
                        buf = io.BytesIO()
                        gray.save(buf, format="PNG")
                        new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
                        new_page.insert_image(new_page.rect, stream=buf.getvalue())
                    try:
                        out_doc.set_metadata(self.doc.metadata or {})
                    except Exception:
                        pass
                    out_doc.save(out_path, garbage=4, deflate=True, deflate_images=True, use_objstms=1)
                finally:
                    out_doc.close()
            else:
                out_doc = self._clone_document_with_current_state()
                try:
                    for idx in range(len(out_doc)):
                        page = out_doc[idx]
                        rect = page.rect
                        box = fitz.Rect(rect.width * 0.12, rect.height * 0.4, rect.width * 0.88, rect.height * 0.6)
                        page.insert_textbox(
                            box,
                            "ENTWURF",
                            fontsize=42,
                            color=(160 / 255.0, 160 / 255.0, 160 / 255.0),
                            align=1,
                            overlay=True,
                            stroke_opacity=0.18,
                            fill_opacity=0.18,
                            render_mode=0,
                            morph=(fitz.Point(0, 0), fitz.Matrix(1, 0, 0, 1).prerotate(45)),
                        )
                        num_box = fitz.Rect(rect.width - 138, rect.height - 38, rect.width - 18, rect.height - 18)
                        page.insert_textbox(num_box, str(idx + 1), fontsize=11, color=(80 / 255.0, 80 / 255.0, 80 / 255.0), align=2)
                    out_doc.save(out_path, garbage=3, deflate=True)
                finally:
                    out_doc.close()
            QMessageBox.information(self, "Erfolg", f"Preset-Export gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Preset angewendet: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Preset-Export fehlgeschlagen:\n{e}")

    def export_encrypted_pdf_copy(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        user_pw, ok = QInputDialog.getText(
            self,
            "PDF schützen",
            "Öffnungs-Passwort:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not user_pw:
            return
        owner_pw, ok = QInputDialog.getText(
            self,
            "PDF schützen",
            "Owner-/Admin-Passwort (leer = gleiches Passwort):",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        owner_pw = owner_pw or user_pw
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Geschützte PDF speichern",
            str(self.pdf_path.with_name(f"{self.pdf_path.stem}_protected.pdf")),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)
        out_doc = fitz.open()
        try:
            out_doc.insert_pdf(self.doc)
            for idx, rot in self.page_rotations.items():
                if 0 <= idx < len(out_doc) and rot % 360 != 0:
                    out_doc[idx].set_rotation(rot % 360)
            out_doc.save(
                out_path,
                encryption=fitz.PDF_ENCRYPT_AES_256,
                user_pw=user_pw,
                owner_pw=owner_pw,
            )
            QMessageBox.information(self, "Erfolg", f"Geschützte PDF gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Geschützte PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"PDF-Schutz fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    def export_decrypted_pdf_copy(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Entschlüsselte PDF speichern",
            str(self.pdf_path.with_name(f"{self.pdf_path.stem}_decrypted.pdf")),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)
        out_doc = fitz.open()
        try:
            out_doc.insert_pdf(self.doc)
            for idx, rot in self.page_rotations.items():
                if 0 <= idx < len(out_doc) and rot % 360 != 0:
                    out_doc[idx].set_rotation(rot % 360)
            out_doc.save(out_path)
            QMessageBox.information(self, "Erfolg", f"Entschlüsselte PDF gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Entschlüsselte PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Entschlüsseltes Speichern fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    def export_optimized_pdf_copy(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        profile, ok = QInputDialog.getItem(
            self,
            "PDF optimieren",
            "Optimierungsprofil:",
            ["Standard", "Stark komprimieren"],
            0,
            False,
        )
        if not ok:
            return
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Optimierte PDF speichern",
            str(self.pdf_path.with_name(f"{self.pdf_path.stem}_optimized.pdf")),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)
        out_doc = fitz.open()
        try:
            out_doc.insert_pdf(self.doc)
            for idx, rot in self.page_rotations.items():
                if 0 <= idx < len(out_doc) and rot % 360 != 0:
                    out_doc[idx].set_rotation(rot % 360)
            save_kwargs = {"garbage": 3, "deflate": True}
            if profile == "Stark komprimieren":
                save_kwargs.update({"garbage": 4, "deflate_images": True, "use_objstms": 1})
            out_doc.save(out_path, **save_kwargs)
            try:
                orig_size = self.pdf_path.stat().st_size
                new_size = Path(out_path).stat().st_size
                diff = orig_size - new_size
                pct = (diff / orig_size * 100.0) if orig_size else 0.0
                msg = f"Optimierte PDF gespeichert:\n{out_path}\n\nAlt: {orig_size/1024:.1f} KB\nNeu: {new_size/1024:.1f} KB\nErsparnis: {pct:.1f}%"
            except Exception:
                msg = f"Optimierte PDF gespeichert:\n{out_path}"
            QMessageBox.information(self, "Erfolg", msg)
            self.statusBar().showMessage(f"Optimierte PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Optimierung fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    def show_document_info(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        metadata = self.doc.metadata or {}
        try:
            file_size = self.pdf_path.stat().st_size
            size_text = f"{file_size / 1024:.1f} KB"
        except Exception:
            size_text = "unbekannt"
        total_pages = len(self.doc)
        widths: list[float] = []
        heights: list[float] = []
        images = 0
        annotations = 0
        form_fields = 0
        try:
            for i in range(total_pages):
                page = self.doc[i]
                widths.append(float(page.rect.width))
                heights.append(float(page.rect.height))
                images += len(page.get_images(full=True))
                annotations += len(list(page.annots() or []))
                form_fields += len(list(page.widgets() or []))
        except Exception:
            pass
        info_lines = [
            f"Datei: {self.pdf_path.name}",
            f"Seiten: {total_pages}",
            f"Dateigröße: {size_text}",
            f"Verschlüsselt: {'ja' if bool(getattr(self.doc, 'is_encrypted', False)) else 'nein'}",
            f"Eingebettete Bilder: {images}",
            f"Annotationen: {annotations}",
            f"Formularfelder: {form_fields}",
        ]
        if widths and heights:
            info_lines.append(f"Seitengröße erste Seite: {widths[0]:.0f} × {heights[0]:.0f} pt")
        for key, label in (("title", "Titel"), ("author", "Autor"), ("subject", "Betreff"), ("keywords", "Schlüsselwörter"), ("creator", "Erzeugt von"), ("producer", "Producer")):
            value = metadata.get(key)
            if value:
                info_lines.append(f"{label}: {value}")
        QMessageBox.information(self, "Dokumentinfos", "\n".join(info_lines))

    def show_page_overview(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Seitenübersicht")
        dlg.resize(860, 520)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Übersicht über Seiten, Größen, Bilder und Annotationen."))
        table = QTableWidget(len(self.doc), 6, dlg)
        table.setHorizontalHeaderLabels(["Seite", "Größe (pt)", "Rotation", "Bilder", "Annotationen", "Formulare"])
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for idx in range(len(self.doc)):
            page = self.doc[idx]
            image_count = len(page.get_images(full=True))
            annot_count = len(list(page.annots() or []))
            widget_count = len(list(page.widgets() or []))
            rotation = self.page_rotations.get(idx, 0) % 360
            table.setItem(idx, 0, SortableTableWidgetItem(str(idx + 1), idx + 1))
            table.setItem(idx, 1, QTableWidgetItem(f"{page.rect.width:.0f} × {page.rect.height:.0f}"))
            table.setItem(idx, 2, SortableTableWidgetItem(f"{rotation}°", rotation))
            table.setItem(idx, 3, SortableTableWidgetItem(str(image_count), image_count))
            table.setItem(idx, 4, SortableTableWidgetItem(str(annot_count), annot_count))
            table.setItem(idx, 5, SortableTableWidgetItem(str(widget_count), widget_count))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

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

    def export_pages_as_images(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page_spec, ok = QInputDialog.getText(
            self,
            "Seiten als Bilder exportieren",
            "Welche Seiten exportieren? (z.B. current, 1-3, odd, even, all)",
            text="current",
        )
        if not ok or not page_spec.strip():
            return

        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return

        fmt, ok = QInputDialog.getItem(self, "Bildformat", "Format:", ["PNG", "JPG"], 0, False)
        if not ok:
            return
        zoom, ok = QInputDialog.getDouble(self, "Auflösung", "Zoomfaktor fürs Rendering:", 2.0, 0.5, 6.0, 1)
        if not ok:
            return

        out_dir = QFileDialog.getExistingDirectory(self, "Zielordner für Bilder auswählen", str(self.pdf_path.parent))
        if not out_dir:
            return

        suffix = ".png" if fmt == "PNG" else ".jpg"
        try:
            for idx in page_indices:
                page = self.doc[idx]
                rotation = self.page_rotations.get(idx, 0)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom).prerotate(rotation), alpha=False)
                target = Path(out_dir) / f"{self.pdf_path.stem}_page_{idx + 1:03d}{suffix}"
                pix.save(str(target))
            self.statusBar().showMessage(f"{len(page_indices)} Seite(n) als {fmt} exportiert")
            QMessageBox.information(self, "Erfolg", f"{len(page_indices)} Seite(n) nach\n{out_dir}\nexportiert.")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Bildexport fehlgeschlagen:\n{e}")

    def extract_images_from_pdf(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        page_spec, ok = QInputDialog.getText(
            self,
            "Bilder extrahieren",
            "Welche Seiten durchsuchen? (z.B. current, 1-3, all)",
            text="all",
        )
        if not ok or not page_spec.strip():
            return
        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "Zielordner für extrahierte Bilder auswählen", str(self.pdf_path.parent))
        if not out_dir:
            return
        written = 0
        seen_xrefs: set[int] = set()
        try:
            for idx in page_indices:
                page = self.doc[idx]
                for img_no, img in enumerate(page.get_images(full=True), start=1):
                    xref = int(img[0])
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    info = self.doc.extract_image(xref)
                    ext = info.get("ext", "bin")
                    data = info.get("image")
                    if not data:
                        continue
                    target = Path(out_dir) / f"{self.pdf_path.stem}_page_{idx + 1:03d}_img_{img_no:02d}.{ext}"
                    target.write_bytes(data)
                    written += 1
            if written == 0:
                QMessageBox.information(self, "Hinweis", "Keine eingebetteten Bilder gefunden.")
                return
            QMessageBox.information(self, "Erfolg", f"{written} Bild(er) nach\n{out_dir}\nextrahiert.")
            self.statusBar().showMessage(f"{written} Bild(er) extrahiert")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Bildextraktion fehlgeschlagen:\n{e}")

    def export_grayscale_pdf_copy(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        page_spec, ok = QInputDialog.getText(
            self,
            "Graustufen-PDF exportieren",
            "Welche Seiten in Graustufen konvertieren? (z.B. all, 1-3, current)",
            text="all",
        )
        if not ok or not page_spec.strip():
            return
        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Graustufen-PDF speichern",
            str(self.pdf_path.with_name(f"{self.pdf_path.stem}_grayscale.pdf")),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)
        out_doc = fitz.open()
        try:
            selected = set(page_indices)
            for idx in range(len(self.doc)):
                page = self.doc[idx]
                if idx in selected:
                    rotation = self.page_rotations.get(idx, 0)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2).prerotate(rotation), alpha=False)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    gray = ImageOps.grayscale(img)
                    buffer = io.BytesIO()
                    gray.save(buffer, format="PNG")
                    buffer.seek(0)
                    new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
                    new_page.insert_image(new_page.rect, stream=buffer.getvalue())
                else:
                    out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
            out_doc.save(out_path, garbage=3, deflate=True)
            QMessageBox.information(self, "Erfolg", f"Graustufen-PDF gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Graustufen-PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Graustufen-Export fehlgeschlagen:\n{e}")
        finally:
            out_doc.close()

    def insert_page_numbers(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        page_spec, ok = QInputDialog.getText(
            self,
            "Seitennummern einfügen",
            "Welche Seiten nummerieren? (z.B. all, 1-3, current)",
            text="all",
        )
        if not ok or not page_spec.strip():
            return
        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return
        position, ok = QInputDialog.getItem(
            self,
            "Position",
            "Seitennummer-Position:",
            ["Unten mittig", "Unten rechts", "Unten links", "Oben rechts", "Oben links"],
            0,
            False,
        )
        if not ok:
            return
        font_size, ok = QInputDialog.getInt(self, "Schriftgröße", "Schriftgröße in pt:", 11, 6, 48, 1)
        if not ok:
            return
        color_raw, ok = QInputDialog.getText(
            self,
            "Farbe",
            "RGB oder Hex (z.B. 80,80,80 oder #505050):",
            text="80, 80, 80",
        )
        if not ok:
            return
        color = self._parse_rgb_color(color_raw, (80 / 255.0, 80 / 255.0, 80 / 255.0))
        start_number, ok = QInputDialog.getInt(self, "Startnummer", "Start bei:", 1, -99999, 99999, 1)
        if not ok:
            return
        try:
            self._push_undo_state()
            for offset, idx in enumerate(page_indices):
                page = self.doc[idx]
                rect = page.rect
                text = str(start_number + offset)
                box_width = 120
                box_height = max(24, font_size + 10)
                margin = 18
                if position == "Unten mittig":
                    box = fitz.Rect((rect.width - box_width) / 2, rect.height - box_height - margin, (rect.width + box_width) / 2, rect.height - margin)
                    align = 1
                elif position == "Unten rechts":
                    box = fitz.Rect(rect.width - box_width - margin, rect.height - box_height - margin, rect.width - margin, rect.height - margin)
                    align = 2
                elif position == "Unten links":
                    box = fitz.Rect(margin, rect.height - box_height - margin, margin + box_width, rect.height - margin)
                    align = 0
                elif position == "Oben rechts":
                    box = fitz.Rect(rect.width - box_width - margin, margin, rect.width - margin, margin + box_height)
                    align = 2
                else:
                    box = fitz.Rect(margin, margin, margin + box_width, margin + box_height)
                    align = 0
                rc = page.insert_textbox(box, text, fontsize=font_size, color=color, align=align)
                if rc < 0:
                    raise ValueError(f"Seitennummer passt auf Seite {idx + 1} nicht in den Zielbereich.")
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            self.statusBar().showMessage(f"Seitennummern auf {len(page_indices)} Seite(n) eingefügt")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Seitennummern konnten nicht eingefügt werden:\n{e}")

    def insert_header_footer(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        text, ok = QInputDialog.getText(self, "Kopf-/Fußzeile", "Text:")
        if not ok or not text.strip():
            return
        text = text.strip()
        position, ok = QInputDialog.getItem(
            self,
            "Position",
            "Wo soll der Text erscheinen?",
            [
                "Kopfzeile links", "Kopfzeile mittig", "Kopfzeile rechts",
                "Fußzeile links", "Fußzeile mittig", "Fußzeile rechts",
            ],
            1,
            False,
        )
        if not ok:
            return
        page_spec, ok = QInputDialog.getText(
            self,
            "Seitenbereich",
            "Auf welchen Seiten? (z.B. all, 1-3, current)",
            text="all",
        )
        if not ok or not page_spec.strip():
            return
        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return
        font_size = 10
        color = (80 / 255.0, 80 / 255.0, 80 / 255.0)
        is_header = position.startswith("Kopfzeile")
        align = 0 if position.endswith("links") else (1 if position.endswith("mittig") else 2)
        try:
            self._push_undo_state()
            for idx in page_indices:
                page = self.doc[idx]
                rect = page.rect
                margin = 18
                box_height = font_size + 8
                if is_header:
                    box = fitz.Rect(margin, margin, rect.width - margin, margin + box_height)
                else:
                    box = fitz.Rect(margin, rect.height - box_height - margin, rect.width - margin, rect.height - margin)
                rc = page.insert_textbox(box, text, fontsize=font_size, color=color, align=align)
                if rc < 0:
                    raise ValueError(f"Text passt auf Seite {idx + 1} nicht in den Zielbereich.")
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            self.statusBar().showMessage(f"{'Kopfzeile' if is_header else 'Fußzeile'} auf {len(page_indices)} Seite(n) eingefügt")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Kopf-/Fußzeile konnte nicht eingefügt werden:\n{e}")

    def insert_table(self) -> None:
        page = self._require_current_pdf_page()
        if page is None:
            return
        rows, ok = QInputDialog.getInt(self, "Tabelle einfügen", "Anzahl Zeilen:", 3, 1, 100, 1)
        if not ok:
            return
        cols, ok = QInputDialog.getInt(self, "Tabelle einfügen", "Anzahl Spalten:", 3, 1, 50, 1)
        if not ok:
            return
        try:
            self._push_undo_state()
            rect = page.rect
            # Zentrierter Bereich: 80 % Breite, ab 25 % Höhe, max. 50 % Höhe.
            left = rect.width * 0.10
            right = rect.width * 0.90
            top = rect.height * 0.25
            row_height = min(28.0, (rect.height * 0.5) / rows)
            bottom = top + row_height * rows
            col_width = (right - left) / cols
            line_color = (0.2, 0.2, 0.2)
            line_width = 1.0
            for r in range(rows + 1):
                y = top + r * row_height
                page.draw_line(fitz.Point(left, y), fitz.Point(right, y), color=line_color, width=line_width)
            for c in range(cols + 1):
                x = left + c * col_width
                page.draw_line(fitz.Point(x, top), fitz.Point(x, bottom), color=line_color, width=line_width)
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            self.statusBar().showMessage(f"Tabelle {rows}×{cols} auf Seite {self.current_page + 1} eingefügt")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Tabelle konnte nicht eingefügt werden:\n{e}")

    def add_text_watermark(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return
        watermark, ok = QInputDialog.getText(
            self,
            "Wasserzeichen",
            "Wasserzeichen-Text:",
            text="ENTWURF",
        )
        if not ok or not watermark.strip():
            return
        page_spec, ok = QInputDialog.getText(
            self,
            "Wasserzeichen",
            "Welche Seiten markieren? (z.B. all, 1-3, current)",
            text="all",
        )
        if not ok or not page_spec.strip():
            return
        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return
        font_size, ok = QInputDialog.getInt(self, "Wasserzeichen", "Schriftgröße in pt:", 42, 12, 200, 1)
        if not ok:
            return
        opacity_pct, ok = QInputDialog.getInt(self, "Wasserzeichen", "Deckkraft in %:", 18, 5, 100, 1)
        if not ok:
            return
        diagonal, ok = QInputDialog.getItem(self, "Wasserzeichen", "Ausrichtung:", ["Diagonal", "Horizontal"], 0, False)
        if not ok:
            return
        color_raw, ok = QInputDialog.getText(
            self,
            "Farbe",
            "RGB oder Hex (z.B. 160,160,160 oder #a0a0a0):",
            text="160, 160, 160",
        )
        if not ok:
            return
        color = self._parse_rgb_color(color_raw, (160 / 255.0, 160 / 255.0, 160 / 255.0))
        try:
            self._push_undo_state()
            morph = None
            if diagonal == "Diagonal":
                morph = (fitz.Point(0, 0), fitz.Matrix(1, 0, 0, 1).prerotate(45))
            for idx in page_indices:
                page = self.doc[idx]
                rect = page.rect
                box = fitz.Rect(rect.width * 0.12, rect.height * 0.4, rect.width * 0.88, rect.height * 0.6)
                kwargs = {
                    "fontsize": font_size,
                    "color": color,
                    "align": 1,
                    "overlay": True,
                    "stroke_opacity": opacity_pct / 100.0,
                    "fill_opacity": opacity_pct / 100.0,
                    "render_mode": 0,
                }
                if morph is not None:
                    kwargs["morph"] = morph
                rc = page.insert_textbox(box, watermark.strip(), **kwargs)
                if rc < 0:
                    raise ValueError(f"Wasserzeichen passt auf Seite {idx + 1} nicht in den Zielbereich.")
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            self.statusBar().showMessage(f"Wasserzeichen auf {len(page_indices)} Seite(n) eingefügt")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Wasserzeichen konnte nicht eingefügt werden:\n{e}")

    def export_folder_aggregate(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "PDF-Ordner auswählen")
        if not folder:
            return
        pdfs = _list_pdf_files(Path(folder))
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

    @staticmethod
    def _filename_confidence_label(info: ParsedDocInfo, is_fallback: bool) -> str:
        score = 0
        if info.date:
            score += 1
        if info.doc_type and info.doc_type != "Dokument":
            score += 1
        if info.vendor:
            score += 1
        if info.number:
            score += 1
        if is_fallback:
            score = min(score, 1)

        if score >= 4:
            return "hoch"
        if score >= 2:
            return "mittel"
        return "niedrig"

    @staticmethod
    def _build_filename_source(info: ParsedDocInfo) -> str:
        parts: list[str] = []
        if info.date:
            parts.append("Datum")
        if info.doc_type and info.doc_type != "Dokument":
            parts.append("Typ")
        if info.vendor:
            parts.append("Absender")
        if info.number:
            parts.append("Nummer")
        return "+".join(parts) if parts else "Fallback"

    def _show_batch_rename_preview(
        self,
        proposals: list[BatchRenameProposal],
        selected_mode: str,
        skipped_stats: dict[str, int] | None = None,
    ) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Batch-Rename Vorschau (Dry-Run)")
        dlg.resize(1080, 620)

        layout = QVBoxLayout(dlg)
        confidence_counts = {"hoch": 0, "mittel": 0, "niedrig": 0}
        unchanged = 0
        for p in proposals:
            key = p.confidence.lower()
            if key in confidence_counts:
                confidence_counts[key] += 1
            if p.src == p.dst:
                unchanged += 1
        actionable = len(proposals) - unchanged

        skipped_line = ""
        if skipped_stats:
            skipped_total = sum(skipped_stats.values())
            if skipped_total:
                skipped_line = (
                    f"Übersprungen vor Vorschau: {skipped_total} "
                    f"(Modus={skipped_stats.get('mode_filter', 0)}, "
                    f"Safe={skipped_stats.get('safe_filter', 0)}, "
                    f"Konflikt={skipped_stats.get('conflict_skip', 0)})\n"
                )

        summary = QLabel(
            f"Modus: {selected_mode} | Einträge: {len(proposals)} | Umbenennbar: {actionable} | Unverändert: {unchanged}\n"
            f"Confidence: hoch={confidence_counts['hoch']}, mittel={confidence_counts['mittel']}, niedrig={confidence_counts['niedrig']}\n"
            f"{skipped_line}"
            "Legende: grün=hoch, gelb=mittel, rot=niedrig. Klick auf Spaltenkopf zum Sortieren.\n"
            "Prüfe Altname → Neuer Name. Erst mit 'Umbenennen' wird geschrieben."
        )
        layout.addWidget(summary)

        table = QTableWidget(len(proposals), 6, dlg)
        table.setHorizontalHeaderLabels(["Status", "Altname", "Neuer Name", "Confidence", "Quelle", "Grund"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        # Keep sorting disabled while populating rows to avoid reordering artifacts.
        table.setSortingEnabled(False)

        confidence_row_colors = {
            "hoch": QColor(232, 245, 233),
            "mittel": QColor(255, 248, 225),
            "niedrig": QColor(255, 235, 238),
        }

        confidence_sort_order = {"hoch": 0, "mittel": 1, "niedrig": 2}

        for row, proposal in enumerate(proposals):
            unchanged_row = proposal.src == proposal.dst
            status_text = "Unverändert" if unchanged_row else "Umbenennen"
            values = [
                status_text,
                proposal.src.name,
                proposal.dst.name,
                proposal.confidence,
                proposal.source,
                proposal.reason,
            ]
            row_color = QColor(238, 238, 238) if unchanged_row else confidence_row_colors.get(proposal.confidence.lower())
            for col, value in enumerate(values):
                sort_key = None
                if col == 0:
                    sort_key = 1 if unchanged_row else 0
                elif col == 3:
                    sort_key = confidence_sort_order.get(proposal.confidence.lower(), 99)
                item = SortableTableWidgetItem(value, sort_key=sort_key)
                if row_color is not None:
                    item.setBackground(row_color)
                table.setItem(row, col, item)

        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        table.setSortingEnabled(True)
        table.sortItems(3, Qt.SortOrder.AscendingOrder)
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            if actionable > 0:
                ok_button.setText("Umbenennen")
            else:
                ok_button.setText("Nichts umzubenennen")
                ok_button.setEnabled(False)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button:
            cancel_button.setText("Abbrechen")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        return dlg.exec() == QDialog.DialogCode.Accepted

    def batch_rename_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Ordner für Batch-Rename auswählen")
        if not folder:
            return
        folder_path = Path(folder)
        files = _list_pdf_files(folder_path)
        if not files:
            QMessageBox.information(self, "Hinweis", "Keine PDFs gefunden.")
            return

        mode_labels = [
            "Alle Dateien",
            "Nur unsichere Vorschläge",
            "Nur Fallback-Namen (Dokument.pdf)",
        ]
        selected_mode, ok_mode = QInputDialog.getItem(
            self,
            "Stapel-Umbenennen Modus",
            "Welche Dateien sollen umbenannt werden?",
            mode_labels,
            0,
            False,
        )
        if not ok_mode:
            return

        rename_policy_labels = [
            "Konflikte mit (1), (2), … auflösen",
            "Konflikte überspringen",
        ]
        selected_policy, ok_policy = QInputDialog.getItem(
            self,
            "Konflikt-Regel",
            "Wie sollen Namenskonflikte behandelt werden?",
            rename_policy_labels,
            0,
            False,
        )
        if not ok_policy:
            return

        safe_only_answer, ok_safe = QInputDialog.getItem(
            self,
            "Sicherheitsfilter",
            "Sollen nur sichere Vorschläge (Confidence hoch) automatisch umbenannt werden?",
            ["Nein, alle aus Modus", "Ja, nur Confidence hoch"],
            0,
            False,
        )
        if not ok_safe:
            return
        safe_only = safe_only_answer.startswith("Ja")

        proposals: list[BatchRenameProposal] = []
        used_targets: set[str] = set()
        analysis_errors: list[str] = []
        skipped_stats = {
            "mode_filter": 0,
            "safe_filter": 0,
            "conflict_skip": 0,
        }

        progress = QProgressDialog("Analysiere PDFs für Stapel-Umbenennen …", "Abbrechen", 0, len(files), self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        for idx, src in enumerate(files):
            progress.setValue(idx)
            progress.setLabelText(f"Analysiere {src.name} ({idx + 1}/{len(files)}) …")
            QApplication.processEvents()
            if progress.wasCanceled():
                QMessageBox.information(self, "Abgebrochen", "Analyse für Stapel-Umbenennen wurde abgebrochen.")
                return

            try:
                with fitz.open(str(src)) as doc:
                    first_page_text = doc[0].get_text("text").strip() if len(doc) else ""
                    if len(first_page_text) < 40 and len(doc):
                        pix = doc[0].get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                        ocr_text, _ = self._ocr_image(img, show_error=False)
                        if ocr_text:
                            first_page_text = f"{first_page_text}\n{ocr_text}".strip()
                base_name = suggest_filename_from_text(first_page_text)
                info = parse_doc_info(first_page_text)
            except Exception as e:
                analysis_errors.append(f"{src.name}: {e}")
                base_name = "Dokument.pdf"
                info = ParsedDocInfo()

            stem = Path(base_name).stem
            candidate = f"{stem}.pdf"
            n = 1
            conflict = candidate.lower() in used_targets or ((folder_path / candidate).exists() and (folder_path / candidate) != src)
            if conflict and selected_policy == "Konflikte überspringen":
                analysis_errors.append(f"{src.name}: Namenskonflikt für '{candidate}' (übersprungen)")
                skipped_stats["conflict_skip"] += 1
                continue
            while candidate.lower() in used_targets or ((folder_path / candidate).exists() and (folder_path / candidate) != src):
                candidate = f"{stem}({n}).pdf"
                n += 1
            used_targets.add(candidate.lower())

            is_fallback = Path(base_name).name.lower() == "dokument.pdf"
            is_uncertain = is_fallback or not info.date or not info.number
            confidence = self._filename_confidence_label(info, is_fallback)

            include = (
                selected_mode == "Alle Dateien"
                or (selected_mode == "Nur unsichere Vorschläge" and is_uncertain)
                or (selected_mode == "Nur Fallback-Namen (Dokument.pdf)" and is_fallback)
            )
            if not include:
                skipped_stats["mode_filter"] += 1
                continue

            if safe_only and confidence != "hoch":
                skipped_stats["safe_filter"] += 1
                continue

            reason_parts: list[str] = []
            if is_fallback:
                reason_parts.append("Fallback")
            if not info.date:
                reason_parts.append("kein Datum")
            if not info.number:
                reason_parts.append("keine Nummer")
            if not info.vendor:
                reason_parts.append("kein Absender")
            reason = ", ".join(reason_parts) if reason_parts else "ok"
            source = self._build_filename_source(info)
            proposals.append(
                BatchRenameProposal(
                    src=src,
                    dst=folder_path / candidate,
                    reason=reason,
                    source=source,
                    confidence=confidence,
                )
            )

        progress.setValue(len(files))

        if not proposals:
            skipped_total = sum(skipped_stats.values())
            details = (
                f"\nÜbersprungen gesamt: {skipped_total}"
                f" (Modus-Filter: {skipped_stats['mode_filter']},"
                f" Safe-Filter: {skipped_stats['safe_filter']},"
                f" Konflikt-Überspringen: {skipped_stats['conflict_skip']})"
            ) if skipped_total else ""
            analysis_hint = f"\nAnalysefehler: {len(analysis_errors)}" if analysis_errors else ""
            QMessageBox.information(
                self,
                "Hinweis",
                "Für den gewählten Modus gibt es keine umbenennbaren Dateien."
                f"{details}"
                f"{analysis_hint}",
            )
            return

        if not self._show_batch_rename_preview(proposals, selected_mode, skipped_stats=skipped_stats):
            return

        unchanged_count = sum(1 for p in proposals if p.src == p.dst)
        renamed = 0
        rename_errors: list[str] = []
        renamed_pairs: list[tuple[str, str, str]] = []
        for proposal in proposals:
            src, dst, reason = proposal.src, proposal.dst, proposal.reason
            if src == dst:
                continue
            try:
                src.rename(dst)
                renamed += 1
                renamed_pairs.append((src.name, dst.name, reason))
            except Exception as e:
                rename_errors.append(f"{src.name} -> {dst.name}: {e}")

        summary = (
            f"Stapel-Umbenennen abgeschlossen: {renamed} Datei(en) umbenannt."
            f"\nUnverändert belassen: {unchanged_count}"
            f"\nIn Vorschau berücksichtigt: {len(proposals)}"
        )
        skipped_total = sum(skipped_stats.values())
        if skipped_total:
            summary += (
                f"\nÜbersprungen gesamt: {skipped_total}"
                f" (Modus-Filter: {skipped_stats['mode_filter']},"
                f" Safe-Filter: {skipped_stats['safe_filter']},"
                f" Konflikt-Überspringen: {skipped_stats['conflict_skip']})"
            )
        if analysis_errors:
            summary += f"\nAnalysefehler: {len(analysis_errors)}"
        if rename_errors:
            summary += f"\nRename-Fehler: {len(rename_errors)}"

        QMessageBox.information(self, "Fertig", summary)

        if renamed_pairs or analysis_errors or rename_errors:
            save_log = QMessageBox.question(
                self,
                "Batch-Rename Protokoll speichern",
                "Möchtest du ein Protokoll mit den Umbenennungen/Fehlern speichern?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if save_log == QMessageBox.StandardButton.Yes:
                log_fmt, ok_log_fmt = QInputDialog.getItem(
                    self,
                    "Log-Format",
                    "Format:",
                    ["TXT", "CSV"],
                    0,
                    False,
                )
                if ok_log_fmt:
                    suffix = ".txt" if log_fmt == "TXT" else ".csv"
                    default_log = folder_path / f"batch-rename-log{suffix}"
                    log_path, _ = QFileDialog.getSaveFileName(
                        self,
                        "Batch-Rename Protokoll speichern",
                        str(default_log),
                        "Textdateien (*.txt);;CSV-Dateien (*.csv)",
                    )
                    if log_path:
                        out = Path(log_path)
                        if log_fmt == "CSV":
                            rows: list[dict[str, str]] = []
                            proposal_meta = {
                                p.src.name: {"confidence": p.confidence, "source": p.source, "reason": p.reason}
                                for p in proposals
                            }
                            for old, new, reason in renamed_pairs:
                                meta = proposal_meta.get(old, {})
                                rows.append(
                                    {
                                        "status": "renamed",
                                        "old": old,
                                        "new": new,
                                        "reason": reason,
                                        "confidence": str(meta.get("confidence", "")),
                                        "source": str(meta.get("source", "")),
                                        "detail": "",
                                    }
                                )
                            for err in analysis_errors:
                                rows.append({"status": "analysis_error", "old": "", "new": "", "reason": "", "confidence": "", "source": "", "detail": err})
                            for err in rename_errors:
                                rows.append({"status": "rename_error", "old": "", "new": "", "reason": "", "confidence": "", "source": "", "detail": err})
                            rows.append({
                                "status": "summary",
                                "old": "",
                                "new": "",
                                "reason": "run_counts",
                                "confidence": "",
                                "source": "",
                                "detail": (
                                    f"preview={len(proposals)};"
                                    f"renamed={renamed};"
                                    f"unchanged={unchanged_count};"
                                    f"mode_filter={skipped_stats['mode_filter']};"
                                    f"safe_filter={skipped_stats['safe_filter']};"
                                    f"conflict_skip={skipped_stats['conflict_skip']}"
                                ),
                            })
                            if out.suffix.lower() != ".csv":
                                out = out.with_suffix(".csv")
                            with out.open("w", encoding="utf-8", newline="") as f:
                                writer = csv.DictWriter(
                                    f,
                                    fieldnames=["status", "old", "new", "reason", "confidence", "source", "detail"],
                                )
                                writer.writeheader()
                                writer.writerows(rows)
                        else:
                            if out.suffix.lower() != ".txt":
                                out = out.with_suffix(".txt")
                            lines_out: list[str] = []
                            lines_out.append(f"Modus: {selected_mode}")
                            lines_out.append(f"Konfliktregel: {selected_policy}")
                            lines_out.append(f"Nur sichere Vorschläge: {'Ja' if safe_only else 'Nein'}")
                            lines_out.append(f"Umbenannt: {renamed}")
                            lines_out.append(f"Unverändert: {unchanged_count}")
                            lines_out.append(f"In Vorschau: {len(proposals)}")
                            lines_out.append(
                                "Übersprungen: "
                                f"Modus-Filter={skipped_stats['mode_filter']}, "
                                f"Safe-Filter={skipped_stats['safe_filter']}, "
                                f"Konflikt-Überspringen={skipped_stats['conflict_skip']}"
                            )
                            lines_out.append("")
                            if renamed_pairs:
                                lines_out.append("=== Renamed ===")
                                for old, new, reason in renamed_pairs:
                                    lines_out.append(f"{old} -> {new} [{reason}]")
                                lines_out.append("")
                            if analysis_errors:
                                lines_out.append("=== Analysefehler ===")
                                lines_out.extend(analysis_errors)
                                lines_out.append("")
                            if rename_errors:
                                lines_out.append("=== Rename-Fehler ===")
                                lines_out.extend(rename_errors)
                                lines_out.append("")
                            out.write_text("\n".join(lines_out).strip() + "\n", encoding="utf-8")

                        self.statusBar().showMessage(f"Batch-Rename Log gespeichert: {out.name}")

    def merge_pdfs(self) -> None:
        file_names, _ = QFileDialog.getOpenFileNames(self, "PDFs zum Mergen auswählen", "", "PDF-Dateien (*.pdf)")
        if not file_names:
            return

        unique_file_names = list(dict.fromkeys(file_names))
        skipped_duplicates = len(file_names) - len(unique_file_names)
        if len(unique_file_names) < 2:
            QMessageBox.information(self, "Hinweis", "Bitte mindestens zwei unterschiedliche PDFs auswählen.")
            return

        out_path, _ = QFileDialog.getSaveFileName(self, "Zusammengeführte PDF speichern", "zusammengefuehrt.pdf", "PDF-Dateien (*.pdf)")
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)

        merged = fitz.open()
        try:
            for path in unique_file_names:
                src = None
                try:
                    src = fitz.open(path)
                    merged.insert_pdf(src)
                finally:
                    if src is not None:
                        src.close()
            merged.save(out_path)
            duplicate_note = ""
            if skipped_duplicates > 0:
                duplicate_note = f"\n\nHinweis: {skipped_duplicates} doppelte Auswahl(en) wurden ignoriert."
            QMessageBox.information(self, "Erfolg", f"Zusammengeführte PDF gespeichert:\n{out_path}{duplicate_note}")
            self.statusBar().showMessage(f"Zusammenführung erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Zusammenführung fehlgeschlagen:\n{e}")
        finally:
            merged.close()

    def images_to_pdf(self) -> None:
        file_names, _ = QFileDialog.getOpenFileNames(
            self,
            "Bilder für PDF auswählen",
            "",
            "Bilder (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff)",
        )
        if not file_names:
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "PDF aus Bildern speichern",
            str(Path(file_names[0]).with_suffix(".pdf")),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)

        doc = fitz.open()
        try:
            for file_name in file_names:
                img = Image.open(file_name)
                if img.mode not in {"RGB", "L"}:
                    img = img.convert("RGB")
                width_px, height_px = img.size
                dpi = img.info.get("dpi", (72, 72))
                dpi_x = dpi[0] if isinstance(dpi, tuple) and dpi and dpi[0] else 72
                dpi_y = dpi[1] if isinstance(dpi, tuple) and len(dpi) > 1 and dpi[1] else dpi_x
                width_pt = max(72.0, width_px * 72.0 / float(dpi_x or 72))
                height_pt = max(72.0, height_px * 72.0 / float(dpi_y or 72))
                page = doc.new_page(width=width_pt, height=height_pt)
                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                page.insert_image(page.rect, stream=buffer.getvalue())
            doc.save(out_path)
            QMessageBox.information(self, "Erfolg", f"PDF aus {len(file_names)} Bild(er)n gespeichert:\n{out_path}")
            self.statusBar().showMessage(f"Bild-PDF erstellt: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Bild-zu-PDF fehlgeschlagen:\n{e}")
        finally:
            doc.close()

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
        out_path, _ = QFileDialog.getSaveFileName(self, "Extrakt speichern", str(self.pdf_path.with_name(default_name)), "PDF-Dateien (*.pdf)")
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

    def add_text_annotation(self) -> None:
        self.select_annotation_tool("text", activate=True)

    def add_rectangle_annotation(self) -> None:
        self.select_annotation_tool("rect", activate=True)

    def add_ellipse_annotation(self) -> None:
        self.select_annotation_tool("ellipse", activate=True)

    def add_star_annotation(self) -> None:
        self.select_annotation_tool("star", activate=True)

    def insert_symbol(self) -> None:
        if self._require_current_pdf_page() is None:
            return
        symbols = [
            "©", "®", "™", "§", "¶", "†", "‡", "•", "–", "—",
            "€", "£", "¥", "°", "±", "×", "÷",
            "→", "←", "↑", "↓", "⇒", "⇐",
            "✓", "✗", "★", "☆", "☑", "☐", "●",
        ]
        choice, ok = QInputDialog.getItem(
            self,
            "Symbol einfügen",
            "Symbol wählen (oder eigenes eingeben):",
            symbols,
            0,
            True,
        )
        if not ok or not choice:
            return
        self.annotation_tool_kind = "text"
        self._set_annotation_defaults("text")
        self._sync_annotation_tool_buttons()
        self._begin_pending_annotation(
            {"kind": "text", "text": choice, "font_size": 24, "fontname": "helv", "color": (0.0, 0.0, 0.0)},
            f"Symbol '{choice}' – ziehe in der Vorschau den Platzierungsbereich auf.",
        )

    def insert_textart(self) -> None:
        if self._require_current_pdf_page() is None:
            return
        text, ok = QInputDialog.getText(self, "TextArt einfügen", "Text:")
        if not ok or not text.strip():
            return
        text = text.strip()
        styles = {
            "Rot, fett": ((0.86, 0.08, 0.24), "hebo"),
            "Blau, fett": ((0.0, 0.40, 0.80), "hebo"),
            "Grün, fett": ((0.0, 0.55, 0.30), "hebo"),
            "Violett, fett": ((0.50, 0.10, 0.60), "hebo"),
            "Schwarz, fett": ((0.0, 0.0, 0.0), "hebo"),
        }
        style_choice, ok = QInputDialog.getItem(
            self, "TextArt-Stil", "Farbe / Stil:", list(styles.keys()), 0, False
        )
        if not ok:
            return
        size, ok = QInputDialog.getInt(self, "Schriftgröße", "Größe in pt:", 40, 12, 200, 2)
        if not ok:
            return
        color, fontcode = styles[style_choice]
        self.annotation_tool_kind = "text"
        self._set_annotation_defaults("text")
        self._sync_annotation_tool_buttons()
        self._begin_pending_annotation(
            {"kind": "text", "text": text, "font_size": size, "fontname": fontcode, "color": color},
            "TextArt – ziehe in der Vorschau den Platzierungsbereich auf.",
        )

    def add_link_annotation(self) -> None:
        self.select_annotation_tool("link", activate=True)

    def add_highlight_annotation(self) -> None:
        self.select_annotation_tool("highlight", activate=True)

    def add_strikeout_annotation(self) -> None:
        self.select_annotation_tool("strikeout", activate=True)

    def add_underline_annotation(self) -> None:
        self.select_annotation_tool("underline", activate=True)

    def add_line_annotation(self) -> None:
        self.select_annotation_tool("line", activate=True)

    def add_arrow_annotation(self) -> None:
        self.select_annotation_tool("arrow", activate=True)

    def add_image_annotation(self) -> None:
        self.select_annotation_tool("image", activate=True)

    def add_redaction_annotation(self) -> None:
        self.select_annotation_tool("redact", activate=True)

    def add_note_annotation(self) -> None:
        self.select_annotation_tool("note", activate=True)

    def add_freehand_annotation(self) -> None:
        self.select_annotation_tool("freehand", activate=True)

    def replace_text_annotation(self) -> None:
        self.select_annotation_tool("text-replace", activate=True)

    def edit_text_tool(self) -> None:
        self.select_annotation_tool("text-edit", activate=True)

    def pick_annotation_image(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Bild für PDF auswählen",
            "",
            "Bilder (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if not file_name:
            return
        path = Path(file_name)
        preview = QPixmap(str(path))
        if preview.isNull():
            QMessageBox.warning(self, "Ungültig", "Die ausgewählte Bilddatei konnte nicht geladen werden.")
            return
        self.annotation_image_path = path
        self.annotation_image_preview = preview
        self.annotation_image_path_label.setText(path.name)
        self.statusBar().showMessage(f"Bild ausgewählt: {path.name}")

    def edit_form_fields_on_current_page(self) -> None:
        page = self._require_current_pdf_page()
        if page is None:
            return
        widgets = list(page.widgets() or [])
        if not widgets:
            QMessageBox.information(self, "Hinweis", "Auf der aktuellen Seite wurden keine Formularfelder gefunden.")
            return

        items: list[str] = []
        widget_map: dict[str, object] = {}
        for idx, widget in enumerate(widgets, start=1):
            field_name = getattr(widget, "field_name", None) or f"Feld {idx}"
            field_label = getattr(widget, "field_label", None) or ""
            field_value = getattr(widget, "field_value", None)
            field_type = getattr(widget, "field_type_string", None) or str(getattr(widget, "field_type", "Widget"))
            display = f"{field_name} ({field_type})"
            if field_label:
                display += f" – {field_label}"
            if field_value not in (None, ""):
                display += f" = {field_value}"
            items.append(display)
            widget_map[display] = widget

        choice, ok = QInputDialog.getItem(
            self,
            "Formularfeld bearbeiten",
            "Feld auswählen:",
            items,
            0,
            False,
        )
        if not ok or not choice:
            return
        widget = widget_map[choice]
        field_name = getattr(widget, "field_name", None) or "Formularfeld"
        current_value = getattr(widget, "field_value", None)
        try:
            field_type_text = str(getattr(widget, "field_type_string", None) or getattr(widget, "field_type", "")).lower()
            is_bool_field = any(token in field_type_text for token in ("checkbox", "check", "radio", "button", "bool"))
            if any(token in field_type_text for token in ("choice", "combo", "list", "select")):
                options = []
                for attr_name in ("choice_values", "options"):
                    try:
                        values = getattr(widget, attr_name, None)
                        if values:
                            options = [str(v) for v in values]
                            break
                    except Exception:
                        pass
                if not options:
                    options_text, ok = QInputDialog.getText(
                        self,
                        "Auswahloptionen",
                        "Optionen konnten nicht gelesen werden. Erlaubte Werte manuell eingeben (kommagetrennt):",
                        text="",
                    )
                    if not ok:
                        return
                    options = [p.strip() for p in options_text.split(",") if p.strip()]
                if not options:
                    QMessageBox.information(self, "Hinweis", "Für dieses Auswahlfeld sind keine Optionen verfügbar.")
                    return
                current_index = options.index(str(current_value)) if current_value is not None and str(current_value) in options else 0
                selected_value, ok = QInputDialog.getItem(
                    self,
                    "Formularwert bearbeiten",
                    f"Wert für '{field_name}':",
                    options,
                    current_index,
                    False,
                )
                if not ok:
                    return
                new_value = selected_value
            elif is_bool_field:
                current_bool = str(current_value).lower() in {"1", "true", "yes", "on"}
                choice_value, ok = QInputDialog.getItem(
                    self,
                    "Formularwert bearbeiten",
                    f"Wert für '{field_name}':",
                    ["Aus", "An"],
                    1 if current_bool else 0,
                    False,
                )
                if not ok:
                    return
                new_value = choice_value == "An"
            else:
                new_value, ok = QInputDialog.getText(
                    self,
                    "Formularwert bearbeiten",
                    f"Neuer Wert für '{field_name}':",
                    text="" if current_value is None else str(current_value),
                )
                if not ok:
                    return
            self._push_undo_state()
            widget.field_value = new_value
            widget.update()
            self._set_dirty(True)
            self.render_current_page()
            self.statusBar().showMessage(f"Formularfeld aktualisiert: {field_name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Formularfeld konnte nicht aktualisiert werden:\n{e}")

    def crop_pages_to_new_pdf(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        page_spec, ok = QInputDialog.getText(
            self,
            "Seiten zuschneiden",
            "Welche Seiten zuschneiden? (z.B. current, 1-3, odd, even, all)",
            text="current",
        )
        if not ok or not page_spec.strip():
            return

        page_indices = self._parse_page_spec(page_spec, len(self.doc))
        if not page_indices:
            QMessageBox.warning(self, "Ungültig", "Kein gültiger Seitenbereich erkannt.")
            return

        margins_raw, ok = QInputDialog.getText(
            self,
            "Ränder in Prozent",
            "Ränder links, oben, rechts, unten in Prozent eingeben (z.B. 5, 5, 5, 5):",
            text="5, 5, 5, 5",
        )
        if not ok or not margins_raw.strip():
            return

        margins = self._parse_crop_margins_percent(margins_raw)
        if margins is None:
            QMessageBox.warning(
                self,
                "Ungültig",
                "Bitte genau vier nichtnegative Prozentwerte angeben, z.B. 5, 5, 5, 5.",
            )
            return

        left_pct, top_pct, right_pct, bottom_pct = margins

        default_name = f"{self.pdf_path.stem}_crop.pdf"
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Zugeschnittene PDF speichern",
            str(self.pdf_path.with_name(default_name)),
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        out_path = self._ensure_pdf_suffix(out_path)

        out_doc = fitz.open()
        try:
            for idx in page_indices:
                out_doc.insert_pdf(self.doc, from_page=idx, to_page=idx)
                out_page = out_doc[-1]
                base_rect = out_page.rect
                crop_rect = fitz.Rect(
                    base_rect.x0 + (base_rect.width * left_pct / 100.0),
                    base_rect.y0 + (base_rect.height * top_pct / 100.0),
                    base_rect.x1 - (base_rect.width * right_pct / 100.0),
                    base_rect.y1 - (base_rect.height * bottom_pct / 100.0),
                )
                if crop_rect.width < 36 or crop_rect.height < 36:
                    raise ValueError(
                        f"Seite {idx + 1} würde zu klein werden ({crop_rect.width:.1f} x {crop_rect.height:.1f} pt)."
                    )
                out_page.set_cropbox(crop_rect)
                rot = self.page_rotations.get(idx, 0) % 360
                if rot:
                    out_page.set_rotation(rot)

            out_doc.save(out_path)
            QMessageBox.information(
                self,
                "Erfolg",
                "Zugeschnittene PDF gespeichert:\n"
                f"{out_path}\n\n"
                f"Ränder: links {left_pct:g}%, oben {top_pct:g}%, rechts {right_pct:g}%, unten {bottom_pct:g}%",
            )
            self.statusBar().showMessage(f"PDF zugeschnitten: {Path(out_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Zuschneiden fehlgeschlagen:\n{e}")
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
                r"(first|start|begin|erste|anfang|last|end|ende|letzte|middle|mid|center|centre|mitte|current|cur|here|hier|aktuell|jetzt)\s*([+-]\s*\d+)?",
                tk,
            )
            if alias_with_offset:
                base_name, offset_raw = alias_with_offset.groups()
                if base_name in {"first", "start", "begin", "erste", "anfang"}:
                    base = 1
                elif base_name in {"last", "end", "ende", "letzte"}:
                    base = total_pages
                elif base_name in {"middle", "mid", "center", "centre", "mitte"}:
                    base = ((total_pages - 1) // 2) + 1
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

            # Accept textual range connectors as well, e.g. "1 up to 5", "1 to/thru/through/until 5" / "1 bis zu/durch 5".
            body = re.sub(r"\b(?:up\s+to|bis\s+zu|to|thru|through|until|till|bis|durch)\b", "-", body, flags=re.IGNORECASE)
            # Normalize Unicode dashes users often paste from rich text.
            body = re.sub(r"[–—−]", "-", body)

            # Range bounds can themselves contain signed alias offsets like
            # "last-1" or "current+2". Use a bound-aware regex instead of a
            # naive split("-", 1), otherwise "last-1-last" is parsed wrongly.
            bound = r"(?:\d+|first|start|begin|erste|anfang|last|end|ende|letzte|middle|mid|center|centre|mitte|current|cur|here|hier|aktuell|jetzt)(?:\s*[+-]\s*\d+)?"
            m = re.fullmatch(rf"\s*({bound})\s*-\s*({bound})\s*", body)
            if not m:
                return None
            start_raw, end_raw = m.groups()
            return start_raw, end_raw, step

        # Support comma/semicolon lists plus line-based pasted input.
        for part in re.split(r"[;,\n\r]+", spec):
            token = part.strip().lower()
            if not token:
                continue

            if token in {"all", "alle", "*"}:
                for idx in range(total_pages):
                    add_page(idx)
                continue
            if token in {"odd", "ungerade"}:
                for idx in range(0, total_pages, 2):
                    add_page(idx)
                continue
            if token in {"even", "gerade"}:
                for idx in range(1, total_pages, 2):
                    add_page(idx)
                continue
            if token in {"reverse", "rev", "backward", "backwards", "desc", "descending", "absteigend", "rückwärts", "rueckwaerts", "umgekehrt"}:
                for idx in reversed(range(total_pages)):
                    add_page(idx)
                continue
            if token in {"first", "start", "begin", "erste", "anfang"}:
                add_page(0)
                continue
            if token in {"last", "end", "ende", "letzte"}:
                add_page(total_pages - 1)
                continue
            if token in {"middle", "mid", "center", "centre", "mitte"}:
                add_page((total_pages - 1) // 2)
                continue
            if token in {"current", "cur", "here", "hier", "aktuell", "jetzt"}:
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

    def _parse_crop_margins_percent(self, spec: str) -> tuple[float, float, float, float] | None:
        parts = [p.strip().replace("%", "") for p in re.split(r"[;,\s]+", spec or "") if p.strip()]
        if len(parts) != 4:
            return None
        try:
            left, top, right, bottom = (float(part.replace(",", ".")) for part in parts)
        except ValueError:
            return None
        margins = (left, top, right, bottom)
        if any(value < 0 for value in margins):
            return None
        if left + right >= 95 or top + bottom >= 95:
            return None
        return margins

    def _parse_rect_percent(self, spec: str) -> tuple[float, float, float, float] | None:
        parts = [p.strip().replace("%", "") for p in re.split(r"[;,\s]+", spec or "") if p.strip()]
        if len(parts) != 4:
            return None
        try:
            left, top, width, height = (float(part.replace(",", ".")) for part in parts)
        except ValueError:
            return None
        if min(left, top, width, height) < 0:
            return None
        if width <= 0 or height <= 0:
            return None
        if left + width > 100 or top + height > 100:
            return None
        return left, top, width, height

    def _anchor_rect_percent(
        self,
        anchor_x: float,
        anchor_y: float,
        width_pct: float,
        height_pct: float,
    ) -> tuple[float, float, float, float]:
        left = min(max(anchor_x, 0.0), max(0.0, 100.0 - width_pct))
        top = min(max(anchor_y, 0.0), max(0.0, 100.0 - height_pct))
        return left, top, width_pct, height_pct

    def _rect_percent_from_drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> tuple[float, float, float, float] | None:
        left = max(0.0, min(start_x, end_x))
        top = max(0.0, min(start_y, end_y))
        right = min(100.0, max(start_x, end_x))
        bottom = min(100.0, max(start_y, end_y))
        width = right - left
        height = bottom - top
        if width < 1.0 or height < 1.0:
            return None
        return left, top, width, height

    def _rect_from_percent(self, page: fitz.Page, spec: tuple[float, float, float, float]) -> fitz.Rect:
        left, top, width, height = spec
        base = page.rect
        return fitz.Rect(
            base.x0 + base.width * left / 100.0,
            base.y0 + base.height * top / 100.0,
            base.x0 + base.width * (left + width) / 100.0,
            base.y0 + base.height * (top + height) / 100.0,
        )

    def _rect_percent_to_view_box(
        self,
        spec: tuple[float, float, float, float],
        view_width: int,
        view_height: int,
    ) -> tuple[int, int, int, int] | None:
        left, top, width, height = spec
        if width <= 0 or height <= 0 or view_width <= 0 or view_height <= 0:
            return None
        x = int(round((left / 100.0) * view_width))
        y = int(round((top / 100.0) * view_height))
        w = int(round((width / 100.0) * view_width))
        h = int(round((height / 100.0) * view_height))
        if w <= 0 or h <= 0:
            return None
        return x, y, w, h

    def _require_current_pdf_page(self) -> fitz.Page | None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return None
        if not (0 <= self.current_page < len(self.doc)):
            QMessageBox.warning(self, "Ungültig", "Aktuelle Seite ist nicht verfügbar.")
            return None
        return self.doc[self.current_page]

    def _parse_rgb_color(self, spec: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
        value = (spec or "").strip()
        if value.startswith("#"):
            hex_value = value[1:]
            if len(hex_value) == 3:
                hex_value = "".join(ch * 2 for ch in hex_value)
            if len(hex_value) == 6:
                try:
                    return tuple(int(hex_value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
                except ValueError:
                    return default
        parts = [p.strip() for p in re.split(r"[;,\s]+", spec or "") if p.strip()]
        if len(parts) != 3:
            return default
        try:
            values = [max(0, min(255, int(float(part.replace(",", "."))))) / 255.0 for part in parts]
        except ValueError:
            return default
        return values[0], values[1], values[2]

    def _color_tuple_to_text(self, color: tuple[float, float, float]) -> str:
        values = [max(0, min(255, round(channel * 255))) for channel in color]
        return f"{values[0]}, {values[1]}, {values[2]}"

    def _color_tuple_to_hex(self, color: tuple[float, float, float]) -> str:
        values = [max(0, min(255, round(channel * 255))) for channel in color]
        return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"

    def _resolve_fontname(self) -> str:
        """Bildet die gewählte Schriftart + Fett/Kursiv auf einen
        PyMuPDF-Base-14-Fontcode ab (ohne externe Schriftdatei einbettbar)."""
        return base14_fontcode(
            self.annotation_font_family_combo.currentText(),
            self.annotation_bold_check.isChecked(),
            self.annotation_italic_check.isChecked(),
        )

    def _set_annotation_defaults(self, kind: str) -> None:
        defaults = {
            "text": {"hint": "Textfeld aufziehen", "text": "", "width": 35.0, "height": 12.0, "font": 12, "line": 2.0, "color": (220 / 255.0, 20 / 255.0, 60 / 255.0)},
            "rect": {"hint": "Rechteck ziehen", "width": 40.0, "height": 20.0, "font": 12, "line": 2.0, "color": (0.0, 120 / 255.0, 215 / 255.0)},
            "ellipse": {"hint": "Ellipse ziehen", "width": 40.0, "height": 20.0, "font": 12, "line": 2.0, "color": (0.0, 120 / 255.0, 215 / 255.0)},
            "star": {"hint": "Stern aufziehen", "width": 25.0, "height": 25.0, "font": 12, "line": 2.0, "color": (1.0, 180 / 255.0, 0.0)},
            "link": {"hint": "Linkbereich ziehen", "width": 40.0, "height": 10.0, "font": 12, "line": 1.0, "color": (0.0, 90 / 255.0, 200 / 255.0)},
            "highlight": {"hint": "Marker ziehen", "width": 45.0, "height": 8.0, "font": 12, "line": 2.0, "color": (1.0, 235 / 255.0, 59 / 255.0)},
            "strikeout": {"hint": "Über den Text ziehen", "width": 45.0, "height": 8.0, "font": 12, "line": 2.0, "color": (220 / 255.0, 20 / 255.0, 60 / 255.0)},
            "underline": {"hint": "Über den Text ziehen", "width": 45.0, "height": 8.0, "font": 12, "line": 2.0, "color": (0.0, 120 / 255.0, 215 / 255.0)},
            "line": {"hint": "Linie ziehen", "width": 35.0, "height": 12.0, "font": 12, "line": 2.0, "color": (0.0, 120 / 255.0, 215 / 255.0)},
            "arrow": {"hint": "Pfeil ziehen", "width": 35.0, "height": 12.0, "font": 12, "line": 2.5, "color": (220 / 255.0, 20 / 255.0, 60 / 255.0)},
            "image": {"hint": "Bild platzieren", "width": 35.0, "height": 20.0, "font": 12, "line": 2.0, "color": (0.0, 120 / 255.0, 215 / 255.0)},
            "redact": {"hint": "Bereich irreversibel schwärzen", "width": 35.0, "height": 12.0, "font": 12, "line": 2.0, "color": (0.0, 0.0, 0.0)},
            "note": {"hint": "Notiz platzieren", "text": "", "width": 30.0, "height": 14.0, "font": 12, "line": 2.0, "color": (255 / 255.0, 235 / 255.0, 59 / 255.0)},
            "freehand": {"hint": "Freihand zeichnen", "width": 35.0, "height": 12.0, "font": 12, "line": 2.5, "color": (220 / 255.0, 20 / 255.0, 60 / 255.0)},
            "text-replace": {"hint": "Bereich wählen und Text ersetzen", "text": "", "width": 35.0, "height": 12.0, "font": 12, "line": 2.0, "color": (17 / 255.0, 24 / 255.0, 39 / 255.0)},
            "text-edit": {"hint": "Auf Textabschnitt klicken", "width": 35.0, "height": 12.0, "font": 12, "line": 1.0, "color": (0.0, 0.0, 0.0)},
        }
        cfg = defaults.get(kind, defaults["text"])
        self.annotation_form_hint.setText(cfg["hint"])
        self.annotation_width_spin.setValue(float(cfg["width"]))
        self.annotation_height_spin.setValue(float(cfg["height"]))
        self.annotation_font_size_spin.setValue(int(cfg["font"]))
        self.annotation_line_width_spin.setValue(float(cfg["line"]))
        if kind in {"text", "note", "text-replace"}:
            self.annotation_text_input.clear()
        self._set_annotation_color(cfg["color"])
        self._update_annotation_form_visibility(kind)

    def _update_annotation_form_visibility(self, kind: str) -> None:
        is_text = kind in {"note", "text-replace"}
        uses_size = kind in {"rect", "ellipse", "star", "link", "highlight", "strikeout", "underline"}
        uses_line = kind in {"rect", "ellipse", "star", "line", "arrow", "freehand"}
        uses_color = kind in {"text", "rect", "ellipse", "star", "link", "highlight", "strikeout", "underline", "line", "arrow", "freehand", "text-replace"}
        uses_image = kind == "image"
        uses_font = kind in {"text", "text-replace"}
        self.annotation_text_label.setVisible(is_text)
        self.annotation_text_input.setVisible(is_text)
        self.annotation_font_family_label.setVisible(uses_font)
        self.annotation_font_family_combo.setVisible(uses_font)
        self.annotation_bold_check.setVisible(uses_font)
        self.annotation_italic_check.setVisible(uses_font)
        self.annotation_image_label.setVisible(uses_image)
        self.annotation_image_path_label.setVisible(uses_image)
        self.btn_annotation_pick_image.setVisible(uses_image)
        self.annotation_width_label.setVisible(uses_size)
        self.annotation_width_spin.setVisible(uses_size)
        self.annotation_height_label.setVisible(uses_size)
        self.annotation_height_spin.setVisible(uses_size)
        self.annotation_font_label.setVisible(is_text or uses_font)
        self.annotation_font_size_spin.setVisible(is_text or uses_font)
        self.annotation_line_width_label.setVisible(uses_line)
        self.annotation_line_width_spin.setVisible(uses_line)
        self.annotation_color_label.setVisible(uses_color)
        self.annotation_color_input.setVisible(uses_color)
        for button, _ in self.annotation_color_buttons:
            button.setVisible(uses_color)

    def _set_annotation_color(self, color: str | tuple[float, float, float]) -> None:
        if isinstance(color, str):
            parsed = self._parse_rgb_color(color, (0.0, 120 / 255.0, 215 / 255.0))
        else:
            parsed = color
        self.annotation_color_input.setText(self._color_tuple_to_text(parsed))
        self._refresh_annotation_color_buttons()

    def _refresh_annotation_color_buttons(self) -> None:
        current = self._color_tuple_to_hex(self._parse_rgb_color(self.annotation_color_input.text(), (0.0, 120 / 255.0, 215 / 255.0)))
        for button, color_hex in self.annotation_color_buttons:
            is_active = current.lower() == color_hex.lower()
            border = "#1f2937" if color_hex.lower() == "#ffffff" else ("#1e2432" if is_active else "#d6dbea")
            width = 3 if is_active else 1
            button.setStyleSheet(
                f"background:{color_hex}; border:{width}px solid {border}; border-radius:14px;"
            )

    def _sync_annotation_tool_buttons(self) -> None:
        for kind, button in self.annotation_tool_buttons.items():
            button.setProperty("toolActive", "true" if kind == self.annotation_tool_kind else "false")
            button.style().unpolish(button)
            button.style().polish(button)

    def select_annotation_tool(self, kind: str, activate: bool = False) -> None:
        if self._require_current_pdf_page() is None:
            return
        self.annotation_tool_kind = kind
        self._set_annotation_defaults(kind)
        self._sync_annotation_tool_buttons()
        if activate:
            self.activate_selected_annotation_tool()

    def activate_selected_annotation_tool(self) -> None:
        if self._require_current_pdf_page() is None:
            return
        kind = self.annotation_tool_kind
        default_color = {
            "text": (220 / 255.0, 20 / 255.0, 60 / 255.0),
            "rect": (0.0, 120 / 255.0, 215 / 255.0),
            "highlight": (1.0, 235 / 255.0, 59 / 255.0),
            "line": (0.0, 120 / 255.0, 215 / 255.0),
            "arrow": (220 / 255.0, 20 / 255.0, 60 / 255.0),
        }.get(kind, (0.0, 120 / 255.0, 215 / 255.0))
        color = self._parse_rgb_color(self.annotation_color_input.text(), default_color)

        if kind == "text":
            self._begin_pending_annotation(
                {
                    "kind": "text",
                    "text": "",
                    "font_size": self.annotation_font_size_spin.value(),
                    "fontname": self._resolve_fontname(),
                    "color": color,
                },
                "Textmodus aktiv – ziehe in der Vorschau ein Textfeld auf.",
            )
            return

        if kind == "note":
            text = self.annotation_text_input.toPlainText().strip()
            if not text:
                QMessageBox.information(self, "Hinweis", "Bitte zuerst den Notiztext in der Sidebar eingeben.")
                self.annotation_text_input.setFocus()
                return
            self._begin_pending_annotation(
                {
                    "kind": "note",
                    "text": text,
                },
                "Notizmodus aktiv – klicke in der Vorschau auf die gewünschte Position.",
            )
            return

        if kind == "text-replace":
            text = self.annotation_text_input.toPlainText().strip()
            if not text:
                QMessageBox.information(self, "Hinweis", "Bitte zuerst den neuen Text in der Sidebar eingeben.")
                self.annotation_text_input.setFocus()
                return
            self._begin_pending_annotation(
                {
                    "kind": "text-replace",
                    "text": text,
                    "font_size": self.annotation_font_size_spin.value(),
                    "fontname": self._resolve_fontname(),
                    "color": color,
                },
                "Text-Ersetzen aktiv – ziehe den Bereich auf, der neu gesetzt werden soll.",
            )
            return

        if kind in {"rect", "ellipse", "star"}:
            hints = {
                "rect": "Rechteckmodus aktiv – in der Vorschau klicken und ziehen.",
                "ellipse": "Ellipsenmodus aktiv – in der Vorschau klicken und ziehen.",
                "star": "Sternmodus aktiv – in der Vorschau klicken und ziehen.",
            }
            self._begin_pending_annotation(
                {
                    "kind": kind,
                    "width_pct": self.annotation_width_spin.value(),
                    "height_pct": self.annotation_height_spin.value(),
                    "color": color,
                    "line_width": self.annotation_line_width_spin.value(),
                },
                hints[kind],
            )
            return

        if kind == "text-edit":
            self._begin_pending_annotation(
                {"kind": "text-edit"},
                "Text-Bearbeiten aktiv – klicke auf einen vorhandenen Textabschnitt. Mit 'Modus verlassen' beenden.",
            )
            return

        if kind == "link":
            url, ok = QInputDialog.getText(
                self,
                "Hyperlink einfügen",
                "Ziel-URL (z. B. https://example.com):",
                text="https://",
            )
            if not ok or not url.strip() or url.strip() == "https://":
                return
            self._begin_pending_annotation(
                {
                    "kind": "link",
                    "uri": url.strip(),
                    "color": color,
                    "line_width": self.annotation_line_width_spin.value(),
                },
                "Linkmodus aktiv – ziehe den klickbaren Bereich auf.",
            )
            return

        if kind in {"highlight", "strikeout", "underline"}:
            hints = {
                "highlight": "Markierungsmodus aktiv – in der Vorschau klicken und ziehen.",
                "strikeout": "Durchstreichen aktiv – über den zu streichenden Text ziehen.",
                "underline": "Unterstreichen aktiv – über den zu unterstreichenden Text ziehen.",
            }
            self._begin_pending_annotation(
                {
                    "kind": kind,
                    "width_pct": self.annotation_width_spin.value(),
                    "height_pct": self.annotation_height_spin.value(),
                    "color": color,
                },
                hints[kind],
            )
            return

        if kind == "image":
            if self.annotation_image_path is None:
                self.pick_annotation_image()
            if self.annotation_image_path is None:
                return
            self._begin_pending_annotation(
                {
                    "kind": "image",
                    "image_path": str(self.annotation_image_path),
                },
                "Bildmodus aktiv – ziehe in der Vorschau die gewünschte Größe auf.",
            )
            return

        if kind == "redact":
            self._begin_pending_annotation(
                {"kind": "redact"},
                "Schwärzungsmodus aktiv – ziehe den Bereich auf. Danach kannst du ihn noch verschieben oder löschen.",
            )
            return

        if kind == "freehand":
            self._begin_pending_annotation(
                {
                    "kind": "freehand",
                    "color": color,
                    "line_width": self.annotation_line_width_spin.value(),
                },
                "Freihandmodus aktiv – mit gedrückter Maus zeichnen.",
            )
            return

        self._begin_pending_annotation(
            {
                "kind": kind,
                "color": color,
                "line_width": self.annotation_line_width_spin.value(),
            },
            "Pfeilmodus aktiv – in der Vorschau klicken und ziehen." if kind == "arrow" else "Linienmodus aktiv – in der Vorschau klicken und ziehen.",
        )

    def start_visual_crop(self) -> None:
        if self._require_current_pdf_page() is None:
            return
        self.selected_annotation_xref = None
        self.selected_widget_xref = None
        self._update_selected_annotation_ui()
        self._begin_pending_annotation(
            {"kind": "crop"},
            "Crop-Modus aktiv – ziehe in der Vorschau den gewünschten Seitenausschnitt auf.",
        )
        self.render_current_page()

    def _set_annotation_hint(self, text: str, active: bool = False) -> None:
        self.annotation_hint.setText(text)
        self.annotation_hint.setProperty("active", active)
        self.annotation_hint.style().unpolish(self.annotation_hint)
        self.annotation_hint.style().polish(self.annotation_hint)

    def _update_thumbnail_meta(self) -> None:
        if not self.doc:
            self.thumb_meta_label.setText("Noch kein PDF geladen")
            return
        total = len(self.doc)
        current = self.current_page + 1 if 0 <= self.current_page < total else 0
        selected = len(self.thumb_list.selectedItems()) if self.thumb_list.count() else 0
        if selected > 1:
            self.thumb_meta_label.setText(f"{total} Seiten · Auswahl: {selected}")
        else:
            self.thumb_meta_label.setText(f"{total} Seiten · aktuell {current}")

    def _update_selected_annotation_ui(self) -> None:
        def _no_selection() -> None:
            self.annotation_selection_label.setText("Keine Annotation ausgewählt")
            self.btn_delete_annotation.setEnabled(False)
            self.btn_edit_annotation_style.setEnabled(False)
            self.btn_edit_annotation_comment.setEnabled(False)
            self.btn_reply_annotation.setEnabled(False)
            self.inline_edit_card.setVisible(False)

        # ── Formularfeld ausgewählt? ──────────────────────────────────────
        if self.selected_widget_xref is not None and self.doc and 0 <= self.current_page < len(self.doc):
            for w in self.doc[self.current_page].widgets() or []:
                if w.xref == self.selected_widget_xref:
                    label = w.field_label or w.field_name or "Formularfeld"
                    ftype = w.field_type_string or ""
                    self.annotation_selection_label.setText(
                        f"Ausgewählt: Formularfeld\n{label}" + (f" ({ftype})" if ftype else "")
                    )
                    self.btn_delete_annotation.setEnabled(False)
                    self.btn_edit_annotation_style.setEnabled(False)
                    self.btn_edit_annotation_comment.setEnabled(False)
                    self.btn_reply_annotation.setEnabled(False)
                    self.inline_text_edit.setPlainText(str(w.field_value or ""))
                    self.inline_edit_card.setVisible(True)
                    return
            self.selected_widget_xref = None

        # ── Annotation ausgewählt? ────────────────────────────────────────
        if self.selected_annotation_xref is None or not self.doc or not (0 <= self.current_page < len(self.doc)):
            _no_selection()
            return
        info = None
        inline_text: str | None = None
        for annot in self.doc[self.current_page].annots() or []:
            if annot.xref == self.selected_annotation_xref:
                subtype = annot.type[1] if isinstance(annot.type, tuple) and len(annot.type) > 1 else "Annotation"
                rect = annot.rect
                info = f"Ausgewählt: {subtype}\nPos: {rect.x0:.0f}, {rect.y0:.0f} · {rect.width:.0f}×{rect.height:.0f} pt\nTipp: mit der Maus ziehen zum Verschieben"
                if subtype in {"FreeText", "Text"}:
                    try:
                        ainfo = annot.info or {}
                        inline_text = str(ainfo.get("content") or "")
                    except Exception:
                        pass
                break
        if info is None:
            self.selected_annotation_xref = None
            self.selected_widget_xref = None
            _no_selection()
        else:
            self.annotation_selection_label.setText(info)
            self.btn_delete_annotation.setEnabled(True)
            self.btn_edit_annotation_style.setEnabled(True)
            self.btn_edit_annotation_comment.setEnabled(True)
            self.btn_reply_annotation.setEnabled(True)
            if inline_text is not None:
                self.inline_text_edit.setPlainText(inline_text)
                self.inline_edit_card.setVisible(True)
            else:
                self.inline_edit_card.setVisible(False)

    RESOLVE_TAG = "erledigt"

    _ANNOT_TYPE_LABELS = {
        "FreeText": "Text",
        "Text": "Notiz",
        "Highlight": "Markierung",
        "StrikeOut": "Durchstreichung",
        "Underline": "Unterstreichung",
        "Square": "Rechteck",
        "Line": "Linie/Pfeil",
        "Ink": "Freihand",
        "Redact": "Schwärzung",
    }

    def _is_annot_resolved(self, annot) -> bool:
        try:
            return str((annot.info or {}).get("subject", "")).strip().lower() == self.RESOLVE_TAG
        except Exception:
            return False

    def _set_annot_resolved(self, annot, resolved: bool) -> None:
        subject = self.RESOLVE_TAG if resolved else ""
        try:
            annot.set_info(subject=subject)
        except Exception:
            try:
                annot.set_info(info={"subject": subject})
            except Exception:
                pass
        try:
            annot.update()
        except Exception:
            pass

    def _refresh_comment_list(self) -> None:
        if not hasattr(self, "comment_list"):
            return
        self.comment_list.clear()
        if not self.doc:
            return
        for pidx in range(len(self.doc)):
            try:
                annots = list(self.doc[pidx].annots() or [])
            except Exception:
                continue
            for annot in annots:
                subtype = annot.type[1] if isinstance(annot.type, tuple) and len(annot.type) > 1 else "Annotation"
                label_type = self._ANNOT_TYPE_LABELS.get(subtype, subtype)
                try:
                    content = str((annot.info or {}).get("content", "")).strip()
                except Exception:
                    content = ""
                snippet = f": {content[:40]}" if content else ""
                resolved = self._is_annot_resolved(annot)
                prefix = "✓ " if resolved else ""
                item = QListWidgetItem(f"{prefix}S{pidx + 1} · {label_type}{snippet}")
                item.setData(Qt.ItemDataRole.UserRole, (pidx, getattr(annot, "xref", None)))
                if resolved:
                    font = item.font()
                    font.setStrikeOut(True)
                    item.setFont(font)
                    item.setForeground(QColor("#9aa3b2"))
                self.comment_list.addItem(item)

    def _refresh_outline(self) -> None:
        if not hasattr(self, "outline_list"):
            return
        self.outline_list.clear()
        toc = []
        if self.doc:
            try:
                toc = self.doc.get_toc() or []
            except Exception:
                toc = []
        has_entries = bool(toc)
        self.outline_empty_label.setVisible(not has_entries)
        self.outline_list.setVisible(has_entries)
        for entry in toc:
            try:
                level, title, page = entry[0], entry[1], entry[2]
            except Exception:
                continue
            indent = "    " * max(0, int(level) - 1)
            item = QListWidgetItem(f"{indent}{title}")
            item.setData(Qt.ItemDataRole.UserRole, int(page) - 1)
            self.outline_list.addItem(item)

    def _on_outline_item_clicked(self, item: QListWidgetItem) -> None:
        page_idx = item.data(Qt.ItemDataRole.UserRole)
        if page_idx is None or not self.doc:
            return
        page_idx = max(0, min(int(page_idx), len(self.doc) - 1))
        self.current_page = page_idx
        self.render_current_page()
        self.statusBar().showMessage(f"Lesezeichen: Seite {page_idx + 1}")

    def _on_comment_item_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        pidx, xref = data
        if not self.doc or not (0 <= pidx < len(self.doc)):
            return
        self.current_page = pidx
        self.selected_widget_xref = None
        self.selected_annotation_xref = xref
        self.render_current_page()
        self._update_selected_annotation_ui()
        self.statusBar().showMessage(f"Kommentar auf Seite {pidx + 1} ausgewählt")

    def toggle_selected_comment_resolved(self) -> None:
        item = self.comment_list.currentItem() if hasattr(self, "comment_list") else None
        if item is None:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst einen Kommentar in der Liste auswählen.")
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        pidx, xref = data
        if not self.doc or not (0 <= pidx < len(self.doc)) or xref is None:
            return
        target = None
        for annot in self.doc[pidx].annots() or []:
            if getattr(annot, "xref", None) == xref:
                target = annot
                break
        if target is None:
            self._refresh_comment_list()
            return
        self._push_undo_state()
        new_state = not self._is_annot_resolved(target)
        self._set_annot_resolved(target, new_state)
        self._set_dirty(True)
        self._refresh_comment_list()
        self.render_current_page()
        self.statusBar().showMessage("Kommentar als erledigt markiert" if new_state else "Kommentar wieder geöffnet")

    def _view_to_page_point(self, x: float, y: float) -> fitz.Point | None:
        if not self.doc or not (0 <= self.current_page < len(self.doc)):
            return None
        pixmap = self.preview.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        page = self.doc[self.current_page]
        rot = self.page_rotations.get(self.current_page, 0) % 360
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        px = max(0.0, min(float(pixmap.width()), x)) / max(self.zoom_factor, 1e-6)
        py = max(0.0, min(float(pixmap.height()), y)) / max(self.zoom_factor, 1e-6)
        if rot == 0:
            return fitz.Point(px, py)
        if rot == 90:
            return fitz.Point(py, page_height - px)
        if rot == 180:
            return fitz.Point(page_width - px, page_height - py)
        if rot == 270:
            return fitz.Point(page_width - py, px)
        return fitz.Point(px, py)

    def _find_annotation_at_page_point(self, point: fitz.Point) -> int | None:
        if not self.doc or not (0 <= self.current_page < len(self.doc)):
            return None
        page = self.doc[self.current_page]
        selected = None
        for annot in page.annots() or []:
            rect = annot.rect
            if rect.contains(point):
                selected = annot.xref
        return selected

    def _find_widget_at_page_point(self, point: fitz.Point) -> "fitz.Widget | None":
        if not self.doc or not (0 <= self.current_page < len(self.doc)):
            return None
        for w in self.doc[self.current_page].widgets() or []:
            if w.rect.contains(point):
                return w
        return None

    def _select_annotation_at_page_point(self, point: fitz.Point) -> bool:
        selected = self._find_annotation_at_page_point(point)
        self.selected_annotation_xref = selected
        if selected is not None:
            self.selected_widget_xref = None
        self._update_selected_annotation_ui()
        self.render_current_page()
        return selected is not None

    def _move_selected_annotation(self, dx: float, dy: float) -> None:
        if self.selected_annotation_xref is None or not self.doc or not (0 <= self.current_page < len(self.doc)):
            return
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            return
        page = self.doc[self.current_page]
        annot = page.load_annot(self.selected_annotation_xref)
        if annot is None:
            return
        rect = fitz.Rect(annot.rect)
        page_rect = fitz.Rect(page.rect)
        new_rect = fitz.Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)
        if new_rect.x0 < page_rect.x0:
            new_rect.x1 += page_rect.x0 - new_rect.x0
            new_rect.x0 = page_rect.x0
        if new_rect.y0 < page_rect.y0:
            new_rect.y1 += page_rect.y0 - new_rect.y0
            new_rect.y0 = page_rect.y0
        if new_rect.x1 > page_rect.x1:
            new_rect.x0 -= new_rect.x1 - page_rect.x1
            new_rect.x1 = page_rect.x1
        if new_rect.y1 > page_rect.y1:
            new_rect.y0 -= new_rect.y1 - page_rect.y1
            new_rect.y1 = page_rect.y1
        try:
            self._push_undo_state()
            annot.set_rect(new_rect)
            annot.update()
        except Exception as e:
            QMessageBox.warning(self, "Verschieben nicht möglich", f"Diese Annotation konnte nicht verschoben werden:\n{e}")
            return
        self._set_dirty(True)
        self._update_selected_annotation_ui()
        self._refresh_thumbnails()
        self.render_current_page()
        self.statusBar().showMessage("Annotation verschoben")

    def _set_line_end_style(self, annot, arrow: bool = False) -> None:
        if not arrow:
            return
        for start_style, end_style in ((0, 4), (0, 5), (0, 2)):
            try:
                annot.set_line_ends(start_style, end_style)
                return
            except Exception:
                continue

    def _normalize_preview_percent(self, x: float, y: float) -> tuple[float, float] | None:
        pixmap = self.preview.pixmap()
        if pixmap is None or pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return None
        px = max(0.0, min(100.0, (x / pixmap.width()) * 100.0))
        py = max(0.0, min(100.0, (y / pixmap.height()) * 100.0))
        return px, py

    def _begin_pending_annotation(self, config: dict, hint: str) -> None:
        self.pending_annotation = config
        self.preview_drag_points = []
        self.preview.setCursor(Qt.CursorShape.CrossCursor)
        self._set_annotation_hint(hint, active=True)
        self.statusBar().showMessage(hint)

    def _clear_pending_annotation(self) -> None:
        self.pending_annotation = None
        self.preview_drag_start = None
        self.preview_drag_current = None
        self.preview_drag_points = []
        self.preview.setCursor(Qt.CursorShape.ArrowCursor)
        self._set_annotation_hint("Bereit", active=False)
        # Laufende direkte Textblock-Bearbeitung verwerfen.
        if self._editing_text_block is not None:
            self._editing_text_block = None
            self.inline_edit_card.setVisible(False)

    def _handle_preview_drag_start(self, x: float, y: float) -> None:
        if not self.pending_annotation:
            point = self._view_to_page_point(x, y)
            annot_xref = self._find_annotation_at_page_point(point) if point is not None else None
            self.annotation_drag_state = None
            if annot_xref is not None and point is not None:
                self.selected_annotation_xref = annot_xref
                self._update_selected_annotation_ui()
                self.annotation_drag_state = {
                    "xref": annot_xref,
                    "start_point": point,
                }
                self.preview.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        norm = self._normalize_preview_percent(x, y)
        if norm is None:
            return
        self.preview_drag_start = norm
        self.preview_drag_current = norm
        self.preview_drag_points = [norm]

    def _handle_preview_drag_move(self, x: float, y: float) -> None:
        if not self.pending_annotation:
            if self.annotation_drag_state is not None:
                self.statusBar().showMessage("Annotation verschieben – Maus loslassen zum Ablegen.")
            return
        if self.preview_drag_start is None:
            return
        norm = self._normalize_preview_percent(x, y)
        if norm is None:
            return
        self.preview_drag_current = norm
        self.preview_drag_points.append(norm)
        kind = self.pending_annotation.get("kind")
        if kind == "crop":
            self._set_annotation_hint("Loslassen zum Zuschneiden – nur der markierte Bereich bleibt sichtbar.", active=True)
            self.render_current_page()
        elif kind == "image":
            self._set_annotation_hint("Loslassen zum Einfügen – Bild wird in den aufgezogenen Bereich gesetzt.", active=True)
            self.render_current_page()
        elif kind == "redact":
            self._set_annotation_hint("Loslassen zum Platzieren – final entfernt wird der Inhalt erst nach 'Schwärzungen final anwenden'.", active=True)
            self.render_current_page()
        elif kind == "freehand":
            self._set_annotation_hint("Loslassen zum Abschließen – du zeichnest direkt auf die Seite.", active=True)
            self.render_current_page()
        elif kind == "text-replace":
            self._set_annotation_hint("Loslassen zum Platzieren – das Ersatz-Textfeld wird über den Bereich gelegt.", active=True)
            self.render_current_page()
        elif kind in {"text", "rect", "ellipse", "star", "link", "highlight", "strikeout", "underline"}:
            hint = "Loslassen zum Platzieren – danach gibst du den Text ein." if kind == "text" else "Loslassen zum Platzieren – Ziehen definiert Größe und Position."
            self._set_annotation_hint(hint, active=True)
            self.render_current_page()
        elif kind in {"line", "arrow"}:
            self.render_current_page()

    def _detected_fontcode(self, font_name: str, flags: int) -> str:
        """Bildet eine erkannte Schrift (Name + Span-Flags) auf einen
        PyMuPDF-Base-14-Fontcode ab, der ohne Schriftdatei einbettbar ist."""
        return detected_fontcode(font_name, flags)

    def _estimate_background_color(self, page, rect) -> tuple[float, float, float]:
        """Schätzt die Hintergrundfarbe eines Bereichs, indem die Eckpixel eines
        gerenderten Ausschnitts abgetastet werden (hellste Ecke = Hintergrund)."""
        try:
            pix = page.get_pixmap(clip=fitz.Rect(rect), alpha=False)
            if pix.width and pix.height:
                corners = [(0, 0), (pix.width - 1, 0), (0, pix.height - 1), (pix.width - 1, pix.height - 1)]
                cols = [pix.pixel(x, y) for x, y in corners]
                best = max(cols, key=lambda c: sum(c))
                return (best[0] / 255.0, best[1] / 255.0, best[2] / 255.0)
        except Exception:
            pass
        return (1.0, 1.0, 1.0)

    def _remove_content_in_rect(self, page, rect) -> tuple[float, float, float] | None:
        """Entfernt Text/Inhalt im Rechteck **endgültig** per Redaction (statt ihn
        nur zu übermalen) und füllt mit der erkannten Hintergrundfarbe.

        Gibt die Füllfarbe zurück oder ``None``, wenn der Nutzer abbricht. Da
        ``apply_redactions`` alle offenen Schwärzungen der Seite finalisiert,
        wird bei vorhandenen Schwärzungen zuvor rückgefragt.
        """
        bg = self._estimate_background_color(page, rect)
        existing = 0
        try:
            existing = sum(1 for _ in (page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []))
        except Exception:
            existing = 0
        if existing:
            answer = QMessageBox.question(
                self,
                "Schwärzungen vorhanden",
                "Auf dieser Seite sind noch nicht angewandte Schwärzungen vorhanden. "
                "Beim endgültigen Entfernen des Texts werden diese ebenfalls final "
                "angewandt. Fortfahren?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
        page.add_redact_annot(fitz.Rect(rect), fill=bg)
        page.apply_redactions()
        return bg

    def _find_text_block_at(self, page, point: fitz.Point) -> dict | None:
        """Findet den kleinsten Textblock, der den Klickpunkt enthält, und
        rekonstruiert Text, dominante Schriftgröße, Farbe und Font."""
        try:
            data = page.get_text("dict")
        except Exception:
            return None
        best: dict | None = None
        best_area: float | None = None
        for block in data.get("blocks", []):
            if block.get("type", 1) != 0:  # nur Textblöcke
                continue
            bbox = block.get("bbox")
            if not bbox:
                continue
            rect = fitz.Rect(bbox)
            if not rect.contains(point):
                continue
            area = rect.width * rect.height
            if best_area is not None and area >= best_area:
                continue
            lines_text: list[str] = []
            sizes: list[float] = []
            colors: list[int] = []
            fonts: list[str] = []
            flags_list: list[int] = []
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                lines_text.append("".join(s.get("text", "") for s in spans))
                for s in spans:
                    sizes.append(s.get("size", 11))
                    colors.append(s.get("color", 0))
                    fonts.append(s.get("font", ""))
                    flags_list.append(s.get("flags", 0))
            text = "\n".join(lines_text).strip("\n")
            if not text.strip():
                continue
            size = float(sizes[0]) if sizes else 11.0
            color_int = colors[0] if colors else 0
            color = (
                ((color_int >> 16) & 255) / 255.0,
                ((color_int >> 8) & 255) / 255.0,
                (color_int & 255) / 255.0,
            )
            fontcode = self._detected_fontcode(fonts[0] if fonts else "", flags_list[0] if flags_list else 0)
            best = {"bbox": bbox, "text": text, "size": size, "color": color, "fontcode": fontcode}
            best_area = area
        return best

    def _edit_text_block_at_view(self, x: float, y: float) -> None:
        if not self.doc or not (0 <= self.current_page < len(self.doc)):
            return
        page = self.doc[self.current_page]
        point = self._view_to_page_point(x, y)
        if point is None:
            return
        block = self._find_text_block_at(page, point)
        if block is None:
            self.statusBar().showMessage("Kein bearbeitbarer Textabschnitt an dieser Stelle gefunden.")
            return
        # Direkt im Sidebar-Feld bearbeiten (löschen + neu schreiben), statt
        # einen Modal-Dialog zu öffnen.
        block["page"] = self.current_page
        self._editing_text_block = block
        self.selected_annotation_xref = None
        self.selected_widget_xref = None
        self.inline_text_edit.setPlainText(block["text"])
        self.inline_edit_card.setVisible(True)
        self.inline_text_edit.setFocus()
        self.inline_text_edit.selectAll()
        self.statusBar().showMessage(
            "Textabschnitt geladen – im Feld unten ändern (löschen/neu schreiben) und 'Änderung speichern'."
        )

    def _apply_text_block_edit(self, new_text: str) -> None:
        block = self._editing_text_block
        if not block or not self.doc:
            return
        pidx = block.get("page", self.current_page)
        if not (0 <= pidx < len(self.doc)):
            self._editing_text_block = None
            return
        page = self.doc[pidx]
        try:
            self._push_undo_state()
            rect = fitz.Rect(block["bbox"])
            pad = 1.0
            cover = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
            # Originaltext endgültig entfernen (Redaction) statt nur zu übermalen.
            if self._remove_content_in_rect(page, cover) is None:
                return
            rc = -1.0
            attempt_size = block["size"]
            while attempt_size >= 5.0:
                rc = page.insert_textbox(
                    rect,
                    new_text,
                    fontsize=attempt_size,
                    fontname=block["fontcode"],
                    color=block["color"],
                    align=0,
                )
                if rc >= 0:
                    break
                attempt_size -= 0.5
            if rc < 0:
                raise ValueError(
                    "Der bearbeitete Text passt nicht in den Block. "
                    "Bitte den Text kürzen."
                )
            self._set_dirty(True)
            self._refresh_thumbnails()
            self.render_current_page()
            shrunk = " (Schrift verkleinert)" if attempt_size < block["size"] else ""
            self.statusBar().showMessage(f"Textblock bearbeitet{shrunk}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Text konnte nicht bearbeitet werden:\n{e}")
        finally:
            self._editing_text_block = None
            self.inline_edit_card.setVisible(False)

    def _handle_preview_click(self, x: float, y: float) -> None:
        if not self.doc:
            return
        if not self.pending_annotation:
            point = self._view_to_page_point(x, y)
            if point is not None:
                if self._select_annotation_at_page_point(point):
                    self.statusBar().showMessage("Annotation ausgewählt")
                else:
                    widget = self._find_widget_at_page_point(point)
                    if widget is not None:
                        self.selected_annotation_xref = None
                        self.selected_widget_xref = None
                        self.selected_widget_xref = widget.xref
                        self._update_selected_annotation_ui()
                        self.render_current_page()
                        name = widget.field_label or widget.field_name or "Feld"
                        self.statusBar().showMessage(f"Formularfeld ausgewählt: {name}")
                    else:
                        self.selected_annotation_xref = None
                        self.selected_widget_xref = None
                        self.selected_widget_xref = None
                        self._update_selected_annotation_ui()
                        self.render_current_page()
            return
        norm = self._normalize_preview_percent(x, y)
        if norm is None:
            return

        anchor_x, anchor_y = norm
        kind = self.pending_annotation.get("kind")
        if kind == "text-edit":
            self._edit_text_block_at_view(x, y)
            return
        if kind in {"text", "rect", "ellipse", "star", "link", "highlight", "strikeout", "underline", "line", "arrow", "crop", "image", "redact", "freehand", "text-replace"}:
            self._set_annotation_hint("Für dieses Werkzeug bitte mit der Maus ziehen.", active=True)
            self.statusBar().showMessage("Für dieses Werkzeug bitte klicken und ziehen.")
            return
        try:
            self._push_undo_state()
            page = self.doc[self.current_page]
            if kind == "text":
                rect_spec = self._anchor_rect_percent(anchor_x, anchor_y, self.pending_annotation["width_pct"], self.pending_annotation["height_pct"])
                rect = self._rect_from_percent(page, rect_spec)
                rc = page.insert_textbox(
                    rect,
                    self.pending_annotation["text"],
                    fontsize=self.pending_annotation["font_size"],
                    color=self.pending_annotation["color"],
                )
                if rc < 0:
                    raise ValueError("Der Text passt an dieser Position nicht in das Feld.")
                message = "Text platziert"
            elif kind == "note":
                point = self._view_to_page_point(x, y)
                if point is None:
                    raise ValueError("Notiz konnte nicht platziert werden.")
                annot = page.add_text_annot(point, self.pending_annotation["text"])
                annot.update()
                self.selected_annotation_xref = getattr(annot, "xref", None)
                message = "Notiz platziert"
            else:
                return

            if kind == "text":
                self.selected_annotation_xref = None
                self.selected_widget_xref = None
            self._update_selected_annotation_ui()
            self._set_dirty(True)
            self._refresh_thumbnails()
            self._refresh_comment_list()
            self.render_current_page()
            self.statusBar().showMessage(message)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Platzierung fehlgeschlagen:\n{e}")
        finally:
            self._clear_pending_annotation()

    def _handle_preview_drag_finish(self, x: float, y: float) -> None:
        if self.annotation_drag_state is not None and self.doc:
            try:
                end_point = self._view_to_page_point(x, y)
                start_point = self.annotation_drag_state.get("start_point")
                if end_point is not None and start_point is not None:
                    dx = float(end_point.x - start_point.x)
                    dy = float(end_point.y - start_point.y)
                    self._move_selected_annotation(dx, dy)
            finally:
                self.annotation_drag_state = None
                self.preview.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if not self.pending_annotation or not self.doc:
            return
        if self.preview_drag_start is None:
            return
        end = self._normalize_preview_percent(x, y)
        if end is None:
            self._clear_pending_annotation()
            return
        start_x, start_y = self.preview_drag_start
        end_x, end_y = end
        kind = self.pending_annotation.get("kind")
        rect_spec = self._rect_percent_from_drag(start_x, start_y, end_x, end_y)
        if kind != "freehand" and rect_spec is None:
            self._set_annotation_hint("Auswahl zu klein – bitte etwas größer ziehen.", active=True)
            self.statusBar().showMessage("Auswahl zu klein für Annotation.")
            self._clear_pending_annotation()
            return
        try:
            self._push_undo_state()
            page = self.doc[self.current_page]
            if kind == "freehand":
                if len(self.preview_drag_points) < 2:
                    raise ValueError("Freihandspur ist zu kurz.")
                stroke: list[fitz.Point] = []
                for px, py in self.preview_drag_points:
                    point = self._view_to_page_point(
                        (px / 100.0) * self.preview.pixmap().width(),
                        (py / 100.0) * self.preview.pixmap().height(),
                    )
                    if point is not None:
                        stroke.append(point)
                if len(stroke) < 2:
                    raise ValueError("Freihandspur konnte nicht platziert werden.")
                annot = page.add_ink_annot([stroke])
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.set_border(width=self.pending_annotation["line_width"])
                annot.update()
                message = "Freihand-Markierung platziert"
            elif kind == "crop":
                rect = self._rect_from_percent(page, rect_spec)
                if rect.width < 36 or rect.height < 36:
                    raise ValueError("Der gewählte Ausschnitt ist zu klein.")
                page.set_cropbox(rect)
                message = "Seite zugeschnitten"
                self.selected_annotation_xref = None
                self.selected_widget_xref = None
            else:
                rect = self._rect_from_percent(page, rect_spec)
            if kind == "text":
                text = (self.pending_annotation.get("text") or "").strip()
                if not text:
                    text, ok = QInputDialog.getMultiLineText(
                        self,
                        "Textfeld einfügen",
                        "Text für dieses Feld:",
                    )
                    if not ok or not text.strip():
                        return
                    text = text.strip()
                annot = page.add_freetext_annot(
                    rect,
                    text,
                    fontsize=self.pending_annotation["font_size"],
                    fontname=self.pending_annotation.get("fontname", "helv"),
                    text_color=self.pending_annotation["color"],
                    fill_color=(1, 1, 1),
                    border_color=self.pending_annotation["color"],
                    align=0,
                )
                try:
                    annot.set_border(width=1)
                except Exception:
                    pass
                annot.update()
                message = "Textfeld platziert"
            elif kind == "rect":
                annot = page.add_rect_annot(rect)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.set_border(width=self.pending_annotation["line_width"])
                annot.update()
                message = "Rechteck platziert"
            elif kind == "ellipse":
                annot = page.add_circle_annot(rect)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.set_border(width=self.pending_annotation["line_width"])
                annot.update()
                message = "Ellipse platziert"
            elif kind == "star":
                pts = [fitz.Point(px, py) for px, py in star_points(rect.x0, rect.y0, rect.x1, rect.y1)]
                annot = page.add_polygon_annot(pts)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.set_border(width=self.pending_annotation["line_width"])
                annot.update()
                message = "Stern platziert"
            elif kind == "link":
                uri = self.pending_annotation.get("uri")
                if not uri:
                    raise ValueError("Keine Ziel-URL angegeben.")
                page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": uri})
                # Sichtbare blaue Unterstreichung als Hinweis auf den Link.
                color = self.pending_annotation["color"]
                page.draw_line(
                    fitz.Point(rect.x0, rect.y1),
                    fitz.Point(rect.x1, rect.y1),
                    color=color,
                    width=max(0.5, float(self.pending_annotation.get("line_width", 1.0))),
                )
                annot = None
                message = f"Hyperlink eingefügt: {uri}"
            elif kind == "highlight":
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.update()
                message = "Markierung platziert"
            elif kind == "strikeout":
                annot = page.add_strikeout_annot(rect)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.update()
                message = "Text durchgestrichen"
            elif kind == "underline":
                annot = page.add_underline_annot(rect)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.update()
                message = "Text unterstrichen"
            elif kind in {"line", "arrow"}:
                start_point = self._view_to_page_point(
                    (start_x / 100.0) * self.preview.pixmap().width(),
                    (start_y / 100.0) * self.preview.pixmap().height(),
                )
                end_point = self._view_to_page_point(
                    (end_x / 100.0) * self.preview.pixmap().width(),
                    (end_y / 100.0) * self.preview.pixmap().height(),
                )
                if start_point is None or end_point is None:
                    raise ValueError("Linie konnte nicht platziert werden.")
                annot = page.add_line_annot(start_point, end_point)
                annot.set_colors(stroke=self.pending_annotation["color"])
                annot.set_border(width=self.pending_annotation["line_width"])
                self._set_line_end_style(annot, arrow=(kind == "arrow"))
                annot.update()
                message = "Pfeil platziert" if kind == "arrow" else "Linie platziert"
            elif kind == "image":
                image_path = self.pending_annotation.get("image_path")
                if not image_path:
                    raise ValueError("Keine Bilddatei ausgewählt.")
                page.insert_image(rect, filename=str(image_path), overlay=True)
                annot = None
                message = f"Bild eingefügt: {Path(str(image_path)).name}"
            elif kind == "redact":
                annot = page.add_redact_annot(rect, fill=(0, 0, 0))
                annot.update()
                message = "Schwärzung platziert"
            elif kind == "text-replace":
                # Originaltext endgültig entfernen (Redaction) statt nur zu
                # übermalen, dann Ersatztext in den freigeräumten Bereich setzen.
                if self._remove_content_in_rect(page, rect) is None:
                    return
                rc = page.insert_textbox(
                    rect,
                    self.pending_annotation["text"],
                    fontsize=self.pending_annotation["font_size"],
                    fontname=self.pending_annotation.get("fontname", "helv"),
                    color=self.pending_annotation["color"],
                    align=0,
                )
                if rc < 0:
                    raise ValueError("Der Ersetzungstext passt nicht in den markierten Bereich – bitte einen größeren Bereich wählen oder die Schriftgröße verringern.")
                annot = None
                message = "Text ersetzt"
            elif kind == "crop":
                annot = None
            else:
                return
            self.selected_annotation_xref = getattr(annot, "xref", None) if annot is not None else None
            self._update_selected_annotation_ui()
            self._set_dirty(True)
            self._refresh_thumbnails()
            self._refresh_comment_list()
            self.render_current_page()
            self.statusBar().showMessage(message)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Platzierung fehlgeschlagen:\n{e}")
        finally:
            self._clear_pending_annotation()

    def _parse_order_spec(self, spec: str, total_pages: int) -> list[int]:
        ordered: list[int] = []

        def parse_single(token: str) -> int | None:
            tk = token.strip().lower()
            alias_with_offset = re.fullmatch(
                r"(first|start|begin|erste|anfang|last|end|ende|letzte|middle|mid|center|centre|mitte|current|cur|here|hier|aktuell|jetzt)\s*([+-]\s*\d+)?",
                tk,
            )
            if alias_with_offset:
                base_name, offset_raw = alias_with_offset.groups()
                if base_name in {"first", "start", "begin", "erste", "anfang"}:
                    base = 1
                elif base_name in {"last", "end", "ende", "letzte"}:
                    base = total_pages
                elif base_name in {"middle", "mid", "center", "centre", "mitte"}:
                    base = ((total_pages - 1) // 2) + 1
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

            # Accept textual range connectors as well, e.g. "1 up to 5", "1 to/thru/through/until 5" / "1 bis zu/durch 5".
            body = re.sub(r"\b(?:up\s+to|bis\s+zu|to|thru|through|until|till|bis|durch)\b", "-", body, flags=re.IGNORECASE)
            # Normalize Unicode dashes users often paste from rich text.
            body = re.sub(r"[–—−]", "-", body)

            # Range bounds can themselves contain signed alias offsets like
            # "last-1" or "current+2". Use a bound-aware regex instead of a
            # naive split("-", 1), otherwise "last-1-last" is parsed wrongly.
            bound = r"(?:\d+|first|start|begin|erste|anfang|last|end|ende|letzte|middle|mid|center|centre|mitte|current|cur|here|hier|aktuell|jetzt)(?:\s*[+-]\s*\d+)?"
            m = re.fullmatch(rf"\s*({bound})\s*-\s*({bound})\s*", body)
            if not m:
                return None
            start_raw, end_raw = m.groups()
            return start_raw, end_raw, step

        # Support comma/semicolon lists plus line-based pasted input.
        for part in re.split(r"[;,\n\r]+", spec):
            token = part.strip().lower()
            if not token:
                continue

            if token in {"all", "alle", "*"}:
                ordered.extend(range(total_pages))
                continue
            if token in {"odd", "ungerade"}:
                ordered.extend(range(0, total_pages, 2))
                continue
            if token in {"even", "gerade"}:
                ordered.extend(range(1, total_pages, 2))
                continue
            if token in {"reverse", "rev", "backward", "backwards", "desc", "descending", "absteigend", "rückwärts", "rueckwaerts", "umgekehrt"}:
                ordered.extend(reversed(range(total_pages)))
                continue
            if token in {"middle", "mid", "center", "centre", "mitte"}:
                ordered.append((total_pages - 1) // 2)
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

    # Normalize common chat/launcher wrapping artifacts. We loop until stable
    # so values like "<file:///tmp/doc.pdf>." first lose punctuation and then
    # lose the remaining wrappers.
    wrapper_pairs = {"<": ">", "(": ")", "[": "]", "{": "}"}
    while True:
        changed = False

        # Some launch wrappers pass the argument with surrounding quotes intact.
        # Repeatedly unwrap matching quote pairs so doubly-wrapped values like
        # '"/tmp/doc.pdf"' still resolve as file paths.
        while len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
            changed = True
            if not value:
                return None

        # Chat/launcher wrappers sometimes include surrounding delimiters around
        # links, e.g. <file:///tmp/doc.pdf> or (file:///tmp/doc.pdf).
        while len(value) >= 2 and value[0] in wrapper_pairs and value[-1] == wrapper_pairs[value[0]]:
            value = value[1:-1].strip()
            changed = True
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
                changed = True
                continue
            break

        if not changed:
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
