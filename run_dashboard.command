#!/bin/bash

# ==============================================================================
# ⚡ AI Analytics - Standalone Analytics Platform Launcher
# Launches the CURRENT standalone UI (streamlit thin API client) + its FastAPI
# backend. This is standalone_ui.py, NOT the legacy SQL-generator app.py.
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

# 0. Handle Tenant Isolation
TENANT_DIR=$1
if [ -n "$TENANT_DIR" ]; then
    echo "🏢 Booting isolated tenant from: $TENANT_DIR"
    export ANALYTICS_DATA_DIR="$TENANT_DIR"
    
    # Source tenant-specific .env if it exists
    if [ -f "$TENANT_DIR/.env" ]; then
        echo "🔑 Loading tenant secrets from $TENANT_DIR/.env"
        set -a
        source "$TENANT_DIR/.env"
        set +a
    fi
fi

# Client read timeout (seconds). Default high enough for a *cold* junior refresh
# (which may trigger a live LLM call + executor schema probes; the server-side LLM
# client allows up to 120s). Tighten with ANALYTICS_API_TIMEOUT if you prefer.
export ANALYTICS_API_TIMEOUT="${ANALYTICS_API_TIMEOUT:-120}"

# 2. Check Python Virtual Environment
if [ -f ".venv/bin/streamlit" ]; then
    echo "✅ Found virtual environment (.venv)."
else
    echo "❌ Error: Virtual environment (.venv) not found or streamlit is missing."
    echo "   Please create it using: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    read -p "Press [Enter] to exit..."
    exit 1
fi

# 3. Ensure the FastAPI backend (analytics_platform.serve) is running on 8000
export ANALYTICS_API_URL="${ANALYTICS_API_URL:-http://localhost:8000}"
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "🛡 Backend API already running on ${ANALYTICS_API_URL}."
else
    echo "🛜 Starting backend API on ${ANALYTICS_API_URL} ..."
    nohup .venv/bin/python -m analytics_platform serve 8000 \
        >/tmp/ai_analytics_api.log 2>&1 &
    echo "   Backend log: /tmp/ai_analytics_api.log"
    # Wait until the backend actually answers before starting the UI, so a
    # double-click "just works" instead of the UI racing a still-booting API.
    HEALTH_URL="${ANALYTICS_API_URL}/tenants"
    echo -n "⏳ Waiting for backend to answer at ${HEALTH_URL}"
    API_READY=0
    for _ in $(seq 1 30); do
        if curl -sfS -m 2 "$HEALTH_URL" >/dev/null 2>&1; then
            API_READY=1
            echo " ✅"
            break
        fi
        echo -n "."
        sleep 1
    done
    if [ "$API_READY" -ne 1 ]; then
        echo ""
        echo "⚠️  Backend did not respond on ${ANALYTICS_API_URL} after ~30s."
        echo "   Open /tmp/ai_analytics_api.log for the API error."
        read -r -p "Press [Enter] to exit ..."
        exit 1
    fi
fi

# 4. Auto-free port 3000 if previously occupied
OCCUPIED_PID=$(lsof -ti :3000 2>/dev/null)
if [ -n "$OCCUPIED_PID" ]; then
    echo "🧹 Freeing port 3000 (terminating previous instance PID: $OCCUPIED_PID)..."
    kill -9 $OCCUPIED_PID 2>/dev/null || true
    sleep 1
fi

echo ""
echo "🌐 Starting Next.js UI on http://localhost:3000 (API: ${ANALYTICS_API_URL})..."
echo "ℹ️  Press [Ctrl + C] in this window anytime to stop the server."
echo "========================================================================"
echo ""

# Open browser after a short delay in background
(sleep 3 && open "http://localhost:3000") &

# 5. Launch the Next.js frontend
cd frontend
if [ ! -d "node_modules" ]; then
    echo "📦 Installing frontend dependencies..."
    npm install
fi
npm run dev
