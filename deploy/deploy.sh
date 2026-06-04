#!/bin/sh
# Usage: run this on the VPS inside /opt/bobianalyser
# First deploy: git clone https://github.com/thorbengrosser/bobianalyser.git /opt/bobianalyser
# Subsequent:   ./deploy/deploy.sh
set -e
cd "$(dirname "$0")/.."
echo "[deploy] pulling latest..."
git pull
echo "[deploy] rebuilding containers..."
docker compose build --pull
echo "[deploy] restarting..."
docker compose up -d
echo "[deploy] done — status:"
docker compose ps
