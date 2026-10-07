"""Thin task handlers; sequencing and branching belong to the workflow JSON."""
from dataclasses import dataclass

from conductor_demo.services import (
    CaseService, CrmService, ExecutionGateway, ExtractionService,
    FulfillmentService, IntentService,
)

TASK_NAMES = ("aao_classify_intent", "aao_extract_application", "aao_validate",
              "aao_fulfill", "aao_update_crm", "aao_complete_case")


@dataclass
class Workers:
    intent: IntentService
    extraction: ExtractionService
    gateway: ExecutionGateway
    fulfillment: FulfillmentService
    crm: CrmService
    cases: CaseService

    def execute(self, name: str, data: dict) -> dict:
        if name == "aao_classify_intent":
            return self.intent.classify(data["document_refs"])
        if name == "aao_extract_application":
            return self.extraction.extract(data["document_refs"])
        if name == "aao_validate":
            return self.gateway.execute(data["operation"], data["application"], data["intent"])
        if name == "aao_fulfill":
            if data["decision"] != "APPROVED":
                raise ValueError("fulfillment requires approval")
            return self.fulfillment.fulfill(data["idempotency_key"], data["application"])
        if name == "aao_update_crm":
            return self.crm.update(data["idempotency_key"], data["payload"])
        if name == "aao_complete_case":
            return self.cases.complete(data["idempotency_key"], data["payload"])
        raise ValueError("unknown task type")
