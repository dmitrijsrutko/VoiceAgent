#!/usr/bin/env bash
# Keep a local copy of what the Fly machine knows, before a deploy wipes it.
#
#   scripts/pull-fly.sh            # into fly-archive/ (gitignored: personal data)
#
# - Fly's log buffer holds only the last ~100 lines and is lost when the machine
#   is replaced, so each pull is saved as-is and merged, without duplicates,
#   into fly-archive/logs/all.log.
# - The app's own log file and the conversation records live on the volume
#   and survive deploys, but not a deleted volume; they are mirrored into
#   fly-archive/logs/volume/ and fly-archive/sessions/.
#   `--purge-sessions` on the server does not reach this copy: delete it too
#   (AGENTS.md §10).
set -euo pipefail

cd "$(dirname "$0")/.."
archive="fly-archive"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$archive/logs" "$archive/sessions"

# Colour codes stripped: the buffer is meant for a terminal.
fly logs --no-tail 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' > "$archive/logs/fly-$stamp.log"
cat "$archive"/logs/fly-*.log | sort -u > "$archive/logs/all.log"
echo "logs: $(wc -l < "$archive/logs/fly-$stamp.log") lines pulled, $(wc -l < "$archive/logs/all.log") kept in all.log"

remote="/tmp/sessions-$stamp.tgz"
# `logs` only once the app has written one (VOICE_AGENT_LOGS).
fly ssh console -C "sh -c 'cd /data && tar -czf $remote sessions \$(ls -d logs 2>/dev/null)'" > /dev/null
fly ssh sftp get "$remote" "$archive/sessions-$stamp.tgz" > /dev/null
fly ssh console -C "rm -f $remote" > /dev/null
unpacked="$archive/.unpack-$stamp"
mkdir -p "$unpacked"
tar -xzf "$archive/sessions-$stamp.tgz" -C "$unpacked"
rm -f "$archive/sessions-$stamp.tgz"
cp -R "$unpacked/sessions/." "$archive/sessions/"
if [ -d "$unpacked/logs" ]; then
  mkdir -p "$archive/logs/volume"
  cp -R "$unpacked/logs/." "$archive/logs/volume/"
  echo "volume log: $(cat "$archive"/logs/volume/voice-agent.log* | wc -l | tr -d ' ') lines in $archive/logs/volume/"
fi
rm -rf "$unpacked"
echo "sessions: $(ls "$archive/sessions" | wc -l | tr -d ' ') records in $archive/sessions/"
