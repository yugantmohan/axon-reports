#!/bin/zsh
# Installs (or re-installs) the two scheduled jobs. Safe to re-run.
set -e
cd "$(dirname "$0")"
mkdir -p logs ~/Library/LaunchAgents
for job in com.axon.sync com.axon.woo; do
    plutil -lint "$job.plist"
    launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
    cp "$job.plist" ~/Library/LaunchAgents/
    launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/"$job.plist"
    echo "installed $job"
done
launchctl list | grep com.axon
