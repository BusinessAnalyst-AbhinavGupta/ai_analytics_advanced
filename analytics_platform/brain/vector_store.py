"""Local Vector Store (ChromaDB + SentenceTransformers) for the Company Brain."""

import os
from typing import Any, Dict, List, Optional
import chromadb
from chromadb.utils import embedding_functions


class BrainVectorStore:
    def __init__(self, db_path: str = ".chroma_db"):
        os.makedirs(db_path, exist_ok=True)
        # Initialize a local persistent Chroma client
        self.client = chromadb.PersistentClient(path=db_path)
        
        # Use BAAI/bge-large-en-v1.5 for state-of-the-art local semantic matching
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="BAAI/bge-large-en-v1.5"
        )
        
        self.collection = self.client.get_or_create_collection(
            name="company_brain",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

    def upsert_node(self, node_id: str, text: str, metadata: Dict[str, Any]) -> None:
        """Upsert a node's semantic content into the vector index."""
        # Chroma expects all metadata values to be str, int, float or bool
        safe_metadata = {}
        for k, v in metadata.items():
            if isinstance(v, (str, int, float, bool)):
                safe_metadata[k] = v
            else:
                safe_metadata[k] = str(v)
                
        self.collection.upsert(
            ids=[node_id],
            documents=[text],
            metadatas=[safe_metadata]
        )

    def search_similar(self, query: str, limit: int = 20, 
                       metadata_filters: Optional[Dict[str, Any]] = None) -> List[str]:
        """Return ordered list of node_ids that semantically match the query."""
        where = {}
        if metadata_filters:
            # Simple equality filters. For more complex, Chroma requires "$and" logic
            # but standard dict equality works for ANDing multiple basic fields.
            for k, v in metadata_filters.items():
                if v is not None:
                    where[k] = v

        kwargs = {
            "query_texts": [query],
            "n_results": limit
        }
        if where:
            kwargs["where"] = where

        results = self.collection.query(**kwargs)
        
        if results and results.get("ids") and len(results["ids"]) > 0:
            return results["ids"][0]
        return []

    def delete_node(self, node_id: str) -> None:
        """Remove a node from the index."""
        try:
            self.collection.delete(ids=[node_id])
        except Exception:
            pass
