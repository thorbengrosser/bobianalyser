#!/bin/sh
set -e

# On first start, initialise the database from schema.sql.
if [ ! -f /data/db.sqlite ]; then
  echo "[init] creating /data/db.sqlite from schema.sql"
  sqlite3 /data/db.sqlite < /app/schema.sql
fi

# Ensure persistent symlinks so app code always writes to the volume.
ln -sf /data/db.sqlite  /app/db.sqlite
ln -sf /data/snapshots  /app/snapshots
ln -sf /data/img-cache  /app/img-cache
mkdir -p /data/snapshots /data/img-cache

exec "$@"
