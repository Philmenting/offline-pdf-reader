from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict
from pathlib import Path

import fitz
import pytesseract
from pytesseract import TesseractError, TesseractNotFoundError
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColor,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QTableWidget,
    QVBoxLayout,
)

from models import BatchRenameProposal, ExportRecord, ParsedDocInfo
from parsing import (
    _list_pdf_files,
    build_export_record,
    export_records_as_txt,
    parse_doc_info,
    sanitize_filename,
    suggest_filename_from_text,
)
from ui_widgets import SortableTableWidgetItem


class ExportMixin:
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
