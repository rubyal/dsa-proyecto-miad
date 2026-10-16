import csv
import os
import sys
from datetime import datetime

import requests

BASE = os.environ["JIRA_BASE_URL"].rstrip("/")
PROJECT = os.environ["JIRA_PROJECT_KEY"]
DRY = os.environ.get("DRY_RUN", "true").lower() == "true"
CSV_PATH = os.environ.get("CSV_PATH", "jira/tareas_entrega1.csv")

S = requests.Session()
S.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
S.headers.update({"Accept": "application/json"})


def call(method, path, **kw):
    r = S.request(method, f"{BASE}/rest/api/3{path}", timeout=30, **kw)
    if not r.ok:
        sys.exit(f"HTTP {r.status_code} en {path}: {r.text[:300]}")
    return r.json() if r.text else {}


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


def account_id(name):
    if not name:
        return None
    if name not in _users:
        res = call("GET", "/user/search", params={"query": name})
        _users[name] = res[0]["accountId"] if res else None
        if not _users[name]:
            print(f"  ! usuario no encontrado en Jira: {name}")
    return _users[name]


def existing(summary):
    clean = summary.replace('"', "")
    jql = 'project = "%s" AND summary ~ "\\"%s\\""' % (PROJECT, clean)
    res = call("POST", "/search/jql", json={"jql": jql, "fields": ["summary"], "maxResults": 50})
    for issue in res.get("issues", []):
        if issue["fields"]["summary"].strip() == summary.strip():
            return issue["key"]
    return None


def main():
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    epic_key = None
    for row in rows:
        summary = row["Summary"].strip()
        tipo = (row.get("Issue Type") or "Task").strip()

        key = existing(summary)
        if key:
            print(f"= ya existe {key}: {summary}")
            if tipo == "Epic":
                epic_key = key
            continue

        fields = {
            "project": {"key": PROJECT},
            "summary": summary,
            "issuetype": {"name": tipo},
            "description": adf((row.get("Description") or "").strip() or summary),
        }
        labels = [x.strip() for x in (row.get("Labels") or "").split(";") if x.strip()]
        if labels:
            fields["labels"] = labels
        due = parse_date(row.get("Due Date") or "")
        if due:
            fields["duedate"] = due
        if tipo != "Epic" and epic_key:
            fields["parent"] = {"key": epic_key}
        acc = account_id((row.get("Assignee") or "").strip())
        if acc:
            fields["assignee"] = {"accountId": acc}

        if DRY:
            print(f"[dry-run] {tipo}: {summary}")
            continue

        res = call("POST", "/issue", json={"fields": fields})
        print(f"+ {res['key']}: {summary}")
        if tipo == "Epic":
            epic_key = res["key"]


if __name__ == "__main__":
    main()
