"""Exercise real pinned SDK serialization and routes with mocked HTTP transport."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from conductor_demo.conductor import Conductor, review_target
from conductor_demo.workers import TASK_NAMES

ROOT = Path(__file__).resolve().parents[1]


def pending_review():
    return {"workflowName": "aao", "workflowVersion": 1, "status": "RUNNING",
            "workflowId": "wf-1", "tasks": [
                {"referenceTaskName": "human_review", "taskType": "WAIT",
                 "taskId": "wait-1", "status": "IN_PROGRESS", "outputData": {}}
            ]}


class ReviewTests(unittest.TestCase):
    def test_pending_approval_and_rejection(self):
        for decision in ("APPROVED", "REJECTED"):
            _, duplicate = review_target(pending_review(), decision, "reviewer-1")
            self.assertFalse(duplicate)

    def test_duplicate_is_noop_and_conflict_rejected(self):
        workflow = pending_review()
        task = workflow["tasks"][0]
        task.update(status="COMPLETED", outputData={"decision": "REJECTED", "reviewer": "first"})
        workflow["status"] = "COMPLETED"
        self.assertTrue(review_target(workflow, "REJECTED", "second")[1])
        with self.assertRaises(ValueError):
            review_target(workflow, "APPROVED", "second")

    def test_no_review_wrong_workflow_and_terminal_status_rejected(self):
        for alteration in ({"tasks": []}, {"workflowName": "other"},
                           {"workflowVersion": 2}, {"status": "TIMED_OUT"}):
            workflow = pending_review()
            workflow.update(alteration)
            with self.assertRaises(ValueError):
                review_target(workflow, "APPROVED", "reviewer")

    def test_empty_reviewer_or_invalid_decision_rejected(self):
        for decision, reviewer in (("APPROVED", " "), ("yes", "reviewer")):
            with self.assertRaises(ValueError):
                review_target(pending_review(), decision, reviewer)


class SdkContractTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.workflow = pending_review()
        self.polled = False
        self.fail_ack = False
        self.timeout_review = False

        def handle(request):
            body = json.loads(request.content) if request.content else None
            self.requests.append((request.method, request.url.path, body))
            if request.url.path == "/api/workflow" and request.method == "POST":
                return httpx.Response(200, text="wf-1")
            if request.url.path == "/api/workflow/wf-1":
                return httpx.Response(200, json=copy.deepcopy(self.workflow))
            if request.url.path.startswith("/api/tasks/poll/"):
                self.assertEqual(request.url.params["workerid"], "test-worker")
                if request.url.path.endswith(TASK_NAMES[0]) and not self.polled:
                    self.polled = True
                    return httpx.Response(200, json={"taskId": "t-1", "workflowInstanceId": "wf-1",
                        "status": "IN_PROGRESS", "taskType": TASK_NAMES[0],
                        "inputData": {"document_refs": ["fixture:automatic.json"]}})
                return httpx.Response(204)
            if request.url.path == "/api/tasks":
                if self.timeout_review:
                    self.workflow["tasks"][0].update(status="COMPLETED", outputData=body["outputData"])
                    raise httpx.ReadTimeout("synthetic lost response", request=request)
                if self.fail_ack:
                    return httpx.Response(503, text="unavailable")
                return httpx.Response(200, text=body["taskId"])
            if request.url.path.startswith("/api/metadata/"):
                return httpx.Response(200, json={})
            self.fail(f"unexpected endpoint: {request.url}")

        connection = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(connection.close)
        self.client = Conductor("http://localhost:8000/api", connection=connection)

    def test_registration_uses_oss_metadata_endpoints(self):
        self.client.register(ROOT / "workflows")
        self.assertEqual([(m, p) for m, p, _ in self.requests],
                         [("POST", "/api/metadata/taskdefs"), ("PUT", "/api/metadata/workflow")])
        self.assertEqual(self.requests[-1][2][0]["version"], 1)

    def test_start_request_maps_case_and_document_refs(self):
        self.assertEqual(self.client.start("case-1", ["fixture:review.json"]), "wf-1")
        method, path, body = self.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/workflow"))
        self.assertEqual(body["name"], "aao")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["correlationId"], "case-1")
        self.assertEqual(body["input"], {"case_id": "case-1", "document_refs": ["fixture:review.json"]})

    def test_empty_start_input_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.client.start("", [])
        self.assertEqual(self.requests, [])

    def test_status_and_review_serialization_targets_wait_task_id(self):
        result = self.client.review("wf-1", "REJECTED", "reviewer-1")
        self.assertFalse(result["duplicate"])
        method, path, body = self.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/tasks"))
        self.assertEqual(body["taskId"], "wait-1")
        self.assertEqual(body["workflowInstanceId"], "wf-1")
        self.assertEqual(body["status"], "COMPLETED")
        self.assertEqual(body["outputData"], {"decision": "REJECTED", "reviewer": "reviewer-1"})

    def test_duplicate_review_does_not_update_task(self):
        self.workflow["tasks"][0].update(status="COMPLETED", outputData={"decision": "APPROVED"})
        self.assertTrue(self.client.review("wf-1", "APPROVED", "reviewer")["duplicate"])
        self.assertEqual(len(self.requests), 1)

    def test_review_reconciles_committed_decision_after_response_timeout(self):
        self.timeout_review = True
        result = self.client.review("wf-1", "APPROVED", "reviewer")
        self.assertTrue(result["reconciled"])
        self.assertEqual(len([p for m, p, _ in self.requests if m == "POST"]), 1)

    def test_worker_polls_executes_and_acknowledges(self):
        workers = Mock()
        workers.execute.return_value = {"intent": "AAO"}
        self.assertEqual(self.client.run_once(workers, "test-worker"), 1)
        workers.execute.assert_called_once_with(TASK_NAMES[0],
                                                {"document_refs": ["fixture:automatic.json"]})
        updates = [b for m, p, b in self.requests if p == "/api/tasks"]
        self.assertEqual(updates[0]["status"], "COMPLETED")
        self.assertEqual(updates[0]["outputData"], {"intent": "AAO"})

    def test_transient_service_error_is_retryable(self):
        workers = Mock()
        workers.execute.side_effect = OSError("synthetic service unavailable")
        self.client.run_once(workers, "test-worker")
        updates = [b for _, p, b in self.requests if p == "/api/tasks"]
        self.assertEqual(updates[0]["status"], "FAILED")

    def test_invalid_input_is_terminal(self):
        workers = Mock()
        workers.execute.side_effect = ValueError("invalid document")
        self.client.run_once(workers, "test-worker")
        updates = [b for _, p, b in self.requests if p == "/api/tasks"]
        self.assertEqual(updates[0]["status"], "FAILED_WITH_TERMINAL_ERROR")

    def test_ack_failure_does_not_send_second_failure_result(self):
        self.fail_ack = True
        workers = Mock()
        workers.execute.return_value = {"intent": "AAO"}
        with self.assertRaises(Exception):
            self.client.run_once(workers, "test-worker")
        updates = [b for _, p, b in self.requests if p == "/api/tasks"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["status"], "COMPLETED")
