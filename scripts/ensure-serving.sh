#!/bin/sh
# Ensure the RSSgate supervised loop is running (cron every 5 min; this
# container has no init). Idempotent via pgrep, silently no-ops when up.
# The pgrep pattern is the ABSOLUTE path: filmNotes' loop also matches
# "scripts/serve-loop.sh", and a generic pattern would make each project's
# watchdog believe the other's loop was its own (2026-10-21 scar: generic
# pkill -f patterns are how our server got collateral-killed).
APP=/home/nick/rssgate
if [ -x "$APP/scripts/serve-loop.sh" ]; then
    if ! pgrep -f "$APP/scripts/serve-loop.sh" >/dev/null 2>&1; then
        ( cd "$APP" && setsid nohup "$APP/scripts/serve-loop.sh" >/dev/null 2>&1 & )
    fi
fi
