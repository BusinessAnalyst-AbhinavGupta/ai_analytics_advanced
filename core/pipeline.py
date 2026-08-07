import os
import json
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any

from core.parser import parse_analytics_logic
from core.normalizer import build_canonical
from core.validator import Validator
from core.neo4j_adapter import Neo4jAdapter
from schema.models import CanonicalKnowledge

class IngestionPipeline:
    """Orchestrates the flow from Raw SQL to Cypher Generation with state logging and checkpointing."""
    
    def __init__(self):
        self.parser = parse_analytics_logic
        self.validator = Validator()
        self.adapter = Neo4jAdapter()
        
        # Ensure output directories exist
        os.makedirs("checkpoints", exist_ok=True)
        os.makedirs("logs", exist_ok=True)

    def _log_event(self, run_id: str, stage: str, status: str, details: Any = None):
        log_file = os.path.join("logs", f"run_{run_id}.json")
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "status": status,
            "details": details
        }
        log_line = json.dumps(event) + "\n"
        with open(log_file, "a") as f:
            f.write(log_line)

    def run(self, raw_sql: str, metadata: Dict[str, Any] = None, run_id: str = None) -> List[str]:
        if not run_id:
            run_id = str(uuid.uuid4())
        self._log_event(run_id, "pipeline", "started", {"raw_sql": raw_sql, "metadata": metadata})
        
        try:
            self._log_event(run_id, "parsing", "started")
            print("Step 1: Parsing SQL Logic...")
            parsed_data = parse_analytics_logic(raw_sql, metadata=metadata)
            self._log_event(run_id, "parsing", "success", {
                "metric_count": len(parsed_data.get("extracted_metrics", []))
            })
            
            print(f"Found {len(parsed_data.get('extracted_metrics', []))} metrics.")

            self._log_event(run_id, "normalization", "started")
            print("Step 2: Normalizing Knowledge...")
            canonical_json = build_canonical(parsed_data, metadata=metadata)
            self._log_event(run_id, "normalization", "success")
            
            self._log_event(run_id, "validation", "started")
            print("Step 3: Validating Canonical JSON...")
            is_valid, warnings = self.validator.validate(canonical_json)
            self._log_event(run_id, "validation", "finished", {
                "is_valid": is_valid,
                "warnings": warnings
            })
            
            if warnings:
                for w in warnings:
                    print(f"[WARN] {w}")
            
            if not is_valid:
                raise ValueError("Validation failed. Cannot proceed to Cypher generation.")

            # Save Checkpoint
            checkpoint_path = os.path.join("checkpoints", f"canonical_{canonical_json.id}.json")
            self._log_event(run_id, "checkpointing", "started", {"path": checkpoint_path})
            print(f"Saving checkpoint to {checkpoint_path}...")
            with open(checkpoint_path, "w") as f:
                f.write(canonical_json.model_dump_json(indent=2))
            self._log_event(run_id, "checkpointing", "success")

            self._log_event(run_id, "cypher_generation", "started")
            print("Step 4: Generating Cypher...")
            cypher_statements = self.adapter.generate_cypher(canonical_json)
            self._log_event(run_id, "cypher_generation", "success", {
                "statement_count": len(cypher_statements)
            })
            
            self._log_event(run_id, "pipeline", "completed")
            return cypher_statements

        except Exception as e:
            self._log_event(run_id, "pipeline", "failed", {"error": str(e)})
            raise

    def ingest(self, raw_sql: str, uri: str, auth: tuple, database: str = None, metadata: Dict[str, Any] = None, run_id: str = None) -> Dict[str, Any]:
        """Runs the parsing, normalization, validation, checkpointing, and database ingestion flow."""
        if not run_id:
            run_id = str(uuid.uuid4())
        self._log_event(run_id, "pipeline_ingest", "started", {"raw_sql": raw_sql, "uri": uri, "database": database, "metadata": metadata})
        checkpoint_path = None
        canonical_id = None
        
        try:
            self._log_event(run_id, "parsing", "started")
            print("Running full Ingestion Pipeline...")
            parsed_data = parse_analytics_logic(raw_sql, metadata=metadata)
            self._log_event(run_id, "parsing", "success")
            
            self._log_event(run_id, "normalization", "started")
            canonical_json = build_canonical(parsed_data, metadata=metadata)
            canonical_id = str(canonical_json.id)
            self._log_event(run_id, "normalization", "success", {"canonical_id": canonical_id})
            
            self._log_event(run_id, "validation", "started")
            is_valid, warnings = self.validator.validate(canonical_json)
            self._log_event(run_id, "validation", "finished", {"is_valid": is_valid, "warnings": warnings})
            
            if warnings:
                for w in warnings:
                    print(f"[WARN] {w}")
            if not is_valid:
                raise ValueError("Validation failed. Cannot proceed to ingestion.")
                
            # Save Checkpoint
            checkpoint_path = os.path.join("checkpoints", f"canonical_{canonical_json.id}.json")
            self._log_event(run_id, "checkpointing", "started", {"path": checkpoint_path})
            print(f"Saving checkpoint to {checkpoint_path}...")
            with open(checkpoint_path, "w") as f:
                f.write(canonical_json.model_dump_json(indent=2))
            self._log_event(run_id, "checkpointing", "success")
                
            self._log_event(run_id, "database_ingestion", "started")
            print("Step 5: Executing database ingestion...")
            results = self.adapter.ingest(canonical_json, uri, auth, database)
            
            # If deep SQL reasoning is available, persist intents, column roles, idioms, and synapses
            deep_data = parsed_data.get("deep_reasoning")
            if deep_data:
                try:
                    from schema.models import DeepSqlReasoning
                    deep_model = DeepSqlReasoning.model_validate(deep_data)
                    self.adapter.ingest_deep_sql_reasoning(deep_model, uri, auth, database, metadata=metadata)
                    print(f"  [Deep Reasoning] Ingested business intent '{deep_model.intent_name}', {len(deep_model.column_usages)} column synapses, and {len(deep_model.sql_idioms)} SQL idioms.")
                except Exception as de:
                    print(f"  [WARN] Deep reasoning persistence notice: {de}")

            # Apply Ingestion-Time Hebbian Baseline Weight Reinforcement (+0.02)
            try:
                from core.graph_learner import GraphLearner
                learner = GraphLearner(uri=uri, auth=auth, database=database)
                tbls = [t.name for t in canonical_json.tables]
                cols = [c.name for c in canonical_json.columns]
                learner.reinforce_ingested_query(tables_used=tbls, columns_used=cols)
                print(f"  [Hebbian Ingestion] Applied baseline weight boost (+0.02) to {len(tbls)} tables and {len(cols)} columns.")
            except Exception as he:
                print(f"  [WARN] Hebbian ingestion reinforcement notice: {he}")

            self._log_event(run_id, "database_ingestion", "success", {"results_count": len(results)})
            
            self._log_event(run_id, "pipeline_ingest", "completed")
            return {
                "run_id": run_id,
                "checkpoint_path": checkpoint_path,
                "canonical_id": canonical_id,
                "canonical_json": canonical_json,
                "deep_reasoning": deep_data,
                "results": results
            }
            
        except Exception as e:
            self._log_event(run_id, "pipeline_ingest", "failed", {"error": str(e), "checkpoint_path": checkpoint_path, "canonical_id": canonical_id})
            raise

    def resume_from_checkpoint(self, checkpoint_path: str, uri: str, auth: tuple, database: str = None, run_id: str = None) -> Dict[str, Any]:
        """Loads a previously validated checkpoint and retries ingestion directly."""
        if not run_id:
            run_id = str(uuid.uuid4())
        self._log_event(run_id, "pipeline_resume", "started", {
            "checkpoint_path": checkpoint_path,
            "uri": uri,
            "database": database
        })
        
        try:
            self._log_event(run_id, "load_checkpoint", "started")
            print(f"Resuming ingestion from checkpoint: {checkpoint_path}")
            with open(checkpoint_path, "r") as f:
                data_dict = json.load(f)
            
            canonical_knowledge = CanonicalKnowledge.model_validate(data_dict)
            canonical_id = str(canonical_knowledge.id)
            self._log_event(run_id, "load_checkpoint", "success", {"canonical_id": canonical_id})
            
            self._log_event(run_id, "database_ingestion", "started")
            print("Executing database ingestion from checkpoint...")
            results = self.adapter.ingest(canonical_knowledge, uri, auth, database)
            self._log_event(run_id, "database_ingestion", "success", {"results_count": len(results)})
            
            self._log_event(run_id, "pipeline_resume", "completed")
            return {
                "run_id": run_id,
                "checkpoint_path": checkpoint_path,
                "canonical_id": canonical_id,
                "canonical_json": canonical_knowledge,
                "results": results
            }
            
        except Exception as e:
            self._log_event(run_id, "pipeline_resume", "failed", {"error": str(e)})
            raise

if __name__ == "__main__":
    test_sql = "SELECT 1"
    pipeline = IngestionPipeline()
    try:
        cyphers = pipeline.run(test_sql)
        print("\nGenerated Cypher:")
        for c in cyphers:
            print(c)
    except Exception as e:
        print(f"Pipeline failed: {e}")
