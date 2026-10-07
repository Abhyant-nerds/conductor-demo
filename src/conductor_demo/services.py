"""Service boundaries and deterministic local implementations.

The ledger is the mock side effect itself, never a substitute for remote
service idempotency. Real adapters must pass the key to the remote service.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol


class IntentService(Protocol):
    def classify(self, document_refs: list[str]) -> dict: ...


class ExtractionService(Protocol):
    def extract(self, document_refs: list[str]) -> dict: ...


class ValidationService(Protocol):
    def validate(self, application: dict) -> list[str]: ...


class ExecutionGateway(Protocol):
    def execute(self, operation: str, application: dict, intent: str) -> dict: ...


class FulfillmentService(Protocol):
    def fulfill(self, key: str, application: dict) -> dict: ...


class CrmService(Protocol):
    def update(self, key: str, payload: dict) -> dict: ...


class CaseService(Protocol):
    def complete(self, key: str, payload: dict) -> dict: ...


class LocalDocuments:
    """Read synthetic JSON documents already stored by the simulated intake."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def read(self, refs: list[str]) -> dict:
        if not isinstance(refs, list) or len(refs) != 1:
            raise ValueError("demo requires exactly one synthetic document reference")
        ref = refs[0]
        if not isinstance(ref, str) or not ref.startswith("fixture:"):
            raise ValueError("local adapter only accepts fixture:<filename>.json")
        path = (self.root / ref.removeprefix("fixture:")).resolve()
        if path.parent != self.root or path.suffix != ".json":
            raise ValueError("document reference must identify a JSON fixture in the document root")
        return json.loads(path.read_text(encoding="utf-8"))

    def classify(self, document_refs: list[str]) -> dict:
        return {"intent": self.read(document_refs).get("intent", "UNKNOWN")}

    def extract(self, document_refs: list[str]) -> dict:
        return {"application": self.read(document_refs)["application"]}


class LocalValidation:
    def validate(self, application: dict) -> list[str]:
        errors = []
        for name in ("customer_id", "account_type"):
            if not isinstance(application.get(name), str) or not application[name].strip():
                errors.append(f"missing {name}")
        if application.get("account_type") not in ("SAVINGS", "CURRENT"):
            errors.append("unsupported account_type")
        if application.get("kyc_current") is not True:
            errors.append("KYC not current")
        if not isinstance(application.get("needs_review"), bool):
            errors.append("needs_review must be a boolean")
        return errors


class LocalGateway:
    def __init__(self, validation: ValidationService):
        self.validation = validation

    def execute(self, operation: str, application: dict, intent: str) -> dict:
        if operation != "validate_aao":
            raise ValueError("unsupported gateway operation")
        errors = self.validation.validate(application)
        if intent != "AAO":
            errors.append("unsupported intent")
        route = "REJECT" if errors else (
            "REVIEW" if application.get("needs_review") is True else "AUTO"
        )
        return {"route": route, "errors": errors}


class Ledger:
    """Persist an atomic synthetic side effect and reject key/payload conflicts."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS effects "
                       "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def apply(self, key: str, payload: dict, result: dict) -> dict:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload, result FROM effects WHERE key = ?", (key,)).fetchone()
            if row:
                if row[0] != canonical:
                    raise ValueError("idempotency key reused with different input")
                return json.loads(row[1])
            db.execute("INSERT INTO effects VALUES (?, ?, ?)",
                       (key, canonical, json.dumps(result)))
            return result


class LocalFulfillment:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def fulfill(self, key: str, application: dict) -> dict:
        account_id = "mock-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.ledger.apply(key, application, {"account_id": account_id})


class LocalCrm:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def update(self, key: str, payload: dict) -> dict:
        return self.ledger.apply(key, payload, {"updated": True})


class LocalCases:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def complete(self, key: str, payload: dict) -> dict:
        return self.ledger.apply(key, payload, {"case_id": payload["case_id"],
                                              "outcome": payload["decision"],
                                              "account_id": payload.get("account_id")})
