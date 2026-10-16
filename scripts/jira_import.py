"""Crea una epica y tareas en Jira (Cloud) a partir de un CSV versionado.

Variables de entorno obligatorias:
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
Opcionales:
  CSV_PATH  (default jira/tareas_entrega1.csv)
  DRY_RUN   (default true)  -> solo simula
  DEBUG     (default false) -> logging detallado en consola
  LOG_FILE  (default jira_import.log) -> el archivo SIEMPRE guarda nivel DEBUG
"""
import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime

import requests

# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------
REQUIRED_ENV = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"]
missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
if missing:
    sys.exit("Faltan secrets/variables de entorno: " + ", ".join(missing))

# .strip() evita el clasico espacio o salto de linea al pegar un secret.
BASE = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
EMAIL = os.environ["JIRA_EMAIL"].strip()
TOKEN = os.environ["JIRA_API_TOKEN"].strip()
PROJECT = os.environ["JIRA_PROJECT_KEY"].strip()

DRY = os.environ.get("DRY_RUN", "true").strip().lower() == "true"
# RUNNER_DEBUG=1 lo pone GitHub al re-ejecutar con "Enable debug logging".
DEBUG = (
    os.environ.get("DEBUG", "false").strip().lower() == "true"
    or os.environ.get("RUNNER_DEBUG") == "1"
)
CSV_PATH = os.environ.get("CSV_PATH", "jira/tareas_entrega1.csv").strip()
LOG_FILE = os.environ.get("LOG_FILE", "jira_import.log").strip()
IN_GH = os.environ.get("GITHUB_ACTIONS") == "true"
MAX_RETRIES = 3
TIMEOUT = 30

# --------------------------------------------------------------------------
# Logging: consola (INFO o DEBUG) + archivo (siempre DEBUG)
# --------------------------------------------------------------------------
log = logging.getLogger("jira_import")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.DEBUG if DEBUG else logging.INFO)
_console.setFormatter(_fmt)
log.addHandler(_console)

try:
    _file = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    _file.setLevel(logging.DEBUG)
    _file.setFormatter(_fmt)
    log.addHandler(_file)
except OSError as exc:
    log.warning("No se pudo crear el archivo de log %s: %s", LOG_FILE, exc)


def redact(text):
    """Nunca deja pasar el token a logs, errores o resumenes."""
    return str(text).replace(TOKEN, "***") if TOKEN else str(text)


def annotate(level, message):
    """Anotacion visible en la pagina de la ejecucion (error|warning|notice)."""
    if IN_GH:
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level}::{safe}", flush=True)


@contextmanager
def group(title):
    """Pliega el detalle de cada fila en el log de GitHub Actions (solo en debug)."""
    use = IN_GH and DEBUG
    if use:
        print(f"::group::{title}", flush=True)
    try:
        yield
    finally:
        if use:
            print("::endgroup::", flush=True)


# --------------------------------------------------------------------------
# Cliente HTTP con reintentos
# --------------------------------------------------------------------------
class JiraError(Exception):
    def __init__(self, status, path, body):
        super().__init__(f"HTTP {status} en {path}: {body}")
        self.status = status
        self.path = path
        self.body = body


HINTS = {
    400: "Mensaje de validacion de Jira: revisa el campo mencionado en el detalle.",
    401: "Credenciales invalidas: revisa JIRA_EMAIL y JIRA_API_TOKEN.",
    403: "La cuenta del token no tiene permiso para esta accion en el espacio.",
    404: "No existe el recurso: revisa JIRA_BASE_URL y JIRA_PROJECT_KEY.",
}

S = requests.Session()
S.auth = (EMAIL, TOKEN)
S.headers.update({"Accept": "application/json"})


def call(method, path, **kw):
    url = f"{BASE}/rest/api/3{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        started = time.monotonic()
        try:
            r = S.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException as exc:
            log.warning("%s %s: fallo de red (intento %d/%d): %s",
                        method, path, attempt, MAX_RETRIES, redact(exc))
            if attempt == MAX_RETRIES:
                raise JiraError(0, path, redact(exc))
            time.sleep(2 ** attempt)
            continue

        ms = int((time.monotonic() - started) * 1000)
        log.debug("%s %s -> %s (%d ms)", method, path, r.status_code, ms)

        if (r.status_code == 429 or r.status_code >= 500) and attempt < MAX_RETRIES:
            try:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
            except ValueError:
                wait = 2 ** attempt
            log.warning("%s %s: HTTP %s, reintento %d/%d en %.0f s",
                        method, path, r.status_code, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue

        if not r.ok:
            raise JiraError(r.status_code, path, redact(r.text[:500]))

        # Cuerpos de respuesta solo de /issue (evita volcar datos de usuarios).
        if DEBUG and r.text and path.startswith("/issue"):
            log.debug("respuesta: %s", redact(r.text[:500]))
        return r.json() if r.text else {}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def parse_date(value):
    for fmt in ("%Y-%m-%d", "%d/%b/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def adf(text):
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


_users = {}
users_not_found = set()


def account_id(name):
    if not name:
        return None
    if name not in _users:
        res = call("GET", "/user/search", params={"query": name})
        _users[name] = res[0]["accountId"] if res else None
        if _users[name]:
            log.debug("usuario '%s' -> accountId %s", name, _users[name])
        else:
            users_not_found.add(name)
            log.warning("usuario no encontrado en Jira: %s (la tarea quedara sin asignar)", name)
    return _users[name]


def existing(summary):
    clean = summary.replace('"', "").replace("\\", "")
    jql = f'project = "{PROJECT}" AND summary ~ "\\"{clean}\\""'
    log.debug("JQL: %s", jql)
    res = call("POST", "/search/jql", json={"jql": jql, "fields": ["summary"], "maxResults": 50})
    for issue in res.get("issues", []):
        if issue["fields"]["summary"].strip() == summary.strip():
            return issue["key"]
    return None


def preflight():
    """Valida credenciales, proyecto y tipos antes de tocar nada."""
    try:
        me = call("GET", "/myself")
        log.info("Autenticado como: %s", me.get("displayName"))
        proj = call("GET", f"/project/{PROJECT}")
    except JiraError as exc:
        log.error("Preflight fallo: %s", exc)
        log.error("Pista: %s", HINTS.get(exc.status, "Revisa el detalle anterior."))
        annotate("error", f"Preflight fallo: HTTP {exc.status}. {HINTS.get(exc.status, '')}")
        sys.exit(1)
    tipos = {t["name"] for t in proj.get("issueTypes", [])}
    log.info("Proyecto: %s (%s)", proj.get("name"), PROJECT)
    log.debug("Tipos de incidencia disponibles: %s", ", ".join(sorted(tipos)))
    return tipos


def read_csv():
    try:
        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cols = set(reader.fieldnames or [])
            faltan = {"Summary", "Issue Type"} - cols
            if faltan:
                log.error("Al CSV le faltan columnas: %s (tiene: %s)", sorted(faltan), sorted(cols))
                annotate("error", f"Columnas faltantes en {CSV_PATH}: {sorted(faltan)}")
                sys.exit(1)
            rows = list(reader)
    except FileNotFoundError:
        log.error("No existe el CSV: %s", CSV_PATH)
        annotate("error", f"No existe el CSV: {CSV_PATH}")
        sys.exit(1)
    log.info("CSV leido: %s (%d filas)", CSV_PATH, len(rows))
    return rows


# --------------------------------------------------------------------------
# Proceso por fila
# --------------------------------------------------------------------------
def process_row(row, summary, tipo, tipos, epics):
    """Devuelve (estado, clave). Estados: creada, simulada, existente, omitida, error."""
    if not summary:
        log.warning("fila sin Summary: se omite")
        return "omitida", None

    if tipo not in tipos:
        log.error("el espacio no tiene el tipo '%s' (disponibles: %s)", tipo, ", ".join(sorted(tipos)))
        return "error", None

    key = existing(summary)
    if key:
        log.info("= ya existe %s: %s", key, summary)
        return "existente", key

    fields = {
        "project": {"key": PROJECT},
        "summary": summary,
        "issuetype": {"name": tipo},
        "description": adf((row.get("Description") or "").strip() or summary),
    }
    labels = [x.strip() for x in (row.get("Labels") or "").split(";") if x.strip()]
    if labels:
        fields["labels"] = labels

    raw_due = (row.get("Due Date") or "").strip()
    due = parse_date(raw_due) if raw_due else None
    if raw_due and not due:
        log.warning("fecha no reconocida '%s' (se ignora). Usa yyyy-MM-dd", raw_due)
    if due:
        fields["duedate"] = due

    parent_name = (row.get("Epic Link") or "").strip()
    if tipo != "Epic" and parent_name:
        parent = epics.get(parent_name)
        if parent:
            fields["parent"] = {"key": parent}
        elif DRY:
            log.debug("epica '%s' aun no existe: en la ejecucion real se vinculara", parent_name)
        else:
            log.warning("epica '%s' no encontrada: la tarea se crea sin padre", parent_name)

    acc = account_id((row.get("Assignee") or "").strip())
    if acc:
        fields["assignee"] = {"accountId": acc}

    log.debug("payload: %s", json.dumps(fields, ensure_ascii=False))

    if DRY:
        log.info("[dry-run] %s: %s", tipo, summary)
        return "simulada", None

    res = call("POST", "/issue", json={"fields": fields})
    log.info("+ %s: %s", res["key"], summary)
    return "creada", res["key"]


def write_step_summary(stats, results, elapsed):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## Importacion a Jira ({'simulacion' if DRY else 'creacion real'})",
        "",
        f"Proyecto `{PROJECT}` | CSV `{CSV_PATH}` | {elapsed:.1f} s",
        "",
        "| Estado | Cantidad |",
        "|---|---|",
    ]
    for estado in ("creada", "simulada", "existente", "omitida", "error"):
        if stats.get(estado):
            lines.append(f"| {estado} | {stats[estado]} |")
    if users_not_found:
        lines += ["", "**Usuarios no encontrados:** " + ", ".join(sorted(users_not_found))]
    lines += ["", "| # | Tipo | Resumen | Estado | Clave |", "|---|---|---|---|---|"]
    for i, tipo, summary, estado, key in results:
        lines.append(f"| {i} | {tipo} | {summary.replace('|', '/')} | {estado} | {key or ''} |")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("No se pudo escribir el resumen del paso: %s", exc)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    started = time.monotonic()
    log.info("Modo: %s | debug=%s | proyecto=%s | csv=%s",
             "SIMULACION (dry-run)" if DRY else "CREACION REAL", DEBUG, PROJECT, CSV_PATH)

    tipos = preflight()
    rows = read_csv()

    stats = Counter()
    results = []
    epics = {}  # resumen de la epica -> clave
    total = len(rows)

    for i, row in enumerate(rows, 1):
        summary = (row.get("Summary") or "").strip()
        tipo = (row.get("Issue Type") or "Task").strip()
        with group(f"[{i}/{total}] {tipo}: {summary}"):
            log.debug("[%d/%d] procesando %s: %s", i, total, tipo, summary)
            try:
                estado, key = process_row(row, summary, tipo, tipos, epics)
            except JiraError as exc:
                estado, key = "error", None
                log.error("[%d/%d] %s", i, total, exc)
                log.error("Pista: %s", HINTS.get(exc.status, "Revisa el detalle anterior."))
                annotate("error", f"Fila {i} ({summary}): HTTP {exc.status}")
            except Exception as exc:  # noqa: BLE001 - queremos seguir con las demas filas
                estado, key = "error", None
                log.exception("[%d/%d] error inesperado: %s", i, total, redact(exc))
                annotate("error", f"Fila {i} ({summary}): error inesperado")

        if tipo == "Epic" and key:
            epics[summary] = key
        stats[estado] += 1
        results.append((i, tipo, summary, estado, key))

    elapsed = time.monotonic() - started
    log.info("=" * 56)
    log.info("RESUMEN (%.1f s)", elapsed)
    for estado in ("creada", "simulada", "existente", "omitida", "error"):
        if stats.get(estado):
            log.info("  %-10s %d", estado, stats[estado])
    if users_not_found:
        log.warning("  usuarios no encontrados: %s", ", ".join(sorted(users_not_found)))
        annotate("warning", "Usuarios no encontrados en Jira: " + ", ".join(sorted(users_not_found)))
    log.info("=" * 56)

    write_step_summary(stats, results, elapsed)

    if stats.get("error"):
        annotate("error", f"{stats['error']} fila(s) con error. Revisa el log.")
        sys.exit(1)


if __name__ == "__main__":
    main()
