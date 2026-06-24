#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
echo "Starting Bankr Shilling Tracker on http://127.0.0.1:8080"
python3 app.py
