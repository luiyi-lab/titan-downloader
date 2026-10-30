import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QColor, QFont, QFontInfo, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from backend import DATA_DIR, Config
from backend.database import DatabaseManager
from backend.download_manager import DownloadManager
from backend.engine_resolver import EngineResolver
from backend.processor import MediaProcessor
from backend.rate_limiter import RateLimiter

from frontend.main_window import MainWindow

ACCENT = "#1ed760"

QSS = f"""
* {{
    outline: none;
}}

QWidget {{
    background-color: #131418;
    color: #e8eaed;
    font-size: 12px;
}}

QMainWindow, QDialog {{
    background-color: #0d0e11;
}}

QLabel {{
    background: transparent;
}}

QLabel#lblVersions {{
    color: #6f7681;
    font-size: 11px;
}}

QLabel#appTitle {{
    font-size: 14px;
    font-weight: 700;
    color: #ffffff;
}}

QToolButton {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #23252b, stop:1 #1d1f24);
    border: 1px solid #30343e;
    border-radius: 8px;
    padding: 6px 14px;
    font-weight: 500;
}}
QToolButton:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2b2e36, stop:1 #24262d);
    border-color: #3f4450;
}}
QToolButton:pressed, QToolButton:checked {{
    background-color: #191a1f;
}}
QToolButton::menu-indicator {{
    image: none;
    width: 0;
}}

QLabel#lblStatus {{
    color: #9aa1ab;
    font-size: 11px;
}}

QLabel#hintLabel {{
    color: #7d848f;
    font-size: 11px;
}}

QToolTip {{
    background-color: #1d1f25;
    color: #e8eaed;
    border: 1px solid #333742;
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 11px;
}}

QPushButton {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #23252b, stop:1 #1d1f24);
    border: 1px solid #30343e;
    border-radius: 8px;
    padding: 6px 14px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2b2e36, stop:1 #24262d);
    border-color: #3f4450;
}}
QPushButton:pressed {{
    background-color: #191a1f;
}}
QPushButton:focus {{
    border-color: #3f4450;
}}
QPushButton:disabled {{
    color: #565a64;
    background-color: #191a1e;
    border-color: #25272d;
}}

QPushButton#btnPrimary {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #24e06b, stop:1 #17b551);
    color: #05130a;
    border: 1px solid #16a04a;
    font-weight: 700;
}}
QPushButton#btnPrimary:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #35ea7c, stop:1 #1fc65c);
    border-color: #1ed760;
}}
QPushButton#btnPrimary:pressed {{
    background-color: #14a04a;
}}
QPushButton#btnPrimary:disabled {{
    background-color: #22402e;
    color: #5f7a6a;
    border-color: #1d3126;
}}

QPushButton#btnDanger {{
    color: #ff8a80;
    border-color: #4a2626;
}}
QPushButton#btnDanger:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a1e1e, stop:1 #2e1717);
    border-color: #8a3a3a;
    color: #ffb4ab;
}}
QPushButton#btnDanger:pressed {{
    background-color: #241212;
}}

QLineEdit, QComboBox, QSpinBox {{
    background-color: #18191e;
    border: 1px solid #2c2f38;
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
    selection-color: #05130a;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
    border-color: #383d49;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid {ACCENT};
    background-color: #1a1c22;
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: #565a64;
}}

QComboBox::drop-down {{
    border: none;
    width: 26px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #8b93a0;
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: #1d1f25;
    border: 1px solid #333742;
    border-radius: 8px;
    padding: 4px;
    selection-background-color: #2b3a31;
    selection-color: #ffffff;
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    width: 16px;
    border: none;
    background: transparent;
}}

QCheckBox {{
    spacing: 8px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #3a3e48;
    background-color: #18191e;
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox:disabled {{
    color: #565a64;
}}

QTableView {{
    background-color: #15161a;
    alternate-background-color: #17181d;
    gridline-color: transparent;
    border: 1px solid #212329;
    border-radius: 10px;
    selection-background-color: #22352a;
    selection-color: #ffffff;
}}
QTableView::item {{
    padding: 3px 8px;
    border-bottom: 1px solid #1a1b20;
}}
QTableView::item:selected {{
    border-bottom: 1px solid #22352a;
}}

QHeaderView {{
    background-color: transparent;
    border: none;
}}
QHeaderView::section {{
    background-color: #15161a;
    color: #7d848f;
    padding: 8px 6px;
    border: none;
    border-bottom: 1px solid #212329;
    font-size: 10px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background-color: #15161a;
    border: none;
}}

QProgressBar {{
    background-color: #18191e;
    border: none;
    border-radius: 6px;
    min-height: 12px;
    max-height: 12px;
    text-align: center;
    color: #cfd3d9;
    font-size: 9px;
}}
QProgressBar::chunk {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #17b551, stop:1 {ACCENT});
    border-radius: 6px;
}}

QTextEdit {{
    background-color: #121316;
    border: 1px solid #212329;
    border-radius: 10px;
    padding: 6px;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11px;
    selection-background-color: #22352a;
}}

QListWidget {{
    background-color: #18191e;
    border: 1px solid #26282f;
    border-radius: 8px;
    padding: 4px;
}}
QListWidget::item {{
    padding: 4px 8px;
    border-bottom: 1px solid #1a1b20;
}}
QListWidget::item:selected {{
    background-color: #22352a;
    color: #ffffff;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 3px;
}}
QScrollBar::handle:vertical {{
    background: #363a44;
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #484d59;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 3px;
}}
QScrollBar::handle:horizontal {{
    background: #363a44;
    border-radius: 4px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #484d59;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QTabWidget::pane {{
    border: 1px solid #26282f;
    border-radius: 10px;
    top: -1px;
}}
QTabBar {{
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: #7d848f;
    padding: 8px 18px;
    margin-right: 6px;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:selected {{
    color: #ffffff;
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: #cfd3d9;
}}

QMenu {{
    background-color: #1d1f25;
    border: 1px solid #333742;
    border-radius: 9px;
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 24px;
    border-radius: 6px;
    background: transparent;
}}
QMenu::item:selected {{
    background-color: #2b3a31;
}}
QMenu::separator {{
    height: 1px;
    background: #30343e;
    margin: 5px 10px;
}}

QMessageBox {{
    background-color: #15161a;
}}
QMessageBox QPushButton {{
    min-width: 84px;
}}

QGroupBox {{
    border: 1px solid #26282f;
    border-radius: 10px;
    margin-top: 12px;
    padding-top: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #9aa1ab;
}}
"""


def apply_dark(app):
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(13, 14, 17))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(232, 234, 237))
    palette.setColor(QPalette.ColorRole.Base, QColor(21, 22, 26))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(23, 24, 29))
    palette.setColor(QPalette.ColorRole.Text, QColor(232, 234, 237))
    palette.setColor(QPalette.ColorRole.Button, QColor(29, 31, 36))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(232, 234, 237))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(30, 215, 96))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(29, 31, 37))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(232, 234, 237))
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.Text, QColor(86, 90, 100))
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.ButtonText, QColor(86, 90, 100))
    app.setPalette(palette)
    app.setStyleSheet(QSS)
    font = QFont("Segoe UI Variable Display", 9)
    if QFontInfo(font).family().lower() != "segoe ui variable display":
        font = QFont("Segoe UI", 9)
    app.setFont(font)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Titan Downloader")
    app.setQuitOnLastWindowClosed(False)
    apply_dark(app)

    lock = QLockFile(str(Path(tempfile.gettempdir()) / "TitanDownloader.lock"))
    if not lock.tryLock(0):
        QMessageBox.warning(
            None, "Titan Downloader",
            "Titan Downloader ya se está ejecutando.\n\n"
            "Búscalo en la bandeja del sistema (icono junto al reloj).")
        return 1

    config = Config()
    db = DatabaseManager(DATA_DIR / "titan.db")
    limiter = RateLimiter(int(config.get("request_delay_ms", 1500)))
    processor = MediaProcessor(config.get("download_dir"))
    resolver = EngineResolver(config, limiter)
    manager = DownloadManager(db, config, limiter, resolver, processor)

    window = MainWindow(config, db, limiter, manager, resolver, processor)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
