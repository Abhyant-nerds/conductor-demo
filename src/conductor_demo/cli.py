import argparse
import json
import os
import socket
from pathlib import Path

from conductor_demo.conductor import Conductor
from conductor_demo.services import (
    Ledger, LocalCases, LocalCrm, LocalDocuments, LocalFulfillment,
    LocalGateway, LocalValidation,
)
from conductor_demo.workers import Workers


def main():
    parser = argparse.ArgumentParser(description="Local Conductor OSS AAO proof of concept")
    parser.add_argument("--url", default=os.getenv("CONDUCTOR_SERVER_URL", "http://localhost:8000/api"))
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--definitions", type=Path, default=Path("workflows"))
    workers = commands.add_parser("workers")
    workers.add_argument("--documents", type=Path, default=Path("examples/documents"))
    workers.add_argument("--ledger", type=Path, default=Path(".local/effects.sqlite3"))
    workers.add_argument("--worker-id", default=f"aao-{socket.gethostname()}")
    start = commands.add_parser("start")
    start.add_argument("--case-id", required=True)
    start.add_argument("--document-ref", action="append", required=True)
    status = commands.add_parser("status")
    status.add_argument("workflow_id")
    review = commands.add_parser("review")
    review.add_argument("workflow_id")
    review.add_argument("--decision", choices=("APPROVED", "REJECTED"), required=True)
    review.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    if not args.url.rstrip("/").endswith("/api"):
        parser.error("--url must include /api")
    client = Conductor(args.url.rstrip("/"))
    try:
        if args.command == "register":
            client.register(args.definitions)
            output = {"registered": "aao", "version": 1}
        elif args.command == "workers":
            docs = LocalDocuments(args.documents)
            ledger = Ledger(args.ledger)
            client.run(Workers(docs, docs, LocalGateway(LocalValidation()),
                               LocalFulfillment(ledger), LocalCrm(ledger), LocalCases(ledger)),
                       args.worker_id)
            return
        elif args.command == "start":
            output = {"workflow_id": client.start(args.case_id, args.document_ref)}
        elif args.command == "status":
            output = client.status(args.workflow_id)
        else:
            output = client.review(args.workflow_id, args.decision, args.reviewer)
        print(json.dumps(output, indent=2))
    except ValueError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
