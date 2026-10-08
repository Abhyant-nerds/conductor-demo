"""Official SDK adapter using only baseline OSS REST endpoints."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from conductor.client.configuration.configuration import Configuration
from conductor.client.http.api_client import ApiClient
from conductor.client.http.api.metadata_resource_api import MetadataResourceApi
from conductor.client.http.api.task_resource_api import TaskResourceApi
from conductor.client.http.api.workflow_resource_api import WorkflowResourceApi
from conductor.client.http.models.start_workflow_request import StartWorkflowRequest
from conductor.client.http.models.task_result import TaskResult
from conductor.client.http.rest import ApiException

from conductor_demo.workers import TASK_NAMES, Workers


class Conductor:
    def __init__(self, url: str, connection=None):
        config = Configuration(server_api_url=url)
        # This OSS demo uses no Orkes authentication/token endpoint.
        config.authentication_settings = None
        config.http_connection = connection
        self.api = ApiClient(config)
        self.metadata = MetadataResourceApi(self.api)
        self.tasks = TaskResourceApi(self.api)
        self.workflows = WorkflowResourceApi(self.api)

    def register(self, directory: Path):
        definitions = json.loads((directory / "tasks.json").read_text(encoding="utf-8"))
        workflow = json.loads((directory / "aao-v1.json").read_text(encoding="utf-8"))
        self.metadata.register_task_def(definitions, _request_timeout=10)
        self.metadata.update1([workflow], _request_timeout=10)

    def start(self, case_id: str, document_refs: list[str]) -> str:
        if not case_id.strip() or not document_refs or any(not r.strip() for r in document_refs):
            raise ValueError("case_id and document_refs are required")
        request = StartWorkflowRequest(name="aao", version=1, correlation_id=case_id,
                                       input={"case_id": case_id, "document_refs": document_refs})
        return self.workflows.start_workflow(request, _request_timeout=10)

    def status(self, workflow_id: str) -> dict:
        workflow = self.workflows.get_execution_status(workflow_id, include_tasks=True,
                                                       _request_timeout=10)
        return self.api.sanitize_for_serialization(workflow)

    def review(self, workflow_id: str, decision: str, reviewer: str) -> dict:
        workflow = self.status(workflow_id)
        task, duplicate = review_target(workflow, decision, reviewer)
        if duplicate:
            return {"decision": decision, "duplicate": True}
        try:
            self.tasks.update_task(TaskResult(
                workflow_instance_id=workflow_id, task_id=task["taskId"],
                status="COMPLETED", worker_id="aao-review-cli",
                output_data={"decision": decision, "reviewer": reviewer},
            ), _request_timeout=10)
        except ApiException as error:
            # The server may commit the decision before its response times out.
            # Read back the exact task instead of blindly sending it again.
            if error.status == 0 or error.status >= 500:
                try:
                    _, completed = review_target(self.status(workflow_id), decision, reviewer)
                except Exception:
                    raise error
                if completed:
                    return {"decision": decision, "duplicate": True, "reconciled": True}
            raise
        return {"decision": decision, "duplicate": False}

    def run_once(self, workers: Workers, worker_id: str) -> int:
        count = 0
        for name in TASK_NAMES:
            task = self.tasks.poll(name, workerid=worker_id, _request_timeout=10)
            if not task or not task.task_id:
                continue
            result = TaskResult(workflow_instance_id=task.workflow_instance_id,
                                task_id=task.task_id, worker_id=worker_id)
            try:
                result.output_data = workers.execute(name, task.input_data)
                result.status = "COMPLETED"
            except (ValueError, KeyError, FileNotFoundError, TypeError):
                result.status = "FAILED_WITH_TERMINAL_ERROR"
                result.reason_for_incompletion = "Invalid task input or local fixture"
            except Exception:
                result.status = "FAILED"
                result.reason_for_incompletion = "Service operation failed; retry permitted"
            # If acknowledgement fails, leave the task for Conductor redelivery.
            # Never reclassify an acknowledgement error as an operation failure.
            self.tasks.update_task(result, _request_timeout=10)
            count += 1
        return count

    def run(self, workers: Workers, worker_id: str):
        while True:
            try:
                self.run_once(workers, worker_id)
            except Exception:
                logging.warning("Conductor poll/update failed; will retry polling")
            time.sleep(0.5)


def review_target(workflow: dict, decision: str, reviewer: str) -> tuple[dict, bool]:
    """Sequential duplicate decisions are safe; concurrent writers need API locking."""
    if decision not in ("APPROVED", "REJECTED") or not reviewer.strip():
        raise ValueError("valid decision and reviewer are required")
    if workflow.get("workflowName") != "aao" or workflow.get("workflowVersion") != 1:
        raise ValueError("review is only supported for AAO v1")
    matches = [t for t in workflow.get("tasks", [])
               if t.get("referenceTaskName") == "human_review"]
    if len(matches) != 1:
        raise ValueError("workflow has no unique human review task yet")
    task = matches[0]
    if task.get("taskType") != "WAIT":
        raise ValueError("review target must be a WAIT task")
    if task["status"] == "COMPLETED":
        if task.get("outputData", {}).get("decision") == decision:
            return task, True
        raise ValueError("review already has a conflicting decision")
    if workflow["status"] != "RUNNING" or task["status"] != "IN_PROGRESS":
        raise ValueError("workflow is not awaiting review")
    return task, False
