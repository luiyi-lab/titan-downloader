# Titan Downloader

![Estado](https://img.shields.io/badge/estado-en_desarrollo_activo-yellow) ![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white) ![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52) ![Licencia](https://img.shields.io/badge/licencia-MIT-blue)

> 🚧 **Proyecto en desarrollo activo — no es una versión final.** Se sigue
> corrigiendo y mejorando constantemente; las funciones y la interfaz pueden
> cambiar entre versiones.

Descargador de escritorio para Windows que convierte enlaces de **YouTube,
Spotify, TikTok, Facebook y sitios genéricos** en archivos **MP3 etiquetados o
video MP4**, con cola de descargas, reintentos con backoff y una cadena de
motores de respaldo para máxima tolerancia a fallos.

Pega una URL, elige el formato en el diálogo y deja que la cola trabaje.

## Características

- 📋 **Flujo simple**: pegar URL → diálogo con vista previa (video o playlist
  completa) → elegir formato → cola automática
- 🎵 **MP3 con etiquetas ID3** completas (título, artista, álbum, portada) vía
  mutagen, o **MP4** por formato
- 🔗 **Playlists y álbumes completos** de Spotify (API propia o resguardo por
  embed/oEmbed), canales y listas de YouTube
- 🔁 **Cadena de motores de respaldo**: yt-dlp (con variantes de cliente) →
  Piped (múltiples instancias) → scraper genérico. Si uno falla, sigue el
  siguiente
- 🛡️ **Validación de medios**: firma binaria de todo archivo descargado —
  nunca se marca "completado" un archivo corrupto
- ⏱️ **Rate limiting adaptativo**: el delay crece automáticamente ante
  errores 429
- 💾 **Cola persistente en SQLite** (WAL) con reintentos, backoff
  exponencial y recuperación de descargas huérfanas al reabrir
- ⚡ **Motores auto-actualizables**: yt-dlp y FFmpeg se actualizan solos solo
  cuando hay versión nueva publicada
- 🖥️ **Instancia única**, bandeja del sistema, apagado programado al
  terminar
- 🍪 **Importación de cookies** (Netscape, header, JSON o directo del
  navegador) para contenido con verificación anti-bot

## Requisitos

- Python 3.10+
- Dependencias: `pip install -r requirements.txt`
- FFmpeg: no hace falta instalarlo — el botón **⚡ Actualizar** lo descarga a
  `assets/` la primera vez (y yt-dlp standalone)

## Uso (desde código)

```bat
pip install -r requirements.txt
python main.py
```

1. Pega una URL (o usa 📋) — se abre el diálogo de formato
2. Elige MP3 320/192 o Video 1080p/720p y confirma
3. ▶️ **Iniciar** — la cola procesa con reintentos automáticos

Clic derecho sobre un ítem: cambiar formato, reencolar, o re-descargar toda
la cola en otro formato.

## Compilar el .exe

```bat
python build_script.py
```

Genera `dist\TitanDownloader\` (PyInstaller, modo onedir). El script copia
`assets/` junto al ejecutable automáticamente.

## Arquitectura

```
backend/
  database.py         Cola SQLite: estados, reintentos, backoff, WAL
  download_manager.py Workers en QThreadPool, progreso agregado, ETA
  engine_resolver.py  Cadena de motores: yt-dlp → piped → scraper,
                      expansion de playlists, timeouts por paso
  processor.py        FFmpeg (conversion/loudnorm), etiquetado ID3,
                      validacion de firma binaria
  rate_limiter.py     Delay adaptativo ante 429
  update_manager.py   Actualizacion inteligente de binarios
frontend/
  main_window.py      UI principal (PySide6), dialogo de formato,
                      bandeja del sistema
  settings_window.py  Credenciales Spotify, proxy, cookies, velocidad
```

**Separación clara**: los motores producen archivos temporales; el procesador
convierte, valida y etiqueta; la base de datos registra el estado. Cada motor
puede fallar sin comprometer el resultado final.

## Estado

- ✅ **Funcional y probado en uso real**: descargas YouTube/Spotify/TikTok,
  playlists, conversión, etiquetado
- 🔄 **En desarrollo activo**: se sigue corrigiendo y mejorando — pendientes:
  más formatos de salida, previsualización de portadas, empaquetado con
  instalador, pruebas automatizadas

## Notas

- Los binarios (`ffmpeg.exe`, `yt-dlp.exe`) no se incluyen en el repo: se
  descargan de sus fuentes oficiales con el botón **⚡ Actualizar**.
- Usa la app respetando los términos de servicio de cada plataforma y los
  derechos de autor del contenido que descargues.

## Licencia

[MIT](LICENSE) © luiyi-lab
