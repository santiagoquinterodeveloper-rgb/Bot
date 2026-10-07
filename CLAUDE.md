# RPA Hosvital – contexto para Claude Code

Bot en Python (pywinauto, backend `win32`) que maneja la app de escritorio **Hosvital HIS** (sin API, ventana de tamaño fijo)
para descargar los documentos clínicos de cada paciente del Excel diario, protegerlos con contraseña (= documento del
paciente) y enviarlos por Microsoft Graph. Lee `README.md` para el flujo, estados y operación.

## Cómo trabajar aquí
- Windows. Entorno: `.venv`. Pruebas: `python -m pytest -q` (66 deben pasar; usan un Hosvital simulado en `tests/fake_hosvital.py`).
- Correr: `python -m rpa run --excel input\CITAS.xlsx` (correo en `dry_run: true`; nunca enviar real sin que el usuario lo pida).
- Ambiente de prueba separado en `prueba/` (`python -m rpa --config prueba\config_prueba.yaml run [--real]`; `--config` va antes del
  subcomando). Hereda de `config.yaml` (`extends`), usa su propia BD y, con `entorno: prueba`, vacía la BD tras un envío real
  (`reset-db` a mano). Detalle en `GUIA_PUESTA_EN_MARCHA.md`. Nunca poner `entorno: prueba` en `config.yaml`.
- La validación de prefijo del nombre del PDF se relajó: `_take_pdf` acepta cualquier nombre (solo avisa).
- Si una corrida falla, los pacientes quedan en `ERROR_DESCARGA` y NO se reintentan solos: `python reset.py` los devuelve a PENDIENTE.
- Herramientas de diagnóstico: `python tools\dump_ui.py <nombre>` (volcado de controles de la ventana al frente; da 8 s para
  hacer clic en su barra de título) y `tools\probe_grid.py`.
- Hosvital real solo se puede validar en esta máquina. Pide al usuario dejar la ventana que toque al frente y corre `dump_ui.py`.
- No guardar secretos en archivos: usuario en `config.yaml`, clave con `python -m rpa set-secret hosvital_password`.

## Hechos confirmados en Hosvital real (no volver a suponer)
- Login: 2 `Edit` (usuario, contraseña) y un `Button` sin texto de 17×24 pegado a la derecha del campo de contraseña (►).
  Los dos botones de 29×29 de abajo a la izquierda son usuario/salir: NO pulsarlos.
- El `Edit` de la contraseña devuelve `window_text() == ''` aunque se vean asteriscos: no se puede verificar lo escrito.
- Controles nativos `SWT_Window0`; los diálogos modales son `#32770`. `click()` por mensajes funciona.
- Los botones de ícono miden 29×29 y no tienen texto; ✓ es el penúltimo de la fila inferior (?, ✓, X).
- Grillas DataWindow: los `Edit` ocultos con la fila activa NO son hijos de la grilla (`grid.descendants()` = []); en el orden de
  enumeración del diálogo aparecen justo DESPUÉS de la grilla (ver `_row_texts` en `rpa/hosvital/driver.py`).
- `grid.top_level_parent()` es la ventana principal SWT, NO el diálogo `#32770` (es una ventana propia): su `descendants()`
  sale vacío. Para enumerar el diálogo sube por `parent()` (así lo hace `_row_texts`). `_press` aún usa `top_level_parent()`
  para `set_focus()`; si fallan las flechas, revisar ahí.
- Para volcar un diálogo sin hacer clic en él: `Desktop(backend="win32").windows()` filtrando por título y `descendants()`.
- Ciclo ("Selección Cliclo de Proceso", typo de la app): al abrir ya queda seleccionada la última fila (2026 | 10 | OCTUBRE);
  el usuario normalmente pulsa FIN y ✓. Dos `SWT_Window0` visibles: el grande (contenedor) y la grilla (la segunda por área).
  `pick_cycle` ya lee la fila correctamente.
- **Grilla de "Ingresos de Pacientes" (confirmado con 2 ingresos el mismo día, 7-oct-2026):** los `Edit` ocultos NO siguen a
  la fila activa (siempre muestran la ÚLTIMA fila); las flechas no la mueven; el `{ENTER}` en F. Fin actúa como ✓ (abre
  el Rango con la fila activa) y NO filtra por fecha, ni con TAB ni con clics. Un clic por mensajes con coordenadas
  (`grid.click(coords=(x, y))`, celda del consecutivo) SÍ mueve la fila activa. La fila se elige leyendo la grilla de la
  pantalla con OCR de Windows (`rpa/hosvital/vision.py`, winrt, idioma es-MX) y se comprueba el recuadro de foco; luego
  el título del Rango ("Consecutivo de Ingreso N del dd/mm/aaaa hh:mm:ss", texto dibujado, no es un control) se lee con
  OCR y debe coincidir con el Excel, si no se aborta (`INGRESO_NO_ENCONTRADO`) sin descargar nada.
- Con Hosvital ocupado (generando un reporte) todas las ventanas devuelven título vacío un instante: `state()` da
  `OCUPADO` (no `NO_APP` ni `MENSAJE`); la sesión solo se da por perdida si el estado se mantiene `lost_grace_seconds` (3 s).
- El primer intento de cada paciente suele fallar con "No apareció el formulario del paciente tras 'Resumen de Historia'" y
  el segundo pasa (anterior a estos cambios, sin investigar).
- Pendiente de validar en real: coordenadas de los menús (`layout` en config.yaml), Seleccionar Folio con varias filas,
  expiración de sesión, grillas de Ingresos con 3 o más filas.

## Estado actual (7-oct-2026)
Login, ciclo y selección de ingreso funcionan en real, incluidos 2 ingresos el mismo día (ambos se descargaron, cada uno
con su HC verificada y su correo simulado). Siguiente: flujo completo con varios pacientes. El ingreso del Excel con
especialidad fuera de la lista blanca (p. ej. AUXILIAR DE ENFERMERIA) se omite a propósito. Si quedaron pacientes en
`ERROR_DESCARGA`, `python reset.py`.
