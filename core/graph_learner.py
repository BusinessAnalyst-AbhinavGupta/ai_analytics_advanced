import os
import re
import json
import uuid
import datetime
from typing import Dict, Any, List, Optional
from neo4j import GraphDatabase

class GraphLearner:
    """
    Hebbian Learning & Feedback Engine for the Neo4j Knowledge Graph.
    Dynamically manages synaptic edge weights, verified golden queries,
    error signatures, and learned column aliases.
    """

    def __init__(self, uri: str = "neo4j://127.0.0.1:7687", auth: tuple = None, database: str = "neo4j"):
        self.uri = uri or os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
        self.auth = auth or (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
        self.database = database or "neo4j"

    def _get_driver(self):
        return GraphDatabase.driver(self.uri, auth=self.auth)

    def reinforce_success(
        self,
        question: str,
        sql: str,
        tables_used: List[str],
        columns_used: Optional[List[str]] = None,
        metrics_used: Optional[List[str]] = None,
        dialect: str = "AWS Athena / Presto",
        notes: str = ""
    ) -> Dict[str, Any]:
        """
        Positive Reinforcement (Long-Term Potentiation):
        Strengthens connection weights on tables, columns, and metrics used.
        Persists a VerifiedGoldenQuery node in the Knowledge Graph.
        """
        driver = self._get_driver()
        query_id = f"gq_{uuid.uuid4().hex[:8]}"
        boosted_tables = []
        boosted_columns = []
        boosted_metrics = []

        try:
            with driver.session(database=self.database) as session:
                # 1. Boost Table weights
                for tname in tables_used:
                    session.run("""
                        MATCH (t:Table {name: $tname})
                        SET t.success_count = coalesce(t.success_count, 0) + 1,
                            t.weight = CASE WHEN coalesce(t.weight, 0.5) + 0.05 > 1.0 THEN 1.0 ELSE coalesce(t.weight, 0.5) + 0.05 END,
                            t.last_verified_at = datetime()
                    """, tname=tname)
                    boosted_tables.append(tname)

                # 2. Boost Column weights
                if columns_used:
                    for cname in columns_used:
                        session.run("""
                            MATCH (c:Column {name: $cname})
                            SET c.success_count = coalesce(c.success_count, 0) + 1,
                                c.weight = CASE WHEN coalesce(c.weight, 0.5) + 0.05 > 1.0 THEN 1.0 ELSE coalesce(c.weight, 0.5) + 0.05 END,
                                c.last_verified_at = datetime()
                        """, cname=cname)
                        
                        # Also boost HAS_COLUMN / CONTAINS relationship
                        session.run("""
                            MATCH (t:Table)-[r:HAS_COLUMN|CONTAINS]->(c:Column {name: $cname})
                            SET r.success_count = coalesce(r.success_count, 0) + 1,
                                r.weight = CASE WHEN coalesce(r.weight, 0.5) + 0.05 > 1.0 THEN 1.0 ELSE coalesce(r.weight, 0.5) + 0.05 END,
                                r.last_verified_at = datetime()
                        """, cname=cname)
                        boosted_columns.append(cname)

                # 3. Boost Metric weights
                if metrics_used:
                    for mname in metrics_used:
                        session.run("""
                            MATCH (m:Metric {name: $mname})
                            SET m.success_count = coalesce(m.success_count, 0) + 1,
                                m.weight = CASE WHEN coalesce(m.weight, 0.5) + 0.05 > 1.0 THEN 1.0 ELSE coalesce(m.weight, 0.5) + 0.05 END,
                                m.last_verified_at = datetime()
                        """, mname=mname)
                        boosted_metrics.append(mname)

                # 4. Save VerifiedGoldenQuery Node
                session.run("""
                    MERGE (q:VerifiedGoldenQuery {id: $query_id})
                    SET q.question = $question,
                        q.sql = $sql,
                        q.dialect = $dialect,
                        q.notes = $notes,
                        q.verified_at = datetime(),
                        q.weight = 1.0
                    WITH q
                    UNWIND $tables_used AS tname
                    MATCH (t:Table {name: tname})
                    MERGE (q)-[:USES_TABLE]->(t)
                """, query_id=query_id, question=question, sql=sql, dialect=dialect, notes=notes, tables_used=tables_used)

            return {
                "status": "success",
                "message": "Graph Brain Synapses Strengthened (+0.05 Weight Boost)",
                "golden_query_id": query_id,
                "boosted_tables": boosted_tables,
                "boosted_columns": boosted_columns,
                "boosted_metrics": boosted_metrics
            }
        finally:
            driver.close()

    def penalize_failure(
        self,
        tables_used: List[str],
        columns_used: Optional[List[str]] = None,
        error_msg: str = ""
    ) -> Dict[str, Any]:
        """
        Negative Reinforcement (Synaptic Depression):
        Reduces connection weights on problematic paths and increments failure count.
        """
        driver = self._get_driver()
        try:
            with driver.session(database=self.database) as session:
                for tname in tables_used:
                    session.run("""
                        MATCH (t:Table {name: $tname})
                        SET t.failure_count = coalesce(t.failure_count, 0) + 1,
                            t.weight = CASE WHEN coalesce(t.weight, 0.5) - 0.10 < 0.05 THEN 0.05 ELSE coalesce(t.weight, 0.5) - 0.10 END
                    """, tname=tname)

                if columns_used:
                    for cname in columns_used:
                        session.run("""
                            MATCH (c:Column {name: $cname})
                            SET c.failure_count = coalesce(c.failure_count, 0) + 1,
                                c.weight = CASE WHEN coalesce(c.weight, 0.5) - 0.15 < 0.05 THEN 0.05 ELSE coalesce(c.weight, 0.5) - 0.15 END
                        """, cname=cname)

            return {"status": "penalized", "message": "Problematic Synapses Depressed (-0.15 Weight)"}
        finally:
            driver.close()

    def record_correction_rule(
        self,
        table_name: str,
        rule_text: str,
        rule_type: str = "SYNTAX_OR_COLUMN",
        invalid_term: Optional[str] = None,
        correct_term: Optional[str] = None,
        error_snippet: str = "",
        failed_sql: str = "",
        healed_sql: str = ""
    ) -> Dict[str, Any]:
        """
        Episodic Memory Formation:
        Stores a learned correction rule and registers column synonyms into Neo4j.
        """
        driver = self._get_driver()
        rule_id = f"rule_{uuid.uuid4().hex[:8]}"

        try:
            with driver.session(database=self.database) as session:
                # 1. Create CorrectionRule Node
                session.run("""
                    MERGE (r:CorrectionRule {id: $rule_id})
                    SET r.rule_text = $rule_text,
                        r.rule_type = $rule_type,
                        r.table_name = $table_name,
                        r.invalid_term = $invalid_term,
                        r.correct_term = $correct_term,
                        r.error_snippet = $error_snippet,
                        r.failed_sql = $failed_sql,
                        r.healed_sql = $healed_sql,
                        r.created_at = datetime(),
                        r.times_applied = 1,
                        r.weight = 1.0
                    WITH r
                    OPTIONAL MATCH (t:Table {name: $table_name})
                    FOREACH (_ IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END |
                        MERGE (t)-[:HAS_CORRECTION_RULE]->(r)
                    )
                """, rule_id=rule_id, rule_text=rule_text, rule_type=rule_type, table_name=table_name,
                     invalid_term=invalid_term or "", correct_term=correct_term or "",
                     error_snippet=error_snippet[:500], failed_sql=failed_sql, healed_sql=healed_sql)

                # 2. Register Column Synonym / Alias if terms provided
                if invalid_term and correct_term:
                    session.run("""
                        MATCH (c:Column {name: $correct_term})
                        MERGE (s:Synonym {term: $invalid_term})
                        MERGE (c)-[alias:HAS_ALIAS]->(s)
                        SET alias.weight = 1.0,
                            alias.source = 'error_auto_healer',
                            alias.updated_at = datetime()
                    """, correct_term=correct_term, invalid_term=invalid_term)

            return {
                "status": "recorded",
                "rule_id": rule_id,
                "rule_text": rule_text,
                "synonym_registered": bool(invalid_term and correct_term)
            }
        finally:
            driver.close()

    def get_learned_rules(self, table_names: List[str]) -> List[Dict[str, Any]]:
        """Retrieves active correction rules and learned synonyms for the given tables."""
        driver = self._get_driver()
        rules = []
        try:
            with driver.session(database=self.database) as session:
                # Fetch rules linked to tables or global rules
                res = session.run("""
                    MATCH (r:CorrectionRule)
                    WHERE $tables IS NULL OR size($tables) = 0 OR r.table_name IN $tables OR r.table_name = 'GLOBAL' OR r.table_name IS NULL
                    RETURN r.id as id, r.rule_text as rule_text, r.rule_type as rule_type,
                           r.invalid_term as invalid_term, r.correct_term as correct_term,
                           r.table_name as table_name, r.weight as weight,
                           r.created_at as created_at
                    ORDER BY created_at DESC LIMIT 10
                """, tables=table_names or [])
                for rec in res:
                    rules.append(dict(rec))

                # Fetch column synonyms
                syn_res = session.run("""
                    MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)-[alias:HAS_ALIAS]->(s:Synonym)
                    WHERE $tables IS NULL OR size($tables) = 0 OR t.name IN $tables
                    RETURN c.name as column_name, s.term as invalid_term, t.name as table_name
                """, tables=table_names or [])
                for rec in syn_res:
                    rules.append({
                        "id": f"syn_{rec['column_name']}_{rec['invalid_term']}",
                        "rule_text": f"Do NOT use column `{rec['invalid_term']}` for table `{rec['table_name']}`; use `{rec['column_name']}` instead.",
                        "rule_type": "COLUMN_SYNONYM",
                        "invalid_term": rec["invalid_term"],
                        "correct_term": rec["column_name"],
                        "table_name": rec["table_name"],
                        "weight": 1.0
                    })
            return rules
        except Exception as e:
            print(f"[GraphLearner] get_learned_rules warning: {e}")
            return []
        finally:
            driver.close()

    def get_golden_queries(self, table_names: List[str] = None, question: str = "", stage: Optional[str] = None, limit: int = 2) -> List[Dict[str, Any]]:
        """Retrieves top verified golden queries for few-shot context, matched by question keywords or stage."""
        driver = self._get_driver()
        golden = []
        try:
            q_keywords = [w.lower() for w in re.findall(r'\w+', question or "") if len(w) > 2]
            with driver.session(database=self.database) as session:
                res = session.run("""
                    MATCH (gq:VerifiedGoldenQuery)
                    OPTIONAL MATCH (intent:BusinessIntent)-[:HAS_GOLDEN_TEMPLATE]->(gq)
                    WITH gq, intent,
                         CASE WHEN $stage IS NOT NULL AND (gq.journey_stage = $stage OR intent.journey_stage = $stage) THEN 3
                              WHEN $stage IS NOT NULL AND toLower(coalesce(gq.name, gq.question, '')) CONTAINS toLower($stage) THEN 2
                              ELSE 0 END as stage_score,
                         coalesce(gq.name, gq.question, '') as gq_text,
                         coalesce(gq.intent, intent.description, '') as gq_intent
                    WITH gq, intent, stage_score, gq_text, gq_intent,
                         reduce(acc = 0, kw IN $keywords | acc + CASE WHEN toLower(gq_text + ' ' + gq_intent) CONTAINS kw THEN 2 ELSE 0 END) as kw_score
                    ORDER BY (stage_score + kw_score) DESC, gq.last_verified DESC
                    RETURN DISTINCT coalesce(gq.question, gq.name) as question, gq.sql as sql, gq.dialect as dialect,
                                    coalesce(gq.intent, intent.description, gq.notes) as notes,
                                    coalesce(gq.journey_stage, intent.journey_stage) as stage_name,
                                    intent.name as intent_name, gq.card_id as card_id
                    LIMIT $limit
                """, stage=stage, keywords=q_keywords, limit=limit)
                
                for rec in res:
                    golden.append(dict(rec))
            return golden
        except Exception as e:
            print(f"[GraphLearner] get_golden_queries warning: {e}")
            return []
        finally:
            driver.close()

    def get_sql_idioms(self, stage: Optional[str] = None, question: str = "", limit: int = 4) -> List[Dict[str, Any]]:
        """Retrieves learned SQL idioms and architectural patterns from Neo4j."""
        driver = self._get_driver()
        idioms = []
        try:
            q_keywords = [w.lower() for w in re.findall(r'\w+', question or "") if len(w) > 2]
            with driver.session(database=self.database) as session:
                res = session.run("""
                    MATCH (i:SqlIdiom)
                    OPTIONAL MATCH (intent:BusinessIntent)-[:IMPLEMENTS_IDIOM]->(i)
                    WITH i, count(intent) as usage_cnt,
                         CASE WHEN $stage IS NOT NULL AND (any(st IN collect(intent.journey_stage) WHERE st = $stage) OR i.journey_stage = $stage) THEN 3
                              WHEN $stage IS NOT NULL AND toLower(i.name) CONTAINS toLower($stage) THEN 2
                              ELSE 0 END as stage_match,
                         coalesce(i.name, '') + ' ' + coalesce(i.description, '') + ' ' + coalesce(i.category, '') as idiom_text
                    WITH i, usage_cnt, stage_match,
                         reduce(acc = 0, kw IN $keywords | acc + CASE WHEN toLower(idiom_text) CONTAINS kw THEN 2 ELSE 0 END) as kw_score
                    ORDER BY (stage_match + kw_score) DESC, usage_cnt DESC
                    RETURN DISTINCT i.name as name, i.category as category, i.description as description,
                                    i.sql_skeleton as template, i.when_to_use as when_to_use
                    LIMIT $limit
                """, stage=stage, keywords=q_keywords, limit=limit)
                for rec in res:
                    idioms.append(dict(rec))
            return idioms
        except Exception as e:
            print(f"[GraphLearner] get_sql_idioms warning: {e}")
            return []
        finally:
            driver.close()

    def get_learned_domain_rules(self, stage: Optional[str] = None, limit: int = 6) -> List[Dict[str, Any]]:
        """Retrieves learned domain rules (LearnedRule nodes) distilled from Metabase dashboards."""
        driver = self._get_driver()
        rules = []
        try:
            with driver.session(database=self.database) as session:
                res = session.run("""
                    MATCH (r:LearnedRule)
                    OPTIONAL MATCH (intent:BusinessIntent)-[:APPLIES_RULE]->(r)
                    WITH r, count(intent) as usage_cnt,
                         CASE WHEN $stage IS NOT NULL AND any(st IN collect(intent.journey_stage) WHERE st = $stage) THEN 2 ELSE 0 END as stage_match
                    ORDER BY stage_match DESC, usage_cnt DESC
                    RETURN DISTINCT r.description as description, r.rule_type as rule_type,
                                    r.reasoning as reasoning
                    LIMIT $limit
                """, stage=stage, limit=limit)
                for rec in res:
                    rules.append(dict(rec))
            return rules
        except Exception as e:
            print(f"[GraphLearner] get_learned_domain_rules warning: {e}")
            return []
        finally:
            driver.close()

    def get_learned_aliases(self, table_names: Optional[List[str]] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """Retrieves dynamically learned column aliases and business shorthand terms from Neo4j."""
        driver = self._get_driver()
        aliases = []
        try:
            with driver.session(database=self.database) as session:
                res = session.run("""
                    MATCH (c:Column)-[r:ALIASED_AS]->(a:Alias)
                    OPTIONAL MATCH (t:Table)-[:HAS_COLUMN]->(c)
                    WHERE $tables IS NULL OR size($tables) = 0 OR t.name IN $tables OR c.table_name IN $tables
                    RETURN DISTINCT coalesce(t.name, c.table_name) as table_name,
                                    c.name as physical_column,
                                    a.name as alias,
                                    r.expression as expression,
                                    coalesce(r.frequency, 1) as frequency
                    ORDER BY frequency DESC, alias
                    LIMIT $limit
                """, tables=table_names, limit=limit)
                for rec in res:
                    aliases.append(dict(rec))
            return aliases
        except Exception as e:
            print(f"[GraphLearner] get_learned_aliases warning: {e}")
            return []
        finally:
            driver.close()

    def get_column_business_definitions(
        self,
        table_names: Optional[List[str]] = None,
        question: str = "",
        limit: int = 6
    ) -> Dict[str, Any]:
        """
        Extracts nuanced business filter definitions from verified golden queries.

        Parses the WHERE-clause filter conditions from production-verified Metabase SQL
        to build a per-column dictionary of known business filter values.

        Example output:
          {
            "action": ["appointmentBooked", "appointmentViewed", "onecheckoutinitiated"],
            "page_name": ["appointment/start", "checkout/confirmation"],
            "status": ["COMPLETED", "CANCELLED", "NO_SHOW"],
            "_source_queries": [{"question": "...", "sql": "..."}]
          }
        This is injected into the LLM prompt so it knows the exact event/status
        values that define each business concept without having to infer from raw SQL.
        """
        driver = self._get_driver()
        definitions: Dict[str, set] = {}
        source_queries = []

        try:
            with driver.session(database=self.database) as session:
                # Pull golden queries linked to relevant tables
                res = session.run("""
                    MATCH (gq:VerifiedGoldenQuery)
                    OPTIONAL MATCH (gq)-[:USES_TABLE]->(t:Table)
                    WHERE $tables IS NULL OR size($tables) = 0 OR t.name IN $tables
                    WITH gq, collect(t.name) as linked_tables
                    WHERE gq.sql IS NOT NULL AND size(gq.sql) > 20
                    RETURN gq.question as question, gq.sql as sql, gq.dialect as dialect
                    ORDER BY gq.weight DESC
                    LIMIT $limit
                """, tables=table_names or [], limit=limit)

                for rec in res:
                    sql_text = rec.get("sql", "") or ""
                    intent = rec.get("question", "") or ""
                    if not sql_text:
                        continue
                    source_queries.append({"question": intent, "sql": sql_text[:600]})

                    # Extract: column = 'value'  or  column IN ('a','b','c')
                    # Pattern 1: col = 'val'  or  col = "val"
                    eq_matches = re.findall(
                        r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*[\'"]([^\'"]{1,80})[\'"]\s',
                        sql_text,
                        re.IGNORECASE
                    )
                    for col, val in eq_matches:
                        col_lower = col.lower()
                        # Skip boring comparison columns (dates, ids, numbers)
                        if any(skip in col_lower for skip in ["date", "time", "id", "count", "year", "month", "day", "limit", "offset"]):
                            continue
                        if col_lower not in definitions:
                            definitions[col_lower] = set()
                        definitions[col_lower].add(val.strip())

                    # Pattern 2: col IN ('a', 'b', 'c')
                    in_matches = re.findall(
                        r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s+IN\s*\(([^)]{1,500})\)',
                        sql_text,
                        re.IGNORECASE
                    )
                    for col, vals_raw in in_matches:
                        col_lower = col.lower()
                        if any(skip in col_lower for skip in ["date", "time", "id", "count", "year", "month", "day"]):
                            continue
                        extracted_vals = re.findall(r"['\"]([^'\"]{1,80})['\"]", vals_raw)
                        if col_lower not in definitions:
                            definitions[col_lower] = set()
                        definitions[col_lower].update(extracted_vals)

                    # Pattern 3: LIKE 'pattern%'
                    like_matches = re.findall(
                        r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s+LIKE\s+[\'"]([^\'"]{1,80})[\'"]\s',
                        sql_text,
                        re.IGNORECASE
                    )
                    for col, pattern in like_matches:
                        col_lower = col.lower()
                        if col_lower not in definitions:
                            definitions[col_lower] = set()
                        definitions[col_lower].add(f"LIKE '{pattern}'")

        except Exception as e:
            print(f"[GraphLearner] get_column_business_definitions warning: {e}")
        finally:
            driver.close()

        # Convert sets to sorted lists, keep only columns with ≥1 known values
        result = {col: sorted(list(vals)) for col, vals in definitions.items() if vals}
        result["_source_queries"] = source_queries
        return result

    def get_stage_synapses(self, stage_name: str) -> List[Dict[str, Any]]:
        """Retrieves reinforced column synapses for a given funnel stage."""
        driver = self._get_driver()
        synapses = []
        try:
            with driver.session(database=self.database) as session:
                res = session.run("""
                    MATCH (c:Column)-[syn:SYNAPSE_REINFORCED]->(s:JourneyStage {name: $stage})
                    RETURN c.name as column_name, syn.weight as weight, syn.activation_count as activations
                    ORDER BY syn.weight DESC, syn.activation_count DESC LIMIT 15
                """, stage=stage_name)
                for rec in res:
                    synapses.append(dict(rec))
            return synapses
        except Exception as e:
            print(f"[GraphLearner] get_stage_synapses warning: {e}")
            return []
        finally:
            driver.close()

    def get_synaptic_health_stats(self) -> Dict[str, Any]:
        """Returns high-level statistics on graph synaptic health and memory."""
        driver = self._get_driver()
        try:
            with driver.session(database=self.database) as session:
                golden_cnt = session.run("MATCH (q:VerifiedGoldenQuery) RETURN count(q) as c").single()["c"]
                rules_cnt = session.run("MATCH (r:CorrectionRule) RETURN count(r) as c").single()["c"]
                synonym_cnt = session.run("MATCH (s:Synonym) RETURN count(s) as c").single()["c"]
                
                # Average weight of table & column connections
                avg_weight_rec = session.run("""
                    MATCH ()-[r:HAS_COLUMN|CONTAINS]->()
                    RETURN avg(coalesce(r.weight, 0.5)) as avg_w, max(coalesce(r.weight, 0.5)) as max_w
                """).single()
                avg_weight = avg_weight_rec["avg_w"] or 0.5
                max_weight = avg_weight_rec["max_w"] or 0.5
                
                # Top reinforced columns
                top_cols_res = session.run("""
                    MATCH (t:Table)-[r:HAS_COLUMN|CONTAINS]->(c:Column)
                    WHERE coalesce(r.success_count, 0) > 0
                    RETURN t.name as table_name, c.name as column_name, r.weight as weight, r.success_count as successes
                    ORDER BY r.weight DESC, r.success_count DESC LIMIT 5
                """).data()

                # Recent correction rules
                recent_rules_res = session.run("""
                    MATCH (r:CorrectionRule)
                    RETURN r.rule_text as text, r.table_name as table_name, toString(r.created_at) as created_at
                    ORDER BY r.created_at DESC LIMIT 5
                """).data()

                return {
                    "golden_queries_count": golden_cnt,
                    "correction_rules_count": rules_cnt,
                    "synonyms_count": synonym_cnt,
                    "average_synaptic_weight": round(float(avg_weight), 3),
                    "max_synaptic_weight": round(float(max_weight), 3),
                    "top_reinforced_columns": top_cols_res,
                    "recent_rules": recent_rules_res
                }
        except Exception as e:
            return {
                "golden_queries_count": 0,
                "correction_rules_count": 0,
                "synonyms_count": 0,
                "average_synaptic_weight": 0.5,
                "max_synaptic_weight": 0.5,
                "top_reinforced_columns": [],
                "recent_rules": [],
                "error": str(e)
            }
        finally:
            driver.close()

    def reinforce_ingested_query(
        self,
        tables_used: List[str],
        columns_used: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Ingestion-Time Hebbian Learning:
        Boosts baseline weights (+0.02) for tables & columns present in newly ingested production Metabase cards.
        """
        driver = self._get_driver()
        try:
            with driver.session(database=self.database) as session:
                for tname in tables_used:
                    session.run("""
                        MATCH (t:Table {name: $tname})
                        SET t.ingest_count = coalesce(t.ingest_count, 0) + 1,
                            t.weight = CASE WHEN coalesce(t.weight, 0.5) + 0.02 > 1.0 THEN 1.0 ELSE coalesce(t.weight, 0.5) + 0.02 END
                    """, tname=tname)
                if columns_used:
                    for cname in columns_used:
                        session.run("""
                            MATCH (c:Column {name: $cname})
                            SET c.ingest_count = coalesce(c.ingest_count, 0) + 1,
                                c.weight = CASE WHEN coalesce(c.weight, 0.5) + 0.02 > 1.0 THEN 1.0 ELSE coalesce(c.weight, 0.5) + 0.02 END
                        """, cname=cname)
            return {"status": "ingest_reinforced", "message": "Baseline weights boosted for ingested query assets (+0.02)"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
        finally:
            driver.close()

    def get_schema_sitemap(self) -> Dict[str, Any]:
        """
        High-Level Schema Sitemap for Phase 1 Architectural Planning.
        Returns high-level metadata (tables, row counts, journey stages, key metrics)
        without dumping thousands of columns into context.
        """
        driver = self._get_driver()
        try:
            with driver.session(database=self.database) as session:
                tables = session.run("""
                    MATCH (t:Table)
                    RETURN t.name as name, t.database as database, coalesce(t.row_count, 0) as row_count,
                           coalesce(t.weight, 0.5) as weight
                    ORDER BY t.weight DESC
                """).data()
                
                stages = session.run("""
                    MATCH (js:JourneyStage)
                    RETURN js.name as stage_name, js.description as description
                """).data()
                
                metrics = session.run("""
                    MATCH (m:Metric)
                    RETURN m.name as name, m.stage as stage, m.description as description
                    ORDER BY coalesce(m.weight, 0.5) DESC LIMIT 15
                """).data()

                return {
                    "tables": tables,
                    "stages": stages,
                    "metrics": metrics
                }
        except Exception as e:
            return {"tables": [], "stages": [], "metrics": [], "error": str(e)}
        finally:
            driver.close()

