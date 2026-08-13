#!/bin/zsh
# Run the AI Analytics Dashboard for the DTDL Tenant
cd "$(dirname "$0")"
export ANALYTICS_WATCHER=1
export ANALYTICS_MB_LIVE=1
./run_dashboard.command tenants/DTDL
