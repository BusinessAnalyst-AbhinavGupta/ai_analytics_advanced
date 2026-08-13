import os
import shutil
import pytest
from analytics_platform.brain.vector_store import BrainVectorStore
from analytics_platform.domain import KnowledgeNode, NodeKind, ReviewStatus, new_id

TEST_DB_PATH = ".test_chroma_db"

@pytest.fixture(scope="module")
def vector_store():
    # Setup
    if os.path.exists(TEST_DB_PATH):
        shutil.rmtree(TEST_DB_PATH)
    
    store = BrainVectorStore(db_path=TEST_DB_PATH)
    yield store
    
    # Teardown
    if os.path.exists(TEST_DB_PATH):
        shutil.rmtree(TEST_DB_PATH)

def test_semantic_match_churn_attrition(vector_store):
    node1_id = new_id("node")
    node2_id = new_id("node")
    
    # Insert a node about "user churn"
    vector_store.upsert_node(
        node_id=node1_id,
        text="High user churn observed in Q3 for the European market.",
        metadata={"tenant_id": "t1", "kind": NodeKind.FINDING.value, "status": ReviewStatus.APPROVED.value}
    )
    
    # Insert a completely unrelated node
    vector_store.upsert_node(
        node_id=node2_id,
        text="New product feature successfully increased server latency.",
        metadata={"tenant_id": "t1", "kind": NodeKind.FINDING.value, "status": ReviewStatus.APPROVED.value}
    )
    
    # Query with "customer attrition" which doesn't share keywords with "user churn"
    matched_ids = vector_store.search_similar(
        query="customer attrition",
        limit=2,
        metadata_filters={"tenant_id": "t1"}
    )
    
    # Because BAAI/bge-large-en-v1.5 understands semantics, it should match node1
    assert node1_id in matched_ids
    
    # The first result should be the most semantically similar
    assert matched_ids[0] == node1_id

def test_metadata_filtering(vector_store):
    node_id = new_id("node")
    
    vector_store.upsert_node(
        node_id=node_id,
        text="Sales revenue increased.",
        metadata={"tenant_id": "t2", "kind": NodeKind.QUERY.value, "status": ReviewStatus.APPROVED.value}
    )
    
    # Querying on wrong tenant should return nothing
    matched_ids = vector_store.search_similar(
        query="revenue",
        limit=1,
        metadata_filters={"tenant_id": "t1"}
    )
    assert node_id not in matched_ids
    
    # Querying on correct tenant should return it
    matched_ids = vector_store.search_similar(
        query="revenue",
        limit=1,
        metadata_filters={"tenant_id": "t2"}
    )
    assert node_id in matched_ids
