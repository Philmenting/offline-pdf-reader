from __future__ import annotations

import fitz
from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox, QRubberBand


class AnnotationMixin:
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
                self._annot_rubber_band.setGeometry(QRect(pos, pos))
                self._annot_rubber_band.show()
            return True

        if etype == QEvent.Type.MouseMove and self.annotation_mode == "highlight" and self._annot_drag_start:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if self._annot_rubber_band:
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
