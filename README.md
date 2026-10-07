# RPA Hosvital – descarga y envío de documentos clínicos

Bot en Python que maneja la aplicación de escritorio **Hosvital HIS** (sin API) para, a partir del Excel
diario de citas, descargar los documentos clínicos de cada paciente, protegerlos con contraseña y enviarlos
por **Microsoft Graph**. Deja una auditoría completa (incluidos los errores) y al terminar borra los archivos.

```
Excel → filtros (estado/especialidad/correo) → [por paciente en Hosvital]
  login/ciclo/menús → Resumen de Historia → Ingresos (por día + consecutivo) → Rango de Impresión (EN PDF)
  HC (rango de folios) · Incapacidad · Formulación · Órdenes médicas
→ verificación del PDF (aparece el documento del paciente) → cifrado AES-256 (clave = documento)
→ un correo por ingreso con cada PDF adjunto → auditoría .xlsx → limpieza
```

## Estado del proyecto

| Parte | Estado |
|---|---|
| Excel, filtros, auditoría SQLite, PDF (verificación y cifrado), Graph, orquestador, limpieza | Implementado y probado (`pytest`) |
| Driver de Hosvital | Implementado y probado **contra un Hosvital simulado** con la geometría de tus volcados. **Falta validarlo en la máquina real** (ver más abajo) |
| Correo | Arranca en **modo simulación** (`mail.dry_run: true`): no envía nada hasta que lo cambies |

> **Puesta en marcha** de los ambientes de prueba y producción: ver [`GUIA_PUESTA_EN_MARCHA.md`](GUIA_PUESTA_EN_MARCHA.md).

## Instalación (Windows, en la máquina del bot)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q          # deben pasar todas
```

## Configuración (`config.yaml`)

Lo mínimo para empezar:

* `hosvital.launch_command`: ruta al `.exe` o al acceso directo `.lnk` de Hosvital.
* `hosvital.username`: usuario de Hosvital.
* `paths.reports_dir`: carpeta donde Hosvital deja los PDF (`C:\reportes`). **El bot la vacía** (mueve a una
  cuarentena) antes de cada documento; no guardes ahí nada que quieras conservar.
* `filters`: estados (ATENDIDA, FACTURADA) y lista blanca de especialidades. Se aplican en el código: el filtro
  de Excel solo oculta filas, pandas las lee igual.
* `tipos_documento`: equivalencia `tdoc` → texto del combo de Hosvital. CC y TI están confirmados; verifica CE,
  RC y PA con `tools/dump_ui.py` (imprime los ítems del combo).
* `mail.*`: ver "Microsoft Graph".

Secretos (nunca en archivos): se guardan en el Administrador de credenciales de Windows.

```powershell
python -m rpa set-secret hosvital_password
python -m rpa set-secret graph_client_secret      # solo si mail.auth = secret
```

## Uso

```powershell
python -m rpa run --excel input\CITAS.xlsx        # todo: preparar, descargar, enviar (simulado), auditoría
python -m rpa run --excel input\CITAS.xlsx --real # envío REAL (y limpieza al terminar)
```

Por pasos (cada uno reanuda donde quedó; la corrida se identifica por fecha + huella del Excel):

```powershell
python -m rpa prepare  --excel input\CITAS.xlsx   # lee el Excel y crea la corrida
python -m rpa download                            # solo Hosvital
python -m rpa send --real                         # solo cifrar y enviar
python -m rpa audit                               # auditoria\auditoria_<fecha>_run<n>.xlsx
python -m rpa cleanup                             # borra PDFs de los ENVIADOS, cuarentena y capturas
```

### Estados de cada paciente (hoja "Pacientes" de la auditoría)

| Estado | Significado |
|---|---|
| `PENDIENTE` / `DESCARGANDO` | Falta descargar (se reanuda solo) |
| `DESCARGADO` | Documentos verificados; listo para enviar |
| `ENVIADO` | Correo aceptado por Graph (202). Sus archivos se borran en la limpieza |
| `YA_ENVIADO` | Ese ingreso ya se envió en otra corrida del mismo día (p. ej. se guardó de nuevo el Excel): se omite para no duplicar |
| `PACIENTE_NO_ENCONTRADO` | Hosvital mostró un mensaje al buscar el documento |
| `INGRESO_NO_ENCONTRADO` | No apareció el consecutivo del Excel en ese día |
| `DOC_DISTINTO` | El documento que muestra Hosvital no coincide con el del Excel (se aborta ese paciente) |
| `SIN_HC` | No se obtuvo la historia clínica (documento obligatorio): no se envía nada |
| `ERROR_DESCARGA` | Falló un documento tras los reintentos: no se envía un paquete incompleto |
| `ERROR_VERIFICACION` | Un PDF no contiene el documento del paciente / está corrupto: **no se envía** |
| `ERROR_ENVIO` | Falló el cifrado o Graph rechazó el correo (detalle en la auditoría) |
| `ENVIO_INCIERTO` | Timeout/5xx: puede haber salido. **No se reenvía solo**; revisa Elementos enviados |

Las filas del Excel omitidas (estado/especialidad/correo inválido, etc.) quedan en la hoja "Omitidas" con su
motivo. Los correos con dominio sospechoso (`gmal.com`) se omiten y **no se corrigen**: enviar historias clínicas a
una dirección adivinada es un riesgo.

## Microsoft Graph

1. Registrar una aplicación en Microsoft Entra ID (single tenant).
2. Permiso de **aplicación** `Mail.Send` con consentimiento de administrador (opcional `Mail.Read` si luego se
   quiere detectar rebotes).
3. **Restringir la app a un solo buzón emisor** (Exchange RBAC for Applications o ApplicationAccessPolicy). Sin
   esto, la app podría enviar desde cualquier buzón de la empresa.
4. Entregar: tenant ID, client ID, la dirección del buzón emisor y un certificado (mejor) o secreto.

Luego llenar `mail.tenant_id`, `mail.client_id`, `mail.sender`, guardar el secreto y probar primero hacia tu propio
correo con `--real` sobre un Excel de una sola fila.

## Lista de validación en la máquina real (en este orden)

El driver se escribió contra los volcados que compartiste y se probó con un simulador; estos puntos solo se
confirman con Hosvital real. Cada uno tiene un interruptor en `config.yaml` o una herramienta en `tools/`.

1. **Login (sin volcar aún).** Cierra Hosvital, deja la pantalla de login y ejecuta
   `python tools\dump_ui.py login`. Debe haber 2 `Edit` visibles (usuario y clave). Si no, ajustar `login()`.
2. **Flechas en grillas.** Con la ventana del ciclo (o Ingresos con varias filas) ejecuta
   `python tools\probe_grid.py`. Si "tras DOWN silencioso" no cambia pero "con foco" sí → `hosvital.keys_mode: focus`.
   (El driver también lo detecta y cambia solo, pero conviene saberlo.)
3. **Clic por mensajes en todos los diálogos.** Ya funciona en el menú. Si algún ✓ no responde → `hosvital.click_mode: input`.
4. **Posiciones de los menús** `menu_principal_sis` y `menu_sis_gestion`: salieron de tus capturas. Si el bot dice
   "Botón ... no está en la posición esperada", vuelca esa ventana con `dump_ui.py` y copia las coordenadas relativas.
5. **Prueba de un paciente** con un Excel de **una fila** y `mail.dry_run: true`:
   `python -m rpa run --excel input\uno.xlsx`. Comprobar: se descargan los PDF a `work\staging\...`, Edge se cierra,
   `C:\reportes` queda vacía, y la auditoría sale en `auditoria\`.
6. **Casos a probar a propósito** y qué mirar:
   * Dos ingresos el mismo día → **validado en real**: la grilla se lee con OCR de Windows (paquetes `winrt-*` de
    `requirements.txt`; requiere un idioma de OCR instalado en Windows), se hace clic en la fila del consecutivo y se
    comprueba el título del Rango. Con 3 o más filas falta probar.
   * Formulación/Órdenes con **2 o más filas** en "Seleccionar Folio" → ¿baja todas? ¿la ventana se cierra o queda abierta
     tras cada PDF? (el bot soporta ambas, pero hay que confirmar que la fila activa se mueve y se lee bien).
   * Paciente con incapacidad / sin incapacidad (mensaje "Debe seleccionar un Registro de Incapacidad").
   * Historia con folio abierto o en blanco (¿genera PDF vacío? la verificación debería marcarlo).
   * Tipos de documento distintos de CC/TI (completar `tipos_documento`).
7. **Expiración de sesión.** Cierra Hosvital a mitad de una corrida con varios pacientes: debe relanzar, volver a
   iniciar sesión, elegir el ciclo y continuar con el mismo paciente.
8. **Correo real** a tu propio buzón (`--real`), abrir un adjunto con el documento como contraseña.

## Operación diaria

* Máquina dedicada con **sesión de Windows iniciada y desbloqueada** (el bot no puede correr como servicio ni con la
  pantalla bloqueada). Si usan RDP, no minimizar la ventana; mejor consola de VM.
* Programador de tareas → "Ejecutar solo cuando el usuario haya iniciado sesión" →
  `.venv\Scripts\python.exe -m rpa run --excel <ruta> --real`.
* Si algo se interrumpe, volver a ejecutar el mismo comando: reanuda sin duplicar correos. Aunque el Excel cambie
  (otra huella = otra corrida), los ingresos ya enviados ese día se marcan `YA_ENVIADO` y no se reenvían.
* Reinicio planificado de Hosvital a los 330 min (`hosvital.session_max_minutes`) y reinicio reactivo si aparece el login.
* Si fallan 5 pacientes seguidos por el driver, la corrida se detiene (`run.max_consecutive_errors`).

## Privacidad

* La auditoría (SQLite y Excel) guarda documento, correo, estados y errores; **no** guarda contenido clínico.
* PDFs, carpetas de trabajo, cuarentena y capturas de error se borran en `cleanup` (los PDF solo de pacientes ENVIADOS;
  los demás se conservan para revisión; se reconocen por su estado en la hoja "Pacientes").
* Las capturas de error pueden mostrar datos de pacientes: se borran en la limpieza.
* La contraseña de cada PDF es el documento del paciente; el correo no la incluye, solo indica que es su documento.

## Estructura

```
config.yaml              reglas y parámetros (PRODUCCIÓN)
prueba/                  ambiente de prueba: config_prueba.yaml (hereda de config.yaml), Excel de prueba, BD y auditoría propias
rpa/excel_loader.py      Excel + filtros + dedupe + validación de correo
rpa/db.py                auditoría y reanudación (SQLite)
rpa/orchestrator.py      preparar → descargar → enviar → limpiar
rpa/pdf_tools.py         verificación del PDF y cifrado AES-256
rpa/mailer.py            Microsoft Graph
rpa/reports.py           carpeta de reportes (barrido, detección de PDF estable)
rpa/hosvital/driver.py   automatización de Hosvital (pywinauto, solo Windows)
rpa/hosvital/parsing.py  lógica pura del driver (probada en cualquier SO)
rpa/hosvital/vision.py   OCR de la grilla de Ingresos y recuadro de foco (Windows)
tests/                   pruebas (incluye un Hosvital simulado: tests/fake_hosvital.py)
tools/dump_ui.py         volcado de controles de la ventana al frente
tools/probe_grid.py      prueba de lectura/movimiento en grillas
tools/crear_excel_prueba.py  genera el Excel de prueba (correos de buzones internos) desde el Excel diario
```
