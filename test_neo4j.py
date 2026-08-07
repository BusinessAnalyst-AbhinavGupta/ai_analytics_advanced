from neo4j import GraphDatabase

# Connection details provided by user
URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "password")
# The ID provided is a UUID-style string, which might need specific handling 
# in some Neo4j versions if it's not the default 'neo4j' database name.
TARGET_DATABASE = "90031eca-686e-4ad3-9bb3-2b854c601f1c"

def run_test_query():
    driver = GraphDatabase.driver(URI, auth=AUTH)
    try:
        # First try to connect to the specific database provided.
        # If that fails or is unavailable, we could fall back to 'neo4j'.
        with driver.session(database=TARGET_DATABASE) as session:
            result = session.run("MATCH (n) RETURN count(n) AS total_nodes")
            record = result.single()
            if record:
                print(f"Success! Total nodes in {TARGET_DATABASE}: {record['total_nodes']}")
            else:
                print("Query executed but returned no results.")
    except Exception as e:
        # If the specific DB is not found, it might be because the name 
        # isn't recognized by the driver. We provide a more descriptive error.
        import traceback
        print(f"Connection/Query failed: {e}")
        # Fallback attempt on standard 'neo4j' db if the specific one fails.
        try:
            with driver.session(database="neo4j") as session:
                result = session.run("MATCH (n) RETURN count(n) AS total_nodes")
                record = result.single()
                print(f"Fallback successful! Total nodes in 'neo4j' db: {record['total_nodes']}")
        except Exception as e2:
            print(f"Fallback also failed: {e2}")
    finally:
        driver.close()

if __name__ == "__main__":
    run_test_query()
