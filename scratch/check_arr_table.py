from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (t:Table {name: 'eshop_data.es_events_arr_v2'})
        RETURN t
    """)
    records = list(res)
    if records:
        print("Table node exists:", records[0]["t"])
    else:
        print("Table node DOES NOT EXIST.")
        
    res2 = session.run("""
        MATCH (c:Column {table_name: 'eshop_data.es_events_arr_v2'})
        RETURN count(c) AS col_count
    """)
    print("Number of columns linked to it:", list(res2)[0]["col_count"])

driver.close()
