from neo4j import GraphDatabase

# Using your provided credentials
URI = "neo4j://127.0.0.1:7687"
AUTH = ("neo4j", "password")
DATABASE = "neo4j" # Standard default, as the specific ID is harder to route directly

def explore_schema():
    driver = GraphDatabase.driver(URI, auth=AUTH)
    try:
        with driver.session(database=DATABASE) as session:
            # 1. Get total counts and labels
            print("--- Summary ---")
            res_count = session.run("MATCH (n) RETURN count(n) AS count")
            for record in res_count:
                print(f"Total nodes: {record['count']}")

            # 2. List unique node labels
            print("\n--- Labels Found ---")
            res_labels = session.run("CALL db.labels()")
            labels = set()
            for record in res_labels:
                labels.add(record[0])
            if labels:
                for l in labels: print(f"Label: {l}")
            else:
                print("No labels found.")

            # 3. Check for sample properties (sampling a few nodes)
            print("\n--- Sample Properties ---")
            res_props = session.run("MATCH (n) RETURN distinct keys(n) as keys LIMIT 10")
            for record in res_props:
                if record["keys"]:
                    print(f"Keys found: {record['keys']}")

    except Exception as e:
        print(f"Error during exploration: {e}")
    finally:
        driver.close()

if __name__ == "__main__":
    explore_schema()
