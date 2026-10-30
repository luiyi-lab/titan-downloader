from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from backend.database import DatabaseManager

BATCH_SIZE = 300

COLUMNS = ["ID", "Título", "Plataforma", "Motor", "Progreso", "Estado",
           "Reintentos", "Acción"]

STATUS_ES = {
    "pending": "Pendiente",
    "downloading": "Descargando",
    "retrying": "En espera",
    "completed": "Completado",
    "failed": "Fallido",
    "cancelled": "Cancelado",
}

ENGINE_CODE = {
    "yt-dlp": "P",
    "yt-dlp#2": "S",
    "meta→yt-dlp": "P",
    "piped": "R",
    "tikwm": "T",
    "spotdl": "P",
    "spotipy→yt-dlp": "R",
    "scraper": "R",
}

PLATFORM_ICON = {
    "youtube": "▶ YouTube",
    "spotify": "🎵 Spotify",
    "facebook": "📘 Facebook",
    "tiktok": "📱 TikTok",
    "generic": "🌐 Web",
}

CANCELABLE = ("pending", "downloading", "retrying")

COL_ACTION = 7
ROLE_CANCELLABLE = Qt.ItemDataRole.UserRole + 1
ROLE_ITEM_ID = Qt.ItemDataRole.UserRole + 2


class QueueTableModel(QAbstractTableModel):
    def __init__(self, db: DatabaseManager, parent=None):
        super().__init__(parent)
        self.db = db
        self._rows = []
        self._loaded = 0
        self._total = 0
        self._row_by_id = {}
        self.reload()

    def reload(self):
        self.beginResetModel()
        self._rows = []
        self._loaded = 0
        self._total = self.db.count_total()
        self._row_by_id = {}
        self.endResetModel()
        self.fetchMore(QModelIndex())

    def refresh_ids(self, ids):
        for item_id in ids:
            row = self._row_by_id.get(item_id)
            if row is None:
                continue
            fresh = self.db.get_item(item_id)
            if fresh is None:
                continue
            self._rows[row] = fresh
            self.dataChanged.emit(self.index(row, 0),
                                  self.index(row, len(COLUMNS) - 1))

    def canFetchMore(self, parent=QModelIndex()):
        if parent.isValid():
            return False
        return self._loaded < self._total

    def fetchMore(self, parent=QModelIndex()):
        if parent.isValid():
            return
        count = min(BATCH_SIZE, self._total - self._loaded)
        if count <= 0:
            return
        page = self.db.rows_page(self._loaded, count)
        self.beginInsertRows(QModelIndex(), self._loaded,
                             self._loaded + len(page) - 1)
        for r in page:
            self._row_by_id[r["id"]] = len(self._rows)
            self._rows.append(r)
        self._loaded += len(page)
        self.endInsertRows()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == ROLE_ITEM_ID:
            return row["id"]
        if role == ROLE_CANCELLABLE:
            return col == COL_ACTION and row["status"] in CANCELABLE
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return str(row["id"])
            if col == 1:
                title = row["title"] or "(resolviendo...)"
                return f"{title}   [{row['quality']}]"
            if col == 2:
                return PLATFORM_ICON.get(row["platform"], row["platform"])
            if col == 3:
                engine = (row["engine_used"] or "").strip()
                if not engine:
                    return "—"
                return ENGINE_CODE.get(engine, "?")
            if col == 4:
                status = row["status"]
                if status == "downloading":
                    return f"{int(row['progress'] or 0)}%"
                if status == "completed":
                    return "100%"
                return "0%"
            if col == 5:
                return STATUS_ES.get(row["status"], row["status"])
            if col == 6:
                return f"{row['current_retry']}/{row['max_retries']}"
            if col == 7:
                return "✖" if row["status"] in CANCELABLE else ""
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (4, 6, 7):
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            status = row["status"]
            if status == "completed":
                from PySide6.QtGui import QColor
                return QColor("#4caf50")
            if status == "failed":
                from PySide6.QtGui import QColor
                return QColor("#ef5350")
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == COL_ACTION:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base
