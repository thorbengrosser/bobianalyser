#!/bin/sh
set -e

# On first start, initialise the database from schema.sql.
if [ ! -f /data/db.sqlite ]; then
  echo "[init] creating /data/db.sqlite from schema.sql"
  sqlite3 /data/db.sqlite < /app/schema.sql
fi

# Ensure persistent symlinks so app code always writes to the volume.
# -n: if the link already points at a directory (container restart), replace the
# link itself instead of creating a nested symlink inside the target directory.
mkdir -p /data/snapshots /data/img-cache
ln -sfn /data/db.sqlite  /app/db.sqlite
ln -sfn /data/snapshots  /app/snapshots
ln -sfn /data/img-cache  /app/img-cache

exec "$@"
