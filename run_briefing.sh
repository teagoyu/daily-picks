#!/bin/bash
# Cron wrapper for briefing_collector.py — runs on weekdays
# Collects pre-market technical signals, hot news, and earnings calendar → COS

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="/Users/yuhao"

# Load .env if exists (COS credentials etc.)
ENV_FILE="$(dirname "$0")/.env"
[ -f "$ENV_FILE" ] && export $(grep -v '^#' "$ENV_FILE" | xargs)

cd "$(dirname "$0")" || exit 1

LOG_DIR="output/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/briefing_$(date +%Y-%m-%d_%H%M).log"

python3 briefing_collector.py 2>&1 | tee "$LOG_FILE"
