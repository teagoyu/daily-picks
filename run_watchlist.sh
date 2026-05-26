#!/bin/bash
cd "$(dirname "$0")"
source .env 2>/dev/null || true
mkdir -p logs
python watchlist_collector.py >> logs/watchlist.log 2>&1
