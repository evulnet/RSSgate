#!/usr/bin/env bash
# Supervised server loop for RSSgate (this container has no init; systemd
# user units are unavailable). Respawn the server if it dies; adopt an
# existing listener instead of double-launching (manual starts, test tier).
#
# The server runs as `python -m rssgate`: the command line then contains no
# "run.py", so another project's `pkill -f run.py` (this killed us on
# 2026-10-21) cannot touch us.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PORT=$(awk '$1=="port:"{print $2}' config.yaml 2>/dev/null | tr -d '"')
PORT=${PORT:-8088}
while true; do
  if ss -tln 2>/dev/null | grep -q ":$PORT "; then
    # someone else holds the port (manual start / test tier): adopt, wait
    sleep 10
    continue
  fi
  .venv/bin/python -m rssgate >> rssgate.log 2>&1
  echo "$(date '+%F %T') serve-loop: server exited rc=$?; restarting in 3s" >> rssgate.log
  sleep 3
done
