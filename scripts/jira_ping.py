import os
import sys

import requests

BASE = os.environ["JIRA_BASE_URL"].rstrip("/")
PROJECT = os.environ["JIRA_PROJECT_KEY"]

s = requests.Session()
s.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
s.headers.update({"Accept": "application/json"})

# 1. Autenticacion
r = s.get(f"{BASE}/rest/api/3/myself", timeout=30)
if r.status_code != 200:
    sys.exit(f"Fallo la autenticacion (HTTP {r.status_code}). Revisa JIRA_EMAIL, JIRA_API_TOKEN y JIRA_BASE_URL.")
print("OK autenticado como:", r.json().get("displayName"))

# 2. Proyecto
r = s.get(f"{BASE}/rest/api/3/project/{PROJECT}", timeout=30)
if r.status_code != 200:
    sys.exit(f"No se encontro el proyecto '{PROJECT}' (HTTP {r.status_code}). Revisa JIRA_PROJECT_KEY.")
proyecto = r.json()
print("OK proyecto:", proyecto.get("name"), f"({PROJECT})")

# 3. Tipos de incidencia disponibles
tipos = [t["name"] for t in proyecto.get("issueTypes", [])]
print("Tipos de incidencia:", ", ".join(tipos))
for requerido in ("Epic", "Task"):
    if requerido not in tipos:
        print(f"AVISO: el proyecto no tiene el tipo '{requerido}'. Ajusta la columna 'Issue Type' del CSV.")

print("Todo listo para usar el importador.")
