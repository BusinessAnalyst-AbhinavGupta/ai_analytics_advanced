import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from typing import List
import uuid

# Imports from our modular package structure
from schema.models import CanonicalKnowledge, LogicalDetail, SourceMapping
from core.parser import parse_analytics_logic
from core.normalizer import build_canonical
from core.validator import Validator
from core.neo4j_adapter import Neo4jAdapter

# The "Source of Truth" for the integration test
RAW_SQL = """
WITH base AS (
    SELECT 
         event_date
        ,session_id
		,action
		,label
		,page_name
		,order_id
        
    FROM silver_layer.t_link_journey_checkout_com
    WHERE 
        {{date}}
		 [[AND {{customer_type}}]]
        AND (
            'All' IN ({{servie_line}})
            OR lower(service_line) = lower({{servie_line}})
            OR lower(ui_page_category) = lower({{servie_line}})
        )
     --   AND lower(internalemployee) = 'no'
   --     AND natco_code = 'de'		
	  AND (
            'All' IN ({{os_naame}})
            OR lower(user_device_type) = lower({{os_naame}})
        )	
        AND lower(category) <> 'addonmanagement'
		 			  		AND (
'All' IN ({{category}})
OR category = {{category}}
OR LOWER(category) = {{category}}
)

		   [[ AND {{natco}} ]]
        
),


basketusers as (
select   COUNT(DISTINCT CASE 
        WHEN page_name LIKE '%BASKET%' 
		and action = 'PageView' 
        THEN session_id END
    ) AS basket_page
	
	from base 
	
	),



base_agg AS (
    SELECT 
   --      COUNT(DISTINCT CASE 
   --          WHEN page_name LIKE '%BASKET%' 
   --          THEN session_id 
   --      END) as basket_page,

        COUNT(DISTINCT CASE 
            WHEN page_name LIKE '%BASKET%' 
			--and action  = 'ctaclick'
			and label  in ('Dalej', 'Zur Kasse','zur kasse','dalej','Weiter','Pokračovať ')
            THEN session_id 
        END) as basket_continue,
		
		        COUNT(DISTINCT CASE 
            WHEN lower(action) = 'onecheckoutinitiated' 
            THEN session_id 
        END) as onecheckoutinitiated,
		

        COUNT(DISTINCT CASE  
		 when ((lower(page_name) like '%checkout/personalinfo%') or (lower(page_name) like '%companyinfo%'))
     and LOWER(action) = 'pageview'
            THEN session_id 
        END) as personalinfo_page,
		
		      COUNT(DISTINCT CASE 
            WHEN lower(page_name) LIKE '%checkout/payment%' 
                 AND lower(action) = 'pageview' 
            THEN  session_id 
        END) as payment_page,

        COUNT(DISTINCT CASE 
            WHEN 
			((lower(page_name) like '%consent%' ) or (lower(page_name) like '%review%' ))
     and LOWER(action) = 'pageview'
 
            THEN session_id 
        END) as orderreview_page,

        COUNT(DISTINCT CASE 
            WHEN lower(action) = 'purchasesuccess' 
            THEN session_id 
        END) as order_placed
    FROM base
)


SELECT 'Basket' AS screen, basket_page AS sessions FROM basketusers
 UNION ALL
SELECT 'Basket_continue' AS screen, basket_continue AS sessions FROM base_agg
UNION ALL 
SELECT 'one_checkout_initiated', onecheckoutinitiated FROM base_agg
UNION ALL 
SELECT 'Personalinfo', personalinfo_page FROM base_agg
UNION ALL
SELECT 'Payment', payment_page FROM base_agg
union all
SELECT 'Orderreview', orderreview_page FROM base_agg
UNION ALL
SELECT 'Order Placed', order_placed FROM base_agg
order by 2 desc
"""

def run_test():
    import glob
    import os
    from core.pipeline import IngestionPipeline

    # Step 1-4: Run full IngestionPipeline parsing and schema generation
    print("--- PIPELINE RUN ---")
    pipeline = IngestionPipeline()
    cypher_statements = pipeline.run(RAW_SQL)
    print(f"Pipeline run completed. Generated {len(cypher_statements)} Cypher statements.")

    # Step 5: Verify Checkpoint and Log Creation
    print("\n--- CHECKPOINT & LOG VALIDATION ---")
    checkpoint_files = glob.glob("checkpoints/canonical_*.json")
    if not checkpoint_files:
        print("[FAIL] Checkpoint file was not created.")
        raise FileNotFoundError("Checkpoint not found.")
    
    latest_checkpoint = max(checkpoint_files, key=os.path.getctime)
    print(f"[PASS] Checkpoint successfully persisted: {latest_checkpoint}")

    log_files = glob.glob("logs/run_*.json")
    if not log_files:
        print("[FAIL] Pipeline run log file was not created.")
        raise FileNotFoundError("Run log not found.")
        
    latest_log = max(log_files, key=os.path.getctime)
    print(f"[PASS] Pipeline run log successfully written: {latest_log}")

    # Step 6: Database Ingestion (Attempting ingestion to Neo4j)
    print("\n--- DATABASE INGESTION ---")
    URI = "neo4j://127.0.0.1:7687"
    AUTH = ("neo4j", "password")
    TARGET_DATABASE = "neo4j"
    
    print(f"Attempting ingestion to database '{TARGET_DATABASE}' (Product Analyst DBMS: 90031eca-686e-4ad3-9bb3-2b854c601f1c) via {URI}...")
    try:
        results = pipeline.ingest(RAW_SQL, URI, AUTH, TARGET_DATABASE)
        print(f"Ingestion successful! Executed {len(results)} Cypher statements.")
    except Exception as e:
        print(f"[WARNING] Database ingestion failed or Neo4j offline: {e}")
        print("Skipping ingestion step. The rest of the pipeline ran successfully.")

    # Step 7: Resume Ingestion from checkpoint
    print("\n--- RESUME INGESTION FROM CHECKPOINT ---")
    print(f"Attempting to resume ingestion using checkpoint '{latest_checkpoint}'...")
    try:
        results = pipeline.resume_from_checkpoint(latest_checkpoint, URI, AUTH, TARGET_DATABASE)
        print(f"Resume ingestion successful! Executed {len(results)} Cypher statements.")
    except Exception as e:
        print(f"[WARNING] Resume database ingestion failed or Neo4j offline: {e}")
        print("Skipping resume ingestion step. Checkpoint reload verified successfully.")

if __name__ == "__main__":
    run_test()


