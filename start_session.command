#!/bin/bash

# ==============================================================================
# 🔁 AI Analytics — clean session launcher
#
# Kills any session that is already running (backend on :8000, Next.js UI on
# :3000, plus any stray processes of either), waits for the ports to actually
# come free, then starts a fresh session via run_dashboard_DTDL.command.
#
# Double-click it in Finder, or run:  ./start_session.command
# Any arguments are passed straight through to the launcher.
#
# To stop the session afterwards, press Ctrl+C in this window — or just run
# this script again, since it always kills before it starts.
# ==============================================================================

cd "$(dirname "$0")" || exit 1

BACKEND_PORT=8000
FRONTEND_PORT=3000

echo "========================================================================"
echo "      🔁 Restarting AI Analytics session (clean slate)                  "
echo "========================================================================"
echo ""

# ------------------------------------------------------------------------------
# 1. Kill whatever is already running
# ------------------------------------------------------------------------------

# free_port <port> <label> — TERM the listeners, then KILL anything that clings on
free_port() {
    port=$1
    label=$2
    pids=$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null)

    if [ -z "$pids" ]; then
        echo "✅ Port ${port} (${label}) already free."
        return 0
    fi

    echo "🧹 Port ${port} (${label}) in use by PID(s): $(echo "$pids" | tr '\n' ' ')"
    echo "$pids" | xargs kill 2>/dev/null

    # Give them a moment to shut down cleanly before escalating.
    for _ in $(seq 1 10); do
        sleep 0.5
        remaining=$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null)
        [ -z "$remaining" ] && break
    done

    remaining=$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null)
    if [ -n "$remaining" ]; then
        echo "   ↳ still up, forcing: $(echo "$remaining" | tr '\n' ' ')"
        echo "$remaining" | xargs kill -9 2>/dev/null
        sleep 1
    fi

    if lsof -ti "tcp:${port}" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "❌ Could not free port ${port}. Something is holding it that this"
        echo "   script cannot kill (different user, or a system process)."
        echo "   Inspect it with:  lsof -nP -iTCP:${port} -sTCP:LISTEN"
        read -r -p "Press [Enter] to exit..."
        exit 1
    fi

    echo "   ↳ freed."
}

free_port "$BACKEND_PORT" "backend API"
free_port "$FRONTEND_PORT" "Next.js UI"

# Catch strays that are running but not (or no longer) holding a listening port —
# e.g. a backend that crashed mid-boot, or a `next dev` child left behind.
# Scoped to this project directory so other repos' dev servers are left alone.
PROJECT_DIR="$(pwd)"

for pid in $(pgrep -f "analytics_platform serve" 2>/dev/null); do
    echo "🧹 Killing stray backend process $pid"
    kill -9 "$pid" 2>/dev/null
done

for pid in $(pgrep -f "next dev" 2>/dev/null); do
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)
    case "$cwd" in
        "$PROJECT_DIR"*)
            echo "🧹 Killing stray Next.js process $pid"
            kill -9 "$pid" 2>/dev/null
            ;;
    esac
done

echo ""
echo "🧼 Previous session cleared."
echo ""

# ------------------------------------------------------------------------------
# 2. Start a fresh session
# ------------------------------------------------------------------------------
# run_dashboard_DTDL.command sets the real-tenant env vars and delegates to
# run_dashboard.command, which loads API keys, checks the venv, boots the
# backend, waits for it to answer, and then runs `npm run dev` in the
# foreground. Reusing it keeps all that logic in one place.

LAUNCHER="./run_dashboard_DTDL.command"

if [ ! -x "$LAUNCHER" ]; then
    echo "❌ ${LAUNCHER} is missing or not executable."
    echo "   Fix with:  chmod +x ${LAUNCHER}"
    read -r -p "Press [Enter] to exit..."
    exit 1
fi

exec "$LAUNCHER" "$@"
