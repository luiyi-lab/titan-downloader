import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

ARGS = [
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", "TitanDownloader",
    "--collect-all", "yt_dlp",
    # assets NO va embebido en _internal: el paso post-build lo copia
    # junto al exe, que es donde la app congelada lo busca.
    str(ROOT / "main.py"),
]


def main():
    lock = ROOT / "dist" / "TitanDownloader" / "data" / "titan.db"
    if lock.exists():
        try:
            with open(lock, "a"):
                pass
        except PermissionError:
            print("✖ TitanDownloader está abierto y bloquea su base de "
                  "datos.\n  Ciérralo (bandeja → Salir) y vuelve a "
                  "compilar.")
            raise SystemExit(1)
    cmd = [sys.executable, "-m", "PyInstaller"] + ARGS
    if sys.platform != "win32":
        args = []
        for a in ARGS:
            if a == "--windowed":
                continue
            args.append(a.replace(";", ":") if "assets" in a and ";" in a else a)
        cmd = [sys.executable, "-m", "PyInstaller"] + args
    print("Ejecutando:", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0 and sys.platform == "win32":
        # PyInstaller 6 coloca los datas en _internal/, pero la app (congelada)
        # busca assets/ junto al exe. Copiarlos ahí evita el fallback de icono
        # y deja lista la carpeta donde el updater instala ffmpeg/yt-dlp.
        dest = ROOT / "dist" / "TitanDownloader" / "assets"
        shutil.copytree(ROOT / "assets", dest, dirs_exist_ok=True)
        print(f"✔ assets copiados a {dest}")
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
