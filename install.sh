#!/usr/bin/env bash
# Amethyst installer.
#
# Installs the app into ~/.local/share/amethyst, creates a virtual environment,
# installs Python dependencies and adds the `Amethyst` command to ~/.local/bin.
#
# Usage:
#   ./install.sh                     install / update
#   ./install.sh --uninstall         remove the app (keep account data)
#   ./install.sh --uninstall --purge remove the app and account data
set -euo pipefail

APP_NAME="Amethyst"
PKG_NAME="amethyst"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PY="$SCRIPT_DIR/amethyst.py"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
INSTALL_DIR="${AMETHYST_INSTALL_DIR:-$DATA_HOME/$PKG_NAME}"
BIN_DIR="${AMETHYST_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="${AMETHYST_DATA_DIR:-$CONFIG_HOME/$PKG_NAME}"
LAUNCHER="$BIN_DIR/$APP_NAME"
LOWER_LAUNCHER="$BIN_DIR/$PKG_NAME"
PURGE=0
ACTION="install"

usage() {
    cat <<EOF
Amethyst installer

  ./install.sh                 install or update
  ./install.sh --uninstall     remove the app, keep account data
  ./install.sh --uninstall --purge
                               remove the app and account data

Environment variables:
  AMETHYST_INSTALL_DIR  where to install the app (default: ~/.local/share/amethyst)
  AMETHYST_BIN_DIR      where to put the command (default: ~/.local/bin)
  AMETHYST_DATA_DIR     where user data lives (default: ~/.config/amethyst)
EOF
}

for arg in "$@"; do
    case "$arg" in
        --uninstall|--remove) ACTION="uninstall" ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[!] Unknown argument: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

uninstall() {
    echo "[*] Removing Amethyst..."
    rm -f "$LAUNCHER"
    if [ -L "$LOWER_LAUNCHER" ]; then
        rm -f "$LOWER_LAUNCHER"
    fi
    case "$INSTALL_DIR" in
        ""|"/"|"$HOME") echo "[!] Unsafe install path: $INSTALL_DIR" >&2; exit 1 ;;
    esac
    rm -rf -- "$INSTALL_DIR"
    if [ "$PURGE" -eq 1 ]; then
        case "$CONFIG_DIR" in
            ""|"/"|"$HOME") echo "[!] Unsafe data path: $CONFIG_DIR" >&2; exit 1 ;;
        esac
        rm -rf -- "$CONFIG_DIR"
        echo "[+] App and account data removed."
    else
        echo "[+] App removed. Account data kept in $CONFIG_DIR"
        echo "    To remove it too: ./install.sh --uninstall --purge"
    fi
}

if [ "$(id -u)" -eq 0 ]; then
    echo "[!] Do not run the installer with sudo/root." >&2
    echo "    Amethyst installs into the current user's home directory." >&2
    exit 1
fi

if [ "$ACTION" = "uninstall" ]; then
    uninstall
    exit 0
fi

if [ ! -f "$SOURCE_PY" ] || [ ! -f "$REQ_FILE" ]; then
    echo "[!] amethyst.py or requirements.txt not found next to install.sh" >&2
    exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[!] Python 3 not found. Install python3 and try again." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "[!] Python 3.10 or newer is required." >&2
    exit 1
fi

if ! command -v mpv >/dev/null 2>&1; then
    echo "[!] mpv not found. Install it:"
    echo "    Arch:   sudo pacman -S mpv"
    echo "    Debian: sudo apt install mpv"
    echo "    Fedora: sudo dnf install mpv"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[!] ffmpeg not found — it may be needed for some audio streams:"
    echo "    Arch:   sudo pacman -S ffmpeg"
    echo "    Debian: sudo apt install ffmpeg"
fi

mkdir -p -- "$INSTALL_DIR" "$BIN_DIR"
install -m 0644 -- "$SOURCE_PY" "$INSTALL_DIR/amethyst.py"
install -m 0644 -- "$REQ_FILE" "$INSTALL_DIR/requirements.txt"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "[*] Creating a virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

echo "[*] Installing Python dependencies..."
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check \
    --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check \
    -r "$INSTALL_DIR/requirements.txt"

if ! "$VENV_DIR/bin/python" -c 'import ytmusicapi, yt_dlp, requests, PIL' 2>/dev/null; then
    echo "[!] Dependency check failed. Try reinstalling." >&2
    exit 1
fi

if [ -e "$LAUNCHER" ] && ! grep -qF "$INSTALL_DIR/amethyst.py" "$LAUNCHER" 2>/dev/null; then
    echo "[!] $LAUNCHER already exists and does not belong to Amethyst." >&2
    echo "    Remove it manually or set AMETHYST_BIN_DIR." >&2
    exit 1
fi

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" "$INSTALL_DIR/amethyst.py" "\$@"
EOF
chmod 0755 -- "$LAUNCHER"

# On case-insensitive filesystems this path already exists as Amethyst.
if [ ! -e "$LOWER_LAUNCHER" ]; then
    ln -s -- "$APP_NAME" "$LOWER_LAUNCHER"
fi

echo
echo "[+] Amethyst installed."
echo "    App:     $INSTALL_DIR"
echo "    Data:    $CONFIG_DIR"
echo "    Command: $LAUNCHER"

case ":$PATH:" in
    *":$BIN_DIR:"*)
        echo
        echo "Run: Amethyst"
        ;;
    *)
        echo
        echo "[!] $BIN_DIR is not in PATH."
        echo "    Add this line to ~/.bashrc or ~/.zshrc:"
        echo "      export PATH=\"$BIN_DIR:\$PATH\""
        echo "    Then restart the terminal or run: source ~/.bashrc"
        ;;
esac

# If the installer runs in an interactive terminal, offer to start right away.
if [ -t 0 ] && [ -t 1 ]; then
    printf "Start Amethyst now? [y/N] "
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES) exec "$LAUNCHER" ;;
    esac
fi
