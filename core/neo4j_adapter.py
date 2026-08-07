from typing import List, Dict, Any, Optional
from schema.models import CanonicalKnowledge, DeepSqlReasoning

class Neo4jAdapter:
    """
    Translates canonical logic and DeepSqlReasoning models into standard Cypher queries.
    Persists business intents, column context synapses, SQL idioms, learned rules, and golden queries.
    """
    def generate_cypher(self, data: CanonicalKnowledge) -> List[str]:
        queries = []
        
        # Create Metric Node
        m_type = data.logic.type.value if hasattr(data.logic.type, 'value') else str(data.logic.type)
        m_stage = data.journey_stage.value if hasattr(data.journey_stage, 'value') else str(data.journey_stage)

        m_query = f"MERGE (m:Metric {{id: '{data.id}'}})"
        m_query += f"\nSET m.name='{data.name_canonical}', \n    m.type='{m_type}', \n    m.description='{data.description}', \n    m.stage='{m_stage}'"
        queries.append(m_query)

        # Create Journey Stage Node and Link
        s_query = f"MERGE (s:JourneyStage {{name: '{m_stage}'}})"
        s_query += f"\nMATCH (m:Metric {{id: '{data.id}'}}) \nMERGE (m)-[:PART_OF_STAGE]->(s)"
        queries.append(s_query)

        # Service Line
        service_line = data.metadata.get("service_line") if data.metadata else None
        if service_line:
            sl_query = f"MERGE (sl:ServiceLine {{name: '{service_line}'}})"
            sl_query += f"\nMATCH (m:Metric {{id: '{data.id}'}}) \nMERGE (m)-[:IN_SERVICE_LINE]->(sl)"
            queries.append(sl_query)

        # Category
        category = data.metadata.get("category") if data.metadata else None
        if category:
            c_query = f"MERGE (c:Category {{name: '{category}'}})"
            c_query += f"\nMATCH (m:Metric {{id: '{data.id}'}}) \nMERGE (m)-[:IN_CATEGORY]->(c)"
            queries.append(c_query)

        # Natco
        natco = data.metadata.get("natco") if data.metadata else None
        if natco:
            n_query = f"MERGE (n:Natco {{code: '{natco}'}})"
            n_query += f"\nMATCH (m:Metric {{id: '{data.id}'}}) \nMERGE (m)-[:FOR_NATCO]->(n)"
            queries.append(n_query)

        # Link Tags if they exist
        if data.tags:
            for tag in data.tags:
                t_query = f"MERGE (t:Tag {{name: '{tag}'}})"
                t_query += f"\nMATCH (m:Metric {{id: '{data.id}'}}) \nMERGE (m)-[:HAS_TAG]->(t)"
                queries.append(t_query)

        return queries

    def ingest_deep_sql_reasoning(
        self,
        reasoning: DeepSqlReasoning,
        uri: Optional[str] = None,
        auth: Optional[tuple] = None,
        database: Optional[str] = "neo4j",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes Cypher statements to persist the complete DeepSqlReasoning model into Neo4j.
        """
        from neo4j import GraphDatabase
        import uuid
        import os

        uri = uri or os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
        auth = auth or (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
        database = database or "neo4j"

        metadata = metadata or {}
        intent_id = str(uuid.uuid4())
        stage_name = reasoning.journey_stage or metadata.get("tab_name") or "Checkout"
        
        # Extract root tables and ensure full physical schema is profiled & ingested
        root_tables = getattr(reasoning, "root_tables", None) or [reasoning.primary_table] or ["eshop_data.es_events_v2"]
        card_id = metadata.get("card_id")

        # Auto-Discovery Hook: Ensure physical table & columns are ingested in Neo4j
        try:
            from core.table_fetcher import TableSchemaFetcher
            fetcher = TableSchemaFetcher()
            for tbl in root_tables:
                fetcher.ensure_table_schema_ingested(tbl)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Auto-schema fetcher notification for {root_tables}: {e}")

        driver = GraphDatabase.driver(uri, auth=auth)
        try:
            target_db = database if database and database != "90031eca-686e-4ad3-9bb3-2b854c601f1c" else "neo4j"
            with driver.session(database=target_db) as session:
                def tx_work(tx):
                    # 1. Merge JourneyStage
                    tx.run("MERGE (s:JourneyStage {name: $stage})", stage=stage_name)
                    
                    # 2. Merge BusinessIntent Node
                    tx.run("""
                    MERGE (intent:BusinessIntent {name: $name, journey_stage: $stage})
                    SET intent.id = $id,
                        intent.business_goal = $goal,
                        intent.reasoning_summary = $summary,
                        intent.card_id = $card_id,
                        intent.dialect = $dialect,
                        intent.updated_at = datetime()
                    """, name=reasoning.intent_name, stage=stage_name, id=intent_id,
                         goal=reasoning.business_goal, summary=reasoning.reasoning_summary,
                         card_id=str(card_id) if card_id else None, dialect=reasoning.dialect)
                    
                    # 3. Merge ALL Root Tables and Link Intent to JourneyStage & Tables
                    for tbl in root_tables:
                        tx.run("MERGE (t:Table {name: $tbl})", tbl=tbl)
                        tx.run("""
                        MATCH (intent:BusinessIntent {name: $name, journey_stage: $stage})
                        MATCH (s:JourneyStage {name: $stage})
                        MATCH (t:Table {name: $tbl})
                        MERGE (intent)-[:PART_OF_STAGE]->(s)
                        MERGE (intent)-[:TARGETS_TABLE]->(t)
                        """, name=reasoning.intent_name, stage=stage_name, tbl=tbl)

                    # Determine primary fallback table for columns without explicit table names
                    table_name = root_tables[0] if root_tables else "eshop_data.es_events_v2"

                    # 4. Ingest Column Context Usages and Synaptic Weights
                    for col in reasoning.column_usages:
                        col_name = col.column_name.strip()
                        if not col_name:
                            continue
                        col_id = f"{table_name}.{col_name}"
                        
                        # Create/Merge Column with unique ID
                        tx.run("""
                        MERGE (c:Column {id: $col_id})
                        SET c.name = $col_name,
                            c.table_name = $tbl
                        """, col_id=col_id, col_name=col_name, tbl=table_name)
                        
                        # Link Table -> Column
                        tx.run("""
                        MATCH (t:Table {name: $tbl})
                        MATCH (c:Column {id: $col_id})
                        MERGE (t)-[:HAS_COLUMN]->(c)
                        """, tbl=table_name, col_id=col_id)

                        # Link Intent -> Column with synaptic weight & reasoning
                        tx.run("""
                        MATCH (intent:BusinessIntent {name: $name, journey_stage: $stage})
                        MATCH (c:Column {id: $col_id})
                        MERGE (intent)-[r:REQUIRES_COLUMN {role: $role}]->(c)
                        SET r.predicate = $predicate,
                            r.weight = $weight,
                            r.reasoning = $reasoning
                        """, name=reasoning.intent_name, stage=stage_name,
                             col_id=col_id,
                             role=col.role, predicate=col.predicate_pattern,
                             weight=float(col.importance_weight), reasoning=col.reasoning)

                        # Reinforce Synapse between Column and JourneyStage
                        tx.run("""
                        MATCH (c:Column {id: $col_id})
                        MATCH (s:JourneyStage {name: $stage})
                        MERGE (c)-[syn:SYNAPSE_REINFORCED]->(s)
                        ON CREATE SET syn.activation_count = 1, syn.weight = $weight
                        ON MATCH SET syn.activation_count = syn.activation_count + 1,
                                     syn.weight = CASE WHEN syn.weight < 1.0 THEN syn.weight + 0.05 ELSE 1.0 END
                        """, col_id=col_id, stage=stage_name, weight=float(col.importance_weight))

                    # 4. Ingest Dynamically Learned Column Aliases & Business Terms
                    for alias_item in getattr(reasoning, "column_aliases", []):
                        phys_col = (alias_item.physical_column if hasattr(alias_item, "physical_column") else alias_item.get("physical_column", "")).strip()
                        alias_name = (alias_item.alias if hasattr(alias_item, "alias") else alias_item.get("alias", "")).strip()
                        expr = (alias_item.expression if hasattr(alias_item, "expression") else alias_item.get("expression", ""))
                        item_tbl = (alias_item.table_name if hasattr(alias_item, "table_name") else alias_item.get("table_name")) or table_name

                        if not phys_col or not alias_name:
                            continue

                        col_id = f"{item_tbl}.{phys_col}"

                        tx.run("""
                        MERGE (c:Column {id: $col_id})
                        SET c.name = $phys_col, c.table_name = $tbl
                        MERGE (a:Alias {name: $alias_name})
                        MERGE (c)-[r:ALIASED_AS]->(a)
                        ON CREATE SET r.frequency = 1, r.expression = $expr, r.created_at = datetime()
                        ON MATCH SET r.frequency = r.frequency + 1, r.expression = coalesce($expr, r.expression), r.updated_at = datetime()
                        """, col_id=col_id, phys_col=phys_col, tbl=item_tbl, alias_name=alias_name, expr=expr)

                        tx.run("""
                        MATCH (t:Table {name: $tbl})
                        MATCH (a:Alias {name: $alias_name})
                        MERGE (a)-[:USED_IN_TABLE]->(t)
                        """, tbl=item_tbl, alias_name=alias_name)

                    # 5. Ingest SQL Idioms
                    for idiom in reasoning.sql_idioms:
                        if not idiom.name:
                            continue
                        tx.run("""
                        MERGE (i:SqlIdiom {name: $name})
                        SET i.category = $cat,
                            i.description = $desc,
                            i.sql_skeleton = $skel,
                            i.when_to_use = $when
                        """, name=idiom.name, cat=idiom.category, desc=idiom.description,
                             skel=idiom.sql_skeleton, when=idiom.when_to_use)
                        
                        tx.run("""
                        MATCH (intent:BusinessIntent {name: $name, journey_stage: $stage})
                        MATCH (i:SqlIdiom {name: $idiom_name})
                        MERGE (intent)-[:IMPLEMENTS_IDIOM]->(i)
                        """, name=reasoning.intent_name, stage=stage_name, idiom_name=idiom.name)

                    # 5. Ingest Learned Rules / Anti-Patterns
                    for rule in reasoning.learned_rules:
                        if not rule.description:
                            continue
                        tx.run("""
                        MERGE (r:LearnedRule {description: $desc})
                        SET r.rule_type = $rtype,
                            r.reasoning = $reasoning
                        """, desc=rule.description, rtype=rule.rule_type, reasoning=rule.reasoning)

                        tx.run("""
                        MATCH (intent:BusinessIntent {name: $name, journey_stage: $stage})
                        MATCH (r:LearnedRule {description: $desc})
                        MERGE (intent)-[:APPLIES_RULE]->(r)
                        """, name=reasoning.intent_name, stage=stage_name, desc=rule.description)

                    # 6. Ingest Verified Golden Query
                    if reasoning.canonical_golden_query:
                        tx.run("""
                        MERGE (gq:VerifiedGoldenQuery {name: $name, journey_stage: $stage})
                        SET gq.sql = $sql,
                            gq.intent = $goal,
                            gq.table_name = $tbl,
                            gq.dialect = $dialect,
                            gq.card_id = $card_id,
                            gq.last_verified = datetime()
                        """, name=reasoning.intent_name, stage=stage_name,
                             sql=reasoning.canonical_golden_query, goal=reasoning.business_goal,
                             tbl=table_name, dialect=reasoning.dialect,
                             card_id=str(card_id) if card_id else None)

                        tx.run("""
                        MATCH (intent:BusinessIntent {name: $name, journey_stage: $stage})
                        MATCH (gq:VerifiedGoldenQuery {name: $name, journey_stage: $stage})
                        MATCH (t:Table {name: $tbl})
                        MERGE (intent)-[:HAS_GOLDEN_TEMPLATE]->(gq)
                        MERGE (gq)-[:USES_TABLE]->(t)
                        """, name=reasoning.intent_name, stage=stage_name, tbl=table_name)

                session.execute_write(tx_work)
                return {"status": "SUCCESS", "intent_name": reasoning.intent_name, "stage": stage_name}
        finally:
            driver.close()

    def ingest(self, data: CanonicalKnowledge, uri: str, auth: tuple, database: str = None) -> List[Any]:
        """
        Executes the generated Cypher queries against a Neo4j database.
        Includes automatic fallback to default 'neo4j' database if a DBMS instance ID is provided.
        """
        from neo4j import GraphDatabase
        from neo4j.exceptions import ClientError
        
        queries = self.generate_cypher(data)
        results = []
        
        driver = GraphDatabase.driver(uri, auth=auth)
        try:
            target_db = database if database and database != "90031eca-686e-4ad3-9bb3-2b854c601f1c" else "neo4j"
            
            def run_queries(db_name):
                session_kwargs = {"database": db_name} if db_name else {}
                with driver.session(**session_kwargs) as session:
                    def work(tx):
                        tx_results = []
                        for query in queries:
                            res = tx.run(query)
                            summary = res.consume()
                            tx_results.append(summary)
                        return tx_results
                    return session.execute_write(work)
            
            try:
                results = run_queries(target_db)
            except ClientError as ce:
                if "DatabaseNotFound" in str(ce) and target_db != "neo4j":
                    results = run_queries("neo4j")
                elif "DatabaseNotFound" in str(ce):
                    results = run_queries(None)
                else:
                    raise ce
        finally:
            driver.close()
            
        return results


