#!/usr/bin/env bash
set -euo pipefail

APP_ID="portman"
INSTALL_DIR="$HOME/.local/share/$APP_ID"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$HOME/.config/autostart"
APPS_DIR="$HOME/.local/share/applications"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "╔══════════════════════════════════════════╗"
echo "║      portman — Dev Port Manager          ║"
echo "║              Installer                   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Dependencies ──────────────────────────────────────────────────────────────

echo "▸ Checking dependencies…"

MISSING_APT=()
MISSING_NODE=false

# Node.js
if ! command -v node &>/dev/null; then
    MISSING_NODE=true
    echo "  ✗ node not found (required for CLI)"
else
    echo "  ✓ node $(node --version)"
fi

# Python3 + GTK + AppIndicator
if ! /usr/bin/python3 -c "
import gi
gi.require_version('Gtk','3.0'); gi.require_version('Notify','0.7')
try: gi.require_version('AyatanaAppIndicator3','0.1')
except ValueError: gi.require_version('AppIndicator3','0.1')" 2>/dev/null; then
    MISSING_APT+=("python3-gi" "python3-gi-cairo" "gir1.2-gtk-3.0" "gir1.2-appindicator3-0.1" "gir1.2-notify-0.7")
else
    echo "  ✓ python3-gi / GTK3 / AppIndicator3"
fi

# Cairo
if ! /usr/bin/python3 -c "import cairo" 2>/dev/null; then
    MISSING_APT+=("python3-gi-cairo")
fi

if $MISSING_NODE; then
    echo ""
    echo "  ✗ Node.js is required for the CLI."
    echo "    Install it from https://nodejs.org or via nvm."
    echo "    Then re-run this installer."
    exit 1
fi

if [ ${#MISSING_APT[@]} -gt 0 ]; then
    echo ""
    echo "  ✗ Missing packages: ${MISSING_APT[*]}"
    read -rp "  Install them now with apt? [Y/n] " yn
    case "${yn,,}" in
        n|no) echo "  Aborted."; exit 1 ;;
        *)
            sudo apt-get update -qq
            sudo apt-get install -y "${MISSING_APT[@]}"
            ;;
    esac
fi

echo "  ✓ All dependencies satisfied"
echo ""

# ── Copy files ────────────────────────────────────────────────────────────────

echo "▸ Installing to $INSTALL_DIR …"

mkdir -p "$INSTALL_DIR"
cp "$SRC_DIR/portman.js"       "$INSTALL_DIR/portman.js"
cp "$SRC_DIR/portman_tray.py"  "$INSTALL_DIR/portman_tray.py"
cp "$SRC_DIR/uninstall.sh"     "$INSTALL_DIR/uninstall.sh"
chmod +x "$INSTALL_DIR/portman.js"
chmod +x "$INSTALL_DIR/portman_tray.py"
chmod +x "$INSTALL_DIR/uninstall.sh"

echo "  ✓ Files copied"

# ── CLI wrapper ───────────────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"

# portman CLI wrapper — always points to installed copy
PORTMAN_BIN="$BIN_DIR/portman"
printf '#!/usr/bin/env bash\nexec node "%s/portman.js" "$@"\n' "$INSTALL_DIR" > "$PORTMAN_BIN"
chmod +x "$PORTMAN_BIN"
echo "  ✓ CLI: portman"

# portman-tray wrapper — strips snap-injected env vars
cat > "$BIN_DIR/portman-tray" <<TRAY_EOF
#!/usr/bin/env bash
# portman-tray [start|stop|restart|status|show]

PIDFILE="/tmp/portman-tray.pid"
PYTHON="env -u LD_LIBRARY_PATH -u GTK_EXE_PREFIX -u GTK_PATH -u GDK_PIXBUF_MODULE_FILE -u GSETTINGS_SCHEMA_DIR /usr/bin/python3"

is_running() {
  if [ -f "\$PIDFILE" ]; then
    local pid=\$(cat "\$PIDFILE")
    kill -0 "\$pid" 2>/dev/null && return 0
  fi
  pgrep -f portman_tray.py > /dev/null 2>&1
}

cmd_start() {
  if is_running; then
    echo "portman tray is already running (pid \$(cat \$PIDFILE 2>/dev/null))"
    return 0
  fi
  \$PYTHON "$INSTALL_DIR/portman_tray.py" > /dev/null 2>&1 &
  echo \$! > "\$PIDFILE"
  sleep 0.8
  if is_running; then
    echo "✔ portman tray started (pid \$(cat \$PIDFILE))"
  else
    rm -f "\$PIDFILE"
    echo "✘ failed to start portman tray"
    exit 1
  fi
}

cmd_stop() {
  if ! is_running; then
    echo "portman tray is not running"
    return 0
  fi
  local pid=\$(cat "\$PIDFILE" 2>/dev/null)
  [ -n "\$pid" ] && kill "\$pid" 2>/dev/null || pkill -f portman_tray.py 2>/dev/null
  rm -f "\$PIDFILE"
  echo "✔ portman tray stopped"
}

cmd_restart() { cmd_stop; sleep 0.5; cmd_start; }

# Open the window. A second copy hands the request to the one in the tray and exits.
cmd_show() {
  if ! is_running; then
    \$PYTHON "$INSTALL_DIR/portman_tray.py" --show > /dev/null 2>&1 &
    echo \$! > "\$PIDFILE"
    return 0
  fi
  \$PYTHON "$INSTALL_DIR/portman_tray.py" --show > /dev/null 2>&1
}

cmd_status() {
  if is_running; then
    echo "✔ portman tray is running (pid \$(cat \$PIDFILE 2>/dev/null || pgrep -f portman_tray.py))"
  else
    echo "○ portman tray is not running"
  fi
}

case "\${1:-start}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  show)    cmd_show ;;
  *) echo "Usage: portman-tray [start|stop|restart|status|show]"; exit 1 ;;
esac
TRAY_EOF
chmod +x "$BIN_DIR/portman-tray"
echo "  ✓ Tray launcher: portman-tray"

# ── Autostart ─────────────────────────────────────────────────────────────────

echo ""
echo "▸ Setting up autostart…"

mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=portman
Comment=Dev server port manager
Exec=$BIN_DIR/portman-tray start
Terminal=false
Categories=Development;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF
echo "  ✓ Starts automatically on login"

# ── App launcher entry ────────────────────────────────────────────────────────

mkdir -p "$APPS_DIR"
cat > "$APPS_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=portman
Comment=Dev server port manager
Exec=$BIN_DIR/portman-tray show
Terminal=false
Categories=Development;Utility;
Keywords=port;dev;server;
StartupNotify=false
EOF
echo "  ✓ App launcher entry created"

# ── Preserve existing registry ────────────────────────────────────────────────

if [ -f "$HOME/.portman.json" ]; then
    echo ""
    echo "  ✓ Existing project registry preserved (~/.portman.json)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✓ portman installed successfully!      ║"
echo "║                                          ║"
echo "║   CLI:   portman list                    ║"
echo "║   Window: portman ui                     ║"
echo "║   Tray:  portman tray start              ║"
echo "║   Stop:  portman tray stop               ║"
echo "║                                          ║"
echo "║   Tray starts automatically on login.    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Offer to start tray now
read -rp "Start portman tray now? [Y/n] " yn
case "${yn,,}" in
    n|no) echo "  Run 'portman tray start' whenever you're ready." ;;
    *) "$BIN_DIR/portman-tray" start ;;
esac
