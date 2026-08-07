import re
from neo4j import GraphDatabase
from core.parser import QueryAnalyzer

def batch_learn_all_aliases():
    driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
    with driver.session(database="neo4j") as session:
        # 1. Fetch all Golden Queries
        queries = session.run("""
            MATCH (q:VerifiedGoldenQuery)
            OPTIONAL MATCH (q)-[:USES_TABLE]->(t:Table)
            RETURN coalesce(q.id, q.name) as id, 
                   q.sql as sql,
                   coalesce(q.table_name, t.name, 'silver_layer.t_link_journey_checkout_com') as table_name
        """).data()
        
        print(f"Loaded {len(queries)} golden queries from Neo4j.")
        
        total_extracted = 0
        total_persisted = 0
        
        for item in queries:
            sql = item.get("sql")
            table_name = item.get("table_name")
            if not sql:
                continue
                
            analyzer = QueryAnalyzer(sql)
            res = analyzer.analyze()
            aliases = res.get("column_aliases", [])
            
            for a in aliases:
                col_name = a.get("physical_column")
                alias_name = a.get("alias")
                expr = a.get("expression")
                t_name = a.get("table_name") or table_name
                
                if not col_name or not alias_name or col_name.upper() == "END" and not expr:
                    continue
                if len(alias_name) <= 1 or alias_name.lower() in ["select", "from", "where", "group", "by", "order", "as"]:
                    continue
                    
                total_extracted += 1
                col_id = f"{t_name}.{col_name}"
                
                # Merge Column, Alias, Table and Relationships in Neo4j
                session.run("""
                    MERGE (a:Alias {name: $alias})
                    MERGE (c:Column {id: $col_id})
                    ON CREATE SET c.name = $col_name, c.table_name = $tbl
                    MERGE (c)-[r:ALIASED_AS]->(a)
                    SET r.expression = $expr,
                        r.frequency = coalesce(r.frequency, 0) + 1,
                        r.updated_at = datetime()
                    WITH a, $tbl as tbl_name
                    MERGE (t:Table {name: tbl_name})
                    MERGE (a)-[:USED_IN_TABLE]->(t)
                """, alias=alias_name, col_id=col_id, col_name=col_name, tbl=t_name, expr=expr)
                total_persisted += 1

        # Summary check
        alias_count = session.run("MATCH (a:Alias) RETURN count(a) as c").single()["c"]
        edges_count = session.run("MATCH ()-[r:ALIASED_AS]->() RETURN count(r) as c").single()["c"]
        top_aliases = session.run("""
            MATCH (c:Column)-[r:ALIASED_AS]->(a:Alias)
            OPTIONAL MATCH (t:Table)-[:HAS_COLUMN]->(c)
            RETURN a.name as Alias, c.name as PhysicalColumn, coalesce(t.name, c.table_name) as TableName,
                   r.expression as Expression, r.frequency as Frequency
            ORDER BY Frequency DESC, Alias
            LIMIT 30
        """).data()
        
    driver.close()
    
    print(f"\n✅ Finished Batch Ingestion!")
    print(f"Total Extracted Mentions: {total_extracted}")
    print(f"Total Unique Alias Nodes in Neo4j: {alias_count}")
    print(f"Total ALIASED_AS Synaptic Edges in Neo4j: {edges_count}")
    print("\nTop 30 Learned Aliases in Graph Brain:")
    for r in top_aliases:
        print(f"  • {r['Alias']:25s} -> {r['PhysicalColumn']:25s} | freq: {r['Frequency']:2d} | Table: {r['TableName']}")

if __name__ == "__main__":
    batch_learn_all_aliases()
