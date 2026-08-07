#!/usr/bin/env python3
import sys
import argparse
from core.pipeline import IngestionPipeline

def main():
    parser = argparse.ArgumentParser(description="Resume SQL Ingestion Pipeline from a validated schema checkpoint.")
    parser.add_argument("checkpoint", help="Path to the validated checkpoint JSON file.")
    parser.add_argument("--uri", default="neo4j://127.0.0.1:7687", help="Neo4j Connection URI (default: neo4j://127.0.0.1:7687)")
    parser.add_argument("--user", default="neo4j", help="Neo4j Username (default: neo4j)")
    parser.add_argument("--password", default="password", help="Neo4j Password (default: password)")
    parser.add_argument("--database", default="neo4j", help="Target Database Name (default: neo4j)")

    args = parser.parse_args()

    pipeline = IngestionPipeline()
    auth = (args.user, args.password)
    
    try:
        results = pipeline.resume_from_checkpoint(
            checkpoint_path=args.checkpoint,
            uri=args.uri,
            auth=auth,
            database=args.database
        )
        print(f"Success! Ingested {len(results)} statement transactions from checkpoint.")
        sys.exit(0)
    except Exception as e:
        print(f"Error resuming ingestion: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
