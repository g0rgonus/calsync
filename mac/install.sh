#!/bin/bash
# Build calsync-mirror, install the CLI and the menu bar app, and run the app
# at login.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CLI="$BIN_DIR/calsync-mirror"
APP="$HOME/Applications/Calsync Mirror.app"
LABEL="com.goergen.calsync-mirror"
OLD_LABEL="com.goergen.sports-calendar-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/calsync-mirror.log"
CONFIG="$HOME/.config/calsync-mirror/config.json"

echo "Building..."
swift build --package-path "$HERE" -c release

# Asked of the binary rather than written here. A version literal in an
# installer is one that disagrees with the tool it installs the first time
# somebody bumps one and not the other.
VERSION="$("$HERE/.build/release/calsync-mirror" --version)"
echo "Version: $VERSION"

# The CLI stays: it is how you run a one-off, see a plan, or debug without the
# app in the way.
mkdir -p "$BIN_DIR"
cp "$HERE/.build/release/calsync-mirror" "$CLI"
echo "Installed CLI  -> $CLI"

# --- the app bundle --------------------------------------------------------
#
# Assembled here rather than by an Xcode project: Core, the CLI and the tests
# are a SwiftPM package regardless, and a second build system would be a second
# thing to keep in step. What makes this a real app is the bundle — a name in
# the permission dialog, a bundle id (which notifications require), and no Dock
# icon — not the project file that usually produces one.
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/.build/release/CalsyncMirrorMenu" "$APP/Contents/MacOS/Calsync Mirror"

cat > "$APP/Contents/Info.plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>Calsync Mirror</string>
    <key>CFBundleDisplayName</key>       <string>Calsync Mirror</string>
    <key>CFBundleIdentifier</key>        <string>$LABEL</string>
    <key>CFBundleExecutable</key>        <string>Calsync Mirror</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key>           <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>    <string>14.0</string>

    <!-- No Dock icon and no menu bar of its own: this is a status item. -->
    <key>LSUIElement</key>               <true/>

    <!-- Required. A bundled app that requests calendar access without a usage
         string is terminated by the system rather than refused politely. -->
    <key>NSCalendarsFullAccessUsageDescription</key>
    <string>Calsync Mirror writes your kids' schedules into the shared family calendars, and only touches events it created.</string>
    <key>NSCalendarsUsageDescription</key>
    <string>Calsync Mirror writes your kids' schedules into the shared family calendars, and only touches events it created.</string>
</dict>
</plist>
PLISTEOF

# Ad-hoc, which is what an unsigned local build gets. The signature changes on
# every rebuild, so macOS may ask for calendar access again after one — that is
# the cost of not having a Developer ID, not a bug.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "Installed app  -> $APP"

if [ ! -f "$CONFIG" ]; then
    "$CLI" --init
    echo
    echo "Edit $CONFIG before going further — the Radicale URL and the two"
    echo "calendar names are almost certainly not the defaults."
    exit 0
fi

# --- login item ------------------------------------------------------------
#
# The app holds its own timer, so there is no StartInterval here. Any older
# agent that ran the CLI on a schedule is removed: two processes writing the
# same calendar would race, and EventKit has no lock to arbitrate it.
launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$APP/Contents/MacOS/Calsync Mirror</string></array>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>
    <key>StandardOutPath</key>  <string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded $LABEL (runs at login, syncs on its own timer)"

cat <<NOTE

Check it reads Radicale before letting it near a calendar:

  $CLI --check      no calendar access needed
  $CLI --dry-run    the plan; macOS prompts for Calendar here

The menu bar icon appears once the app is running. Pause, Sync Now and the
review count are there.

  Logs:       $LOG
  Config:     $CONFIG
  Stop:       launchctl bootout gui/\$(id -u)/$LABEL
  Restart:    launchctl kickstart -k gui/\$(id -u)/$LABEL
NOTE
