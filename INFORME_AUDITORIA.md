# Informe de auditoría — Titan Downloader

**Fecha:** 29 de octubre de 2026
**Alcance:** código fuente completo (12 módulos, ~3.450 líneas) — `main.py`, `backend/`, `frontend/`
**Excluido del análisis:** carpetas de build (`build*`, `dist_final`, `installer`) — artefactos de PyInstaller, no código.

## Bugs corregidos

### 1. Atributo inexistente `NO_CREDS_MSG` — `backend/engine_resolver.py`
`_spotipy_client()` hacía `raise EngineError(self.NO_CREDS_MSG)`, pero ese atributo
nunca fue definido en la clase (solo existe `NO_API_MSG`). Resultado: al faltar
credenciales de Spotify, en lugar del mensaje útil "configura tus credenciales"
se generaba un `AttributeError` que quedaba enmascarado por los `except Exception`
de la cadena de fallbacks → el usuario veía "No se pudieron obtener los metadatos",
que no explica la causa real.
**Corrección:** atributo `NO_CREDS_MSG` añadido con el mensaje correcto.

### 2. Crash garantizado al importar CSV — `frontend/main_window.py`
`DatabaseManager.add_items()` devuelve la tupla `(added, updated)`, pero
`_import_from_path()` la asignaba a una sola variable y luego hacía
`skipped = len(items) - added` → `TypeError: unsupported operand type(s)`.
Cada importación de CSV/Exportify agregaba los ítems a la BD pero crasheaba
antes de refrescar la tabla o mostrar confirmación.
**Corrección:** desempaquetado `added, updated = ...` y `skipped` recalculado.

### 3. Backoff anti-429 anulado — `backend/download_manager.py`
El `finally` del worker llamaba siempre a `limiter.report_success()`, que resetea
el contador de 429 consecutivos **incluso cuando la descarga falló por 429**.
Como `resolve()` ya invoca `report_success()` cuando un motor tiene éxito, el
efecto neto era que el delay adaptativo nunca escalaba en el caso más común
(falla → contador a cero → siguiente 429 cuenta como el primero).
**Corrección:** `report_success()` eliminado del `finally`.

### 4. Condición de carrera en el contador de workers activos — `backend/download_manager.py`
`_active` se incrementaba en el hilo de la GUI (timer `_fill`) y se decrementaba
desde los hilos del `QThreadPool` (`_worker_done`) sin ninguna protección. El
par incremento/decremento no es atómico: en carreras podía perderse un
decremento y dejar slots "fantasmas" que reducían la concurrencia efectiva.
**Corrección:** ambos accesos protegidos con `self._lock`.

### 5. Widget de Qt accedido desde un hilo secundario — `frontend/main_window.py`
`_resolve_thread` (ejecutado en un `threading.Thread`) leía
`self.quality_combo.currentText()`. Qt prohíbe tocar widgets fuera del hilo de
la GUI: comportamiento indefinido (crashes intermitentes difíciles de reproducir).
**Corrección:** la calidad se lee en `_add_url` (hilo GUI) y se pasa como
argumento al hilo.

### 6. Cancelación inoperante con el binario yt-dlp — `backend/engine_resolver.py`
En `_via_ytdlp_process` (ruta usada por la app empaquetada), `cancel_check()`
solo se evaluaba en el `finally`, que corre **después** de que el proceso termina
por sí mismo. Resultado: pulsar ✖ sobre una descarga del .exe no la detenía
hasta que yt-dlp terminara solo.
**Corrección:** cancelación verificada dentro del bucle de lectura de stdout
(termina el proceso y lanza `Cancelled` de inmediato).

### 7. Doble `return ok` — `backend/update_manager.py`
Línea 125: `return ok` duplicado (código muerto). Eliminado.

### 8. Motor "tikwm" sin código en la tabla — `frontend/queue_table_model.py`
`ENGINE_CODE` no incluía `"tikwm"`, así que las descargas resueltas por ese
motor mostraban "?" en la columna Motor. Añadido como `"T"`.

## Observaciones menores (no corregidas, a considerar)

- `Config.save()` se ejecuta en cada arranque aunque no haya cambios (reescritura
  inofensiva del JSON).
- `_download_stream` (update_manager) carga el ZIP de FFmpeg (~170 MB) entero en
  RAM antes de escribirlo. Funciona, pero sería mejor escribir por chunks a disco.
- `_run_with_timeout` deja el hilo interno vivo (daemon) si un motor supera el
  timeout: el proceso colgado seguiría consumiendo red hasta morir solo.
- `main.py` nunca llama a `db.close()` al salir; SQLite lo recupera sin problemas
  (WAL), pero un cierre ordenado sería más limpio.
- El cover art en `_tag_mp3` no verifica cancelación: un cancelar puede tardar
  unos segundos extra en la fase de etiquetado.

## Seguridad

- ⚠️ **`data/config.json` contiene `spotify_client_secret` en texto plano.**
  Es el patrón normal para una app de escritorio con Spotipy (y el secreto es de
  una app propia de developer.spotify.com, no de una cuenta personal), pero:
  1. **No publiques este proyecto en GitHub sin vaciar ese campo** (el `.gitignore`
     debería excluir `data/config.json`).
  2. Si el secreto ya circuló (compartiste la carpeta, hiciste backup online),
     regenera las credenciales en el dashboard de Spotify (rotación gratuita).
- `subprocess` se usa siempre con listas de argumentos (sin shell) — sin riesgo
  de inyección de comandos ✓.
- `sanitize()` neutraliza caracteres problemáticos en nombres de archivo ✓.
- Descarga de binarios (yt-dlp.exe / ffmpeg) sin verificación de hash ni firma —
  se descargan de URLs oficiales por HTTPS; sería más robusto verificar el hash
  publicado en el release de yt-dlp.

## Verificación

`python -m py_compile` ejecutado sobre los 13 módulos: **todos compilan** tras
las correcciones.
