name: Crear tareas en Jira desde CSV

on:
  workflow_dispatch:
    inputs:
      csv_path:
        description: "Ruta del CSV en el repo"
        default: "jira/tareas_entrega1.csv"
      dry_run:
        description: "Solo simular (true/false)"
        default: "true"

permissions:
  contents: read

jobs:
  crear-tareas:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - name: Importar tareas
        env:
          JIRA_BASE_URL: ${{ secrets.JIRA_BASE_URL }}
          JIRA_EMAIL: ${{ secrets.JIRA_EMAIL }}
          JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
          JIRA_PROJECT_KEY: ${{ secrets.JIRA_PROJECT_KEY }}
          CSV_PATH: ${{ inputs.csv_path }}
          DRY_RUN: ${{ inputs.dry_run }}
        run: python scripts/jira_import.py
