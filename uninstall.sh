#!/usr/bin/env bash
set -euo pipefail

APP_ID="portman"
INSTALL_DIR="$HOME/.local/share/$APP_ID"
BIN_DIR="$HOME/.local/bin"

echo "╔══════════════════════════════════════════╗"
echo "║      portman — Uninstaller               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Stop running tray ─────────────────────────────────────────────────────────

echo "▸ Stopping portman tray…"
pkill -f portman_tray.py 2>/dev/null && echo "  ✓ Tray stopped" || echo "  ✓ Tray was not running"
rm -f /tmp/portman-tray.pid

# ── Remove files ──────────────────────────────────────────────────────────────

echo ""
echo "▸ Removing files…"

rm -rf "$INSTALL_DIR"
echo "  ✓ $INSTALL_DIR"

rm -f "$BIN_DIR/portman"
echo "  ✓ portman CLI"

rm -f "$BIN_DIR/portman-tray"
echo "  ✓ portman-tray"

rm -f "$HOME/.config/autostart/$APP_ID.desktop"
echo "  ✓ Autostart entry"

rm -f "$HOME/.local/share/applications/$APP_ID.desktop"
echo "  ✓ App launcher entry"

# ── Registry ──────────────────────────────────────────────────────────────────

echo ""
if [ -f "$HOME/.portman.json" ]; then
    read -rp "Remove project registry (~/.portman.json)? [y/N] " yn
    case "${yn,,}" in
        y|yes)
            rm -f "$HOME/.portman.json"
            echo "  ✓ Registry removed"
            ;;
        *)
            echo "  ✓ Registry preserved (~/.portman.json)"
            ;;
    esac
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "  ✓ portman uninstalled."
echo ""
