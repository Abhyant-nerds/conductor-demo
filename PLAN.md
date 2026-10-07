# Conductor OSS AAO proof of concept — implementation plan

## Repository findings

- The repository currently contains only `.gitignore` and `LICENSE`.
- No applicable `AGENTS.md`, application code, API conventions, package manager,
  or Conductor dependency was found in the repository or its parent directories.
- The existing `.gitignore` is Python-oriented. Use a small Python package and
  pip-compatible pinned dependencies rather than introducing a larger platform.
- No intake, document storage, Platform SDK, Validation Services, Generic
  Execution Gateway, Fulfillment Services, or CRM APIs are present.

## Implementation sequence

1. **Verify and pin the OSS runtime and SDK.** Select a released
   `conductor-oss/conductor` server version and the official
   `conductor-oss/python-sdk` package (`conductor-python`). Check the selected
   versions' official documentation and source for registration, polling,
   workflow start/status, task updates, SWITCH/WAIT behavior, and timeout fields.
   Record exact versions and references. Avoid SaaS-only APIs and credentials.

2. **Define the versioned AAO workflow.** Add `workflows/aao-v1.json` and task
   definitions with explicit input/output mappings:
   intake references → classify intent → extract application → validate through
   the Generic Execution Gateway → conditional human review → approved
   fulfillment → CRM update → case completion. Use workflow-level branches to
   handle invalid applications and human rejection without fulfillment. Expose
   a clear business outcome separately from the Conductor execution status.

3. **Implement small workers and service interfaces.** Add a Python package
   under `src/` with separate classification, extraction, gateway validation,
   fulfillment, CRM, and case interfaces. Supply deterministic local adapters
   and synthetic document fixtures. The gateway adapter delegates deterministic
   validation; fulfillment remains a separate component. Workers execute one
   task at a time and return serializable results.

4. **Make side effects safe to retry.** Supply stable operation keys based on
   case ID and operation, not delivery/task-attempt IDs. Use a local SQLite
   ledger with atomic mock side effects and persisted results so duplicate
   delivery and worker restart do not duplicate fulfillment or CRM writes.
   Document the real services' required idempotency contracts and the limits of
   a local ledger for remote operations. Configure bounded task retries,
   response timeouts, execution timeouts, and workflow timeout behavior.

5. **Add registration and workflow controls.** With no existing API to extend,
   provide a CLI for registering definitions, running workers, starting AAO
   with a case ID and document references, and fetching workflow status/output.
   Model review as an untimed durable WAIT task completed through the supported
   task-update API. Provide approve/reject commands that validate the pending
   review task and send a decision plus reviewer reference. Define behavior
   for duplicate/conflicting decisions and attempts to decide before review.

6. **Document local operation and deployment gaps.** Add `README.md`, example
   inputs, environment configuration without secrets, and a pinned local
   server setup using the selected version's supported distribution. Document
   registration, worker startup, automatic approval, human approval, rejection,
   and status commands. Explain that intake/storage and all banking services
   are placeholders. Describe required AWS components without provisioning:
   server and worker compute, supported durable persistence and queue backend,
   private networking, ingress/authentication, secrets, monitoring, backups,
   human-review UI/API authorization, and deployment/version management.

7. **Verify behavior.** Add focused tests for definition mappings, branch
   outcomes, duplicate/restarted side effects, and review decision handling.
   Run relevant Python checks. Where Docker/server access is available, run
   an end-to-end smoke test for no-review approval, paused review followed by
   approval, and rejection. Report any runtime verification that could not be
   performed; do not present mock-only tests as a real Conductor run.

## Intended files

- `pyproject.toml` and pinned dependency file
- `src/conductor_demo/` — CLI, SDK adapter, workers, interfaces, local adapters
- `workflows/` — versioned workflow and task metadata
- `examples/` — synthetic start inputs/documents
- `tests/` — focused unit and mapping tests
- Local server configuration, if supported by the chosen release
- `README.md` — runbook, architecture, verified references, integration gaps

## Completion criteria

A developer can connect to a local OSS server, register AAO v1, start a case,
run its workers, inspect status, approve or reject durable review, and observe
the correct final outcome. Repeated side-effect delivery produces one local
result per operation. Secrets are absent from definitions and committed source.
All executed checks and any unverified integration assumptions are reported.
