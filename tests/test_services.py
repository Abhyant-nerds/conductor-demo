import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from conductor_demo.services import Ledger, LocalCrm, LocalDocuments, LocalFulfillment, LocalValidation

ROOT = Path(__file__).resolve().parents[1]


class SideEffectTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".local")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.db"
        self.ledger = Ledger(self.path)

    def test_fulfillment_redelivery_survives_restart(self):
        app = {"customer_id": "synthetic"}
        first = LocalFulfillment(self.ledger).fulfill("aao:case:fulfill", app)
        restarted = LocalFulfillment(Ledger(self.path))
        self.assertEqual(first, restarted.fulfill("aao:case:fulfill", app))
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects").fetchone()[0], 1)

    def test_concurrent_crm_delivery_records_one_effect(self):
        crm = LocalCrm(self.ledger)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: crm.update("aao:case:crm", {"decision": "APPROVED"}),
                                    range(8)))
        self.assertEqual(results, [{"updated": True}] * 8)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects").fetchone()[0], 1)

    def test_key_reuse_with_different_payload_rejected(self):
        crm = LocalCrm(self.ledger)
        crm.update("aao:case:crm", {"decision": "APPROVED"})
        with self.assertRaises(ValueError):
            crm.update("aao:case:crm", {"decision": "REJECTED"})

    def test_document_path_escape_rejected(self):
        documents = LocalDocuments(ROOT / "examples/documents")
        with self.assertRaises(ValueError):
            documents.extract(["fixture:../../workflows/aao-v1.json"])
        with self.assertRaises(ValueError):
            documents.extract(["https://bank.example/doc.pdf"])

    def test_malformed_review_flag_cannot_auto_approve(self):
        errors = LocalValidation().validate({"customer_id": "test", "account_type": "SAVINGS",
                                             "kyc_current": True, "needs_review": "true"})
        self.assertIn("needs_review must be a boolean", errors)
