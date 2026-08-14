#!/bin/zsh
# Run the AI Analytics Dashboard against real, already-migrated tenant data.
#
# No ANALYTICS_DATA_DIR override: since the tenant-isolation migration, the
# real tenant (Acme Retail GmbH / tnt_d23cd823d4c6, adopted from the old
# tenants/DTDL/platform.db) lives at the default data paths (data/control.db
# + tenants/<id>/tenant.db), same as run_dashboard.command's own default.
# Passing "tenants/DTDL" here used to point at the pre-migration location,
# which is now empty -- that made this script silently serve no data.
cd "$(dirname "$0")"
export ANALYTICS_WATCHER=1
export ANALYTICS_MB_LIVE=1
./run_dashboard.command
