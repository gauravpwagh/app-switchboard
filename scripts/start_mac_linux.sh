#!/usr/bin/env sh
# Run (or point a desktop shortcut at) this to open the App Switchboard in the background.
cd "$(dirname "$0")/.."
nohup python3 run.py > logs/switchboard.log 2>&1 &
