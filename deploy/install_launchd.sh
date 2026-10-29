#!/usr/bin/env bash
# Install Bandwidth on this Mac (SPEC RUN-4): one agent keeps `bw bot` running whenever you are
# logged in and restarts it if it stops. The bot runs both tools' schedules itself.
# It also stops the four agents of the old separate tools (their folders are left untouched),
# unless --keep-old is given: then both run side by side for a trial (they use different bots).
# Usage: deploy/install_launchd.sh [--keep-old | --uninstall]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BW="$ROOT/.venv/bin/bw"
LABEL="com.bandwidth.bot"
OLD_LABELS=(com.opportunityradar.bot com.opportunityradar.daily com.studyplanner.bot com.studyplanner.run)
AGENTS="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
  rm -f "$AGENTS/$LABEL.plist"
  echo "Removed $LABEL."
  echo "To bring back the old tools, run their installers:"
  echo "  ~/Desktop/event-parser/deploy/install_launchd.sh"
  echo "  ~/Desktop/study-planner/deploy/install_launchd.sh"
  exit 0
fi

[[ -x "$BW" ]] || { echo "missing $BW - run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }
"$BW" doctor || { echo "bw doctor found problems - fix them first" >&2; exit 1; }
mkdir -p "$ROOT/data/logs" "$AGENTS"

[[ "${1:-}" == "--keep-old" ]] && OLD_LABELS=()
for old in ${OLD_LABELS[@]+"${OLD_LABELS[@]}"}; do
  if launchctl print "$GUI/$old" >/dev/null 2>&1; then
    launchctl bootout "$GUI/$old" 2>/dev/null || true
    mv -f "$AGENTS/$old.plist" "$AGENTS/$old.plist.disabled" 2>/dev/null || true
    echo "stopped old agent $old (plist kept as $old.plist.disabled)"
  fi
done

cat > "$AGENTS/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BW</string><string>bot</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/data/logs/$LABEL.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/$LABEL.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
</dict>
</plist>
PLIST
plutil -lint "$AGENTS/$LABEL.plist" >/dev/null
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
launchctl bootstrap "$GUI" "$AGENTS/$LABEL.plist"
echo "Installed $LABEL. Logs: $ROOT/data/logs/  ·  Check: launchctl print $GUI/$LABEL | grep state"
