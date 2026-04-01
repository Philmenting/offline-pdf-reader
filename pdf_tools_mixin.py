from __future__ import annotations

import re
from pathlib import Path

import fitz
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
)

from parsing import sanitize_filename
from ui_widgets import ReorderPagesDialog


class PdfToolsMixin:
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
    @staticmethod
    def _ensure_pdf_suffix(path: str) -> str:
        return path if path.lower().endswith(".pdf") else f"{path}.pdf"
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
