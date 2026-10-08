"""Presentation and bounded polling for the AAO walkthrough notebook."""
from html import escape
from pathlib import Path
import time
import uuid
from urllib.parse import urlparse

import httpx
from IPython.display import HTML, display
from conductor.client.http.rest import ApiException

from conductor_demo.conductor import Conductor
from conductor_demo.services import (
    Ledger, LocalCases, LocalCrm, LocalDocuments, LocalFulfillment,
    LocalGateway, LocalValidation,
)
from conductor_demo.workers import Workers

LABELS = {
    "classify_intent": "Classify request",
    "extract_application": "Extract application",
    "validate": "Validate through gateway",
    "human_review": "Human review",
    "fulfill": "Open mock account",
    "update_crm": "Update CRM",
    "complete_case": "Complete case",
}


def table(headers, rows):
    head = "".join(f'<th style="text-align:left;padding:10px;border-bottom:2px solid #94a3b8">{escape(str(h))}</th>'
                   for h in headers)
    body = "".join("<tr>" + "".join(
        f'<td style="padding:10px;border-bottom:1px solid #cbd5e1">{escape(str(c))}</td>'
        for c in row) + "</tr>" for row in rows)
    display(HTML(f'<table style="width:100%;font-size:14px"><thead><tr>{head}</tr></thead>'
                 f'<tbody>{body}</tbody></table>'))


def message(title, detail, color="#2563eb"):
    display(HTML(f'<div style="border-left:5px solid {color};padding:12px 18px;'
                 f'margin:12px 0"><strong>{escape(title)}</strong><br>{escape(detail)}</div>'))


class Walkthrough:
    def __init__(self, root: Path, url="http://localhost:8000/api"):
        if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("This notebook only connects to a local Conductor server.")
        self.root, self.url = root, url.rstrip("/")
        try:
            response = httpx.get(self.url.removesuffix("/api") + "/health", timeout=10)
            response.raise_for_status()
            if response.json().get("healthy") is not True:
                raise ValueError("Conductor reports unhealthy")
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("Conductor is not ready. Start the Docker stack and wait for "
                               "conductor-server to be healthy, then rerun this cell.") from error
        self.client = Conductor(self.url)
        self.ledger = Ledger(root / ".local/effects.sqlite3")
        self.workers = self.new_workers()
        self.cases = {}
        self.results = {}
        message("Connected to Conductor", "Local health check passed. Workers run inside this notebook; no worker terminal is needed.")

    def new_workers(self):
        docs = LocalDocuments(self.root / "examples/documents")
        return Workers(docs, docs, LocalGateway(LocalValidation()), LocalFulfillment(self.ledger),
                       LocalCrm(self.ledger), LocalCases(self.ledger))

    def register(self):
        self.client.register(self.root / "workflows")
        message("AAO v1 is ready", "The workflow and six worker task definitions are registered.")

    def start(self, label, fixture):
        # Rerunning a scenario cell reuses its workflow. Rerun setup for new cases.
        if label not in self.cases:
            case_id = "notebook-" + uuid.uuid4().hex
            workflow_id = self.client.start(case_id, [f"fixture:{fixture}.json"])
            self.cases[label] = (case_id, workflow_id)
            message(label, f"Started synthetic case {case_id}.")
        return self.cases[label][1]

    def wait(self, label, for_review=False, timeout=180):
        workflow_id = self.cases[label][1]
        deadline = time.monotonic() + timeout
        print("Processing local tasks… this may take up to three minutes.", flush=True)
        while time.monotonic() < deadline:
            try:
                self.client.run_once(self.workers, "aao-notebook")
                workflow = self.client.status(workflow_id)
            except ApiException as error:
                if error.status != 0 and error.status < 500:
                    raise
                time.sleep(0.5)
                continue
            review = next((t for t in workflow.get("tasks", [])
                           if t.get("referenceTaskName") == "human_review"), None)
            if for_review and review and review["status"] == "IN_PROGRESS":
                self.show(label, workflow)
                return workflow
            if workflow["status"] == "COMPLETED":
                self.show(label, workflow)
                return workflow
            if workflow["status"] != "RUNNING":
                self.show(label, workflow)
                raise RuntimeError(f"Execution ended with {workflow['status']}. Inspect the task failure below.")
            time.sleep(0.25)
        raise TimeoutError("Still running after three minutes. Rerun this cell to continue the same case.")

    def effect_count(self, label, operation):
        case_id = self.cases[label][0]
        with self.ledger.connect() as db:
            return db.execute("SELECT count(*) FROM effects WHERE key = ?",
                              (f"aao:{case_id}:{operation}",)).fetchone()[0]

    def show(self, label, workflow=None):
        workflow = workflow or self.client.status(self.cases[label][1])
        output = workflow.get("output") or {}
        pending = any(t.get("referenceTaskName") == "human_review" and t["status"] == "IN_PROGRESS"
                      for t in workflow.get("tasks", []))
        if pending:
            message("Waiting for a person", "The case is safely paused in Conductor. No account has been opened. Run the decision cell below.", "#d97706")
        elif workflow["status"] == "COMPLETED":
            outcome = output.get("outcome")
            detail = "A mock account was created." if outcome == "APPROVED" else "No account was created."
            message(f"Case {str(outcome).lower()}", detail + " CRM and case completion finished.",
                    "#15803d" if outcome == "APPROVED" else "#b45309")
        else:
            message("Execution: " + workflow["status"], "Inspect task status below.")
        table(["What to check", "Result"], [
            ("Execution status", workflow["status"]),
            ("Business decision", output.get("outcome") or ("Waiting for review" if pending else "Not finished")),
            ("Account", output.get("account_id") or "None"),
            ("Accounts created for this case", self.effect_count(label, "fulfill")),
            ("Validation issues", "; ".join(output.get("validation_errors") or []) or "None"),
        ])
        tasks = {t["referenceTaskName"]: t for t in workflow.get("tasks", [])}
        table(["Journey step", "Status"], [
            (description, tasks[ref]["status"] if ref in tasks else
             ("Skipped" if workflow["status"] == "COMPLETED" else "Not reached"))
            for ref, description in LABELS.items()
        ])
        table(["Case ID", "Workflow ID"], [self.cases[label]])
        return workflow

    def decide(self, label, decision):
        self.client.review(self.cases[label][1], decision, "synthetic-notebook-reviewer")
        return self.wait(label)

    def check(self, label, expected):
        workflow = self.client.status(self.cases[label][1])
        output = workflow.get("output") or {}
        counts = {op: self.effect_count(label, op) for op in ("fulfill", "crm", "complete")}
        passed = (workflow["status"] == "COMPLETED" and output.get("outcome") == expected and
                  output.get("case_id") == self.cases[label][0] and
                  counts == {"fulfill": int(expected == "APPROVED"), "crm": 1, "complete": 1} and
                  bool(output.get("account_id")) == (expected == "APPROVED"))
        self.results[label] = (expected, output.get("outcome", "Not finished"), counts["fulfill"], passed)
        message("PASS" if passed else "FAIL", label + ": checked decision, account count, CRM, and case completion.",
                "#15803d" if passed else "#dc2626")
        assert passed, f"{label} did not match the expected result. Inspect its displayed status."

    def summary(self):
        labels = ["Automatic approval", "Human approval", "Human rejection", "Invalid application"]
        rows = []
        for label in labels:
            expected, actual, accounts, passed = self.results.get(label, ("—", "Not run", "—", False))
            rows.append((label, expected, actual, accounts, "PASS" if passed else "Not passed"))
        table(["Scenario", "Expected", "Actual", "Accounts created", "Test"], rows)
        if all(label in self.results and self.results[label][3] for label in labels):
            message("All four scenarios passed", "You have tested the live Conductor workflow, including both human decisions.", "#15803d")

    def demonstrate_duplicate_review(self):
        label = "Human approval"
        before = self.effect_count(label, "fulfill")
        result = self.client.review(self.cases[label][1], "APPROVED", "synthetic-notebook-reviewer")
        assert result["duplicate"] and self.effect_count(label, "fulfill") == before == 1
        message("PASS: repeated approval is safe", "Conductor already has this decision. The CLI sends no second update, and there is still exactly one mock account.", "#15803d")
        try:
            self.client.review(self.cases[label][1], "REJECTED", "synthetic-notebook-reviewer")
        except ValueError as error:
            message("PASS: conflicting decision refused", str(error), "#15803d")
        else:
            raise AssertionError("A conflicting review should be refused")
