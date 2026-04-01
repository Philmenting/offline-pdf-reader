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

from models import BatchRenameProposal, ExportRecord, OCRPassResult, ParsedDocInfo
from parsing import (
    _list_pdf_files,
    _looks_like_subject_line,
    _normalize_filename_part,
    build_export_record,
    candidate_variants,
    export_records_as_txt,
    extract_total_amount,
    extract_total_amount_info,
    parse_doc_info,
    sanitize_filename,
    suggest_filename_from_text,
)
from startup import find_startup_pdf_argument
from ui_widgets import ReorderPagesDialog, SortableTableWidgetItem, ThumbnailListWidget
from annotation_mixin import AnnotationMixin
from export_mixin import ExportMixin
from ocr_mixin import OcrMixin
from pdf_tools_mixin import PdfToolsMixin

import fitz  # PyMuPDF
import pytesseract
from pytesseract import Output, TesseractError, TesseractNotFoundError
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
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
    QRubberBand,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QFormLayout,
    QWidget,
    QHBoxLayout,
)

try:
    import cv2  # type: ignore[import-not-found]
except Exception:
    cv2 = None

try:
    import numpy as np  # type: ignore[import-not-found]
except Exception:
    np = None


APP_TITLE = "Offline PDF Leser — MVP"

class MainWindow(OcrMixin, PdfToolsMixin, ExportMixin, AnnotationMixin, QMainWindow):
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

        self.annotation_mode: str | None = None
        self._annot_drag_start: tuple[int, int] | None = None
        self._annot_rubber_band: QRubberBand | None = None

        self.preview = QLabel("Kein PDF geladen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(460)
        self.preview.setMouseTracking(True)
        self.preview.installEventFilter(self)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidget(self.preview)
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.thumb_list = ThumbnailListWidget()
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

        self.extracted_text = ""
        self.text_dialog: QDialog | None = None
        self.text_output_view: QTextEdit | None = None

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
            return b

        def _primary_btn(label: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            b.setProperty("btnRole", "primary")
            return b

        def _action_btn(label: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if tooltip:
                b.setToolTip(tooltip)
            b.setProperty("btnRole", "action")
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
        btn_extract      = _action_btn("Seite erkennen",  "Text der aktuellen Seite extrahieren")
        btn_extract_all  = _action_btn("Alle erkennen",   "Text aller Seiten extrahieren")
        btn_auto_ocr_name = _primary_btn("OCR + Benennen", "OCR ausführen und Dateinamen vorschlagen")
        btn_save         = _primary_btn("Speichern",      "Direkt speichern (Ctrl+S)")
        btn_saveas       = _action_btn("Speichern als …", "Speichern unter (Ctrl+Shift+S)")
        btn_merge        = _action_btn("Zusammenführen",  "PDFs zusammenführen")
        btn_split        = _action_btn("Extrahieren",     "Seiten extrahieren")
        btn_reorder      = _action_btn("Sortieren",       "Seiten neu anordnen")
        btn_remove_empty = _action_btn("Leer entfernen",  "Leere Seiten entfernen")
        btn_search       = _icon_btn("🔍",                 "Suche starten (Ctrl+F)")
        btn_search_close = _icon_btn("✕",                 "Suche schließen")
        btn_hit_prev     = _icon_btn("◀",                 "Vorheriger Treffer")
        btn_hit_next     = _icon_btn("▶",                 "Nächster Treffer")

        self.search_counter = QLabel("0 / 0")
        self.search_counter.setAccessibleName("Suchtreffer-Zähler")
        self.search_counter.setProperty("role", "counter")

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
        btn_reorder.setAccessibleName("Seiten sortieren")
        btn_remove_empty.setAccessibleName("Leere Seiten entfernen")

        btn_extract.clicked.connect(self.extract_text_and_suggest)
        btn_extract_all.clicked.connect(self.recognize_text_all_pages_and_suggest)
        btn_auto_ocr_name.clicked.connect(self.ocr_and_suggest_filename)
        btn_save.clicked.connect(self.save_in_place)
        btn_saveas.clicked.connect(self.save_as_suggested)
        btn_merge.clicked.connect(self.merge_pdfs)
        btn_split.clicked.connect(self.extract_pages_to_new_pdf)
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

        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar_widget = QWidget()
        toolbar_widget.setProperty("role", "toolbar")
        toolbar_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        def _vsep() -> QFrame:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.VLine)
            sep.setFixedWidth(1)
            sep.setProperty("role", "toolsep")
            return sep

        toolbar_top = QHBoxLayout(toolbar_widget)
        toolbar_top.setContentsMargins(12, 8, 12, 8)
        toolbar_top.setSpacing(4)

        # Öffnen
        toolbar_top.addWidget(btn_open)
        toolbar_top.addWidget(_vsep())

        # Navigation
        toolbar_top.addWidget(btn_first)
        toolbar_top.addWidget(btn_prev)
        toolbar_top.addWidget(btn_next)
        toolbar_top.addWidget(btn_last)
        toolbar_top.addWidget(btn_goto)
        toolbar_top.addWidget(_vsep())

        # Zoom
        toolbar_top.addWidget(btn_zoom_out)
        toolbar_top.addWidget(btn_zoom_in)
        toolbar_top.addWidget(btn_zoom_reset)
        toolbar_top.addWidget(_vsep())

        # Rotation
        toolbar_top.addWidget(btn_rotate_left)
        toolbar_top.addWidget(btn_rotate_right)
        toolbar_top.addWidget(btn_rotate_reset)
        toolbar_top.addWidget(_vsep())

        # Undo/Redo
        toolbar_top.addWidget(self.btn_undo)
        toolbar_top.addWidget(self.btn_redo)
        toolbar_top.addWidget(_vsep())

        # OCR + Aktionen
        toolbar_top.addWidget(btn_extract)
        toolbar_top.addWidget(btn_extract_all)
        toolbar_top.addWidget(btn_auto_ocr_name)
        toolbar_top.addWidget(_vsep())

        # Speichern
        toolbar_top.addWidget(btn_save)
        toolbar_top.addStretch(1)

        # ── Annotations-Toolbar ──────────────────────────────────────────────
        annot_bar = QWidget()
        annot_bar.setProperty("role", "annotbar")
        annot_layout = QHBoxLayout(annot_bar)
        annot_layout.setContentsMargins(12, 4, 12, 4)
        annot_layout.setSpacing(6)
        annot_layout.addWidget(QLabel("Annotationen:"))

        self.btn_annot_highlight = QPushButton("Markieren")
        self.btn_annot_highlight.setCheckable(True)
        self.btn_annot_highlight.setToolTip("Mausziehen auf der Seite zum Markieren von Text")
        self.btn_annot_highlight.toggled.connect(
            lambda on: self._set_annotation_mode("highlight") if on else self._set_annotation_mode(None)
        )
        annot_layout.addWidget(self.btn_annot_highlight)

        self.btn_annot_note = QPushButton("Notiz")
        self.btn_annot_note.setCheckable(True)
        self.btn_annot_note.setToolTip("Klick auf die Seite zum Platzieren einer Notiz")
        self.btn_annot_note.toggled.connect(
            lambda on: self._set_annotation_mode("note") if on else self._set_annotation_mode(None)
        )
        annot_layout.addWidget(self.btn_annot_note)

        self.btn_annot_delete = QPushButton("Löschen")
        self.btn_annot_delete.setCheckable(True)
        self.btn_annot_delete.setToolTip("Klick auf eine Annotation zum Entfernen")
        self.btn_annot_delete.toggled.connect(
            lambda on: self._set_annotation_mode("delete") if on else self._set_annotation_mode(None)
        )
        annot_layout.addWidget(self.btn_annot_delete)

        annot_layout.addStretch(1)
        self.annot_mode_label = QLabel("Modus: Navigation")
        self.annot_mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        annot_layout.addWidget(self.annot_mode_label)

        # ── Dateiname-Zeile ──────────────────────────────────────────────────
        name_widget = QWidget()
        name_widget.setProperty("role", "namebar")
        name_layout = QHBoxLayout(name_widget)
        name_layout.setContentsMargins(12, 6, 12, 6)
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
        search_layout.setContentsMargins(12, 6, 12, 6)
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
        ocr_layout.setContentsMargins(12, 5, 12, 5)
        ocr_layout.setSpacing(8)
        ocr_layout.addWidget(self.ocr_feedback, 1)
        ocr_layout.addWidget(self.ocr_mode_label)
        ocr_layout.addWidget(self.btn_cancel_ocr)
        ocr_layout.addWidget(self.btn_retry_failed_ocr)
        ocr_layout.addWidget(self.btn_reset_ocr_prefs)

        # ── Splitter (Thumbnails + Vorschau) ─────────────────────────────────
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(6)
        self.content_splitter.addWidget(self.thumb_list)
        self.content_splitter.addWidget(self.preview_scroll)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([200, 980])

        # ── Haupt-Layout ─────────────────────────────────────────────────────
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar_widget)
        layout.addWidget(annot_bar)
        layout.addWidget(name_widget)
        layout.addWidget(self.search_bar_widget)
        layout.addWidget(self.search_results_list)
        layout.addWidget(self.content_splitter, 1)
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

        menu_file.addSeparator()

        act_save = QAction("Speichern", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self.save_in_place)
        menu_file.addAction(act_save)

        act_save_as = QAction("Speichern als …", self)
        act_save_as.setShortcut("Ctrl+Shift+S")
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

        act_merge = QAction("PDFs zusammenführen", self)
        act_merge.triggered.connect(self.merge_pdfs)
        menu_tools.addAction(act_merge)

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

        menu_tools.addSeparator()
        act_images_to_pdf = QAction("Bilder zu PDF konvertieren …", self)
        act_images_to_pdf.triggered.connect(self.convert_images_to_pdf)
        menu_tools.addAction(act_images_to_pdf)

        act_export_images = QAction("PDF-Seiten als Bilder exportieren …", self)
        act_export_images.triggered.connect(self.export_pages_as_images)
        menu_tools.addAction(act_export_images)

        menu_tools.addSeparator()
        act_metadata = QAction("PDF-Metadaten bearbeiten …", self)
        act_metadata.triggered.connect(self.edit_pdf_metadata)
        menu_tools.addAction(act_metadata)

        menu_tools.addSeparator()
        act_encrypt = QAction("PDF verschlüsseln …", self)
        act_encrypt.triggered.connect(self.encrypt_pdf)
        menu_tools.addAction(act_encrypt)

        act_decrypt = QAction("Verschlüsselung entfernen …", self)
        act_decrypt.triggered.connect(self.remove_pdf_encryption)
        menu_tools.addAction(act_decrypt)

        menu_export = self.menuBar().addMenu("Exportieren")
        act_export_current = QAction("Aktuelle Datei exportieren …", self)
        act_export_current.triggered.connect(self.export_current_file)
        menu_export.addAction(act_export_current)

        act_export_folder = QAction("Ordner aggregiert exportieren …", self)
        act_export_folder.triggered.connect(self.export_folder_aggregate)
        menu_export.addAction(act_export_folder)

        act_batch_rename = QAction("Ordner stapelweise umbenennen …", self)
        act_batch_rename.triggered.connect(self.batch_rename_folder)
        menu_export.addAction(act_batch_rename)

        # Improve menu accessibility/discoverability (screen readers + status hints).
        all_actions = [
            act_open,
            act_close_pdf,
            act_save,
            act_save_as,
            act_undo,
            act_redo,
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
            act_merge,
            act_split_chunks,
            act_remove_empty,
            act_rotate_left,
            act_rotate_right,
            act_delete_pages,
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
        self._set_dirty(True)
        self._refresh_thumbnails()
        self.render_current_page()
        self.statusBar().showMessage(f"{len(selected)} Seite(n) gelöscht (noch nicht gespeichert)")

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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()

    startup_candidate = find_startup_pdf_argument(sys.argv[1:])

    if startup_candidate is not None:
        win._open_pdf_path(str(startup_candidate))

    win.show()
    sys.exit(app.exec())
