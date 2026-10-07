# Guía de puesta en marcha – RPA Hosvital

Cómo dejar el bot funcionando en dos ambientes: **prueba** (envía correos reales, pero solo a buzones internos)
y **producción** (envía a los pacientes). Para entender el flujo y los estados de cada paciente, ver `README.md`.

Todos los comandos se ejecutan en PowerShell, desde la carpeta del proyecto.

## 1. Los dos ambientes

| | **Prueba** | **Producción** |
|---|---|---|
| Configuración | `prueba\config_prueba.yaml` (hereda de `config.yaml`, solo cambia lo suyo) | `config.yaml` |
| Excel de entrada | `prueba\CITAS_PRUEBA_ENVIO.xlsx` (15 ingresos con correos de 4 buzones internos) | El Excel diario real (`input\CITAS.xlsx`) |
| BD de auditoría | `prueba\rpa_prueba.sqlite` | `work\rpa.sqlite` |
| Auditoría en Excel | `prueba\auditoria\` | `auditoria\` |
| Destinatarios | Los buzones internos del Excel de prueba | Los pacientes |
| Tras un envío real | La BD de prueba **se vacía sola** (se puede repetir con los mismos pacientes) | La BD **se conserva**: es la que impide reenviar un ingreso ya enviado |
| Se activa con | `--config prueba\config_prueba.yaml` | (por defecto) |

Los dos ambientes comparten: Hosvital real, la carpeta `C:\reportes`, el usuario de Hosvital y el buzón emisor de Graph.
**Nunca corran los dos al mismo tiempo** (pelearían por Hosvital y por `C:\reportes`).

> `--config` va **antes** del subcomando: `python -m rpa --config prueba\config_prueba.yaml run`. Después no lo acepta.

## 2. Instalación (una sola vez, en la máquina del bot)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                     # deben pasar todas (66 al 7-oct-2026)
```

Secretos (van al Administrador de credenciales de Windows, nunca a un archivo):

```powershell
python -m rpa set-secret hosvital_password
python -m rpa set-secret graph_client_secret
```

* `graph_client_secret` debe ser el **Value** del secreto en Entra ID, no el *Secret ID*. Con el ID, Graph responde
  `AADSTS7000215 Invalid client secret` (pasó en la primera prueba). El Value solo se ve al crearlo.
* Quién puede enviar: la app de Graph debe estar restringida al buzón de `mail.sender` (ver `README.md`, "Microsoft Graph").

Condiciones de la máquina (aplican a los dos ambientes): sesión de Windows **iniciada y desbloqueada**, ventana de
Hosvital sin minimizar (mejor consola de VM que RDP), Excel de auditoría y Excel de entrada **cerrados** durante la corrida.

## 3. Ambiente de prueba

Sirve para validar cambios (en el bot, en Hosvital o en la plantilla del correo) sin que nada llegue a un paciente.

### 3.1 Preparación
1. Cerrar Hosvital y cualquier Excel que apunte a `prueba\` o a `auditoria\`.
2. El Excel de prueba ya existe. Para regenerarlo con otro día (primeros 15 ingresos que pasan los filtros del bot, con los
   correos reemplazados por los 4 buzones internos y validado al terminar):

   ```powershell
   python tools\crear_excel_prueba.py input\CITAS-<fecha>.xlsx
   ```
   Opciones: `--n 15`, `--semilla 42`, `--salida <ruta>`. Los buzones están en la lista `BUZONES` del script.

### 3.2 Ensayo en simulación (no envía nada)
```powershell
python -m rpa --config prueba\config_prueba.yaml run
```
Descarga todo desde Hosvital y arma cada correo, pero no lo envía (`[simulado] <doc> -> <buzón>` en pantalla). Revisar la
auditoría en `prueba\auditoria\` (hojas *Pacientes*, *Documentos*, *Correos*). Antes de seguir, comprobar que **todos los
destinatarios** son buzones internos.

### 3.3 Envío real a los buzones de prueba
```powershell
python -m rpa --config prueba\config_prueba.yaml run --real
```
Reanuda la misma corrida: no vuelve a descargar lo que ya está `DESCARGADO`, solo envía. Al terminar: borra los PDF de los
enviados, escribe la auditoría en Excel y **vacía la BD de prueba** y sus carpetas temporales. Revisar los 4 buzones y abrir
un adjunto (contraseña = número de documento del paciente).

### 3.4 Repetir
Basta volver a ejecutar 3.2 y 3.3: al estar vacía la BD, los mismos pacientes se procesan como nuevos.

* Si hiciste solo la simulación (3.2) y quieres empezar de cero (o hubo `ERROR_DESCARGA`), vacía a mano:
  `python -m rpa --config prueba\config_prueba.yaml reset-db`.
* El Excel de auditoría de una corrida se llama igual que el de la anterior (`auditoria_<fecha>_run1.xlsx`) y **lo
  reemplaza**: cópialo si quieres conservarlo.
* `reset-db` se niega si la configuración no dice `entorno: prueba`, así que no puede vaciar la BD de producción.
* Si quedó algún paciente `ENVIO_INCIERTO`, mira "Elementos enviados" del buzón emisor antes de repetir.

### 3.5 Cuidados
* En prueba usa **siempre** el Excel de prueba. Si pasas `--excel` con un Excel real y `--real`, los correos sí llegarán
  a los pacientes.
* El Excel de prueba trae documentos y nombres reales: no lo subas al repositorio (`.gitignore` ya lo excluye) ni lo compartas.

## 4. Ambiente de producción

### 4.1 Lista de verificación antes de la primera corrida real
- [ ] `mail.dry_run: true` en `config.yaml` (así debe quedar siempre que no se esté enviando de verdad).
- [ ] **Quitar "(PRUEBA)" del título del correo**: está en `mail.body_html`, línea `<h1 ...>Tus documentos están disponibles (PRUEBA)</h1>`.
      Se dejó así durante las pruebas y no debe llegarle a un paciente.
- [ ] `config.yaml` **no** tiene la clave `entorno` (esa clave es solo del ambiente de prueba).
- [ ] `mail.sender`, `tenant_id`, `client_id` correctos y `graph_client_secret` guardado (sección 2).
- [ ] Prueba completa del ambiente de prueba (sección 3) sin errores y con los adjuntos abriendo con el documento como contraseña.
- [ ] Una corrida real con un Excel de **una sola fila hacia tu propio correo** (`python -m rpa run --excel input\uno.xlsx --real`).
      Si luego quieres repetirla con ese mismo ingreso, la guarda `YA_ENVIADO` te lo impedirá ese día: es lo esperado.

### 4.2 Corrida diaria
```powershell
python -m rpa run --excel input\CITAS.xlsx            # 1) simulación: descarga y arma los correos, no envía
python -m rpa run --excel input\CITAS.xlsx --real     # 2) envío real (reanuda lo descargado) + limpieza
```
La simulación previa es opcional pero recomendable los primeros días. Al finalizar la corrida real quedan: auditoría en
`auditoria\auditoria_<fecha>_run<n>.xlsx`, BD en `work\rpa.sqlite` y los archivos de los ENVIADOS borrados.

Automatizada: Programador de tareas de Windows → *Ejecutar solo cuando el usuario haya iniciado sesión* →
`.venv\Scripts\python.exe -m rpa run --excel <ruta> --real`.

### 4.3 Si algo se interrumpe
* Volver a ejecutar el **mismo comando**: reanuda sin duplicar correos (aunque el Excel cambie, los ingresos ya enviados
  ese día quedan `YA_ENVIADO`).
* Pacientes en `ERROR_DESCARGA` no se reintentan solos: `python reset.py` los devuelve a `PENDIENTE` (actúa solo sobre la BD de
  producción) y se vuelve a lanzar la corrida.
* `ENVIO_INCIERTO` (timeout, 5xx o corte durante el envío): **no se reenvía solo**. Revisar "Elementos enviados" del buzón
  emisor y, si de verdad no salió, reenviarlo a mano.
* Si fallan 5 pacientes seguidos por el driver, la corrida se detiene (`run.max_consecutive_errors`).

### 4.4 Qué no hacer en producción
* No copiar `entorno: prueba` a `config.yaml`: vaciaría la auditoría y permitiría reenviar historias clínicas.
* No borrar `work\rpa.sqlite` para "limpiar": pierde la protección contra reenvíos.
* No guardar secretos en `config.yaml` ni en el repositorio.

## 5. Referencia rápida

| Quiero… | Comando |
|---|---|
| Simular (producción) | `python -m rpa run --excel input\CITAS.xlsx` |
| Enviar de verdad (producción) | `python -m rpa run --excel input\CITAS.xlsx --real` |
| Simular (prueba) | `python -m rpa --config prueba\config_prueba.yaml run` |
| Enviar de verdad (prueba) | `python -m rpa --config prueba\config_prueba.yaml run --real` |
| Vaciar la BD de prueba | `python -m rpa --config prueba\config_prueba.yaml reset-db` |
| Regenerar el Excel de prueba | `python tools\crear_excel_prueba.py input\CITAS-<fecha>.xlsx` |
| Reintentar `ERROR_DESCARGA` (producción) | `python reset.py` |
| Solo descargar / solo enviar | `python -m rpa download` · `python -m rpa send --real` (con `--config …` en prueba) |
| Volcar controles de una ventana de Hosvital | `python tools\dump_ui.py <nombre>` |

## 6. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `unrecognized arguments: --config …` | `--config` se puso después del subcomando; va antes. |
| `AADSTS7000215: Invalid client secret` | Se guardó el Secret ID en vez del Value, o el secreto expiró. Crear otro en Entra ID y `set-secret graph_client_secret`. |
| La auditoría no se escribe / reintenta | El Excel de auditoría está abierto. Cerrarlo. |
| `ERROR_DESCARGA` con "mensaje de Hosvital…" o "No apareció el formulario…" | Reintentar (`reset.py` en producción, `reset-db` en prueba). El primer intento de un paciente suele fallar y el segundo pasa. |
| Un PDF con nombre distinto al esperado (p. ej. `RRslInter.pdf` en Órdenes médicas) | Ya no es error: el bot acepta cualquier nombre y deja un aviso en pantalla. Igual se verifica que el PDF contenga el documento del paciente. |
| `reset-db solo se permite con 'entorno: prueba'` | Es la protección de producción; usa la configuración de `prueba\`. |
