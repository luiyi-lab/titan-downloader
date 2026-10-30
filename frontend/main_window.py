import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QTableView,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from backend import FFMPEG_FILE, Config
from backend.database import DatabaseManager
from backend.download_manager import DownloadManager
from backend.engine_resolver import EngineResolver
from backend.processor import MediaProcessor
from backend.rate_limiter import RateLimiter
from backend.update_manager import UpdateManager

from .queue_table_model import (
    ROLE_CANCELLABLE,
    ROLE_ITEM_ID,
    QueueTableModel,
)
from .settings_window import SettingsWindow
from .tray_icon import TrayIcon, build_icon

COLORS = {
    "info": "#90caf9",
    "success": "#66bb6a",
    "fallback": "#ffa726",
    "warn": "#ffd54f",
    "error": "#ef5350",
}

QUALITIES = ["MP3 320kbps", "MP3 192kbps", "Video 1080p", "Video 720p"]


class MainWindow(QMainWindow):
    resolved = Signal(list, str)

    def __init__(self, config: Config, db: DatabaseManager, limiter: RateLimiter,
                 manager: DownloadManager, resolver: EngineResolver,
                 processor: MediaProcessor):
        super().__init__()
        self.config = config
        self.db = db
        self.limiter = limiter
        self.manager = manager
        self.resolver = resolver
        self.processor = processor
        self.setWindowTitle("Titan Downloader")
        self.setMinimumSize(880, 560)
        self.resize(1024, 640)
        self.setWindowIcon(build_icon())
        self._updating_engines = False
        self._auto_update_pending = False
        self._first_hide = True
        self._versions_pending_text = None
        self._bg_confirmed = False
        self._notif = {"ok": 0, "fail": 0}

        self.model = QueueTableModel(db, self)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(9)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("⬇ Titan Downloader")
        title.setObjectName("appTitle")
        self.lbl_versions = QLabel("")
        self.lbl_versions.setObjectName("lblVersions")
        self.btn_import = QPushButton("📄 Importar")
        self.btn_import.setToolTip(
            "Importa un CSV de Exportify o un TXT con una URL por línea")
        self.btn_update = QPushButton("⚡ Actualizar")
        self.btn_update.setToolTip(
            "Descarga la versión más reciente de yt-dlp (y FFmpeg si falta)")
        btn_settings = QPushButton("⚙️")
        btn_settings.setToolTip("Configuración: credenciales, red, cookies")
        btn_settings.setFixedWidth(44)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.lbl_versions)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_update)
        top.addWidget(btn_settings)
        root.addLayout(top)

        entry = QHBoxLayout()
        entry.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlInput")
        self.url_edit.setPlaceholderText(
            "Pega aquí una URL de YouTube, Spotify, TikTok…")
        btn_paste = QPushButton("📋")
        btn_paste.setToolTip("Pegar desde el portapapeles y agregar a la cola")
        btn_paste.setFixedWidth(44)
        entry.addWidget(self.url_edit, 1)
        entry.addWidget(btn_paste)
        root.addLayout(entry)

        status_row = QHBoxLayout()
        status_row.setSpacing(14)
        self.lbl_status = QLabel(
            "Progreso: [0 / 0] · Restante: — · Velocidad: 0 B/s")
        self.lbl_status.setObjectName("lblStatus")
        self.global_bar = QProgressBar()
        self.global_bar.setRange(0, 100)
        status_row.addWidget(self.lbl_status, 1)
        status_row.addWidget(self.global_bar, 1)
        root.addLayout(status_row)

        self.table = QTableView()
        self.table.setObjectName("queueTable")
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 46)
        header.resizeSection(1, 390)
        header.resizeSection(2, 102)
        header.resizeSection(3, 56)
        header.resizeSection(4, 76)
        header.resizeSection(5, 92)
        header.resizeSection(6, 70)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (0, 3, 6):   # ID, Motor, Reintentos: ocultos por defecto
            self.table.hideColumn(col)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._queue_context_menu)
        root.addWidget(self.table, 3)

        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText("Los eventos de descarga aparecerán aquí…")
        self.logs.setVisible(False)
        root.addWidget(self.logs, 2)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.btn_start = QPushButton("▶️  Iniciar")
        self.btn_start.setObjectName("btnPrimary")
        self.btn_pause = QPushButton("⏸️ Pausar")
        self.btn_pause.setEnabled(False)
        self.shutdown_check = QCheckBox("🛑 Apagar al terminar")
        self.shutdown_check.setChecked(bool(config.get("shutdown_after")))
        self.btn_log = QToolButton()
        self.btn_log.setText("📝 Historial")
        self.btn_log.setCheckable(True)
        self.btn_log.setToolTip("Mostrar u ocultar el registro de eventos")
        btn_folder = QPushButton("📂")
        btn_folder.setToolTip(
            f"Abrir carpeta de descargas: {config.get('download_dir')}")
        btn_folder.setFixedWidth(44)
        controls.addWidget(self.btn_start)
        controls.addWidget(self.btn_pause)
        controls.addWidget(self.shutdown_check)
        controls.addStretch(1)
        self.btn_clear = QPushButton("🗑️ Limpiar")
        self.btn_clear.setToolTip(
            "Elimina de la cola los ítems completados, fallidos y cancelados")
        self.btn_vaciar = QPushButton("💣 Vaciar todo")
        self.btn_vaciar.setObjectName("btnDanger")
        self.btn_vaciar.setToolTip(
            "Elimina TODOS los ítems de la cola (activos, pendientes, "
            "fallidos y completados) y aborta descargas en curso")
        controls.addWidget(self.btn_clear)
        controls.addWidget(self.btn_vaciar)
        controls.addWidget(self.btn_log)
        controls.addWidget(btn_folder)
        root.addLayout(controls)

        self.setCentralWidget(central)

        self.tray = TrayIcon(self)
        self.tray.show_requested.connect(self._show_from_tray)
        self.tray.pause_toggle_requested.connect(self._toggle_pause)
        self.tray.quit_requested.connect(self._quit_app)

        btn_paste.clicked.connect(self._paste_url)
        self.url_edit.returnPressed.connect(self._add_url)
        btn_settings.clicked.connect(self._open_settings)
        btn_folder.clicked.connect(self._open_folder)
        self.btn_import.clicked.connect(self._import_file)
        self.btn_update.clicked.connect(lambda: self._update_engines())
        self.btn_log.toggled.connect(self.logs.setVisible)
        self.btn_clear.clicked.connect(self._clear_completed)
        self.btn_vaciar.clicked.connect(self._vaciar_todo)
        self.btn_start.clicked.connect(self._start_queue)
        self.btn_pause.clicked.connect(self._pause_all)
        self.shutdown_check.toggled.connect(
            lambda v: config.set("shutdown_after", v))
        self.table.clicked.connect(self._table_clicked)

        manager.log.connect(self.append_log)
        limiter.warning.connect(lambda msg: self.append_log(msg, "warn"))
        manager.finished_all.connect(self._on_finished_all)
        manager.item_finished.connect(self._note_item)
        self.resolved.connect(self._on_resolved)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(800)
        self.refresh_timer.timeout.connect(self._refresh_ui)
        self.refresh_timer.start()

        orphaned = db.recover_orphans()
        self._startup_orphans = orphaned
        if orphaned:
            manager.log.emit(
                f"♻ {orphaned} ítems 'downloading' huérfanos vueltos a pendiente",
                "info")

        self.tray.show()
        self._notif_timer = QTimer(self)
        self._notif_timer.setInterval(7000)
        self._notif_timer.timeout.connect(self._flush_tray_notes)
        self._notif_timer.start()

        QTimer.singleShot(250, self._load_versions)
        QTimer.singleShot(400, self._startup_notice)

    def _show_from_tray(self):
        self.showNormal()
        self.activateWindow()

    def _toggle_pause(self):
        if not self.manager._running:
            return
        if self.manager._paused:
            self.manager.resume()
            self.btn_pause.setText("⏸️ Pausar")
        else:
            self.manager.pause()
            self.btn_pause.setText("⏸️ Pausado (reanudar)")
        self.tray.set_paused(self.manager._paused)

    def _paste_url(self):
        text = QApplication.clipboard().text().strip()
        if not text:
            self.append_log("⚠ El portapapeles está vacío", "warn")
            return
        self.url_edit.setText(text.split()[0])
        self._add_url()

    def _add_url(self):
        url = self.url_edit.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            self.append_log("⚠ Ingresa una URL válida (http/https)", "warn")
            return
        self.url_edit.clear()
        self.append_log(f"🔎 Analizando: {url}", "info")
        threading.Thread(target=self._resolve_thread,
                         args=(url,), daemon=True).start()

    def _resolve_thread(self, url):
        try:
            items = self.resolver.expand_url(url)
            self.resolved.emit(items, "")
        except Exception as e:
            self.resolved.emit([], str(e))

    def _choose_format_dialog(self, items):
        """Modal: muestra qué se detectó y pide el formato. Devuelve la
        calidad elegida o None si el usuario cancela."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Agregar descarga")
        dlg.setMinimumWidth(470)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)
        n = len(items)
        if n == 1:
            head = QLabel("🎬 Se detectó 1 video")
        else:
            head = QLabel(f"📋 Se detectó una playlist con {n} videos")
        head.setObjectName("appTitle")
        lay.addWidget(head)
        preview = QListWidget()
        preview.setAlternatingRowColors(True)
        for it in items[:6]:
            title = (it.get("title") or it["url"])[:95]
            preview.addItem(title)
        if n > 6:
            preview.addItem(f"… y {n - 6} más")
        preview.setMaximumHeight(160)
        lay.addWidget(preview, 1)
        form = QHBoxLayout()
        form.addWidget(QLabel("Formato:"))
        combo = QComboBox()
        combo.addItems(QUALITIES)
        combo.setCurrentText(str(self.config.get("quality")))
        form.addWidget(combo, 1)
        lay.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.addButton("➕ Agregar a la cola",
                          QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancelar",
                          QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return combo.currentText()
        return None

    def _on_resolved(self, items, error):
        if error or not items:
            msg = f"✖ No se pudo resolver la URL: {error or 'sin resultados'}"
            self.append_log(msg, "error")
            return
        quality = self._choose_format_dialog(items)
        if quality is None:
            self.append_log("ℹ Agregado cancelado", "info")
            return
        self.config.set("quality", quality)
        for it in items:
            it["quality"] = quality
        added, updated = self.db.add_items(items)
        self.model.reload()
        parts = []
        if added:
            noun = "ítem" if added == 1 else "ítems"
            parts.append(f"➕ {added} {noun} nuevo(s)")
        if updated:
            parts.append(f"🔄 {updated} actualizado(s) de formato")
        skipped = len(items) - added - updated
        if skipped:
            parts.append(f"{skipped} ya estaban igual")
        self.append_log(" · ".join(parts), "success")

    def _queue_context_menu(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        item_id = self.model.data(idx, ROLE_ITEM_ID)
        row = self.db.get_item(item_id)
        if not row:
            return
        menu = QMenu(self)
        bulk = menu.addMenu("🌍 Cambiar formato de TODA la cola")
        for q in QUALITIES:
            if q == str(self.config.get("quality")):
                act = bulk.addAction(f"✔  {q}")
                act.setEnabled(False)
            else:
                bulk.addAction(q, lambda qq=q: self._change_quality_bulk(qq))
        redo = menu.addMenu("♻️ RE-descargar TODO (incluye completados)")
        redo.setToolTip(
            "Reencola incluso los ya descargados para bajarlos en el "
            "formato elegido (los archivos viejos permanecen en la carpeta)")
        for q in QUALITIES:
            redo.addAction(q, lambda qq=q: self._redownload_all(qq))
        menu.addSeparator()
        if row["status"] in ("completed", "downloading"):
            if row["status"] == "downloading":
                act = menu.addAction("En descarga — sin acciones")
                act.setEnabled(False)
            else:
                sub = menu.addMenu("♻️ Re-descargar como")
                for q in QUALITIES:
                    if q == row["quality"]:
                        act = sub.addAction(f"✔  {q}")
                        act.setEnabled(False)
                    else:
                        sub.addAction(
                            q, lambda i=item_id, qq=q:
                            self._change_quality(i, qq))
        else:
            sub = menu.addMenu("🔁 Cambiar formato a")
            for q in QUALITIES:
                if q == row["quality"]:
                    act = sub.addAction(f"✔  {q}")
                    act.setEnabled(False)
                else:
                    sub.addAction(
                        q,
                        lambda i=item_id, qq=q: self._change_quality(i, qq))
            if row["status"] in ("failed", "cancelled"):
                menu.addSeparator()
                menu.addAction("▶️ Reencolar",
                               lambda i=item_id: self._requeue(i))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _change_quality(self, item_id, quality):
        if self.db.update_quality(item_id, quality):
            self.manager.mark_dirty(item_id)
            row = self.db.get_item(item_id)
            if row and row["status"] == "pending" and not row["file_path"]:
                extra = " — se re-descargará al iniciar la cola"
            else:
                extra = " — se aplicará al descargar"
            self.append_log(
                f"🔁 Formato cambiado a {quality} (ID {item_id}){extra}",
                "info")

    def _redownload_all(self, quality):
        stats = self.manager.get_global_stats()
        total = stats.get("total", 0)
        done = stats.get("completed", 0)
        if total == 0:
            self.append_log("ℹ La cola está vacía", "warn")
            return
        ret = QMessageBox.question(
            self,
            "Re-descargar toda la cola",
            f"¿Reencolar los {total} ítem(s) para descargarlos como "
            f"«{quality}»?\n\nIncluye {done} ya completado(s): volverán a "
            "bajarse en el nuevo formato.\nLos archivos anteriores NO se "
            "borran de la carpeta.\nLas descargas ya iniciadas terminarán "
            "en su formato original.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.manager.pause()
        n = self.db.bulk_change_quality(quality, include_completed=True)
        self.model.reload()
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸️ Pausar")
        self.tray.set_paused(False)
        self.append_log(
            f"♻️ {n} ítems reencolados como «{quality}». Pulsa ▶️ Iniciar "
            f"para comenzar la re-descarga", "warn")

    def _change_quality_bulk(self, quality):
        stats = self.manager.get_global_stats()
        elegibles = (stats.get("pending", 0) + stats.get("retrying", 0)
                     + stats.get("failed", 0) + stats.get("cancelled", 0))
        if elegibles == 0:
            self.append_log("ℹ No hay ítems modificables (solo completados "
                            "o en descarga)", "warn")
            return
        ret = QMessageBox.StandardButton.Yes
        if elegibles > 10:
            ret = QMessageBox.question(
                self,
                "Cambiar formato masivo",
                f"¿Cambiar el formato de {elegibles} ítem(s) a "
                f"«{quality}»?\n\nLos completados y las descargas en curso "
                "no se tocan. Los fallidos/cancelados se reencolan.",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        n = self.db.bulk_change_quality(quality)
        self.model.reload()
        self.append_log(
            f"🌍 {n} ítems cambiados a «{quality}» en toda la cola",
            "success")

    def _requeue(self, item_id):
        self.db.update_status(item_id, "pending", error_log="")
        self.manager.mark_dirty(item_id)
        self.append_log(f"▶️ Ítem {item_id} reencolado", "info")

    def _open_settings(self):
        dlg = SettingsWindow(self.config, parent=self)
        if dlg.exec():
            self.limiter.set_delay(int(self.config.get("request_delay_ms")))
            self.manager.set_max_threads(int(self.config.get("max_concurrent")))
            self.append_log("⚙️ Configuración guardada", "success")

    def _clear_completed(self):
        n = self.db.clear_completed()
        self.model.reload()
        self.append_log(f"🗑️ {n} ítems terminados eliminados (completados, "
                        f"fallidos y cancelados)", "info")

    def _vaciar_todo(self):
        stats = self.manager.get_global_stats()
        total = stats.get("total", 0)
        if total == 0:
            self.append_log("ℹ La cola ya está vacía", "info")
            return
        activos = stats.get("active", 0)
        detalle = f"{total} ítem(s)"
        if activos:
            detalle += f" (incluye {activos} descarga(s) en curso, se "
            detalle += "abortarán)"
        ret = QMessageBox.warning(
            self,
            "Vaciar toda la cola",
            f"¿Eliminar {detalle}?\n\nSe borrarán pendientes, descargas en "
            "curso, reintentos, fallidos y completados. Los archivos ya "
            "descargados NO se tocan.\n\nEsta acción no se puede deshacer.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.manager.stop()
        n = self.db.clear_all()
        self.model.reload()
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸️ Pausar")
        self.tray.set_paused(False)
        self._flush_tray_notes()
        self.append_log(f"💣 Cola vaciada: {n} ítems eliminados por completo",
                        "warn")

    def _import_file(self):
        path, _f = QFileDialog.getOpenFileName(
            self, "Importar playlist de Spotify",
            "", "CSV de Spotify (*.csv);;Listas (*.csv *.txt);;Todos (*)")
        if not path:
            return
        self._import_from_path(path)

    def _import_from_path(self, path):
        p = Path(path)
        try:
            if p.suffix.lower() == ".csv":
                items = self._parse_csv(p)
            else:
                items = self._parse_txt(p)
        except Exception as e:
            self.append_log(f"✖ No se pudo leer el archivo: {e}", "error")
            return
        if not items:
            self.append_log("⚠ El archivo no contiene pistas reconocibles "
                            "(¿exportaste con Exportify?)", "warn")
            return
        added, updated = self.db.add_items(items)
        self.model.reload()
        skipped = len(items) - added - updated
        msg = f"📄 Importados {added} ítems desde {p.name}"
        if skipped:
            msg += f" ({skipped} duplicados omitidos)"
        self.append_log(msg, "success")

    def _parse_csv(self, path):
        import csv
        items = []

        def norm(h):
            return re.sub(r"[^a-z0-9]", "", str(h or "").lower())

        with open(path, newline="", encoding="utf-8-sig") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(fh, dialect=dialect)
            cols = {norm(c): c for c in (reader.fieldnames or []) if c}

            def col(*names):
                for n in names:
                    key = norm(n)
                    if key in cols:
                        return cols[key]
                return None

            c_id = col("Spotify ID", "Track ID", "Spotify - id", "ID",
                       "URI")
            c_name = col("Track Name", "Name", "Title")
            c_artists = col("Artist Name(s)", "Artist Name", "Artists",
                            "Artist")
            if not c_id and not (c_name and c_artists):
                raise ValueError(
                    f"columnas no reconocidas: {reader.fieldnames}")
            for row in reader:
                tid = ""
                if c_id:
                    raw_id = str(row.get(c_id, "") or "").strip()
                    m = re.search(
                        r"(?:spotify[:/]+(?:track/)?|)([A-Za-z0-9]{22})\b",
                        raw_id)
                    tid = m.group(1) if m else ""
                    if not tid and len(raw_id) == 22:
                        tid = raw_id
                if not tid:
                    continue
                name = str(row.get(c_name, "") or "").strip() \
                    if c_name else ""
                artists = str(row.get(c_artists, "") or "").strip() \
                    if c_artists else ""
                label = f"{artists} - {name}".strip(" -") or name
                items.append({
                    "url": f"https://open.spotify.com/track/{tid}",
                    "title": label,
                    "platform": "spotify",
                    "quality": str(self.config.get("quality")),
                })
        return items

    def _parse_txt(self, path):
        import re as _re
        items = []
        seen = set()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                m = _re.search(
                    r"(?:track/)([A-Za-z0-9]{16,})"
                    r"|youtu(?:\.be/|be\.com/watch\?v=)([\w-]{6,})",
                    line)
                if not m:
                    continue
                sid, yid = m.groups()
                if sid:
                    if sid in seen:
                        continue
                    seen.add(sid)
                    items.append({
                        "url": f"https://open.spotify.com/track/{sid}",
                        "title": "",
                        "platform": "spotify",
                        "quality": str(self.config.get("quality")),
                    })
                elif yid:
                    if yid in seen:
                        continue
                    seen.add(yid)
                    items.append({
                        "url": f"https://www.youtube.com/watch?v={yid}",
                        "title": "",
                        "platform": "youtube",
                        "quality": str(self.config.get("quality")),
                    })
        return items

    def _open_folder(self):
        d = Path(str(self.config.get("download_dir")))
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _update_engines(self, force_ffmpeg=False):
        if self._updating_engines:
            return
        self._updating_engines = True
        self.btn_update.setEnabled(False)
        self.append_log("⚡ Actualizando motores...", "info")
        self._updater = UpdateManager(force_ffmpeg=force_ffmpeg, parent=self)
        self._updater.output.connect(lambda line: self.append_log(line, "info"))
        self._updater.update_finished.connect(self._on_update_finished)
        self._updater.start()

    def _on_update_finished(self, ok):
        self._updating_engines = False
        self.btn_update.setEnabled(True)
        if ok:
            self.append_log("✔ Motores actualizados", "success")
        else:
            self.append_log("✖ La actualización terminó con errores", "error")
            if not self.logs.isVisible():
                self.btn_log.setChecked(True)
        self._load_versions()

    def _start_queue(self):
        if not (FFMPEG_FILE.exists() or shutil.which("ffmpeg")):
            ret = QMessageBox.warning(
                self,
                "FFmpeg no disponible",
                "No se encontró FFmpeg (es necesario para convertir y "
                "etiquetar los archivos).\n\n¿Quieres descargarlo ahora? "
                "Se guardará en assets/ automáticamente.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                self._update_engines(force_ffmpeg=True)
            return
        if self.manager._running and self.manager._paused:
            self.manager.resume()
        elif not self.manager._running:
            self.manager.start()
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸️ Pausar")
        self.tray.set_paused(False)

    def _pause_all(self):
        self.manager.pause()
        self.tray.set_paused(True)

    def _table_clicked(self, index):
        if index.column() != 7:
            return
        item_id = self.model.data(index, ROLE_ITEM_ID)
        cancellable = self.model.data(index, ROLE_CANCELLABLE)
        if cancellable and self.db.cancel_item(item_id):
            self.append_log(f"🛑 Cancelación solicitada (ID {item_id})", "warn")

    def append_log(self, msg, color="info"):
        hexcolor = COLORS.get(color, COLORS["info"])
        safe = (str(msg).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
        self.logs.append(f'<span style="color:{hexcolor};">{safe}</span>')

    def _refresh_ui(self):
        ids = self.manager.pop_dirty()
        if ids:
            self.model.refresh_ids(ids)
        if self._auto_update_pending and not self._updating_engines:
            self._auto_update_pending = False
            self.append_log("⚡ Nueva versión de yt-dlp detectada — "
                            "actualizando automáticamente", "info")
            self._update_engines()
        if self._versions_pending_text is not None:
            self.lbl_versions.setText(self._versions_pending_text)
            self._versions_pending_text = None
        stats = self.manager.get_global_stats()
        total = stats.get("total", 0)
        completed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        remaining = stats.get("remaining", 0)
        speed = self.manager.speed_bytes_per_sec()
        eta_txt = "—"
        eta = stats.get("eta_sec")
        if eta:
            h, mdiv = divmod(eta, 3600)
            m, s = divmod(mdiv, 60)
            eta_txt = f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")
        self.lbl_status.setText(
            f"Progreso: [{completed} / {total}] · Restante: {remaining} ítems · "
            f"ETA: {eta_txt} · Velocidad: {self._human_speed(speed)}")
        done_units = completed + failed + stats.get("cancelled", 0)
        pct = int(done_units * 100 / total) if total else 0
        self.global_bar.setValue(pct)

    @staticmethod
    def _human_speed(nbytes):
        value = float(nbytes)
        for unit in ("B/s", "KB/s", "MB/s"):
            if value < 1024 or unit == "MB/s":
                fmt = f"{int(value)} {unit}" if unit == "B/s" else \
                    f"{value:.1f} {unit}"
                return fmt
            value /= 1024.0
        return f"{value:.2f} GB/s"

    def _on_finished_all(self):
        self._flush_tray_notes()
        self.tray.showMessage(
            "Titan Downloader",
            "🎉 Todas las descargas terminaron",
            QSystemTrayIcon.MessageIcon.Information, 6000)
        self.append_log("🎉 Cola terminada: no quedan ítems pendientes",
                        "success")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸️ Pausar")
        if self.shutdown_check.isChecked():
            self.append_log("🖥️ Apagando el equipo en 60 segundos...", "warn")
            subprocess.Popen(["shutdown", "/s", "/t", "60"])

    def _load_versions(self):
        def work():
            ytdlp_ver = "?"
            ffmpeg_state = "✗"
            try:
                import yt_dlp
                ytdlp_ver = yt_dlp.version.__version__
            except Exception:
                pass
            from backend import ASSETS_DIR, FFMPEG_FILE
            if FFMPEG_FILE.exists() or shutil.which("ffmpeg"):
                ffmpeg_state = "✓"
            # En modo empaquetado el motor real es assets/yt-dlp.exe:
            # mostrar SU versión, no la bundled dentro del exe.
            if getattr(sys, "frozen", False):
                exe = ASSETS_DIR / ("yt-dlp.exe" if sys.platform == "win32"
                                    else "yt-dlp")
                if exe.exists():
                    try:
                        proc = subprocess.run(
                            [str(exe), "--version"], capture_output=True,
                            text=True, timeout=30,
                            creationflags=(0x08000000
                                           if sys.platform == "win32"
                                           else 0))
                        exe_ver = proc.stdout.strip()
                        if exe_ver:
                            ytdlp_ver = exe_ver
                    except Exception:
                        pass
            # ¿Hay versión nueva publicada?
            note = ""
            try:
                from backend.update_manager import latest_pypi_version
                latest = latest_pypi_version("yt-dlp")
                if latest and latest != str(ytdlp_ver).strip():
                    note = f"  ⚡ v{latest} disponible"
                    self._auto_update_pending = True
            except Exception:
                pass
            self._versions_pending_text = \
                f"yt-dlp: v{ytdlp_ver}{note} | FFmpeg: {ffmpeg_state}"
        threading.Thread(target=work, daemon=True).start()

    def _note_item(self, title, ok):
        self._notif["ok" if ok else "fail"] += 1

    def _flush_tray_notes(self):
        ok = self._notif["ok"]
        fail = self._notif["fail"]
        if not (ok or fail):
            return
        self._notif = {"ok": 0, "fail": 0}
        parts = []
        if ok:
            parts.append(f"✅ {ok} descarga(s) completada(s)")
        if fail:
            parts.append(f"❌ {fail} descarga(s) fallida(s)")
        self.tray.showMessage("Titan Downloader", "\n".join(parts),
                              QSystemTrayIcon.MessageIcon.Information, 5000)

    def _startup_notice(self):
        stats = self.manager.get_global_stats()
        pending = stats.get("pending", 0) + stats.get("retrying", 0)
        orphans = getattr(self, "_startup_orphans", 0)
        if not (orphans or pending):
            return
        lines = []
        if orphans:
            lines.append(
                f"♻ Se recuperaron {orphans} descargas que quedaron a medias "
                f"por un cierre inesperado y volverán a intentarse.")
        lines.append(f"📥 Tienes {pending} ítem(s) pendientes en la cola.")
        lines.append("\nPulsa ▶️ Iniciar / Reanudar para continuar donde "
                     "quedaste.")
        QMessageBox.information(self, "Titan Downloader — Sesión anterior",
                                "\n".join(lines))

    def _downloads_in_flight(self):
        stats = self.manager.get_global_stats()
        active = stats.get("active", 0) > 0 or (
            self.manager.is_running and
            (stats.get("pending", 0) + stats.get("retrying", 0)) > 0)
        return active

    def _ask_background(self):
        box = QMessageBox(self)
        box.setWindowTitle("Descargas en curso")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Hay descargas en curso o pendientes.\n¿Qué quieres "
                    "hacer al cerrar la ventana?")
        bg = box.addButton("Continuar en segundo plano",
                           QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton("Detener y salir",
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(bg)
        box.exec()
        if box.clickedButton() is bg:
            return "background"
        if box.clickedButton() is stop:
            return "stop"
        return "cancel"

    def closeEvent(self, event):
        if not self.tray.isVisible():
            self.manager.stop()
            super().closeEvent(event)
            return
        if self._downloads_in_flight() and not self._bg_confirmed:
            choice = self._ask_background()
            if choice == "cancel":
                event.ignore()
                return
            if choice == "stop":
                self.manager.stop()
                self.tray.hide()
                QApplication.quit()
                event.accept()
                return
            self._bg_confirmed = True
        event.ignore()
        self.hide()
        if self._first_hide:
            self.tray.showMessage(
                "Titan Downloader",
                "Sigue descargando en segundo plano.",
                QSystemTrayIcon.MessageIcon.Information,
                3000)
            self._first_hide = False

    def _quit_app(self):
        self.tray.hide()
        self.manager.stop()
        QApplication.quit()
