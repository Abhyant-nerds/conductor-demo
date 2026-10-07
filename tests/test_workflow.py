"""Definition mapping tests; the interpreter is NOT a Conductor runtime."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from conductor_demo.services import (
    Ledger, LocalCases, LocalCrm, LocalDocuments, LocalFulfillment,
    LocalGateway, LocalValidation,
)
from conductor_demo.workers import TASK_NAMES, Workers

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = json.loads((ROOT / "workflows/aao-v1.json").read_text())
PATTERN = re.compile(r"\$\{([^}]+)\}")


def resolve(value, context):
    if isinstance(value, dict):
        return {key: resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    if not isinstance(value, str):
        return value

    def lookup(expression):
        result = context
        for key in expression.split("."):
            result = result[key]
        return result

    match = PATTERN.fullmatch(value)
    if match:
        return lookup(match[1])
    return PATTERN.sub(lambda m: str(lookup(m[1])), value)


def walk(tasks):
    for task in tasks:
        yield task
        for branch in task.get("decisionCases", {}).values():
            yield from walk(branch)
        yield from walk(task.get("defaultCase", []))


class DefinitionMappingTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".local")
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / "effects.db")
        documents = LocalDocuments(ROOT / "examples/documents")
        self.workers = Workers(documents, documents, LocalGateway(LocalValidation()),
                               LocalFulfillment(self.ledger), LocalCrm(self.ledger),
                               LocalCases(self.ledger))

    def simulate_mapping(self, fixture, decision=None, intent=None):
        context = {"workflow": {"input": {"case_id": "test-case", "document_refs":
                                           [f"fixture:{fixture}.json"]}, "variables": {}}}
        executed = []

        def run(tasks):
            for task in tasks:
                ref = task["taskReferenceName"]
                data = resolve(task.get("inputParameters", {}), context)
                kind = task["type"]
                if kind == "SIMPLE":
                    output = self.workers.execute(task["name"], data)
                    if ref == "classify_intent" and intent:
                        output["intent"] = intent
                    context[ref] = {"output": output}
                    executed.append(task["name"])
                elif kind == "SET_VARIABLE":
                    context["workflow"]["variables"].update(data)
                elif kind == "SWITCH":
                    self.assertEqual(task["evaluatorType"], "value-param")
                    branch = task["decisionCases"].get(data[task["expression"]],
                                                       task["defaultCase"])
                    if not run(branch):
                        return False
                elif kind == "WAIT":
                    self.assertNotIn("duration", data)
                    self.assertNotIn("until", data)
                    if decision is None:
                        return False
                    context[ref] = {"output": {"decision": decision, "reviewer": "test"}}
                else:
                    self.fail(f"unsupported task in mapping test: {kind}")
            return True

        finished = run(DEFINITION["tasks"])
        return finished, executed, resolve(DEFINITION["outputParameters"], context) if finished else None

    def test_automatic_approval_maps_account_and_crm(self):
        finished, executed, output = self.simulate_mapping("automatic")
        self.assertTrue(finished)
        self.assertEqual(executed, list(TASK_NAMES))
        self.assertEqual(output["outcome"], "APPROVED")
        self.assertTrue(output["account_id"].startswith("mock-"))
        self.assertEqual(output["case_id"], "test-case")
        self.assertEqual(output["validation_errors"], [])

    def test_review_stops_before_side_effects(self):
        finished, executed, output = self.simulate_mapping("review")
        self.assertFalse(finished)
        self.assertEqual(executed, list(TASK_NAMES[:3]))
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects").fetchone()[0], 0)

    def test_review_approval_fulfills(self):
        _, executed, output = self.simulate_mapping("review", "APPROVED")
        self.assertIn("aao_fulfill", executed)
        self.assertEqual(output["outcome"], "APPROVED")

    def test_review_rejection_never_fulfills(self):
        _, executed, output = self.simulate_mapping("review", "REJECTED")
        self.assertNotIn("aao_fulfill", executed)
        self.assertEqual(output["outcome"], "REJECTED")
        self.assertIsNone(output["account_id"])
        self.assertEqual(executed[-2:], ["aao_update_crm", "aao_complete_case"])

    def test_invalid_application_rejected_without_review(self):
        _, executed, output = self.simulate_mapping("invalid")
        self.assertNotIn("aao_fulfill", executed)
        self.assertEqual(output["outcome"], "REJECTED")
        self.assertIn("KYC not current", output["validation_errors"])

    def test_unsupported_intent_rejected(self):
        _, executed, output = self.simulate_mapping("automatic", intent="OTHER")
        self.assertNotIn("aao_fulfill", executed)
        self.assertIn("unsupported intent", output["validation_errors"])

    def test_unknown_review_output_fails_closed(self):
        _, executed, output = self.simulate_mapping("review", "unexpected")
        self.assertNotIn("aao_fulfill", executed)
        self.assertEqual(output["outcome"], "REJECTED")

    def test_unique_references_task_metadata_and_timeouts(self):
        tasks = list(walk(DEFINITION["tasks"]))
        refs = [t["taskReferenceName"] for t in tasks]
        self.assertEqual(len(refs), len(set(refs)))
        simple = {t["name"] for t in tasks if t["type"] == "SIMPLE"}
        definitions = json.loads((ROOT / "workflows/tasks.json").read_text())
        self.assertEqual(simple, set(TASK_NAMES))
        self.assertEqual(simple, {t["name"] for t in definitions})
        for definition in definitions:
            self.assertGreater(definition["retryCount"], 0)
            self.assertLess(definition["responseTimeoutSeconds"], definition["timeoutSeconds"])
            self.assertEqual(definition["timeoutPolicy"], "RETRY")
        self.assertEqual(DEFINITION["timeoutPolicy"], "TIME_OUT_WF")


if __name__ == "__main__":
    unittest.main()
