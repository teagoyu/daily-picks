#!/bin/bash
# Cron wrapper for daily_picks.py — runs on weekdays only
# Ensures PATH, Homebrew, and Longbridge credentials are available

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HOME="/Users/yuhao"

# Load .env if exists (COS_SECRET_ID, COS_SECRET_KEY, FEISHU_WEBHOOK etc.)
ENV_FILE="$(dirname "$0")/.env"
[ -f "$ENV_FILE" ] && export $(grep -v '^#' "$ENV_FILE" | xargs)

cd "$(dirname "$0")" || exit 1

LOG_DIR="output/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_picks_$(date +%Y-%m-%d).log"

python3 daily_picks.py 2>&1 | tee "$LOG_FILE"
