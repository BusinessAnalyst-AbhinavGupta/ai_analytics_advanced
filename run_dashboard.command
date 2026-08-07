#!/bin/bash

# ==============================================================================
# ⚡ AI Analytics - Knowledge Graph & SQL Generation Dashboard Launcher
# ==============================================================================

# Navigate to project root directory
cd "$(dirname "$0")" || exit 1

# Clear terminal screen
clear

echo "========================================================================"
echo "      🚀 Launching AI Analytics - Synaptic SQL & Ingestion Dashboard    "
echo "========================================================================"
echo ""

# 1. Load User Shell Environment & API Keys (if available in ~/.zshrc or .env)
if [ -f "$HOME/.zshrc" ]; then
    # Extract API Keys safely from ~/.zshrc if not already in environment
    if [ -z "$OPENROUTER_API_KEY" ]; then
        eval "$(grep -E 'export (OPENROUTER_API_KEY|GEMINI_API_KEY|OPENAI_API_KEY|NEO4J_)' "$HOME/.zshrc" 2>/dev/null)"
    fi
fi

if [ -f ".env" ]; then
    echo "📋 Loading environment settings from .env ..."
    export $(grep -v '^#' .env | xargs) 2>/dev/null
fi

# 2. Check Python Virtual Environment
if [ -f ".venv/bin/streamlit" ]; then
    echo "✅ Found virtual environment (.venv)."
else
    echo "❌ Error: Virtual environment (.venv) not found or streamlit is missing."
    echo "   Please create it using: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    read -p "Press [Enter] to exit..."
    exit 1
fi

# 3. Check Neo4j Connectivity (Port 7687)
echo "🔍 Checking Neo4j Graph Database (bolt://localhost:7687)..."
nc -z 127.0.0.1 7687 2>/dev/null
if [ $? -eq 0 ]; then
    echo "✅ Neo4j Database is active and ready."
else
    echo "⚠️  Warning: Neo4j is not responding on port 7687."
    echo "   Please ensure Neo4j Desktop or the Neo4j DBMS is running."
fi

# 4. Auto-free port 8501 if previously occupied
OCCUPIED_PID=$(lsof -ti :8501 2>/dev/null)
if [ -n "$OCCUPIED_PID" ]; then
    echo "🧹 Freeing port 8501 (terminating previous instance PID: $OCCUPIED_PID)..."
    kill -9 $OCCUPIED_PID 2>/dev/null || true
    sleep 1
fi

echo ""
echo "🌐 Starting Streamlit web app on http://localhost:8501 ..."
echo "ℹ️  Press [Ctrl + C] in this window anytime to stop the server."
echo "========================================================================"
echo ""

# Open browser after a short delay in background
(sleep 2 && open "http://localhost:8501") &

# 5. Launch Streamlit Application
PYTHONPATH=. .venv/bin/streamlit run app.py \
    --server.port 8501 \
    --server.headless false \
    --browser.gatherUsageStats false
