#!/bin/bash
# Cron wrapper for closing_summary_collector.py — runs on weekdays after A-share close
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="/Users/yuhao"

ENV_FILE="$(dirname "$0")/.env"
[ -f "$ENV_FILE" ] && export $(grep -v '^#' "$ENV_FILE" | xargs)

cd "$(dirname "$0")" || exit 1

LOG_DIR="output/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/closing_$(date +%Y-%m-%d).log"

python3 closing_summary_collector.py 2>&1 | tee "$LOG_FILE"
