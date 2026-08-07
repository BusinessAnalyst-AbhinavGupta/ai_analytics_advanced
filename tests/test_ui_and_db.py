import os
import json
import uuid
import unittest
from core.db import init_db, create_run, update_run, get_all_runs, get_run, delete_run
from core.pipeline import IngestionPipeline

class TestUIAndDB(unittest.TestCase):
    def setUp(self):
        self.test_db_path = "checkpoints/test_runs.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        init_db(self.test_db_path)

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_db_crud(self):
        run_id = str(uuid.uuid4())
        # Create
        run = create_run(
            run_id=run_id,
            sql_query="SELECT COUNT(*) FROM orders;",
            journey_stage_or_page="Checkout",
            service_line="Mobile",
            category="Conversion",
            natco="DE",
            tags="Funnel, Core",
            status="RUNNING",
            db_path=self.test_db_path
        )
        self.assertEqual(run["run_id"], run_id)
        self.assertEqual(run["status"], "RUNNING")
        self.assertEqual(run["natco"], "DE")
        self.assertEqual(run["service_line"], "Mobile")

        # Update
        updated = update_run(
            run_id=run_id,
            status="SUCCESS",
            checkpoint_path="checkpoints/dummy.json",
            canonical_id="canonical-123",
            db_path=self.test_db_path
        )
        self.assertEqual(updated["status"], "SUCCESS")
        self.assertEqual(updated["checkpoint_path"], "checkpoints/dummy.json")

        # Get All
        runs = get_all_runs(self.test_db_path)
        self.assertEqual(len(runs), 1)

        # Delete
        delete_run(run_id, self.test_db_path)
        self.assertIsNone(get_run(run_id, self.test_db_path))

    def test_pipeline_with_form_metadata(self):
        sql = """
        SELECT 
            session_id,
            COUNT(DISTINCT item_id) AS items_in_cart
        FROM cart_events
        WHERE event_date >= '2026-01-01'
        GROUP BY session_id;
        """
        metadata = {
            "journey_stage": "Cart",
            "journey_stage_or_page": "Cart Page",
            "service_line": "Broadband",
            "category": "Traffic & Funnel",
            "natco": "UK",
            "tags": "Cart, Primary, KPI",
            "owner": "Broadband Analytics"
        }
        
        pipeline = IngestionPipeline()
        cyphers = pipeline.run(sql, metadata=metadata)
        self.assertTrue(len(cyphers) > 0)
        
        # Verify that the generated Cyphers contain our metadata nodes
        joined_cypher = "\n".join(cyphers)
        self.assertIn("Broadband", joined_cypher)
        self.assertIn("Traffic & Funnel", joined_cypher)
        self.assertIn("UK", joined_cypher)
        self.assertIn("Cart", joined_cypher)

if __name__ == "__main__":
    unittest.main()
