import subprocess
import sys

import json

import requests
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from backend import ASSETS_DIR, Config, COOKIES_FILE
from backend.update_manager import yt_dlp_command


class SettingsWindow(QDialog):
    def __init__(self, config: Config, log_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.log_cb = log_cb or (lambda msg: None)
        self.setWindowTitle("⚙️ Configuración")
        self.setMinimumSize(520, 390)

        tabs = QTabWidget()
        tabs.addTab(self._spotify_tab(), "🎵 Spotify")
        tabs.addTab(self._network_tab(), "🌐 Red")
        tabs.addTab(self._cookies_tab(), "🍪 Cookies")
        tabs.addTab(self._advanced_tab(), "🔧 Avanzado")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def _spotify_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setVerticalSpacing(10)
        self.sp_id = QLineEdit(str(self.config.get("spotify_client_id") or ""))
        self.sp_id.setPlaceholderText("Client ID de developer.spotify.com")
        self.sp_secret = QLineEdit(str(self.config.get("spotify_client_secret") or ""))
        self.sp_secret.setEchoMode(QLineEdit.EchoMode.Password)
        validate_btn = QPushButton("Validar credenciales")
        validate_btn.clicked.connect(self._validate_spotify)
        self.sp_status = QLabel("")
        row = QHBoxLayout()
        row.addWidget(validate_btn)
        row.addWidget(self.sp_status, 1)
        hint = QLabel(
            "En developer.spotify.com/dashboard crea una app y agrega como "
            "Redirect URI:  https://example.com/callback\n(Titan Downloader "
            "no redirige a ningún lado; solo necesita el Client ID y el "
            "Secret de esa app)")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        form.addRow("Client ID:", self.sp_id)
        form.addRow("Client Secret:", self.sp_secret)
        form.addRow("", row)
        form.addRow("", hint)
        return w

    def _network_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setVerticalSpacing(10)
        self.proxy_edit = QLineEdit(str(self.config.get("proxy") or ""))
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:8080 (opcional)")
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 5)
        self.threads_spin.setValue(int(self.config.get("max_concurrent", 3)))
        form.addRow("Proxy:", self.proxy_edit)
        form.addRow("Descargas simultáneas:", self.threads_spin)
        return w

    def _cookies_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)
        info = QLabel(
            "Exporta las cookies del navegador a cookies.txt para descargar "
            "contenido con restricción de edad o verificación anti-bot.\n"
            "Nota: Chrome 127+ usa cifrado app-bound; si falla, usa Firefox."
        )
        info.setObjectName("hintLabel")
        info.setWordWrap(True)
        chrome_btn = QPushButton("Importar desde Chrome")
        firefox_btn = QPushButton("Importar desde Firefox (recomendado)")
        chrome_btn.clicked.connect(lambda: self._export_cookies("chrome"))
        firefox_btn.clicked.connect(lambda: self._export_cookies("firefox"))
        self.cookies_status = QLabel("")
        paste_hint = QLabel(
            "Pega aquí tus cookies. Formatos aceptados:\n"
            "1) NETSCAPE — una por línea con TABs:  dominio\tTRUE\tpath\t"
            "TRUE\texpiración\tnombre\tvalor\n"
            "2) HEADER — Cookie: nombre=valor; otro=valor2\n"
            "3) JSON — exportado con la extensión «Cookie-Editor» o similar")
        paste_hint.setObjectName("hintLabel")
        paste_hint.setWordWrap(True)
        self.cookies_edit = QPlainTextEdit()
        self.cookies_edit.setPlaceholderText(
            "Pega aquí: Netscape (TABs), header Cookie: ... o JSON "
            "[{\"name\":..., \"value\":...}]"
        )
        self.cookies_edit.setMaximumHeight(150)
        dom_row = QHBoxLayout()
        dom_row.addWidget(QLabel("Dominio para header/JSON sin dominio:"))
        self.cookies_domain = QLineEdit(".youtube.com")
        dom_row.addWidget(self.cookies_domain, 1)
        save_pasted = QPushButton("💾 Guardar cookies pegadas")
        save_pasted.clicked.connect(self._save_pasted_cookies)
        layout.addWidget(info)
        layout.addWidget(chrome_btn)
        layout.addWidget(firefox_btn)
        layout.addWidget(self.cookies_status)
        layout.addWidget(paste_hint)
        layout.addWidget(self.cookies_edit)
        layout.addLayout(dom_row)
        layout.addWidget(save_pasted)
        layout.addStretch(1)
        return w

    def _parse_cookies_unified(self, text):
        text = text.strip()
        if not text:
            return [], "", 0
        if text[0] in "{[":
            try:
                data = json.loads(text)
            except Exception:
                return [], "json-invalido", 0
            entries = []
            skipped = 0
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (str, int, float)):
                        entries.append({"name": str(k),
                                        "value": str(v)})
                    else:
                        skipped += 1
            elif isinstance(data, list):
                for it in data:
                    if not isinstance(it, dict) or "name" not in it:
                        skipped += 1
                        continue
                    entries.append({
                        "name": str(it.get("name", "")),
                        "value": str(it.get("value", "")),
                        "domain": str(it.get("domain", "") or ""),
                        "path": str(it.get("path", "/") or "/"),
                        "secure": bool(it.get("secure", False)),
                        "expiry": it.get("expirationDate")
                                  or it.get("expires") or 0,
                    })
            return entries, "json", skipped

        first = next((l for l in text.splitlines()
                      if l.strip() and not l.strip().startswith("#")), "")
        if "\t" in first:
            entries = []
            bad = 0
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split("\t")
                if len(parts) < 7:
                    bad += 1
                    continue
                entries.append({
                    "domain": parts[0], "path": parts[2],
                    "secure": parts[3].upper() == "TRUE",
                    "expiry": parts[4], "name": parts[5],
                    "value": "\t".join(parts[6:]).strip("\n"),
                })
            return entries, "netscape", bad

        hdr = text
        if hdr.lower().startswith("cookie:"):
            hdr = hdr[7:]
        default_domain = self.cookies_domain.text().strip() or \
            ".youtube.com"
        entries = []
        bad = 0
        for pair in hdr.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                bad += 1
                continue
            name, val = pair.split("=", 1)
            entries.append({
                "name": name.strip(), "value": val.strip(),
                "domain": default_domain, "path": "/",
                "secure": True, "expiry": 0,
            })
        return entries, "header", bad

    @staticmethod
    def _entry_to_netscape(e):
        domain = e.get("domain") or ".youtube.com"
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = e.get("path") or "/"
        sec = "TRUE" if e.get("secure") else "FALSE"
        exp = e.get("expiry") or 0
        try:
            exp = int(float(exp))
        except (TypeError, ValueError):
            exp = 0
        return f"{domain}\t{flag}\t{path}\t{sec}\t{exp}\t{e['name']}\t{e['value']}"

    def _save_pasted_cookies(self):
        text = self.cookies_edit.toPlainText()
        if not text.strip():
            self.cookies_status.setText("⚠ Pega primero tus cookies")
            return
        entries, fmt, bad = self._parse_cookies_unified(text)
        if fmt == "json-invalido":
            self.cookies_status.setText(
                "✖ El texto parece JSON pero está mal formado")
            return
        if not entries:
            hint = f" ({bad} entrada(s) inválida(s))" if bad else ""
            fmt_disp = fmt or "desconocido"
            self.cookies_status.setText(
                f"✖ No se detectaron cookies válidas en formato "
                f"{fmt_disp}{hint}")
            return
        new_map = {}
        for e in entries:
            key = (e.get("domain") or "", e["name"], e.get("path") or "/")
            new_map[key] = self._entry_to_netscape(e)
        existing = {}
        if COOKIES_FILE.exists():
            for line in COOKIES_FILE.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    existing[(parts[0], parts[5], parts[2])] = \
                        "\t".join(parts[:7])
        merged = {**existing, **new_map}
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_text(
            "# Netscape HTTP Cookie File\n" +
            "\n".join(merged.values()) + "\n",
            encoding="utf-8")
        extra = f" — {bad} entrada(s) ignorada(s)" if bad else ""
        self.cookies_status.setText(
            f"✔ {len(entries)} cookies ({fmt}) guardadas en cookies.txt "
            f"(total {len(merged)}).{extra}")
        self.cookies_status.setStyleSheet("color:#66bb6a;")
        self.cookies_edit.clear()

    def _advanced_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setVerticalSpacing(10)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setValue(int(self.config.get("request_delay_ms", 1500)))
        self.delay_spin.setSuffix(" ms")
        self.dir_edit = QLineEdit(str(self.config.get("download_dir")))
        self.normalize_check = QCheckBox("Normalizar volumen (ffmpeg loudnorm)")
        self.normalize_check.setChecked(bool(self.config.get("normalize_audio")))
        form.addRow("Delay entre peticiones:", self.delay_spin)
        form.addRow("Carpeta de destino:", self.dir_edit)
        form.addRow("", self.normalize_check)
        return w

    def _validate_spotify(self):
        cid = self.sp_id.text().strip()
        cs = self.sp_secret.text().strip()
        if not cid or not cs:
            self.sp_status.setText("⚠ Completa ambos campos")
            self.sp_status.setStyleSheet("color:#ffb74d;")
            return
        try:
            r = requests.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(cid, cs), timeout=10)
            if r.status_code == 200:
                self.sp_status.setText("✔ Credenciales válidas")
                self.sp_status.setStyleSheet("color:#66bb6a;")
            else:
                self.sp_status.setText(f"✖ Rechazadas ({r.status_code})")
                self.sp_status.setStyleSheet("color:#ef5350;")
        except Exception as e:
            self.sp_status.setText(f"✖ Error de red: {e}")
            self.sp_status.setStyleSheet("color:#ef5350;")

    def _export_cookies(self, browser):
        cmd = yt_dlp_command()
        if cmd is None:
            self.cookies_status.setText(
                "✖ Usa '⚡ Actualizar Motores' primero (falta binario yt-dlp).")
            return
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        full_cmd = cmd + [
            "--cookies-from-browser", browser,
            "--cookies", str(COOKIES_FILE),
            "https://www.youtube.com/",
        ]
        self.cookies_status.setText(f"⏳ Exportando cookies de {browser}...")
        try:
            proc = subprocess.run(full_cmd, capture_output=True, text=True,
                                  timeout=120,
                                  creationflags=(0x08000000 if sys.platform == "win32" else 0))
            if proc.returncode == 0 and COOKIES_FILE.exists():
                self.cookies_status.setText(f"✔ Guardado en {COOKIES_FILE}")
            else:
                tail = ((proc.stderr or proc.stdout or "")[-300:])
                self.cookies_status.setText(f"✖ Fallo: {tail}")
        except Exception as e:
            self.cookies_status.setText(f"✖ Error: {e}")

    def accept(self):
        self.config.set("spotify_client_id", self.sp_id.text().strip())
        self.config.set("spotify_client_secret", self.sp_secret.text().strip())
        self.config.set("proxy", self.proxy_edit.text().strip())
        self.config.set("max_concurrent", int(self.threads_spin.value()))
        self.config.set("request_delay_ms", int(self.delay_spin.value()))
        self.config.set("download_dir", self.dir_edit.text().strip())
        self.config.set("normalize_audio", bool(self.normalize_check.isChecked()))
        super().accept()
