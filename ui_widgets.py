from __future__ import annotations

import fitz  # PyMuPDF
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTableWidgetItem,
    QVBoxLayout,
)


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


class ThumbnailListWidget(QListWidget):
    pagesReordered = Signal()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        super().dropEvent(event)
        self.pagesReordered.emit()
