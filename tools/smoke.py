"""Opt-in live OSS smoke test. Run from repository root against a local server."""
import argparse
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from conductor_demo.conductor import Conductor
from conductor_demo.services import (
    Ledger, LocalCases, LocalCrm, LocalDocuments, LocalFulfillment,
    LocalGateway, LocalValidation,
)
from conductor_demo.workers import Workers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000/api")
    args = parser.parse_args()
    if urlparse(args.url).hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("smoke test only accepts a local server")
    client = Conductor(args.url)
    client.register(Path("workflows"))
    Path(".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".local") as directory:
        ledger = Ledger(Path(directory) / "effects.db")

        def new_workers():
            docs = LocalDocuments(Path("examples/documents"))
            return Workers(docs, docs, LocalGateway(LocalValidation()),
                           LocalFulfillment(ledger), LocalCrm(ledger), LocalCases(ledger))

        for fixture, review, outcome in (
            ("automatic", None, "APPROVED"),
            ("review", "APPROVED", "APPROVED"),
            ("review", "REJECTED", "REJECTED"),
            ("invalid", None, "REJECTED"),
        ):
            case = "smoke-" + uuid.uuid4().hex
            workflow_id = client.start(case, [f"fixture:{fixture}.json"])
            workers = new_workers()
            deadline = time.monotonic() + 90
            reviewed = False
            while time.monotonic() < deadline:
                client.run_once(workers, "aao-smoke")
                workflow = client.status(workflow_id)
                if workflow["status"] == "COMPLETED":
                    assert workflow["output"]["outcome"] == outcome, workflow
                    assert workflow["output"]["case_id"] == case, workflow
                    assert reviewed == bool(review), workflow
                    with ledger.connect() as db:
                        count = db.execute("SELECT count(*) FROM effects WHERE key = ?",
                                           (f"aao:{case}:fulfill",)).fetchone()[0]
                    assert count == (1 if outcome == "APPROVED" else 0), workflow
                    print(f"PASS {fixture}/{review or 'no-review'}: {workflow_id}")
                    break
                if workflow["status"] != "RUNNING":
                    raise RuntimeError(f"Unexpected terminal state: {workflow}")
                waiting = any(t["referenceTaskName"] == "human_review" and
                              t["status"] == "IN_PROGRESS" for t in workflow["tasks"])
                if waiting and review and not reviewed:
                    # Leave the WAIT alone, then reconstruct workers/client to model restart.
                    time.sleep(1)
                    client = Conductor(args.url)
                    assert any(t["referenceTaskName"] == "human_review" and
                               t["status"] == "IN_PROGRESS" for t in client.status(workflow_id)["tasks"])
                    workers = new_workers()
                    client.review(workflow_id, review, "synthetic-smoke-reviewer")
                    reviewed = True
                time.sleep(0.25)
            else:
                raise TimeoutError(f"Workflow did not complete: {workflow_id}")


if __name__ == "__main__":
    main()
