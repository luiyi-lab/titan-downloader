import sys

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from backend import ASSETS_DIR


def build_icon():
    logo = ASSETS_DIR / "logo.png"
    if logo.exists():
        pm = QPixmap(str(logo))
        if not pm.isNull():
            square = QPixmap(256, 256)
            square.fill(Qt.GlobalColor.transparent)
            p = QPainter(square)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scaled = pm.scaled(
                256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            x = (256 - scaled.width()) // 2
            y = (256 - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
            p.end()
            return QIcon(square)
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#0d1a12"))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 17, 17)
    grad = QLinearGradient(4, 4, 60, 60)
    grad.setColorAt(0.0, QColor("#3be477"))
    grad.setColorAt(1.0, QColor("#149e46"))
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(5, 5, 54, 54), 14, 14)
    p.setBrush(QColor("#ffffff"))
    triangle = [QPointF(26, 21), QPointF(26, 43), QPointF(45, 32)]
    p.drawPolygon(triangle)
    p.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    show_requested = Signal()
    pause_toggle_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(build_icon(), parent)
        self.setToolTip("Titan Downloader")
        menu = QMenu()
        if sys.platform == "win32":
            menu.addSection("Titan Downloader")
        self.act_show = QAction("Mostrar", menu)
        self.act_pause = QAction("⏸ Pausar", menu)
        self.act_quit = QAction("Salir", menu)
        menu.addAction(self.act_show)
        menu.addAction(self.act_pause)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        self.setContextMenu(menu)

        self.act_show.triggered.connect(self.show_requested.emit)
        self.act_pause.triggered.connect(self.pause_toggle_requested.emit)
        self.act_quit.triggered.connect(self.quit_requested.emit)
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_requested.emit()

    def set_paused(self, paused):
        self.act_pause.setText("▶ Reanudar" if paused else "⏸ Pausar")
