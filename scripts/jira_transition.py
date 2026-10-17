"""Mueve incidencias de Jira segun eventos de GitHub (rama creada, PR abierto, PR mergeado).

Eventos soportados (se lee el JSON de GITHUB_EVENT_PATH):
  create (rama)                      -> In Progress
  pull_request opened/reopened/
                 ready_for_review    -> In Review   (si es borrador -> In Progress)
  pull_request closed + merged       -> Done
  pull_request closed sin merge      -> In Progress (solo si estaba en In Review)
  workflow_dispatch                  -> ISSUE_KEY y TARGET_STATUS (para probar a mano)

La clave (ej. DM-12) se busca en el nombre de la rama y en el titulo del PR.
Reglas de seguridad: nunca retrocede una tarea que esta en Done, y nunca falla
la ejecucion solo porque falte un estado en el tablero (avisa y sigue).

Variables obligatorias: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
Opcionales: DRY_RUN, DEBUG, ISSUE_KEY, TARGET_STATUS,
            STATUS_TODO, STATUS_PROGRESS, STATUS_REVIEW, STATUS_DONE
"""
import json
import logging
import os
import re
import sys
import time

import requests

# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------
REQUIRED_ENV = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"]
missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
if missing:
    sys.exit("Faltan secrets/variables de entorno: " + ", ".join(missing))

BASE = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
EMAIL = os.environ["JIRA_EMAIL"].strip()
TOKEN = os.environ["JIRA_API_TOKEN"].strip()
PROJECT = os.environ["JIRA_PROJECT_KEY"].strip().upper()

EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "").strip()
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "").strip()
DRY = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
DEBUG = (
    os.environ.get("DEBUG", "false").strip().lower() == "true"
    or os.environ.get("RUNNER_DEBUG") == "1"
)
IN_GH = os.environ.get("GITHUB_ACTIONS") == "true"
MAX_RETRIES = 3
TIMEOUT = 30

ST_TODO = os.environ.get("STATUS_TODO", "To Do").strip()
ST_PROGRESS = os.environ.get("STATUS_PROGRESS", "In Progress").strip()
ST_REVIEW = os.environ.get("STATUS_REVIEW", "In Review").strip()
ST_DONE = os.environ.get("STATUS_DONE", "Done").strip()
ORDER = [s.lower() for s in (ST_TODO, ST_PROGRESS, ST_REVIEW, ST_DONE)]

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
log = logging.getLogger("jira_transition")
log.setLevel(logging.DEBUG if DEBUG else logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
log.addHandler(_h)


def redact(text):
    return str(text).replace(TOKEN, "***") if TOKEN else str(text)


def annotate(level, message):
    if IN_GH:
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level}::{safe}", flush=True)


# --------------------------------------------------------------------------
# Cliente HTTP con reintentos
# --------------------------------------------------------------------------
class JiraError(Exception):
    def __init__(self, status, path, body):
        super().__init__(f"HTTP {status} en {path}: {body}")
        self.status = status


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
        log.debug("%s %s -> %s (%d ms)", method, path, r.status_code,
                  int((time.monotonic() - started) * 1000))
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
            raise JiraError(r.status_code, path, redact(r.text[:300]))
        return r.json() if r.text else {}


# --------------------------------------------------------------------------
# Logica
# --------------------------------------------------------------------------
def load_event():
    if not EVENT_PATH or not os.path.exists(EVENT_PATH):
        log.error("No se encontro el evento de GitHub (GITHUB_EVENT_PATH).")
        sys.exit(1)
    with open(EVENT_PATH, encoding="utf-8") as f:
        return json.load(f)


def decide(event):
    """Devuelve (texto donde buscar claves, estado destino, permitir_retroceso, motivo) o None."""
    if EVENT_NAME == "create":
        if event.get("ref_type") != "branch":
            return None
        ref = event.get("ref", "")
        return ref, ST_PROGRESS, False, f"rama creada: {ref}"

    if EVENT_NAME == "pull_request":
        pr = event.get("pull_request", {})
        action = event.get("action", "")
        text = f"{pr.get('head', {}).get('ref', '')} {pr.get('title', '')}"
        label = f"PR #{pr.get('number')} {action}"
        if action in ("opened", "reopened", "ready_for_review"):
            if pr.get("draft"):
                return text, ST_PROGRESS, False, f"{label} (borrador)"
            return text, ST_REVIEW, False, label
        if action == "closed":
            if pr.get("merged"):
                return text, ST_DONE, False, f"{label} (mergeado)"
            return text, ST_PROGRESS, True, f"{label} (cerrado sin merge)"
        return None

    if EVENT_NAME == "workflow_dispatch":
        key = os.environ.get("ISSUE_KEY", "").strip()
        target = os.environ.get("TARGET_STATUS", "").strip() or ST_PROGRESS
        return key, target, True, "ejecucion manual"

    return None


def find_keys(text):
    found = re.findall(rf"\b{re.escape(PROJECT)}-\d+\b", text, flags=re.IGNORECASE)
    return list(dict.fromkeys(k.upper() for k in found))


def rank(status):
    s = status.lower()
    return ORDER.index(s) if s in ORDER else None


def transition(key, target, allow_back):
    """Devuelve un texto de resultado: movida, simulada, sin cambios, omitida,
    no disponible, no existe."""
    try:
        issue = call("GET", f"/issue/{key}", params={"fields": "status"})
    except JiraError as exc:
        if exc.status == 404:
            log.warning("%s no existe en Jira (o sin permiso para verla).", key)
            annotate("warning", f"{key} no existe en Jira")
            return "no existe"
        raise

    current = issue["fields"]["status"]["name"]
    log.debug("%s estado actual: '%s' | destino: '%s'", key, current, target)

    if current.lower() == target.lower():
        log.info("= %s ya esta en '%s'", key, current)
        return "sin cambios"

    cur_rank, tgt_rank = rank(current), rank(target)
    if cur_rank is not None and tgt_rank is not None and tgt_rank < cur_rank:
        # Nunca sacar una tarea de Done; el retroceso solo se permite desde In Review.
        if cur_rank == ORDER.index(ST_DONE.lower()) or not allow_back:
            log.info("= %s se queda en '%s' (no se retrocede a '%s')", key, current, target)
            return "omitida"
        if cur_rank != ORDER.index(ST_REVIEW.lower()):
            log.info("= %s se queda en '%s' (el retroceso solo aplica desde '%s')",
                     key, current, ST_REVIEW)
            return "omitida"

    options = call("GET", f"/issue/{key}/transitions").get("transitions", [])
    match = next(
        (t for t in options
         if t.get("to", {}).get("name", "").lower() == target.lower()
         or t.get("name", "").lower() == target.lower()),
        None,
    )
    if not match:
        disponibles = sorted({t.get("to", {}).get("name", t.get("name", "?")) for t in options})
        log.warning("%s: no hay transicion a '%s' desde '%s'. Disponibles: %s",
                    key, target, current, ", ".join(disponibles) or "(ninguna)")
        annotate("warning", f"{key}: no existe el estado '{target}' en el tablero "
                            f"(disponibles: {', '.join(disponibles)})")
        return "no disponible"

    if DRY:
        log.info("[dry-run] %s: '%s' -> '%s'", key, current, target)
        return "simulada"

    call("POST", f"/issue/{key}/transitions", json={"transition": {"id": match["id"]}})
    log.info("-> %s: '%s' -> '%s'", key, current, target)
    return "movida"


def write_step_summary(reason, target, results):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Sincronizacion con Jira" + (" (simulacion)" if DRY else ""),
        "",
        f"Evento: `{EVENT_NAME}` | {reason} | destino: **{target}**",
        "",
        "| Incidencia | Resultado |",
        "|---|---|",
    ]
    lines += [f"| {k} | {res} |" for k, res in results]
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("No se pudo escribir el resumen del paso: %s", exc)


def main():
    event = load_event()
    decision = decide(event)
    if decision is None:
        log.info("Evento sin accion (%s / %s).", EVENT_NAME, event.get("action") or event.get("ref_type") or "-")
        return
    text, target, allow_back, reason = decision
    log.info("Evento: %s | destino: '%s' | dry-run=%s", reason, target, DRY)
    log.debug("texto analizado: %r", text)

    keys = find_keys(text)
    if not keys:
        log.info("Sin clave %s-n en la rama ni en el titulo: nada que hacer.", PROJECT)
        annotate("notice", f"Sin clave {PROJECT}-n en la rama o el titulo: no se movio ninguna tarea.")
        return
    log.info("Claves detectadas: %s", ", ".join(keys))

    results, errors = [], 0
    for key in keys:
        try:
            res = transition(key, target, allow_back)
        except JiraError as exc:
            errors += 1
            res = "error"
            log.error("%s: %s", key, exc)
            annotate("error", f"{key}: HTTP {exc.status}")
        results.append((key, res))

    write_step_summary(reason, target, results)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
