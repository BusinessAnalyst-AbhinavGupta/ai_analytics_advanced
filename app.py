import os
import json
import uuid
import streamlit as st
import pandas as pd
from datetime import datetime
import time
from neo4j import GraphDatabase
import streamlit.components.v1 as components

from core.pipeline import IngestionPipeline
from core.table_ingestion import TableSchemaIngestion
from core.query_generator import QueryGenerator
from core.graph_learner import GraphLearner
from core.auto_healer import AutoHealer
from core.db import (
    init_db,
    create_run,
    update_run,
    get_all_runs,
    get_run,
    delete_run
)

# Initialize database
init_db()

def safe_load_csv_sample(uploaded_file, nrows=10):
    """Safely loads sample rows and columns from an uploaded file or filepath with encoding and delimiter fallbacks."""
    if uploaded_file is None:
        return None, [], 0
        
    encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252", "iso-8859-1"]
    for enc in encodings:
        try:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            df = pd.read_csv(uploaded_file, nrows=nrows, encoding=enc, on_bad_lines="skip")
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            return df, list(df.columns), len(df)
        except Exception:
            continue
            
    for enc in ["utf-8", "latin1"]:
        try:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            df = pd.read_csv(uploaded_file, nrows=nrows, sep=None, engine="python", encoding=enc, on_bad_lines="skip")
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            return df, list(df.columns), len(df)
        except Exception:
            continue
            
    return None, [], 0

# Page configuration
st.set_page_config(
    page_title="AI Analytics - Knowledge Graph & SQL Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling (Zinc Dark / Modern Dashboard)
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    
    code, pre, .stCodeBlock {
        font-family: 'JetBrains Mono', monospace !important;
    }
    
    .main-header {
        font-size: 1.8rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        margin-bottom: 0.25rem;
    }
    
    .sub-header {
        font-size: 0.95rem;
        color: #94a3b8;
        margin-bottom: 1.5rem;
    }
    
    .status-badge {
        display: inline-flex;
        align-items: center;
        padding: 0.2rem 0.65rem;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    .status-success {
        background-color: rgba(16, 185, 129, 0.15);
        color: #10b981;
        border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .status-failed {
        background-color: rgba(239, 68, 68, 0.15);
        color: #ef4444;
        border: 1px solid rgba(239, 68, 68, 0.3);
    }
    .status-running {
        background-color: rgba(59, 130, 246, 0.15);
        color: #3b82f6;
        border: 1px solid rgba(59, 130, 246, 0.3);
    }
    .status-pending {
        background-color: rgba(148, 163, 184, 0.15);
        color: #94a3b8;
        border: 1px solid rgba(148, 163, 184, 0.3);
    }
    
    .meta-chip {
        display: inline-block;
        padding: 0.15rem 0.5rem;
        border-radius: 4px;
        font-size: 0.75rem;
        font-weight: 500;
        background-color: rgba(255, 255, 255, 0.06);
        color: #cbd5e1;
        margin-right: 0.35rem;
        margin-bottom: 0.25rem;
        border: 1px solid rgba(255, 255, 255, 0.08);
    }
    
    .metric-card {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 8px;
        padding: 1rem 1.25rem;
        margin-bottom: 1rem;
    }
    
    .metric-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: #f8fafc;
    }
    .metric-label {
        font-size: 0.8rem;
        font-weight: 500;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    
    .run-card {
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid rgba(255, 255, 255, 0.07);
        border-radius: 8px;
        padding: 1.25rem;
        margin-bottom: 1rem;
        transition: border-color 0.2s;
    }
    .run-card:hover {
        border-color: rgba(255, 255, 255, 0.15);
    }
</style>
""", unsafe_allow_html=True)

from core.llm_gateway import LLMGateway

# Default fallback API keys
DEFAULT_OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
DEFAULT_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
DEFAULT_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")

# Pre-initialize session state defaults for dynamic widgets
for key, def_val in [
    ("sidebar_llm_key_openrouter", DEFAULT_OPENROUTER_KEY),
    ("sidebar_llm_key_gemini", DEFAULT_GEMINI_KEY),
    ("sidebar_llm_key_openai", DEFAULT_OPENAI_KEY),
    ("sidebar_llm_ollama_url", "http://127.0.0.1:11434"),
    ("fb_or_key", DEFAULT_OPENROUTER_KEY),
    ("fb_gem_key", DEFAULT_GEMINI_KEY),
    ("fb_oai_key", DEFAULT_OPENAI_KEY),
]:
    if key not in st.session_state:
        st.session_state[key] = def_val

# Callbacks for instant synchronized dropdown updates
def sync_gen_provider():
    p = st.session_state.get("sidebar_gen_provider_select", "OpenRouter API")
    k = st.session_state.get(f"sidebar_llm_key_{p.lower().split()[0]}", DEFAULT_OPENROUTER_KEY if "OpenRouter" in p else "")
    m_list = LLMGateway.get_available_models(p, api_key=k)
    st.session_state["sidebar_gen_model_select"] = m_list[0] if m_list else ""

def sync_fb_provider():
    p = st.session_state.get("sidebar_feedback_provider_select", "OpenRouter API")
    k = st.session_state.get("fb_or_key" if "OpenRouter" in p else ("fb_gem_key" if "Gemini" in p else "fb_oai_key"), DEFAULT_OPENROUTER_KEY if "OpenRouter" in p else "")
    m_list = LLMGateway.get_available_models(p, api_key=k)
    st.session_state["sidebar_feedback_model_select"] = m_list[0] if m_list else ""

def sync_ing_provider():
    p = st.session_state.get("tab1_ing_provider_select", "OpenRouter API")
    k = DEFAULT_OPENROUTER_KEY if "OpenRouter" in p else (DEFAULT_GEMINI_KEY if "Gemini" in p else "")
    m_list = LLMGateway.get_available_models(p, api_key=k)
    st.session_state["tab1_ing_model_select"] = m_list[0] if m_list else ""

# Sidebar: Connection Settings
with st.sidebar:
    st.markdown("### 🔌 Neo4j Connection")
    neo4j_uri = st.text_input("Connection URI", value="neo4j://127.0.0.1:7687")
    neo4j_user = st.text_input("Username", value="neo4j")
    neo4j_password = st.text_input("Password", value="password", type="password")
    neo4j_database = st.text_input("Database", value="neo4j", help="Active database in 'Product Analyst' DBMS")
    
    st.divider()
    st.markdown("### ⚡ Generation Engine (Text-to-SQL)")
    openrouter_default_idx = LLMGateway.PROVIDERS.index("OpenRouter API") if "OpenRouter API" in LLMGateway.PROVIDERS else 0
    llm_provider = st.selectbox(
        "Generation LLM Provider",
        options=LLMGateway.PROVIDERS,
        index=openrouter_default_idx,
        key="sidebar_gen_provider_select",
        on_change=sync_gen_provider,
        help="Model provider used for initial Text-to-SQL generation (Default: OpenRouter API - DeepSeek V4 Flash)."
    )
    
    # Provider-specific API Key / Endpoint configuration
    llm_api_key = ""
    ollama_url = "http://127.0.0.1:11434"
    
    if llm_provider == "OpenRouter API":
        llm_api_key = st.text_input("OpenRouter API Key", value=DEFAULT_OPENROUTER_KEY, type="password", key="sidebar_llm_key_openrouter", help="Enter your OpenRouter API key.")
    elif llm_provider == "Google Gemini API":
        llm_api_key = st.text_input("Google AI Studio API Key", value=DEFAULT_GEMINI_KEY, type="password", key="sidebar_llm_key_gemini", help="Enter your Google Gemini API key.")
    elif llm_provider == "OpenAI API":
        llm_api_key = st.text_input("OpenAI API Key", value=DEFAULT_OPENAI_KEY, type="password", key="sidebar_llm_key_openai", help="Enter your OpenAI API key.")
    else:
        ollama_url = st.text_input("Ollama Host URL", value="http://127.0.0.1:11434", key="sidebar_llm_ollama_url", help="Local Ollama server URL.")

    # Dynamically fetch available models for the selected provider
    available_models = LLMGateway.get_available_models(llm_provider, api_key=llm_api_key, ollama_url=ollama_url)
    default_gen_model = available_models[0]
    for target in ["deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-chat", "qwen2.5-coder:14b", "anthropic/claude-3.5-sonnet", "gpt-4o"]:
        if target in available_models:
            default_gen_model = target
            break
            
    if "sidebar_gen_model_select" not in st.session_state or st.session_state["sidebar_gen_model_select"] not in available_models:
        st.session_state["sidebar_gen_model_select"] = default_gen_model

    selected_model = st.selectbox(
        "Generation Model (Initial)",
        options=available_models,
        key="sidebar_gen_model_select",
        help=f"Model used for first-pass SQL generation (Default: deepseek/deepseek-v4-flash-0731)"
    )
    st.caption(f"Active Provider: **{llm_provider}** | Model: `{selected_model}` ({len(available_models)} models)")

    context_window_kb = st.select_slider(
        "🧠 Context Window (Tokens)",
        options=[32768, 65536, 131072, 262144, 524288, 1048576],
        value=262144,
        format_func=lambda x: f"{x // 1024}K Tokens",
        help="Context window allocated to the LLM (at least 256K by default). Auto-adjusts to model maximum if lower."
    )
    
    st.divider()
    st.markdown("### 🧠 Feedback & Self-Healing Engine")
    st.caption("Architecture: Default reasoning routes to OpenRouter (DeepSeek V4 Flash 0731). Local Ollama, OpenAI, & Gemini are available on-demand.")
    feedback_provider = st.selectbox(
        "Diagnostic / Healing Provider",
        options=LLMGateway.PROVIDERS,
        index=openrouter_default_idx, # Defaults to OpenRouter API
        key="sidebar_feedback_provider_select",
        on_change=sync_fb_provider,
        help="Model provider used when submitting negative feedback / self-healing (Default: OpenRouter API)."
    )
    
    feedback_api_key = ""
    if feedback_provider == "OpenRouter API":
        feedback_api_key = st.text_input("Healing OpenRouter Key", value=DEFAULT_OPENROUTER_KEY, type="password", key="fb_or_key")
    elif feedback_provider == "Google Gemini API":
        feedback_api_key = st.text_input("Healing Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password", key="fb_gem_key")
    elif feedback_provider == "OpenAI API":
        feedback_api_key = st.text_input("Healing OpenAI Key", value=DEFAULT_OPENAI_KEY, type="password", key="fb_oai_key")
        
    fb_available_models = LLMGateway.get_available_models(feedback_provider, api_key=feedback_api_key, ollama_url=ollama_url)
    default_heal_model = fb_available_models[0]
    for target in ["deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-chat", "anthropic/claude-3.5-sonnet", "qwen2.5-coder:14b", "gpt-4o"]:
        if target in fb_available_models:
            default_heal_model = target
            break

    if "sidebar_feedback_model_select" not in st.session_state or st.session_state["sidebar_feedback_model_select"] not in fb_available_models:
        st.session_state["sidebar_feedback_model_select"] = default_heal_model

    feedback_model = st.selectbox(
        "Diagnostic / Healing Model",
        options=fb_available_models,
        key="sidebar_feedback_model_select",
        help="Advanced model tasked with error diagnostics, logical repairs, and Knowledge Graph rule formulation (Default: deepseek/deepseek-v4-flash-0731)."
    )
    st.caption(f"Active Provider: **{feedback_provider}** | Model: `{feedback_model}` ({len(fb_available_models)} models)")
    
    st.divider()
    st.markdown("### 🌐 Knowledge Graph Stats")
    
    @st.cache_data(ttl=15, show_spinner=False)
    def fetch_cached_graph_stats(uri, user, pwd, db):
        try:
            d = GraphDatabase.driver(uri, auth=(user, pwd))
            with d.session(database=db if db else "neo4j") as s:
                m_c = s.run("MATCH (m:Metric) RETURN count(m) as c").single()["c"]
                t_c = s.run("MATCH (t:Table) RETURN count(t) as c").single()["c"]
                c_c = s.run("MATCH (c:Column) RETURN count(c) as c").single()["c"]
                r_c = s.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
            d.close()
            return True, (m_c, t_c, c_c, r_c), ""
        except Exception as e:
            return False, (0, 0, 0, 0), str(e)

    ok, stats, err = fetch_cached_graph_stats(neo4j_uri, neo4j_user, neo4j_password, neo4j_database)
    if ok:
        m_cnt, t_cnt, c_cnt, r_cnt = stats
        st.success(f"🟢 **Connected**\n- 📊 Metrics: **{m_cnt}**\n- 🗄️ Tables: **{t_cnt}**\n- 📋 Columns: **{c_cnt}**\n- 🔗 Relations: **{r_cnt}**")
    else:
        st.warning(f"Neo4j Offline / Not Connected: {err}")

# Main Title
st.markdown("<div class='main-header'>⚡ AI Analytics SQL & Schema Knowledge Engine</div>", unsafe_allow_html=True)
st.markdown("<div class='sub-header'>Ask natural language business questions to generate production SQL, ingest queries, and train the Neo4j Knowledge Graph.</div>", unsafe_allow_html=True)

# Top Tabs: Ask Question, Query Form, Table & Schema Ingestion, Graph Explorer, Runs Tracker, EDA
tab_ask, tab_query, tab_table, tab_graph, tab_runs, tab_eda = st.tabs([
    "💬 Ask Business Question (Text-to-SQL)",
    "📥 Query Ingestion", 
    "🗄️ Table & Schema Ingestion", 
    "🌐 Graph & Schema Explorer",
    "📊 Ingestion Runs & RCA",
    "🔬 Data Explorer (EDA)"
])

# ==========================================
# TAB 0: ASK BUSINESS QUESTION (TEXT-TO-SQL)
# ==========================================
with tab_ask:
    st.markdown("#### 🤖 Ask a Business Problem Statement & Generate SQL")
    st.caption(f"Engine: **{llm_provider}** (`{selected_model}`) | Augmented with Neo4j Physical Schemas, Profiled Columns & Metrics.")
    
    # Preset question helper buttons
    col_pre1, col_pre2, col_pre3 = st.columns(3)
    with col_pre1:
        if st.button("💡 Checkout Drop-off & Logins", use_container_width=True):
            st.session_state["biz_question"] = "In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?"
    with col_pre2:
        if st.button("🛒 Cart to Account Conversion", use_container_width=True):
            st.session_state["biz_question"] = "Calculate daily conversion rate from basket_continue to order_placed split by device type and category."
    with col_pre3:
        if st.button("💳 Payment Error Analysis", use_container_width=True):
            st.session_state["biz_question"] = "Find the top 5 error reasons during payment step in Germany for acquisition users in the past 30 days."

    # Fetch available tables for selection with cache
    @st.cache_data(ttl=20, show_spinner=False)
    def fetch_cached_tables(uri, user, pwd, db):
        tbls = ["Auto-Detect All Tables"]
        try:
            d = GraphDatabase.driver(uri, auth=(user, pwd))
            with d.session(database=db if db else "neo4j") as s:
                rows = s.run("MATCH (t:Table) RETURN t.name as name ORDER BY t.name").data()
                for r in rows:
                    if r["name"] not in tbls:
                        tbls.append(r["name"])
            d.close()
        except Exception:
            pass
        return tbls

    available_tables = fetch_cached_tables(neo4j_uri, neo4j_user, neo4j_password, neo4j_database)

    with st.form("ask_question_form", clear_on_submit=False):
        col_q, col_opts = st.columns([2, 1])
        
        with col_q:
            default_q = st.session_state.get("biz_question", "In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?")
            question_input = st.text_area(
                "Business Problem Statement / Analytical Question *",
                value=default_q,
                height=160,
                placeholder="e.g. In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?",
                help="Describe your business question in plain English. The engine will retrieve the necessary schema and logic from the Neo4j Knowledge Graph."
            )
            custom_instructions = st.text_area(
                "Optional Custom Constraints / Filters",
                height=70,
                placeholder="e.g. Restrict to natco = 'DE', date >= '2026-01-01', service_line = 'fixed', exclude category = 'addonmanagement'",
                help="Add any ad-hoc business filters, date boundaries, or Natco requirements. These are strictly enforced by the compiler."
            )
            
        with col_opts:
            selected_table = st.selectbox(
                "Target Physical Table Context",
                options=available_tables,
                index=0,
                help="Choose a specific table from the Knowledge Graph or allow the engine to search across all tables."
            )
            dialect = st.selectbox(
                "Target SQL Engine / Dialect",
                options=["AWS Athena / Presto", "Metabase Presto / Trino", "PostgreSQL / ANSI SQL", "BigQuery SQL", "Snowflake SQL"],
                index=0
            )
            temp = st.slider("Temperature (0.0 = Deterministic)", 0.0, 0.7, 0.0, 0.05)
            
        generate_btn = st.form_submit_button("⚡ Propose Architecture Plan & Query", use_container_width=True)

    if generate_btn:
        if not question_input.strip():
            st.error("Please enter a business problem statement.")
        else:
            q_gen = QueryGenerator(
                uri=neo4j_uri,
                auth=(neo4j_user, neo4j_password),
                database=neo4j_database if neo4j_database else "neo4j",
                ollama_url=ollama_url
            )
            
            with st.spinner(f"Phase 1: Evaluating graph sitemap and proposing Query Architecture Blueprint with {llm_provider} ({selected_model})..."):
                try:
                    plan_res = q_gen.generate_architectural_plan(
                        question=question_input.strip(),
                        database_dialect=dialect,
                        table_filter=selected_table,
                        provider=llm_provider,
                        model_name=selected_model,
                        api_key=llm_api_key,
                        temperature=temp,
                        context_window=context_window_kb,
                        custom_instructions=custom_instructions.strip()
                    )
                    
                    st.session_state["active_arch_plan"] = plan_res
                    st.session_state["plan_start_time"] = time.time()
                    st.session_state["llm_session_thread"] = plan_res.get("session_messages", [])
                    st.session_state["llm_session_active"] = True
                    st.session_state["llm_session_model"] = selected_model
                    if "last_gen_result" in st.session_state:
                        del st.session_state["last_gen_result"]
                    if "auto_mb_triggered" in st.session_state:
                        del st.session_state["auto_mb_triggered"]
                except Exception as e:
                    st.error(f"Architecture Planning Error: {e}")

    # Phase 1: Display & Refine Query Architecture Blueprint
    if "active_arch_plan" in st.session_state and "last_gen_result" not in st.session_state:
        arch_plan = st.session_state["active_arch_plan"]
        st.markdown("<hr style='margin:1.5rem 0; border-color:rgba(255,255,255,0.08);'>", unsafe_allow_html=True)
        
        st.markdown("#### 🏗️ Phase 1: High-Level Query Architecture Blueprint")
        st.caption(f"Business Problem: *\"{arch_plan.get('question')}\"*")
        
        with st.expander("📋 Review Architecture Plan & Strategy", expanded=True):
            st.markdown(arch_plan.get("plan_markdown", ""))

        elapsed = int(time.time() - st.session_state.get("plan_start_time", time.time()))
        rem_sec = max(0, 120 - elapsed)

        # Dynamic Visual Live Countdown Banner
        timer_html = f"""
        <div style="background: linear-gradient(135deg, rgba(99, 102, 241, 0.12), rgba(168, 85, 247, 0.12)); border: 1px solid rgba(139, 92, 246, 0.35); border-radius: 12px; padding: 14px 18px; margin: 12px 0 16px 0;">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-size: 1.2rem;">⏱️</span>
                    <span style="font-weight: 600; color: #f1f5f9; font-size: 0.95rem;">Interactive Human Review Active</span>
                </div>
                <div style="background: rgba(15, 23, 42, 0.7); border: 1px solid rgba(139, 92, 246, 0.4); border-radius: 20px; padding: 4px 14px; font-family: monospace; font-size: 1.05rem; font-weight: 700; color: #38bdf8;">
                    <span id="countdown_timer_display">{rem_sec // 60:02d}:{rem_sec % 60:02d}</span> remaining
                </div>
            </div>
            <div style="font-size: 0.84rem; color: #94a3b8; margin-bottom: 10px;">
                Review the blueprint above. Type refinement feedback to adjust the strategy, click <b>⚡ Confirm Plan</b> to proceed immediately, or the system will automatically advance to Phase 2 (Targeted Probing & SQL Generation) when the countdown reaches 0.
            </div>
            <div style="width: 100%; background: rgba(255,255,255,0.08); border-radius: 6px; height: 6px; overflow: hidden;">
                <div id="countdown_progress_bar" style="width: {(rem_sec / 120.0) * 100:.1f}%; height: 100%; background: linear-gradient(90deg, #6366f1, #a855f7); border-radius: 6px; transition: width 1s linear;"></div>
            </div>
        </div>
        <script>
        (function() {{
            var totalSec = {rem_sec};
            var initialSec = 120;
            var display = document.getElementById('countdown_timer_display');
            var bar = document.getElementById('countdown_progress_bar');
            if (!display) return;
            function update() {{
                if (totalSec <= 0) {{
                    display.textContent = '00:00 (Advancing...)';
                    if (bar) bar.style.width = '0%';
                    return;
                }}
                var mins = Math.floor(totalSec / 60);
                var secs = totalSec % 60;
                display.textContent = (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;
                if (bar) {{
                    var pct = Math.max(0, Math.min(100, (totalSec / initialSec) * 100));
                    bar.style.width = pct + '%';
                }}
                totalSec--;
                setTimeout(update, 1000);
            }}
            update();
        }})();
        </script>
        """
        st.components.v1.html(timer_html, height=105)

        col_ref1, col_ref2 = st.columns([2, 1])
        with col_ref1:
            refine_text = st.text_input(
                "💬 Refine Architecture Strategy (Optional)",
                key="arch_plan_refine_input",
                placeholder="e.g. 'Use es_events_v2 for checkout stage and order by event_timestamp'"
            )
        with col_ref2:
            confirm_sql_btn = st.button("⚡ Confirm Plan & Generate SQL Now", type="primary", use_container_width=True)
            apply_refine_btn = st.button("💬 Apply Refinement", use_container_width=True)

        trigger_phase2 = False
        user_refinement_to_pass = ""

        if confirm_sql_btn or rem_sec == 0:
            trigger_phase2 = True
        elif apply_refine_btn and refine_text.strip():
            trigger_phase2 = True
            user_refinement_to_pass = refine_text.strip()

        if trigger_phase2:
            q_gen = QueryGenerator(
                uri=neo4j_uri,
                auth=(neo4j_user, neo4j_password),
                database=neo4j_database if neo4j_database else "neo4j",
                ollama_url=ollama_url
            )
            with st.spinner(f"Phase 2: Executing targeted graph probes and writing production SQL..."):
                try:
                    gen_res = q_gen.execute_architectural_plan(
                        architectural_plan_res=arch_plan,
                        user_refinement=user_refinement_to_pass,
                        session_messages=st.session_state.get("llm_session_thread"),
                        api_key=llm_api_key,
                        temperature=temp,
                        context_window=context_window_kb
                    )
                    gen_res["question"] = arch_plan.get("question")
                    gen_res["custom_constraints"] = arch_plan.get("custom_instructions")
                    gen_res["dialect"] = arch_plan.get("database_dialect")
                    gen_res["table_filter"] = arch_plan.get("table_filter")
                    gen_res["generated_sql"] = gen_res.get("sql", "")
                    
                    st.session_state["llm_session_thread"] = gen_res.get("session_messages", [])
                    st.session_state["last_gen_result"] = gen_res
                    st.rerun()
                except Exception as p2_err:
                    st.error(f"Phase 2 Execution Error: {p2_err}")

    # Display generated results if available
    if "last_gen_result" in st.session_state:
        res = st.session_state["last_gen_result"]
        version = res.get("version", 1)
        
        st.markdown("<hr style='margin:1.5rem 0; border-color:rgba(255,255,255,0.08);'>", unsafe_allow_html=True)
        
        col_res_header, col_res_meta = st.columns([2, 1])
        with col_res_header:
            st.markdown(f"#### 🎯 Production SQL Query {'(Revision #' + str(version) + ')' if version > 1 else ''}")
            st.caption(f"Generated for: *\"{res.get('question', question_input)}\"*")
            if res.get("custom_constraints"):
                st.markdown(f"<span style='font-size:0.8rem; color:#94a3b8;'>🚨 <b>Enforced Constraints:</b> <code>{res['custom_constraints']}</code></span>", unsafe_allow_html=True)
        with col_res_meta:
            badge_class = "status-warning" if version > 1 else "status-success"
            badge_text = f"🩺 REVISED (REV #{version})" if version > 1 else "GRAPH-AUGMENTED"
            
            v_status = res.get("verification_status", "VERIFIED_1ST_TRY")
            v_iter = res.get("verification_iterations", 1)
            if v_status == "VERIFIED_1ST_TRY":
                v_badge = f"<span class='status-badge status-success'>🛡️ COMPILER VERIFIED (PASS #1)</span>"
            elif v_status == "HEALED_AND_VERIFIED":
                v_badge = f"<span class='status-badge status-warning'>⚡ AUTO-HEALED (PASS #{v_iter})</span>"
            else:
                v_badge = f"<span class='status-badge status-warning'>⚠️ WARNINGS DETECTED</span>"

            # Multi-turn session status badge
            session_active = st.session_state.get("llm_session_active", False)
            session_thread = st.session_state.get("llm_session_thread") or []
            turn_count = len([m for m in session_thread if m.get("role") == "user"])
            if session_active and turn_count > 0:
                sess_badge = f"<span class='meta-chip' style='background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.4);'>🟢 LLM Session (Turn #{turn_count})</span>"
            else:
                sess_badge = f"<span class='meta-chip' style='color:#94a3b8;'>⚪ Session Closed</span>"

            st.markdown(
                f"<div style='text-align:right;'>"
                f"{sess_badge}"
                f"<span class='meta-chip'>⏱️ {res.get('latency_seconds', 0)}s</span>"
                f"<span class='meta-chip'>🧠 {res.get('model', selected_model)}</span>"
                f"{v_badge}"
                f"</div>",
                unsafe_allow_html=True
            )
        
        # If this query was healed during the pre-return agentic loop, display the fix summary
        if res.get("healed_columns"):
            healed_list = ", ".join([f"<code>{h}</code>" for h in res["healed_columns"]])
            st.markdown(
                f"<div style='background:rgba(245, 158, 11, 0.08); border:1px solid rgba(245, 158, 11, 0.3); border-radius:8px; padding:0.8rem; margin-bottom:1rem;'>"
                f"🛡️ <b>Deterministic Agentic Loop Auto-Repair:</b> Caught and corrected {len(res['healed_columns'])} column(s) before display: {healed_list}."
                f"</div>",
                unsafe_allow_html=True
            )

        # If this query was self-healed via user feedback, display the diagnostic RCA box directly above the SQL
        if res.get("last_heal_meta"):
            h_meta = res["last_heal_meta"]
            st.markdown(
                f"<div style='background:rgba(16, 185, 129, 0.08); border:1px solid rgba(16, 185, 129, 0.3); border-radius:8px; padding:1rem; margin-bottom:1rem;'>"
                f"<b>🩺 AI Self-Healing Diagnostic (Revision #{version}):</b><br>"
                f"• <b>Root Cause:</b> {h_meta.get('root_cause', 'N/A')}<br>"
                f"• <b>What Changed:</b> {h_meta.get('what_changed', 'N/A')}<br>"
                f"• <b>Learned Rule for Brain:</b> <code>{h_meta.get('rule_text', 'N/A')}</code>"
                f"</div>",
                unsafe_allow_html=True
            )

        sql_to_show = res.get("generated_sql") or res.get("sql", "")
        
        # Interactive / Editable SQL Query Area Header with Session Controls
        col_ed_title, col_ed_close = st.columns([3, 1])
        with col_ed_title:
            st.markdown("##### 📝 Active SQL Query:")
        with col_ed_close:
            if st.session_state.get("llm_session_active", False):
                if st.button("🚪 Close / Reset LLM Session", key=f"close_session_btn_v{version}", use_container_width=True, help="Close the active conversation thread with DeepSeek and clear context memory."):
                    st.session_state["llm_session_thread"] = None
                    st.session_state["llm_session_active"] = False
                    st.toast("🚪 LLM Session closed and memory cleared.", icon="ℹ️")
                    st.rerun()

        edited_sql = st.text_area(
            "Production SQL Query (Editable)",
            value=sql_to_show,
            height=240,
            key=f"prod_sql_editor_v{version}",
            help="You can review or directly edit the query before verifying or submitting further feedback."
        )

        # ── Metabase Execution & EDA Section ──────────────────────────
        st.markdown("<hr style='margin:1rem 0; border-color:rgba(255,255,255,0.08);'>", unsafe_allow_html=True)
        col_run_mb, col_eda_btn = st.columns([1, 1])

        with col_run_mb:
            manual_mb_btn = st.button(
                "▶️ Run on Metabase & Validate",
                use_container_width=True,
                type="primary",
                key=f"run_metabase_v{version}",
                help="Execute this SQL against the Metabase database via your authenticated Chrome session."
            )
            
        auto_run_mb = False
        if not st.session_state.get("auto_mb_triggered", False):
            st.session_state["auto_mb_triggered"] = True
            auto_run_mb = True

        run_mb_btn = manual_mb_btn or auto_run_mb

        with col_eda_btn:
            eda_ready = "last_query_result_df" in st.session_state and st.session_state["last_query_result_df"] is not None
            run_eda_btn = st.button(
                "📊 Generate EDA Report" if eda_ready else "📊 EDA (Run query first)",
                use_container_width=True,
                disabled=not eda_ready,
                key=f"run_eda_v{version}",
                help="Generate an interactive sweetviz profiling report on the query results."
            )

        if run_mb_btn:
            from core.metabase_executor import (
                MetabaseExecutor,
                MetabaseUnreachableError,
                MetabaseQueryExecutionError,
            )
            from core.auto_healer import AutoHealer

            executor = MetabaseExecutor()
            healer = AutoHealer(
                uri=neo4j_uri,
                auth=(neo4j_user, neo4j_password),
                database=neo4j_database,
                ollama_url=ollama_url,
            )

            status_box = st.status("▶️ Executing SQL on Metabase & Auto-Healing …", expanded=True)
            t0 = time.time()
            max_heal_attempts = 4  # Initial attempt + up to 3 automated self-healing passes
            current_sql = edited_sql.strip()
            df_result = None
            heal_trail = []
            exec_success = False

            for attempt_idx in range(1, max_heal_attempts + 1):
                is_initial = (attempt_idx == 1)
                pass_label = "Initial Run" if is_initial else f"Auto-Heal Iteration #{attempt_idx - 1}"
                status_box.write(f"🔄 **[Pass #{attempt_idx}]** Dispatching query to Metabase via Chrome ({pass_label})…")

                try:
                    df_result = executor.execute_query(
                        sql=current_sql,
                        progress_cb=lambda msg: status_box.write(f"&nbsp;&nbsp;↳ {msg}")
                    )
                    exec_success = True
                    elapsed = round(time.time() - t0, 1)

                    if is_initial:
                        status_box.update(
                            label=f"✅ Query executed successfully on 1st attempt — {len(df_result):,} rows × {len(df_result.columns)} cols in {elapsed}s",
                            state="complete"
                        )
                    else:
                        status_box.update(
                            label=f"🎉 Query auto-healed & executed successfully on Pass #{attempt_idx}! ({len(df_result):,} rows × {len(df_result.columns)} cols in {elapsed}s)",
                            state="complete"
                        )
                    break

                except MetabaseUnreachableError as unreach_err:
                    # Metabase NOT reachable (Chrome closed, wrong tab, session expired, VPN disconnected)
                    # NOT eligible for SQL auto-healing!
                    elapsed = round(time.time() - t0, 1)
                    status_box.update(label=f"🚫 Metabase Unreachable ({elapsed}s)", state="error")
                    st.warning(
                        f"⚠️ **Metabase Unreachable (Auto-heal skipped):**\n\n{unreach_err}\n\n"
                        "💡 *Please ensure Google Chrome is open with your active Telekom profile and navigated to Metabase before retrying.*"
                    )
                    st.session_state["last_query_result_df"] = None
                    break

                except MetabaseQueryExecutionError as sql_err:
                    # Metabase IS reachable and executed query, but SQL engine returned syntax/runtime error!
                    # ELIGIBLE for automated self-healing loop!
                    status_box.write(f"⚠️ **[Pass #{attempt_idx} Error]** Database Engine: `{sql_err.error_message[:220]}`")

                    if attempt_idx >= max_heal_attempts:
                        elapsed = round(time.time() - t0, 1)
                        status_box.update(
                            label=f"❌ Auto-healing reached max attempts ({max_heal_attempts}) without resolving error ({elapsed}s)",
                            state="error"
                        )
                        st.error(
                            f"**SQL Execution Error after {max_heal_attempts} auto-heal attempts:**\n\n`{sql_err.error_message}`"
                        )
                        st.session_state["last_query_result_df"] = None
                        break

                    # Run AutoHealer with feedback from Metabase runtime error
                    status_box.write(f"🧠 **Triggering AI AutoHealer:** Diagnosing with **{feedback_provider}** (`{feedback_model}`) & recording learned rules in Neo4j…")

                    target_tbl = selected_table if selected_table != "Auto-Detect All Tables" else ""
                    try:
                        heal_res = healer.diagnose_and_heal(
                            failed_sql=current_sql,
                            error_message=sql_err.error_message,
                            question=res.get("question", question_input),
                            database_dialect=dialect,
                            target_table=target_tbl,
                            analyst_notes=f"Auto-healed from Metabase SQL engine error on pass #{attempt_idx}",
                            feedback_type="RUNTIME_ERROR",
                            provider=feedback_provider,
                            model_name=feedback_model,
                            api_key=feedback_api_key,
                            temperature=0.0,
                            context_window=context_window_kb,
                            session_messages=st.session_state.get("llm_session_thread")
                        )

                        # Update active session thread with the latest turn
                        if heal_res.get("session_messages"):
                            st.session_state["llm_session_thread"] = heal_res["session_messages"]
                            st.session_state["llm_session_active"] = True

                        next_sql = heal_res.get("healed_sql", "").strip()
                        if not next_sql or next_sql == current_sql:
                            status_box.write("⚠️ AutoHealer could not produce a different SQL variation. Stopping auto-heal loop.")
                            status_box.update(label="❌ Auto-healing stalled", state="error")
                            st.error(f"**Metabase Error:** {sql_err.error_message}")
                            st.session_state["last_query_result_df"] = None
                            break

                        heal_trail.append({
                            "pass": attempt_idx,
                            "failed_sql": current_sql,
                            "error": sql_err.error_message,
                            "root_cause": heal_res.get("root_cause", "Diagnosed and corrected."),
                            "what_changed": heal_res.get("what_changed", "Repaired SQL structure."),
                            "rule_text": heal_res.get("rule_text", "Learned rule recorded."),
                            "healed_sql": next_sql
                        })

                        status_box.write(f"🩺 **Root Cause:** {heal_res.get('root_cause', 'Diagnosed')}")
                        status_box.write(f"📝 **Fix Applied:** {heal_res.get('what_changed', 'Adjusted query')}")
                        status_box.markdown(f"📄 **Healed Candidate SQL (Pass #{attempt_idx + 1}):**")
                        status_box.code(next_sql, language="sql")
                        status_box.write(f"⚡ **Re-attempting execution on Metabase (Pass #{attempt_idx + 1})**…")

                        current_sql = next_sql

                    except Exception as h_err:
                        status_box.write(f"❌ AutoHealer failed: {h_err}")
                        status_box.update(label="❌ Auto-healing failed", state="error")
                        st.error(f"**AutoHealer Exception:** {h_err}")
                        st.session_state["last_query_result_df"] = None
                        break

            # Handle Post-Loop Outcomes & Update Active SQL Editor
            if heal_trail:
                # One or more auto-healing iterations occurred: update query state and editor
                if "history" not in res:
                    res["history"] = []
                for h_step in heal_trail:
                    res["history"].append({
                        "version": res.get("version", 1),
                        "sql": h_step["failed_sql"],
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "feedback_type": "RUNTIME_ERROR",
                        "feedback_input": h_step["error"]
                    })

                last_step = heal_trail[-1]
                new_version = res.get("version", 1) + len(heal_trail)
                res["generated_sql"] = current_sql
                res["sql"] = current_sql
                res["version"] = new_version
                res["last_heal_meta"] = {
                    "root_cause": last_step["root_cause"],
                    "what_changed": last_step["what_changed"],
                    "rule_text": last_step["rule_text"],
                    "total_iterations": len(heal_trail),
                    "history": heal_trail
                }

                if exec_success and df_result is not None:
                    res["verification_status"] = "HEALED_AND_VERIFIED"
                    res["verification_iterations"] = len(heal_trail) + 1
                    st.session_state["last_query_result_df"] = df_result
                else:
                    res["verification_status"] = "HEAL_ATTEMPTED"
                    st.session_state["last_query_result_df"] = None

                # Seed the new version's editor text state and trigger reactive rerun
                st.session_state[f"prod_sql_editor_v{new_version}"] = current_sql
                st.session_state["last_gen_result"] = res
                st.session_state["query_result"] = res
                st.rerun()

            elif exec_success and df_result is not None:
                # Clean run on first attempt without healing
                st.session_state["last_query_result_df"] = df_result

        # ── Autonomous AI Product Analyst & Exploration Hub ──────────────
        try:
            _hub_df = st.session_state.get("last_query_result_df")
            if _hub_df is not None and not _hub_df.empty:
                active_df = _hub_df
                from core.profiler.fast_summary import FastSummaryProfiler
                from core.rules.engine import BusinessRuleEngine
                from core.reasoning.analyst import ProductAnalystAgent
                from core.exploration.visualizer import ExplorationVisualizer
                from core.local_analytics.router import LocalQueryRouter

                profiler = FastSummaryProfiler()
                profiler_res = profiler.profile(active_df, title=res.get("question", "Dataset Profile"))

                rule_engine = BusinessRuleEngine()
                rule_res = rule_engine.evaluate(active_df)

                briefing_cache_key = f"analyst_briefing_v{version}_{len(active_df)}"
                if briefing_cache_key not in st.session_state:
                    with st.spinner("🧠 Senior Product Analyst synthesizing observations, hypotheses & next actions …"):
                        agent = ProductAnalystAgent(
                            provider=feedback_provider,
                            model_name=feedback_model,
                            api_key=feedback_api_key,
                            ollama_url=ollama_url
                        )
                        briefing = agent.analyze_results(
                            question=res.get("question", question_input),
                            sql=current_sql if 'current_sql' in locals() else res.get("sql", ""),
                            profiler_result=profiler_res,
                            rule_result=rule_res,
                            glossary_context=res.get("glossary_summary"),
                            api_key=feedback_api_key
                        )
                        st.session_state[briefing_cache_key] = briefing
                else:
                    briefing = st.session_state[briefing_cache_key]

                # ── Anomaly Auto-Correction ───────────────────────────────────
                # If CRITICAL/HIGH anomalies are found, auto-trigger AutoHealer
                # using the anomaly description as the feedback error message.
                critical_anomalies = ProductAnalystAgent.get_critical_anomalies(briefing)
                anomaly_heal_key = f"anomaly_heal_triggered_v{version}"

                if critical_anomalies and not st.session_state.get(anomaly_heal_key, False):
                    combined_feedback = "\n\n".join(a["feedback_text"] for a in critical_anomalies)

                    st.markdown("<hr style='margin:1rem 0; border-color:#f59e0b44;'>", unsafe_allow_html=True)
                    st.warning(
                        f"⚠️ **{len(critical_anomalies)} Business Logic Anomaly(ies) Detected in Results** — "
                        f"Auto-correcting SQL in 15 seconds unless you dismiss below.\n\n"
                        + "\n".join(f"- **{a['title']}** [{a['severity']}]: {a['observed_pattern']}" for a in critical_anomalies)
                    )

                    col_ah1, col_ah2 = st.columns([1, 1])
                    with col_ah1:
                        do_heal_now = st.button(
                            "⚡ Auto-Correct SQL Now (Business Logic Fix)",
                            use_container_width=True,
                            type="primary",
                            key=f"anomaly_heal_btn_v{version}"
                        )
                    with col_ah2:
                        dismiss_heal = st.button(
                            "✕ Dismiss (Keep Current SQL)",
                            use_container_width=True,
                            key=f"anomaly_dismiss_btn_v{version}"
                        )

                    # Auto-trigger countdown (15s) if user is away
                    countdown_key = f"anomaly_countdown_start_v{version}"
                    if countdown_key not in st.session_state:
                        st.session_state[countdown_key] = time.time()

                    elapsed = time.time() - st.session_state[countdown_key]
                    remaining = max(0, 15 - int(elapsed))

                    if remaining > 0 and not dismiss_heal and not do_heal_now:
                        st.caption(f"⏱ Auto-correcting in **{remaining}s** if no action taken...")
                        time.sleep(1)
                        st.rerun()

                    if dismiss_heal:
                        st.session_state[anomaly_heal_key] = True  # mark dismissed
                        st.rerun()

                    if do_heal_now or remaining == 0:
                        st.session_state[anomaly_heal_key] = True
                        from core.auto_healer import AutoHealer
                        ah_healer = AutoHealer(
                            uri=neo4j_uri,
                            auth=(neo4j_user, neo4j_password),
                            database=neo4j_database,
                            ollama_url=ollama_url,
                        )
                        _heal_sql = current_sql if 'current_sql' in locals() else res.get("sql", "")
                        with st.status("🧠 Auto-correcting SQL based on business anomaly diagnostics …", expanded=True) as ah_status:
                            st.write(f"📋 Anomaly feedback being sent to `{feedback_provider}` (`{feedback_model}`):")
                            for a in critical_anomalies:
                                st.write(f"  - {a['title']}: {a['observed_pattern'][:120]}")
                            try:
                                ah_res = ah_healer.diagnose_and_heal(
                                    failed_sql=_heal_sql,
                                    error_message=combined_feedback,
                                    question=res.get("question", question_input),
                                    database_dialect=dialect,
                                    target_table=selected_table if selected_table != "Auto-Detect All Tables" else "",
                                    analyst_notes="Auto-correction triggered by business rule anomaly detection.",
                                    feedback_type="BUSINESS_LOGIC",
                                    provider=feedback_provider,
                                    model_name=feedback_model,
                                    api_key=feedback_api_key,
                                    temperature=0.0,
                                    context_window=context_window_kb,
                                    session_messages=st.session_state.get("llm_session_thread")
                                )
                                healed_sql = ah_res.get("healed_sql", "").strip()
                                if healed_sql:
                                    new_ver = res.get("version", 1) + 1
                                    res["generated_sql"] = healed_sql
                                    res["sql"] = healed_sql
                                    res["version"] = new_ver
                                    res["last_heal_meta"] = {
                                        "root_cause": ah_res.get("root_cause", "Business logic anomaly"),
                                        "what_changed": ah_res.get("what_changed", ""),
                                        "rule_text": ah_res.get("rule_text", ""),
                                        "total_iterations": 1,
                                        "history": []
                                    }
                                    if ah_res.get("session_messages"):
                                        st.session_state["llm_session_thread"] = ah_res["session_messages"]
                                        st.session_state["llm_session_active"] = True
                                    st.session_state[f"prod_sql_editor_v{new_ver}"] = healed_sql
                                    st.session_state["last_gen_result"] = res
                                    st.session_state["query_result"] = res
                                    st.session_state["auto_mb_triggered"] = False  # re-run on Metabase
                                    st.session_state["last_query_result_df"] = None  # clear stale data
                                    st.session_state.pop(briefing_cache_key, None)  # clear cached briefing
                                    ah_status.update(label="✅ SQL auto-corrected! Re-running on Metabase …", state="complete")
                                    st.rerun()
                                else:
                                    ah_status.update(label="⚠️ AutoHealer returned empty SQL — keeping original.", state="error")
                            except Exception as ah_err:
                                ah_status.update(label=f"❌ Auto-correction error: {ah_err}", state="error")

                st.markdown("<hr style='margin:1.5rem 0; border-color:rgba(255,255,255,0.12);'>", unsafe_allow_html=True)
                st.markdown("### 🤖 Autonomous AI Product Analyst & Exploration Hub")

                tab_briefing, tab_pyg, tab_eda, tab_chat = st.tabs([
                    "📑 Executive Analyst Briefing",
                    "🎨 Interactive Visual Explorer (PyGWalker)",
                    "📊 Deep Profiling Report",
                    "💬 Conversational Drill-Down (DuckDB)"
                ])

                with tab_briefing:
                    health_status = briefing.get("data_health_status", "CLEAN")
                    health_color = "#10b981" if health_status == "CLEAN" else ("#f59e0b" if health_status == "WARNINGS_DETECTED" else "#ef4444")

                    st.markdown(f"""
                    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-left: 4px solid #6366f1; border-radius: 8px; padding: 1.2rem; margin-bottom: 1rem;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                            <span style="font-size: 1.05rem; font-weight: 700; color: #f8fafc;">💡 Executive Takeaway:</span>
                            <span style="background: {health_color}22; color: {health_color}; border: 1px solid {health_color}55; padding: 0.2rem 0.6rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600;">
                                {health_status} ({rule_res.triggered_count} rules flagged)
                            </span>
                        </div>
                        <p style="margin: 0; font-size: 0.95rem; color: #cbd5e1; line-height: 1.5;">
                            {briefing.get("executive_summary", "Analysis completed.")}
                        </p>
                    </div>
                    """, unsafe_allow_html=True)

                    col_b1, col_b2 = st.columns([1, 1])
                    with col_b1:
                        st.markdown("##### 📌 Key Data Findings:")
                        for finding in briefing.get("key_findings", []):
                            st.markdown(f"- {finding}")
                    with col_b2:
                        st.markdown("##### 🎯 Prioritized Next Actions:")
                        for action in briefing.get("suggested_investigations", []):
                            p_badge = "🔴 High" if action.get("priority") == "HIGH" else "🟡 Medium"
                            st.markdown(f"- **{action.get('title')}** ({p_badge}) — `{action.get('action_type')}`: {action.get('description')}")

                    if briefing.get("anomaly_diagnostics"):
                        st.markdown("##### 🔍 Anomaly & Hypothesis Diagnostics:")
                        for diag in briefing.get("anomaly_diagnostics", []):
                            with st.expander(f"⚠️ {diag.get('title', 'Observation')} [{diag.get('severity', 'WARNING')}]", expanded=True):
                                st.markdown(f"**Observed Pattern:** {diag.get('observed_pattern', '')}")
                                st.markdown("**Plausible Hypotheses:**")
                                for hyp in diag.get("plausible_hypotheses", []):
                                    st.markdown(f"  - 🧪 {hyp}")
                                st.info(f"**Recommended Verification:** {diag.get('recommended_verification', 'Inspect raw records.')}")

                    st.markdown("##### 📋 Raw Query Result Dataset:")
                    st.dataframe(active_df, use_container_width=True)
                    st.caption(f"{len(active_df):,} rows × {len(active_df.columns)} columns | Profiled in {profiler_res.duration_seconds}s")

                with tab_pyg:
                    st.markdown("##### 🎨 PyGWalker Interactive Visual Canvas")
                    st.caption("Drag and drop dimensions/metrics to build charts instantly.")
                    with st.spinner("Rendering PyGWalker canvas …"):
                        try:
                            pyg_html = ExplorationVisualizer.generate_pygwalker_html(active_df)
                            if pyg_html:
                                components.html(pyg_html, height=850, scrolling=True)
                            else:
                                st.warning("Could not generate PyGWalker canvas.")
                        except Exception as pyg_err:
                            st.warning(f"PyGWalker unavailable: {pyg_err}")

                with tab_eda:
                    st.markdown("##### 📊 Deep HTML Profiling Report")
                    col_eda_sel, col_eda_gen = st.columns([2, 1])
                    with col_eda_sel:
                        eda_engine_choice = st.selectbox(
                            "Profiling Engine",
                            ["Sweetviz", "YData-Profiling"],
                            key=f"eda_engine_choice_v{version}"
                        )
                    with col_eda_gen:
                        gen_deep_btn = st.button("🚀 Generate Full HTML Report", use_container_width=True, key=f"gen_deep_btn_v{version}")

                    if gen_deep_btn:
                        with st.spinner(f"Generating {eda_engine_choice} report for {len(active_df):,} rows …"):
                            try:
                                from core.profiler.factory import ProfilerFactory
                                deep_prof = ProfilerFactory.get_profiler(eda_engine_choice)
                                deep_res = deep_prof.profile(active_df, title=res.get("question", "Query Result EDA"), generate_html=True)
                                if deep_res.html_content:
                                    st.success(f"✅ Report generated in {deep_res.duration_seconds}s")
                                    components.html(deep_res.html_content, height=800, scrolling=True)
                                    if deep_res.html_report_path and os.path.exists(deep_res.html_report_path):
                                        with open(deep_res.html_report_path, "rb") as rf:
                                            st.download_button(
                                                f"💾 Download {eda_engine_choice} Report (.html)",
                                                data=rf.read(),
                                                file_name=os.path.basename(deep_res.html_report_path),
                                                mime="text/html",
                                                use_container_width=True
                                            )
                            except Exception as deep_err:
                                st.error(f"Profiling error: {deep_err}")

                with tab_chat:
                    st.markdown("##### 💬 Conversational Drill-Down (DuckDB In-Memory)")
                    st.caption("Ask follow-up questions about the current dataset (e.g. *'Sort by completions desc'*, *'Which month had highest completed_pct?'*).")

                    chat_hist_key = f"chat_history_v{version}"
                    if chat_hist_key not in st.session_state:
                        st.session_state[chat_hist_key] = []

                    for msg in st.session_state[chat_hist_key]:
                        with st.chat_message(msg["role"]):
                            st.markdown(msg["content"])
                            if msg.get("df") is not None:
                                st.dataframe(msg["df"], use_container_width=True)
                            if msg.get("duckdb_sql"):
                                st.code(msg["duckdb_sql"], language="sql")

                    user_q = st.chat_input("Ask a follow-up question or request data slice...", key=f"chat_input_v{version}")
                    if user_q:
                        st.session_state[chat_hist_key].append({"role": "user", "content": user_q})
                        with st.spinner("🦆 Routing to DuckDB or Cloud …"):
                            try:
                                router = LocalQueryRouter(
                                    provider=feedback_provider,
                                    model_name=feedback_model,
                                    api_key=feedback_api_key,
                                    ollama_url=ollama_url
                                )
                                route_res = router.route_and_execute(
                                    user_query=user_q,
                                    active_df=active_df,
                                    original_question=res.get("question", question_input),
                                    original_sql=res.get("sql", ""),
                                    api_key=feedback_api_key
                                )
                                if route_res["handled_locally"]:
                                    resp_content = f"**⚡ In-Memory Result (<50ms):**\n\n{route_res.get('explanation', '')}"
                                    st.session_state[chat_hist_key].append({
                                        "role": "assistant",
                                        "content": resp_content,
                                        "df": route_res.get("result_df"),
                                        "duckdb_sql": route_res.get("duckdb_sql")
                                    })
                                else:
                                    resp_content = f"⚠️ **Requires Cloud Query:**\n\n{route_res.get('reason', '')}"
                                    st.session_state[chat_hist_key].append({"role": "assistant", "content": resp_content})
                            except Exception as chat_err:
                                st.session_state[chat_hist_key].append({"role": "assistant", "content": f"⚠️ Router error: {chat_err}"})
                        st.rerun()
        except Exception as hub_err:
            # Analyst Hub is non-critical — never let it crash the main app
            if st.session_state.get("last_query_result_df") is not None:
                st.markdown("<hr style='margin:1rem 0;'>", unsafe_allow_html=True)
                st.warning(f"⚠️ Analyst Hub encountered an error (non-critical): `{hub_err}`")
                st.dataframe(st.session_state["last_query_result_df"].head(50), use_container_width=True)

        st.markdown("<hr style='margin:1rem 0; border-color:rgba(255,255,255,0.08);'>", unsafe_allow_html=True)

        if res.get("explanation") and not res.get("last_heal_meta"):
            st.markdown("##### 💡 Logic & CTE Breakdown:")
            st.markdown(res["explanation"])
            
        # Synaptic Feedback & AI Self-Healing Loop
        st.markdown("<hr style='margin:1.2rem 0; border-color:rgba(255,255,255,0.08);'>", unsafe_allow_html=True)
        st.markdown("##### 🧠 Knowledge Graph Evaluation & Self-Healing Loop")
        
        col_act1, col_act2 = st.columns([1, 1])
        
        with col_act1:
            st.markdown("###### 👍 Positive Reinforcement")
            st.caption("If this query ran successfully and answered your question, reinforce the Knowledge Graph brain connections.")
            
            if st.button("✨ Approve & Commit as Verified Golden Query (+0.05 Weight Boost)", use_container_width=True, type="primary", key=f"pos_reinforce_btn_v{version}"):
                learner = GraphLearner(uri=neo4j_uri, auth=(neo4j_user, neo4j_password), database=neo4j_database)
                tables_for_boost = res.get("tables_used", [])
                if not tables_for_boost and selected_table != "Auto-Detect All Tables":
                    tables_for_boost = [selected_table]
                
                reinforce_res = learner.reinforce_success(
                    question=res.get("question", question_input),
                    sql=edited_sql.strip(),
                    tables_used=tables_for_boost,
                    dialect=res.get("dialect", dialect),
                    notes=f"Verified by analyst (Revision #{version})"
                )
                
                # Seal & Close the active LLM session thread upon positive reinforcement
                st.session_state["llm_session_thread"] = None
                st.session_state["llm_session_active"] = False
                st.session_state["brain_reinforced"] = True
                st.success(f"🎉 **Graph Brain Reinforced & LLM Session Sealed!** Verified Golden Query `{reinforce_res.get('golden_query_id')}` saved. Synaptic weights boosted for: {reinforce_res.get('boosted_tables')}")
                
            # Revert to Previous Revision Option
            if version > 1 and res.get("history"):
                if st.button("⏮️ Revert to Previous Revision", use_container_width=True, key=f"revert_btn_v{version}"):
                    prev_state = res["history"].pop()
                    res["generated_sql"] = prev_state.get("sql", "")
                    res["sql"] = prev_state.get("sql", "")
                    res["version"] = prev_state.get("version", 1)
                    res["last_heal_meta"] = prev_state.get("last_heal_meta")
                    st.rerun()

        with col_act2:
            st.markdown("###### 🛠️ Revise Logic or Fix Database Error")
            st.caption("If the logic is flawed or the query failed on the database, submit feedback to auto-heal and train the brain.")
            
            with st.expander(f"⚡ Open Feedback & Auto-Heal Form (Revise Revision #{version})", expanded=False):
                with st.form(f"self_heal_form_v{version}", clear_on_submit=False):
                    feedback_mode = st.radio(
                        "Feedback Category",
                        ["💡 Business Logic / Metric Correction", "🛠️ Runtime Database Error"],
                        horizontal=True,
                        help="Choose whether the query produced a database error or if its business logic / stage definitions need correction."
                    )
                    
                    is_logic = "Business Logic" in feedback_mode
                    st.caption(f"🧠 Diagnostic Engine: **{feedback_provider}** (`{feedback_model}`)")
                    
                    err_input = st.text_area(
                        "Your Feedback / Error Details *" if not is_logic else "Explain Business Logic Correction & Desired Funnel Criteria *",
                        height=120,
                        placeholder=(
                            "e.g. 'Checkout initiated' should be identified by page_name = 'checkout/start' and not 'BASKET'. Also, login should be checked before personal info."
                            if is_logic else
                            "e.g. SYNTAX_ERROR: line 12: Column 'category_name' cannot be resolved. Did you mean 'category'?"
                        ),
                        help="Explain what logic was flawed or paste the database error message."
                    )
                    analyst_hint = st.text_input(
                        "Analyst Hint / Additional Context (Optional)",
                        placeholder="e.g. In table silver_layer, use event_timestamp for sequential ordering."
                    )
                    heal_submit = st.form_submit_button("⚡ Diagnose, Self-Heal & Learn", use_container_width=True)
                    
                if heal_submit:
                    if not err_input.strip():
                        st.error("Please provide the feedback or error details.")
                    else:
                        healer = AutoHealer(
                            uri=neo4j_uri,
                            auth=(neo4j_user, neo4j_password),
                            database=neo4j_database,
                            ollama_url=ollama_url
                        )
                        f_type = "BUSINESS_LOGIC" if is_logic else "RUNTIME_ERROR"
                        with st.spinner(f"Diagnosing flaw with {feedback_provider} ({feedback_model}), recording learned rule in Neo4j, and producing healed SQL..."):
                            try:
                                heal_res = healer.diagnose_and_heal(
                                    failed_sql=edited_sql.strip(),
                                    error_message=err_input.strip(),
                                    question=res.get("question", question_input),
                                    database_dialect=dialect,
                                    target_table=selected_table if selected_table != "Auto-Detect All Tables" else "",
                                    analyst_notes=analyst_hint.strip(),
                                    feedback_type=f_type,
                                    provider=feedback_provider,
                                    model_name=feedback_model,
                                    api_key=feedback_api_key,
                                    temperature=0.0,
                                    context_window=context_window_kb,
                                    session_messages=st.session_state.get("llm_session_thread")
                                )
                                
                                # Update active session thread with latest turn
                                if heal_res.get("session_messages"):
                                    st.session_state["llm_session_thread"] = heal_res["session_messages"]
                                    st.session_state["llm_session_active"] = True
                                
                                # Store history for rollback
                                if "history" not in res:
                                    res["history"] = []
                                res["history"].append({
                                    "sql": edited_sql.strip(),
                                    "version": version,
                                    "last_heal_meta": res.get("last_heal_meta")
                                })
                                
                                # Update active query to newly healed candidate
                                res["version"] = version + 1
                                res["generated_sql"] = heal_res.get("healed_sql", "")
                                res["sql"] = heal_res.get("healed_sql", "")
                                res["last_heal_meta"] = heal_res
                                st.session_state["last_gen_result"] = res
                                if "last_healed_result" in st.session_state:
                                    del st.session_state["last_healed_result"]
                                st.rerun()
                            except Exception as heal_err:
                                st.error(f"⚠️ **AI Self-Healing Error:** {heal_err}")

        # Expanders for Deep Transparency
        col_exp1, col_exp2 = st.columns(2)
        with col_exp1:
            with st.expander("🔍 Knowledge Graph Context Injected into LLM"):
                st.markdown(f"**Tables Found**: `{res.get('tables_found', 0)}` | **Columns Injected**: `{res.get('columns_found', 0)}` | **Metrics**: `{res.get('metrics_found', 0)}`")
                st.markdown(f"**Learned Rules Applied**: `{res.get('learned_rules_applied', 0)}` | **Golden Queries Referenced**: `{res.get('golden_queries_referenced', 0)}`")
                
        with col_exp2:
            with st.expander("🧠 Full Model Response"):
                st.text(res.get("raw_response", ""))

# ==========================================
# TAB 1: QUERY INGESTION FORM
# ==========================================
with tab_query:
    st.markdown("#### Submit Query & Metadata for Knowledge Ingestion")
    
    # 1. Ingestion Model Configuration Box
    with st.expander("⚙️ Ingestion Engine Settings (LLM for Parsing & Distillation)", expanded=True):
        c_ing_p, c_ing_m, c_ing_ctx = st.columns([1, 1, 1])
        with c_ing_p:
            ing_providers = LLMGateway.PROVIDERS
            ing_default_p_idx = ing_providers.index("OpenRouter API") if "OpenRouter API" in ing_providers else 0
            ing_provider = st.selectbox(
                "Ingestion LLM Provider",
                options=ing_providers,
                index=ing_default_p_idx,
                key="tab1_ing_provider_select",
                on_change=sync_ing_provider,
                help="Model provider used to parse, classify, and distill SQL queries into Neo4j (Default: OpenRouter API - DeepSeek V4 Flash)."
            )
        with c_ing_m:
            ing_api_k = DEFAULT_OPENROUTER_KEY if ing_provider == "OpenRouter API" else (DEFAULT_GEMINI_KEY if ing_provider == "Google Gemini API" else (llm_api_key if ing_provider == llm_provider else ""))
            ing_models = LLMGateway.get_available_models(ing_provider, api_key=ing_api_k, ollama_url=ollama_url)
            default_ing_model = ing_models[0]
            for tgt in ["deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-chat", "qwen2.5-coder:14b", "anthropic/claude-3.5-sonnet", "gemini-3.1-flash-lite"]:
                if tgt in ing_models:
                    default_ing_model = tgt
                    break
            
            if "tab1_ing_model_select" not in st.session_state or st.session_state["tab1_ing_model_select"] not in ing_models:
                st.session_state["tab1_ing_model_select"] = default_ing_model

            ing_model = st.selectbox(
                "Ingestion Model",
                options=ing_models,
                key="tab1_ing_model_select",
                help="Recommended: deepseek/deepseek-v4-flash-0731 (OpenRouter) or qwen2.5-coder:14b (local Ollama)."
            )
        with c_ing_ctx:
            ing_context_window = st.selectbox(
                "Context Window",
                options=[32768, 65536, 131072, 262144, 524288, 1048576],
                format_func=lambda x: f"{x // 1024}k ({'Default' if x == 262144 else 'Auto-adjusted'})",
                index=3, # 256k default
                key="tab1_ing_context_window_select",
                help="Default 256k. If model's native context window is smaller, the engine auto-adjusts to the model's maximum."
            )

        # Informative live status badge
        if ing_provider == "OpenRouter API":
            st.caption(f"🚀 **OpenRouter Active:** `{ing_model}` | Allocated Context: **{ing_context_window // 1024}k**")
        elif ing_provider == "Local Ollama":
            model_max = LLMGateway.get_ollama_model_max_context(ing_model, ollama_url=ollama_url)
            eff_ctx = min(ing_context_window, model_max)
            st.caption(f"🖥️ **Ollama Active:** `{ing_model}` | Model Native Limit: **{model_max // 1024}k** | Allocated Context: **{eff_ctx // 1024}k** {'(Auto-clamped to max)' if ing_context_window > model_max else ''}")
        elif ing_provider == "Google Gemini API":
            st.caption(f"⚡ **Gemini Active:** `{ing_model}` | Native Window: **1M–2M Tokens** | Allocated Context: **{ing_context_window // 1024}k**")
        else:
            st.caption(f"🌐 **{ing_provider}:** `{ing_model}` | Allocated Context: **{ing_context_window // 1024}k**")

    # 2. File Uploader Outside Form for Instant Visual Confirmation
    sample_output_file = st.file_uploader(
        "Upload Sample Query Output CSV (Optional)", 
        type=["csv"], 
        help="Attach sample query output data for enhanced output column profiling."
    )
    
    sample_columns = []
    sample_rows = 0
    if sample_output_file is not None:
        df_preview, sample_columns, sample_rows = safe_load_csv_sample(sample_output_file, nrows=10)
        if df_preview is not None:
            st.success(f"📁 **CSV Attached Successfully:** `{sample_output_file.name}` ({sample_output_file.size / 1024:.1f} KB) — Detected **{len(sample_columns)} columns**.")
            with st.expander("👁️ Preview First 5 Rows of Attached Sample Data", expanded=False):
                st.dataframe(df_preview.head(5), use_container_width=True)
        else:
            st.warning(f"⚠️ Attached `{sample_output_file.name}` ({sample_output_file.size / 1024:.1f} KB), but could not generate auto-preview. Ingestion will still proceed.")

    # 3. Main Query Metadata Form
    with st.form("sql_ingestion_form", clear_on_submit=False):
        col1, col2 = st.columns([2, 1])
        
        with col1:
            raw_sql = st.text_area(
                "SQL Query *",
                height=220,
                placeholder="-- Paste your analytical SQL query here\nSELECT \n  country,\n  COUNT(DISTINCT visitor_id) AS visitors,\n  COUNT(DISTINCT order_id) AS orders\nFROM silver_layer.t_link_journey_checkout_com\nWHERE event_date >= '2026-01-01'\nGROUP BY country;"
            )
            general_context = st.text_area(
                "General Business Context & Notes (Optional)",
                height=90,
                placeholder="e.g. This query measures drop-off between checkout/account and checkout/personalInfo for acquisition users in fixed service line. Session login status indicates authenticated flow.",
                help="Free-text context describing business goals, funnel definitions, or calculation nuances. This context is passed directly to the LLM during parsing."
            )
            
        with col2:
            journey_stage = st.selectbox(
                "Journey Stage or Page Name *",
                options=["Checkout", "Appointment", "Registration", "PersonalInfo", "Consent", "Shipping", "Identification", "Discovery", "Cart", "Acquisition", "Shared", "Custom Page / Custom Stage"],
                index=0
            )
            if journey_stage == "Custom Page / Custom Stage":
                journey_stage = st.text_input("Specify Custom Stage / Page Name", value="Payment Page")
                
            service_line = st.selectbox(
                "Service Line *",
                options=["Mobile", "Broadband", "Consumer", "B2B", "TV / Entertainment", "Shared Services", "Fixed & Mobile", "Custom"],
                index=6
            )
            if service_line == "Custom":
                service_line = st.text_input("Specify Custom Service Line", value="Enterprise")
                
            category = st.selectbox(
                "Category *",
                options=["Conversion & KPIs", "Traffic & Funnel", "Revenue & ARPU", "Retention & Churn", "Basket & Checkout", "Errors & Quality", "Custom"],
                index=0
            )
            if category == "Custom":
                category = st.text_input("Specify Custom Category", value="Cart Abandonment")
                
            col_natco, col_tags = st.columns(2)
            with col_natco:
                natco = st.text_input("Natco (Country / OpCo) *", value="DE", help="e.g. DE, UK, US, AT, PL, HR, Global")
            with col_tags:
                tags = st.text_input("Tags (comma-separated)", value="Funnel, Core KPI, Production")
                
        submit_button = st.form_submit_button("🚀 Trigger Query Ingestion", use_container_width=True)

    if submit_button:
        if not raw_sql.strip():
            st.error("Please provide a SQL query before triggering ingestion.")
        else:
            run_id = str(uuid.uuid4())
            metadata = {
                "general_context": general_context.strip(),
                "description": general_context.strip(),
                "journey_stage": journey_stage,
                "journey_stage_or_page": journey_stage,
                "service_line": service_line,
                "category": category,
                "natco": natco,
                "tags": tags,
                "owner": "Analytics Team",
                "llm_provider": ing_provider,
                "llm_model": ing_model,
                "context_window": ing_context_window,
                "llm_api_key": ing_api_k if ing_provider in ["OpenRouter API", "Google Gemini API"] else (llm_api_key if ing_provider == llm_provider else "")
            }
            
            if sample_columns:
                metadata["sample_output_columns"] = sample_columns
                metadata["sample_output_rows"] = sample_rows
            
            # Record run as RUNNING
            create_run(
                run_id=run_id,
                sql_query=raw_sql,
                journey_stage_or_page=journey_stage,
                service_line=service_line,
                category=category,
                natco=natco,
                tags=tags,
                status="RUNNING"
            )
            
            status_box = st.status(f"⚡ Ingesting Query with {ing_provider} ({ing_model})...", expanded=True)
            t_ing_start = datetime.now()
            try:
                status_box.write("🧠 **Step 1/3**: Performing deep semantic reasoning & SQL idiom extraction...")
                pipeline = IngestionPipeline()
                
                status_box.write("📐 **Step 2/3**: Validating schema mappings against Neo4j...")
                res = pipeline.ingest(
                    raw_sql=raw_sql,
                    uri=neo4j_uri,
                    auth=(neo4j_user, neo4j_password),
                    database=neo4j_database if neo4j_database else None,
                    metadata=metadata,
                    run_id=run_id
                )
                
                status_box.write("🔗 **Step 3/3**: Injecting Golden Query node & reinforcing synapses...")
                checkpoint_path = res.get("checkpoint_path")
                canonical_id = res.get("canonical_id")
                
                update_run(
                    run_id=run_id,
                    status="SUCCESS",
                    checkpoint_path=checkpoint_path,
                    canonical_id=canonical_id
                )
                
                elapsed = (datetime.now() - t_ing_start).total_seconds()
                status_box.update(label=f"✅ Query Ingested Successfully in {elapsed:.2f}s! (Run ID: {run_id[:8]}...)", state="complete", expanded=False)
                st.success(f"🎉 **Knowledge Ingestion Complete!** Synapses and rules have been updated in Neo4j (Run ID: `{run_id}`).")
                
                with st.expander("🔍 View Ingested Canonical Entity & Graph Checkpoint", expanded=True):
                    if checkpoint_path and os.path.exists(checkpoint_path):
                        with open(checkpoint_path) as cp_f:
                            st.json(json.load(cp_f))
                            
            except Exception as e:
                err_msg = str(e)
                update_run(
                    run_id=run_id,
                    status="FAILED",
                    error_message=err_msg
                )
                status_box.update(label=f"❌ Ingestion Failed: {err_msg}", state="error", expanded=True)
                st.error(f"❌ Ingestion Failed: {err_msg}")

# ==========================================
# TAB 2: TABLE & SCHEMA INGESTION FORM
# ==========================================
with tab_table:
    st.markdown("#### 🗄️ Ingest Physical Base Table & Column Schemas")
    st.markdown("Upload a sample CSV (e.g. 10,000 rows `SELECT *`) to profile columns, distinct value distributions, and link base tables to metrics.")
    
    # Reactive Table CSV Uploader Outside Form
    table_csv_upload = st.file_uploader(
        "Upload Table Sample CSV (e.g. 10k rows) *", 
        type=["csv"],
        key="table_schema_csv_uploader"
    )
    
    if table_csv_upload is not None:
        df_tbl_preview, tbl_cols, _ = safe_load_csv_sample(table_csv_upload, nrows=10)
        if df_tbl_preview is not None:
            st.success(f"📁 **Table CSV Attached:** `{table_csv_upload.name}` ({table_csv_upload.size / 1024:.1f} KB) — Detected **{len(tbl_cols)} columns**.")
            with st.expander("👁️ Preview First 5 Rows of Table Data", expanded=False):
                st.dataframe(df_tbl_preview.head(5), use_container_width=True)
        else:
            st.info(f"📁 Attached `{table_csv_upload.name}` ({table_csv_upload.size / 1024:.1f} KB). Ready for profiling.")

    with st.form("table_ingestion_form"):
        col_t1, col_t2 = st.columns([1, 1])
        with col_t1:
            tbl_name = st.text_input("Physical Table Name *", value="silver_layer.t_link_journey_checkout_com", help="e.g. silver_layer.t_link_journey_checkout_com or eshop_data.es_events_v2")
            db_name = st.text_input("Database / Data Lake *", value="Athena Central Analytics (DB: 59)")
        with col_t2:
            tbl_desc = st.text_input("Table Business Domain / Description", value="E-shop One Checkout Clickstream and Funnel Journey Events")
            
        ingest_tbl_btn = st.form_submit_button("⚡ Ingest Table & Columns into Knowledge Graph", use_container_width=True)
        
    if ingest_tbl_btn:
        if not tbl_name.strip():
            st.error("Please provide a valid table name.")
        elif table_csv_upload is None:
            # Check if we already have local sample
            local_sample = f"data/schema_samples/{tbl_name.replace('.', '_')}.csv"
            if os.path.exists(local_sample):
                st.info(f"Using pre-extracted local sample at `{local_sample}`...")
                ingest_worker = TableSchemaIngestion(uri=neo4j_uri, auth=(neo4j_user, neo4j_password), database=neo4j_database)
                schema_info = ingest_worker.profile_csv(local_sample, table_name=tbl_name, database_name=db_name)
                res = ingest_worker.ingest_schema(schema_info)
                st.success(f"✅ Successfully ingested `{tbl_name}` with **{res['columns_ingested']} columns** into Neo4j!")
            else:
                st.error("Please upload a sample CSV file or extract one first.")
        else:
            try:
                with st.spinner("Profiling table columns, distinct counts, and null percentages..."):
                    ingest_worker = TableSchemaIngestion(uri=neo4j_uri, auth=(neo4j_user, neo4j_password), database=neo4j_database)
                    schema_info = ingest_worker.profile_csv(table_csv_upload, table_name=tbl_name, database_name=db_name)
                    res = ingest_worker.ingest_schema(schema_info)
                    
                    st.success(f"✅ Successfully ingested `{tbl_name}` with **{res['columns_ingested']} columns** into Neo4j!")
                    with st.expander("📋 View Profiled Columns & Distributions"):
                        df_summary = pd.DataFrame([
                            {
                                "Column": col,
                                "Type": d["dtype"],
                                "Null %": f"{d['null_pct']}%",
                                "Distinct Count": d["distinct_count"],
                                "Sample Values": ", ".join(d["sample_values"][:4])
                            }
                            for col, d in schema_info["columns"].items()
                        ])
                        st.dataframe(df_summary, use_container_width=True)
            except Exception as e:
                st.error(f"❌ Failed to ingest table schema: {e}")

# ==========================================
# TAB 3: GRAPH & SCHEMA EXPLORER
# ==========================================
with tab_graph:
    st.markdown("#### 🌐 Knowledge Graph & Schema Intelligence Explorer")
    
    @st.cache_data(ttl=20, show_spinner=False)
    def fetch_cached_graph_explorer_data(uri, user, pwd, db):
        try:
            d = GraphDatabase.driver(uri, auth=(user, pwd))
            with d.session(database=db if db else "neo4j") as s:
                t_recs = s.run("MATCH (t:Table) RETURN t.name as name, t.database as db, t.column_count as cols, t.row_count as rows").data()
                m_recs = s.run("MATCH (m:Metric) RETURN m.name as MetricName, m.sql_aggregation as Aggregation, m.formula as Formula, m.category as Category, m.journey_stage as JourneyStage LIMIT 50").data()
                i_recs = s.run("""
                MATCH (intent:BusinessIntent)
                OPTIONAL MATCH (intent)-[:BELONGS_TO_STAGE]->(stage:JourneyStage)
                OPTIONAL MATCH (intent)-[:IMPLEMENTS_IDIOM]->(idiom:SqlIdiom)
                OPTIONAL MATCH (intent)-[:APPLIES_RULE]->(rule:LearnedRule)
                RETURN intent.name as BusinessIntent, stage.name as Stage, intent.goal as Goal,
                       count(DISTINCT idiom) as IdiomsCount, count(DISTINCT rule) as RulesCount
                ORDER BY stage.name, intent.name
                """).data()
                id_recs = s.run("""
                MATCH (i:SqlIdiom)
                RETURN i.name as IdiomName, i.category as Category, i.description as Description,
                       i.sql_skeleton as SQLSkeleton, i.when_to_use as WhenToUse
                ORDER BY i.category, i.name
                """).data()
                dr_recs = s.run("MATCH (r:LearnedRule) RETURN r.description as RuleDescription, r.rule_type as RuleType, r.reasoning as Reasoning ORDER BY r.rule_type, r.description").data()
                alias_recs = s.run("""
                MATCH (c:Column)-[r:ALIASED_AS]->(a:Alias)
                OPTIONAL MATCH (t:Table)-[:HAS_COLUMN]->(c)
                RETURN a.name as BusinessAlias, c.name as PhysicalColumn, coalesce(t.name, c.table_name) as TableName,
                       r.expression as Expression, coalesce(r.frequency, 1) as Frequency
                ORDER BY Frequency DESC, BusinessAlias
                """).data()
                b_c = s.run("MATCH (b:BusinessIntent) RETURN count(b) as c").single()["c"]
                id_c = s.run("MATCH (i:SqlIdiom) RETURN count(i) as c").single()["c"]
                lr_c = s.run("MATCH (r:LearnedRule) RETURN count(r) as c").single()["c"]
                gq_c = s.run("MATCH (q:VerifiedGoldenQuery) RETURN count(q) as c").single()["c"]
                al_c = s.run("MATCH (a:Alias) RETURN count(a) as c").single()["c"]
                gq_recs = s.run("""
                MATCH (q:VerifiedGoldenQuery)
                OPTIONAL MATCH (q)-[:USES_TABLE]->(t:Table)
                RETURN coalesce(q.id, q.name) as ID, coalesce(q.question, q.name) as Question,
                       coalesce(q.verified_by, 'GroundTruth') as VerifiedBy,
                       coalesce(q.journey_stage, 'Checkout') as Stage,
                       count(DISTINCT t) as TablesCount
                ORDER BY q.created_at DESC
                LIMIT 50
                """).data()
            d.close()
            return True, (t_recs, m_recs, intent_records, id_recs, dr_recs, alias_recs, (b_c, id_c, lr_c, gq_c, al_c), gq_recs), ""
        except Exception as e:
            return False, ([], [], [], [], [], [], (0, 0, 0, 0, 0), []), str(e)

    try:
        ok_exp, exp_data, exp_err = fetch_cached_graph_explorer_data(neo4j_uri, neo4j_user, neo4j_password, neo4j_database)
        if ok_exp:
            t_records, m_records, intent_records, idiom_records, drule_records, alias_records, syn_counts, gq_records = exp_data
            
            st.markdown("##### 🗄️ Ingested Base Tables")
            if t_records:
                st.table(pd.DataFrame(t_records))
            else:
                st.info("No tables ingested in the graph yet.")
                
            st.divider()
            st.markdown("##### 🔍 Inspect Table Columns & Samples")
            if t_records:
                selected_tbl = st.selectbox("Select Table to Inspect", [r["name"] for r in t_records])
                
                @st.cache_data(ttl=20, show_spinner=False)
                def fetch_cached_table_columns(uri, user, pwd, db, tbl_name):
                    try:
                        d = GraphDatabase.driver(uri, auth=(user, pwd))
                        with d.session(database=db if db else "neo4j") as s:
                            res = s.run(f"""
                            MATCH (t:Table {{name: '{tbl_name}'}})-[:HAS_COLUMN]->(c:Column)
                            RETURN c.name as Column, c.dtype as DataType, c.null_pct as NullPct, c.distinct_count as DistinctCount, c.sample_values as Samples
                            ORDER BY c.name
                            """).data()
                        d.close()
                        return res
                    except Exception:
                        return []

                cols_res = fetch_cached_table_columns(neo4j_uri, neo4j_user, neo4j_password, neo4j_database, selected_tbl)
                if cols_res:
                    df_cols = pd.DataFrame(cols_res)
                    st.dataframe(df_cols, use_container_width=True)
            
            st.divider()
            st.markdown("##### 🏷️ Dynamically Learned Column Aliases & Terms (from Ingested Queries)")
            if alias_records:
                st.dataframe(pd.DataFrame(alias_records), use_container_width=True)
            else:
                st.info("No column aliases learned yet. Ingest queries or dashboards to automatically learn aliases.")

            st.divider()
            st.markdown("##### 📊 Ingested Metric Entities")
            if m_records:
                st.dataframe(pd.DataFrame(m_records), use_container_width=True)

            st.divider()
            st.markdown("##### 🎯 Distilled Business Intents & Funnel Stages (Metabase Ground Truth)")
            if intent_records:
                st.dataframe(pd.DataFrame(intent_records), use_container_width=True)

            st.divider()
            st.markdown("##### 🏗️ Learned SQL Architectural Idioms & Patterns")
            if idiom_records:
                st.dataframe(pd.DataFrame(idiom_records), use_container_width=True)

            st.divider()
            st.markdown("##### 📌 Learned Funnel & Domain Rules")
            if drule_records:
                st.dataframe(pd.DataFrame(drule_records), use_container_width=True)

            st.divider()
            st.markdown("##### 🧠 Knowledge Graph Synaptic Health & Episodic Memory")
            b_cnt, id_cnt, lr_cnt, gq_cnt, al_cnt = syn_counts

            col_b1, col_b2, col_b3, col_b4, col_b5 = st.columns(5)
            with col_b1:
                st.metric("🎯 Business Intents", b_cnt)
            with col_b2:
                st.metric("🏗️ SQL Idioms", id_cnt)
            with col_b3:
                st.metric("📌 Domain Rules", lr_cnt)
            with col_b4:
                st.metric("⭐ Golden Queries", gq_cnt)
            with col_b5:
                st.metric("🏷️ Learned Aliases", al_cnt)
                
            # Golden Queries Manager
            if gq_records:
                st.markdown("###### ⭐ Active Golden Queries Pool (Ground Truth Metabase Templates):")
                st.dataframe(pd.DataFrame(gq_records)[["ID", "Question", "Stage", "MetabaseCardID", "Dialect", "Tables"]], use_container_width=True)
                with st.expander("🗑️ Manage / Delete Golden Queries"):
                    col_del1, col_del2 = st.columns([2, 1])
                    with col_del1:
                        del_id = st.selectbox("Select Golden Query ID to Delete", [r["ID"] for r in gq_records])
                    with col_del2:
                        if st.button("🗑️ Delete Selected Golden Query", use_container_width=True):
                            s.run("MATCH (q:VerifiedGoldenQuery) WHERE q.id = $qid OR q.name = $qid DETACH DELETE q", qid=del_id)
                            st.success(f"Golden query `{del_id}` deleted successfully.")
                            st.rerun()
            else:
                st.info("⭐ No Golden Queries in the pool yet. Promote verified queries using the '✨ Approve & Commit as Verified Golden Query' button in the Query Generator tab.")
                
            if stats.get("recent_rules"):
                st.markdown("###### 📜 Recent Learned Correction Rules & Anti-Patterns:")
                st.dataframe(pd.DataFrame(stats.get("recent_rules")), use_container_width=True)
                
            if stats.get("top_reinforced_columns"):
                st.markdown("###### 🏆 Top Reinforced Table Columns:")
                st.dataframe(pd.DataFrame(stats.get("top_reinforced_columns")), use_container_width=True)
        driver.close()
    except Exception as e:
        st.warning(f"Could not connect to Neo4j to explore graph: {e}")

# ==========================================
# TAB 4: RUNS TRACKER & RCA EXPLORER
# ==========================================
with tab_runs:
    runs = get_all_runs()
    
    total_runs = len(runs)
    success_runs = sum(1 for r in runs if r["status"] == "SUCCESS")
    failed_runs = sum(1 for r in runs if r["status"] == "FAILED")
    success_rate = (success_runs / total_runs * 100) if total_runs > 0 else 0
    
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    with col_m1:
        st.markdown(f"""
        <div class='metric-card'>
            <div class='metric-label'>Total Ingestion Runs</div>
            <div class='metric-value'>{total_runs}</div>
        </div>
        """, unsafe_allow_html=True)
    with col_m2:
        st.markdown(f"""
        <div class='metric-card'>
            <div class='metric-label'>Success Rate</div>
            <div class='metric-value'>{success_rate:.1f}%</div>
        </div>
        """, unsafe_allow_html=True)
    with col_m3:
        st.markdown(f"""
        <div class='metric-card'>
            <div class='metric-label'>Successful Runs</div>
            <div class='metric-value' style='color:#10b981;'>{success_runs}</div>
        </div>
        """, unsafe_allow_html=True)
    with col_m4:
        st.markdown(f"""
        <div class='metric-card'>
            <div class='metric-label'>Failed / Needs Retry</div>
            <div class='metric-value' style='color:#ef4444;'>{failed_runs}</div>
        </div>
        """, unsafe_allow_html=True)
        
    st.divider()
    
    # Filters
    col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
    with col_f1:
        status_filter = st.selectbox("Filter Status", ["All", "SUCCESS", "FAILED", "RUNNING"])
    with col_f2:
        natco_filter = st.selectbox("Filter Natco", ["All"] + sorted(list({r["natco"] for r in runs if r["natco"]})))
    with col_f3:
        search_query = st.text_input("Search SQL or Tags", "")

    filtered_runs = runs
    if status_filter != "All":
        filtered_runs = [r for r in filtered_runs if r["status"] == status_filter]
    if natco_filter != "All":
        filtered_runs = [r for r in filtered_runs if r["natco"] == natco_filter]
    if search_query:
        q = search_query.lower()
        filtered_runs = [
            r for r in filtered_runs 
            if q in r["sql_query"].lower() or q in (r["tags"] or "").lower() or q in (r["service_line"] or "").lower()
        ]

    if not filtered_runs:
        st.info("No ingestion runs match the selected criteria.")
    else:
        st.caption(f"Displaying **{min(30, len(filtered_runs))}** of **{len(filtered_runs)}** runs.")
        for run in filtered_runs[:30]:
            r_id = run["run_id"]
            status = run["status"]
            status_class = {
                "SUCCESS": "status-success",
                "FAILED": "status-failed",
                "RUNNING": "status-running"
            }.get(status, "status-pending")
            
            with st.container():
                st.markdown(f"""
                <div class='run-card'>
                    <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem;'>
                        <div>
                            <span class='status-badge {status_class}'>{status}</span>
                            <span style='font-size:0.85rem; color:#94a3b8; margin-left:0.5rem;'>Run ID: <code>{r_id}</code></span>
                        </div>
                        <div style='font-size:0.8rem; color:#64748b;'>{run["timestamp"]}</div>
                    </div>
                    <div>
                        <span class='meta-chip'>🏢 Natco: <b>{run["natco"]}</b></span>
                        <span class='meta-chip'>💼 Service: <b>{run["service_line"]}</b></span>
                        <span class='meta-chip'>📊 Category: <b>{run["category"]}</b></span>
                        <span class='meta-chip'>🧭 Stage/Page: <b>{run["journey_stage_or_page"]}</b></span>
                        <span class='meta-chip'>🏷️ Tags: <b>{run["tags"]}</b></span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                with st.expander("📝 View Submitted SQL Query"):
                    st.code(run["sql_query"], language="sql")
                
                if run["error_message"]:
                    st.error(f"**Error Details**: {run['error_message']}")

                col_act1, col_act2, col_act3, col_act4 = st.columns([1.5, 1.5, 1.5, 1])
                
                with col_act1:
                    has_checkpoint = run["checkpoint_path"] and os.path.exists(run["checkpoint_path"])
                    if status == "FAILED":
                        if st.button(f"🔄 Retry Ingestion", key=f"retry_{r_id}"):
                            pipeline = IngestionPipeline()
                            try:
                                with st.spinner("Retrying ingestion from saved checkpoint..."):
                                    if has_checkpoint:
                                        pipeline.resume_from_checkpoint(
                                            checkpoint_path=run["checkpoint_path"],
                                            uri=neo4j_uri,
                                            auth=(neo4j_user, neo4j_password),
                                            database=neo4j_database if neo4j_database else None,
                                            run_id=r_id
                                        )
                                    else:
                                        pipeline.ingest(
                                            raw_sql=run["sql_query"],
                                            uri=neo4j_uri,
                                            auth=(neo4j_user, neo4j_password),
                                            database=neo4j_database if neo4j_database else None,
                                            metadata={
                                                "journey_stage": run["journey_stage_or_page"],
                                                "service_line": run["service_line"],
                                                "category": run["category"],
                                                "natco": run["natco"],
                                                "tags": run["tags"]
                                            },
                                            run_id=r_id
                                        )
                                update_run(run_id=r_id, status="SUCCESS", error_message=None)
                                st.success("Retry succeeded! Refreshing...")
                                st.rerun()
                            except Exception as retry_err:
                                update_run(run_id=r_id, status="FAILED", error_message=str(retry_err))
                                st.error(f"Retry failed: {retry_err}")
                    else:
                        st.caption("Status: SUCCESS")

                with col_act2:
                    log_file = os.path.join("logs", f"run_{r_id}.json")
                    if os.path.exists(log_file):
                        with st.popover("🔍 View RCA Logs"):
                            st.markdown(f"##### RCA Audit Log: `{r_id}`")
                            events = []
                            with open(log_file, "r") as lf:
                                for line in lf:
                                    if line.strip():
                                        try:
                                            events.append(json.loads(line))
                                        except Exception:
                                            pass
                            if events:
                                df_events = pd.DataFrame(events)
                                st.dataframe(df_events, use_container_width=True)
                            else:
                                st.write("No log events found.")
                    else:
                        st.caption("No log file found")

                with col_act3:
                    if run["checkpoint_path"] and os.path.exists(run["checkpoint_path"]):
                        with st.popover("📄 Checkpoint JSON"):
                            with open(run["checkpoint_path"]) as cf:
                                st.json(json.load(cf))
                    else:
                        st.caption("No checkpoint file")

                with col_act4:
                    if st.button("🗑️ Delete", key=f"del_{r_id}"):
                        delete_run(r_id)
                        st.rerun()

                st.markdown("<hr style='margin:1rem 0; border-color:rgba(255,255,255,0.05);'>", unsafe_allow_html=True)

# ==========================================
# TAB 5: DATA EXPLORER (EDA)
# ==========================================
with tab_eda:
    st.markdown("#### 🔬 Data Explorer — Automated EDA Reports")
    st.caption("Upload a CSV or use the last Metabase query result to generate an interactive profiling report.")

    # Data Source Selection
    eda_data_source = st.radio(
        "Select Data Source",
        ["📁 Upload CSV File", "📡 Use Last Metabase Query Result"],
        horizontal=True,
        key="eda_data_source_radio"
    )

    eda_df = None
    eda_title = "Uploaded CSV EDA"

    if eda_data_source == "📁 Upload CSV File":
        eda_uploaded = st.file_uploader(
            "Upload CSV for EDA",
            type=["csv"],
            key="eda_csv_uploader",
            help="Upload any CSV file to generate an automated profiling report."
        )
        if eda_uploaded is not None:
            try:
                eda_df = pd.read_csv(eda_uploaded, low_memory=False)
                eda_title = eda_uploaded.name.replace(".csv", "")
                st.success(f"✅ Loaded `{eda_uploaded.name}` — **{len(eda_df):,} rows × {len(eda_df.columns)} columns** ({eda_uploaded.size / 1024:.1f} KB)")
            except Exception as csv_err:
                st.error(f"Failed to read CSV: {csv_err}")

    else:  # Use Last Metabase Query Result
        if "last_query_result_df" in st.session_state and st.session_state["last_query_result_df"] is not None:
            eda_df = st.session_state["last_query_result_df"]
            eda_title = "Metabase Query Result"
            st.success(f"✅ Loaded last Metabase query result — **{len(eda_df):,} rows × {len(eda_df.columns)} columns**")
        else:
            st.warning("⚠️ No Metabase query result available. Run a query in the 'Ask Business Question' tab first, or upload a CSV.")

    if eda_df is not None:
        with st.expander("👁️ Preview Data (First 20 Rows)", expanded=False):
            st.dataframe(eda_df.head(20), use_container_width=True)

        eda_generate_btn = st.button(
            "📊 Generate EDA Profiling Report",
            use_container_width=True,
            type="primary",
            key="eda_tab_generate_btn"
        )

        if eda_generate_btn:
            from core.eda_engine import EDAEngine
            engine = EDAEngine()
            with st.spinner(f"📊 Generating sweetviz report for {len(eda_df):,} rows …"):
                try:
                    report_path = engine.generate_report(eda_df, title=eda_title)
                    html_content = engine.load_report_html(report_path)
                    st.success(f"✅ Report saved: `{os.path.basename(report_path)}`")
                    with st.expander("📊 Interactive EDA Report", expanded=True):
                        components.html(html_content, height=800, scrolling=True)
                    with open(report_path, "rb") as rf:
                        st.download_button(
                            "💾 Download EDA Report (.html)",
                            data=rf.read(),
                            file_name=os.path.basename(report_path),
                            mime="text/html",
                            use_container_width=True
                        )
                except Exception as eda_err:
                    st.error(f"**EDA Error:** {eda_err}")

    # Saved Reports Gallery
    st.markdown("<hr style='margin:1.5rem 0; border-color:rgba(255,255,255,0.08);'>", unsafe_allow_html=True)
    st.markdown("##### 📂 Saved EDA Reports")

    from core.eda_engine import EDAEngine
    gallery_engine = EDAEngine()
    saved_reports = gallery_engine.list_saved_reports()

    if not saved_reports:
        st.caption("No saved reports yet. Generate one above to see it here.")
    else:
        for i, rpt in enumerate(saved_reports):
            col_name, col_size, col_date, col_dl = st.columns([3, 1, 1.5, 1])
            with col_name:
                st.markdown(f"📝 `{rpt['name']}`")
            with col_size:
                st.caption(f"{rpt['size_kb']} KB")
            with col_date:
                st.caption(rpt["created"])
            with col_dl:
                with open(rpt["path"], "rb") as rf:
                    st.download_button(
                        "⬇️",
                        data=rf.read(),
                        file_name=rpt["name"],
                        mime="text/html",
                        key=f"dl_report_{i}"
                    )
