#!/bin/bash
# -----------------------------------------------------------------------------
# Setup Daily Automated Execution Schedule for DevOps Automation Suite
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/daily_branch_analyzer.py"
PYTHON_PATH=$(which python3)
REPORTS_DIR="$SCRIPT_DIR/reports"

SMTP_USER="${SMTP_USER:-}"
SMTP_PASS="${SMTP_PASS:-}"
EMAIL_TO="${EMAIL_TO:-}"

echo "⚙️ Setting up automated daily cron job for Branch Analyzer..."
echo "Python binary: $PYTHON_PATH"
echo "Script path:   $SCRIPT_PATH"
echo "Output path:   $REPORTS_DIR"

mkdir -p "$REPORTS_DIR"

# Cron entry for 10:00 AM and 5:00 PM every weekday
CRON_JOB="0 10,17 * * 1-5 SMTP_USER=\"$SMTP_USER\" SMTP_PASS=\"$SMTP_PASS\" EMAIL_TO=\"$EMAIL_TO\" $PYTHON_PATH $SCRIPT_PATH --output-dir $REPORTS_DIR >> $REPORTS_DIR/cron_last_run.log 2>&1"

# Check if crontab already has this entry
(crontab -l 2>/dev/null | grep -F "$SCRIPT_PATH") >/dev/null 2>&1

if [ $? -eq 0 ]; then
    (crontab -l 2>/dev/null | grep -v -F "$SCRIPT_PATH"; echo "$CRON_JOB") | crontab -
    echo "✅ Successfully updated daily cron job in crontab."
else
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "✅ Successfully added daily cron job to crontab."
fi

echo ""
echo "📋 Active User Crontab:"
crontab -l
