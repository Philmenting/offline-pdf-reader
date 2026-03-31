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
        self._update_undo_redo_buttons()
        self._update_ocr_mode_label()
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
        menu.addSeparator()
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

    def convert_images_to_pdf(self) -> None:
        image_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Bilder auswählen",
            "",
            "Bilddateien (*.jpg *.jpeg *.png *.tiff *.tif *.bmp *.webp)",
        )
        if not image_paths:
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "PDF speichern unter",
            "",
            "PDF-Dateien (*.pdf)",
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".pdf"):
            out_path += ".pdf"

        total = len(image_paths)
        progress = QProgressDialog("Konvertiere Bilder …", "Abbrechen", 0, total, self)
        progress.setWindowTitle("Bitte warten")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        out_doc = fitz.open()
        errors: list[str] = []

        for i, img_path in enumerate(image_paths):
            progress.setValue(i)
            progress.setLabelText(f"Bild {i + 1}/{total}: {Path(img_path).name}")
            QApplication.processEvents()
            if progress.wasCanceled():
                out_doc.close()
                return
            try:
                img_doc = fitz.open(img_path)
                pdf_bytes = img_doc.convert_to_pdf()
                img_doc.close()
                pdf_doc = fitz.open("pdf", pdf_bytes)
                out_doc.insert_pdf(pdf_doc)
                pdf_doc.close()
            except Exception as e:
                errors.append(f"{Path(img_path).name}: {e}")

        progress.setValue(total)

        if out_doc.page_count == 0:
            out_doc.close()
            QMessageBox.critical(self, "Fehler", "Keine Seiten erzeugt. Bitte überprüfe die ausgewählten Dateien.")
            return

        page_count = out_doc.page_count
        try:
            out_doc.save(out_path, garbage=4, deflate=True)
        except Exception as e:
            out_doc.close()
            QMessageBox.critical(self, "Fehler", f"PDF konnte nicht gespeichert werden:\n{e}")
            return
        out_doc.close()

        msg = f"PDF mit {page_count} Seite(n) gespeichert:\n{out_path}"
        if errors:
            msg += f"\n\nFehler bei {len(errors)} Bild(ern):\n" + "\n".join(errors)

        reply = QMessageBox.question(
            self,
            "Fertig",
            msg + "\n\nPDF jetzt öffnen?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._open_pdf_path(out_path)

    def export_pages_as_images(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        msg = QMessageBox(self)
        msg.setWindowTitle("Seiten exportieren")
        msg.setText("Welche Seiten sollen als Bilder exportiert werden?")
        btn_current = msg.addButton("Aktuelle Seite", QMessageBox.ButtonRole.YesRole)
        btn_all = msg.addButton("Alle Seiten", QMessageBox.ButtonRole.NoRole)
        msg.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is None or (clicked is not btn_current and clicked is not btn_all):
            return
        export_all = clicked is btn_all

        fmt, ok = QInputDialog.getItem(
            self, "Format wählen", "Bildformat:", ["PNG", "JPEG"], 0, False
        )
        if not ok:
            return
        ext = fmt.lower()
        filter_str = f"{'PNG' if fmt == 'PNG' else 'JPEG'}-Dateien (*.{ext})"

        if export_all:
            out_dir = QFileDialog.getExistingDirectory(self, "Ausgabeordner wählen")
            if not out_dir:
                return
            out_dir = Path(out_dir)
            total = len(self.doc)
            progress = QProgressDialog("Exportiere Seiten …", "Abbrechen", 0, total, self)
            progress.setWindowTitle("Bitte warten")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            errors: list[str] = []
            stem = self.pdf_path.stem
            for idx in range(total):
                progress.setValue(idx)
                progress.setLabelText(f"Seite {idx + 1}/{total} …")
                QApplication.processEvents()
                if progress.wasCanceled():
                    return
                try:
                    rotation = self.page_rotations.get(idx, 0)
                    matrix = fitz.Matrix(2.0, 2.0).prerotate(rotation)
                    pix = self.doc[idx].get_pixmap(matrix=matrix, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    out_file = out_dir / f"{stem}_Seite{idx + 1:04d}.{ext}"
                    img.save(str(out_file), fmt)
                except Exception as e:
                    errors.append(f"Seite {idx + 1}: {e}")
            progress.setValue(total)
            summary = f"{total - len(errors)} von {total} Seiten exportiert nach:\n{out_dir}"
            if errors:
                summary += f"\n\nFehler bei {len(errors)} Seite(n):\n" + "\n".join(errors)
            QMessageBox.information(self, "Fertig", summary)
        else:
            idx = self.current_page
            stem = self.pdf_path.stem
            default_name = str(self.pdf_path.parent / f"{stem}_Seite{idx + 1:04d}.{ext}")
            out_path, _ = QFileDialog.getSaveFileName(self, "Bild speichern unter", default_name, filter_str)
            if not out_path:
                return
            try:
                rotation = self.page_rotations.get(idx, 0)
                matrix = fitz.Matrix(2.0, 2.0).prerotate(rotation)
                pix = self.doc[idx].get_pixmap(matrix=matrix, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                img.save(out_path, fmt)
                QMessageBox.information(self, "Fertig", f"Bild gespeichert:\n{out_path}")
            except Exception as e:
                QMessageBox.critical(self, "Fehler", f"Export fehlgeschlagen:\n{e}")

    def edit_pdf_metadata(self) -> None:
        if not self.doc:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        meta = dict(self.doc.metadata or {})

        dlg = QDialog(self)
        dlg.setWindowTitle("PDF-Metadaten bearbeiten")
        dlg.setMinimumWidth(480)
        layout = QVBoxLayout(dlg)

        form = QFormLayout()
        fields: dict[str, QLineEdit] = {}
        for key, label in [
            ("title", "Titel"),
            ("author", "Autor"),
            ("subject", "Betreff"),
            ("keywords", "Stichwörter"),
            ("creator", "Ersteller"),
        ]:
            edit = QLineEdit(meta.get(key, ""))
            form.addRow(label + ":", edit)
            fields[key] = edit
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        for key, edit in fields.items():
            meta[key] = edit.text().strip()

        try:
            self.doc.set_metadata(meta)
            self._set_dirty(True)
            self.statusBar().showMessage("Metadaten aktualisiert (nicht gespeichert)")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Metadaten konnten nicht gesetzt werden:\n{e}")

    def encrypt_pdf(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        pw, ok = QInputDialog.getText(
            self, "PDF verschlüsseln", "Passwort eingeben:", QLineEdit.EchoMode.Password
        )
        if not ok or not pw:
            return

        pw2, ok2 = QInputDialog.getText(
            self, "PDF verschlüsseln", "Passwort bestätigen:", QLineEdit.EchoMode.Password
        )
        if not ok2 or pw != pw2:
            QMessageBox.warning(self, "Fehler", "Passwörter stimmen nicht überein.")
            return

        default_name = str(self.pdf_path.with_stem(self.pdf_path.stem + "_geschützt"))
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Verschlüsselte PDF speichern unter", default_name, "PDF-Dateien (*.pdf)"
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".pdf"):
            out_path += ".pdf"

        try:
            self.doc.save(
                out_path,
                encryption=fitz.PDF_ENCRYPT_AES_256,
                user_pw=pw,
                owner_pw=pw,
                garbage=4,
                deflate=True,
            )
            QMessageBox.information(self, "Fertig", f"Verschlüsselte PDF gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Verschlüsselung fehlgeschlagen:\n{e}")

    def remove_pdf_encryption(self) -> None:
        if not self.doc or not self.pdf_path:
            QMessageBox.information(self, "Hinweis", "Bitte zuerst ein PDF öffnen.")
            return

        default_name = str(self.pdf_path.with_stem(self.pdf_path.stem + "_entschlüsselt"))
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Entschlüsselte PDF speichern unter", default_name, "PDF-Dateien (*.pdf)"
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".pdf"):
            out_path += ".pdf"

        try:
            self.doc.save(out_path, encryption=fitz.PDF_ENCRYPT_NONE, garbage=4, deflate=True)
            QMessageBox.information(self, "Fertig", f"Entschlüsselte PDF gespeichert:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Entschlüsselung fehlgeschlagen:\n{e}")

    # ── Annotationen ─────────────────────────────────────────────────────────

    def _set_annotation_mode(self, mode: str | None) -> None:
        self.annotation_mode = mode

        # Update toggle button states without re-triggering their signals
        for btn, btn_mode in [
            (self.btn_annot_highlight, "highlight"),
            (self.btn_annot_note, "note"),
            (self.btn_annot_delete, "delete"),
        ]:
            btn.blockSignals(True)
            btn.setChecked(mode == btn_mode)
            btn.blockSignals(False)

        # Cursor
        if mode == "highlight":
            self.preview.setCursor(Qt.CursorShape.CrossCursor)
            self.annot_mode_label.setText("Modus: Markieren – Ziehen zum Auswählen")
        elif mode == "note":
            self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
            self.annot_mode_label.setText("Modus: Notiz – Klick zum Platzieren")
        elif mode == "delete":
            self.preview.setCursor(Qt.CursorShape.ForbiddenCursor)
            self.annot_mode_label.setText("Modus: Löschen – Klick auf Annotation")
        else:
            self.preview.unsetCursor()
            self.annot_mode_label.setText("Modus: Navigation")

        # Hide rubber band when leaving highlight mode
        if mode != "highlight" and self._annot_rubber_band:
            self._annot_rubber_band.hide()
        self._annot_drag_start = None

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is not self.preview or not self.annotation_mode or not self.doc:
            return super().eventFilter(obj, event)

        etype = event.type()

        if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            self._annot_drag_start = (pos.x(), pos.y())

            if self.annotation_mode == "note":
                self._add_note_annotation(pos.x(), pos.y())
                return True
            if self.annotation_mode == "delete":
                self._delete_annotation_at(pos.x(), pos.y())
                return True

            # highlight: initialise rubber band
            if self.annotation_mode == "highlight":
                if self._annot_rubber_band is None:
                    self._annot_rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.preview)
                from PySide6.QtCore import QRect, QPoint
                self._annot_rubber_band.setGeometry(QRect(pos, pos))
                self._annot_rubber_band.show()
            return True

        if etype == QEvent.Type.MouseMove and self.annotation_mode == "highlight" and self._annot_drag_start:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if self._annot_rubber_band:
                from PySide6.QtCore import QRect, QPoint
                origin = QPoint(*self._annot_drag_start)
                self._annot_rubber_band.setGeometry(QRect(origin, pos).normalized())
            return True

        if etype == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self.annotation_mode == "highlight" and self._annot_drag_start:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                if self._annot_rubber_band:
                    self._annot_rubber_band.hide()
                end = (pos.x(), pos.y())
                if abs(end[0] - self._annot_drag_start[0]) > 4 or abs(end[1] - self._annot_drag_start[1]) > 4:
                    self._complete_highlight(self._annot_drag_start, end)
                self._annot_drag_start = None
            return True

        return super().eventFilter(obj, event)

    def _screen_to_pdf_point(self, view_x: int, view_y: int) -> tuple[float, float]:
        """Convert screen coordinates (relative to self.preview) to PDF page coordinates."""
        if not self.doc:
            return (0.0, 0.0)
        page = self.doc[self.current_page]
        scale = self.zoom_factor
        rotation = self.page_rotations.get(self.current_page, 0) % 360
        pw = float(page.rect.width)
        ph = float(page.rect.height)
        vx = view_x / scale
        vy = view_y / scale
        if rotation == 0:
            pdf_x, pdf_y = vx, vy
        elif rotation == 90:
            pdf_x, pdf_y = vy, ph - vx
        elif rotation == 180:
            pdf_x, pdf_y = pw - vx, ph - vy
        else:  # 270
            pdf_x, pdf_y = pw - vy, vx
        return (max(0.0, min(pdf_x, pw)), max(0.0, min(pdf_y, ph)))

    def _complete_highlight(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        if not self.doc:
            return
        x0, y0 = self._screen_to_pdf_point(*start)
        x1, y1 = self._screen_to_pdf_point(*end)
        rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if rect.is_empty or rect.width < 1 or rect.height < 1:
            return
        page = self.doc[self.current_page]
        try:
            words = page.get_text("words", clip=rect)
            if words:
                quads = [fitz.Quad(fitz.Rect(w[:4])) for w in words]
                page.add_highlight_annot(quads)
            else:
                annot = page.add_rect_annot(rect)
                annot.set_colors(fill=(1.0, 1.0, 0.0))
                annot.update()
            self._set_dirty(True)
            self.render_current_page()
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Markierung konnte nicht erstellt werden:\n{e}")

    def _add_note_annotation(self, view_x: int, view_y: int) -> None:
        if not self.doc:
            return
        pdf_x, pdf_y = self._screen_to_pdf_point(view_x, view_y)
        text, ok = QInputDialog.getMultiLineText(self, "Notiz hinzufügen", "Notiztext:")
        if not ok or not text.strip():
            return
        page = self.doc[self.current_page]
        try:
            page.add_text_annot(fitz.Point(pdf_x, pdf_y), text.strip())
            self._set_dirty(True)
            self.render_current_page()
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Notiz konnte nicht erstellt werden:\n{e}")

    def _delete_annotation_at(self, view_x: int, view_y: int) -> None:
        if not self.doc:
            return
        pdf_x, pdf_y = self._screen_to_pdf_point(view_x, view_y)
        pt = fitz.Point(pdf_x, pdf_y)
        page = self.doc[self.current_page]
        for annot in page.annots():
            if annot.rect.contains(pt):
                page.delete_annot(annot)
                self._set_dirty(True)
                self.render_current_page()
                return

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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()

    startup_candidate = find_startup_pdf_argument(sys.argv[1:])

    if startup_candidate is not None:
        win._open_pdf_path(str(startup_candidate))

    win.show()
    sys.exit(app.exec())
