#!/usr/bin/env bash
# Setup-DVForge-Fedora.sh
# Sets up a native Linux build environment for DVForge on Fedora (Linux/Android).
# Automatically uses a Fedora 41 Toolbox container on Fedora 43+ to avoid GCC 16 C++ header mismatches,
# unless overridden with --no-toolbox or forced with --toolbox.
#
# Idempotent — safe to re-run.
#
# Usage:
#   ./Setup-DVForge-Fedora.sh --appimage       # Create Python venv and install packaging dependencies.
#   ./Setup-DVForge-Fedora.sh --toolbox        # Force running inside a Fedora 41 Toolbox container.
#   ./Setup-DVForge-Fedora.sh --no-toolbox     # Force running natively on the host system.
#   ./Setup-DVForge-Fedora.sh --skip-toolchains# Skip toolchain bootstrap.

set -e

# --- default configuration ---
INSTALL_APPIMAGE_DEPS=false
INSTALL_TOOLCHAINS=true

# Auto-detect if host Fedora version requires Toolbox (Fedora 43+ introduces GCC 16 issues)
AUTO_USE_TOOLBOX=false
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [ "$ID" = "fedora" ] && [ "${VERSION_ID:-0}" -ge 43 ]; then
        AUTO_USE_TOOLBOX=true
    fi
fi

FORCE_TOOLBOX="" # "true" or "false"

# --- argument parsing ---
show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --appimage          Create Python venv and install packaging dependencies (appimage-builder).
  --toolbox           Force execution inside a Fedora 41 Toolbox container.
  --no-toolbox        Force running natively on the host system (skip toolbox).
  --skip-toolchains   Skip toolchain bootstrap (Rust, Java, Android SDK/NDK, Flutter, LLVM, vcpkg).
  -h, --help          Show this help message.

Default behavior:
  Auto-detects Fedora version. Fedora 43+ defaults to using a Toolbox container to fix GCC 16 issues.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --appimage)
            INSTALL_APPIMAGE_DEPS=true
            shift
            ;;
        --toolbox)
            FORCE_TOOLBOX="true"
            shift
            ;;
        --no-toolbox)
            FORCE_TOOLBOX="false"
            shift
            ;;
        --skip-toolchains)
            INSTALL_TOOLCHAINS=false
            shift
            ;;
        -h|--help)
            show_help
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run with -h or --help for usage."
            exit 1
            ;;
    esac
done

# Determine final toolbox decision
USE_TOOLBOX="$AUTO_USE_TOOLBOX"
if [ "$FORCE_TOOLBOX" = "true" ]; then
    USE_TOOLBOX=true
elif [ "$FORCE_TOOLBOX" = "false" ]; then
    USE_TOOLBOX=false
fi

# --- Toolbox Isolation Check ---
if [ "$USE_TOOLBOX" = "true" ] && [ ! -f /run/.containerenv ] && [ ! -f /.dockerenv ]; then
    TOOLBOX_NAME="dvforge-f41"
    
    echo "=== Fedora version 43+ detected (or forced toolbox) ==="
    echo "Managing Toolbox container: ${TOOLBOX_NAME} (Fedora 41)..."

    if ! command -v toolbox >/dev/null 2>&1; then
        echo "Installing 'toolbox' on host Fedora..."
        sudo dnf install -y toolbox
    fi

    if ! toolbox list | grep -q "${TOOLBOX_NAME}"; then
        echo "Creating Fedora 41 toolbox container..."
        toolbox create --release f41 -y --container "${TOOLBOX_NAME}"
    fi

    echo "Entering Fedora 41 Toolbox to run setup and build..."
    exec toolbox run --container "${TOOLBOX_NAME}" bash "$0" "$@"
fi

# ====================================================================
# EXECUTION ENVIRONMENT (Host or Toolbox Container)
# ====================================================================

# --- helpers ---
log()  { echo -e "\n=== $1 ==="; }
ok()   { echo "  [OK] $1"; }
skip() { echo "  [SKIP] $1"; }
warn() { echo "  [WARN] $1"; }
fail() { echo "  [ERROR] $1"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
dir_exists() { [ -d "$1" ]; }

# --- sudo keepalive (ask once) ---
sudo -v
trap 'sudo -k' EXIT

# dnf update
log "Updating system packages"
sudo dnf check-update -y || true
sudo dnf upgrade -y
ok "Packages updated"

# Install build dependencies
log "Installing build dependencies"
DEPS=(
    gcc gcc-c++ make git python3 python3-pip python3-devel curl wget unzip zip tar
    perl perl-FindBin perl-IPC-Cmd glycin-loaders
    pkgconf-pkg-config openssl-devel sqlite-devel clang-devel llvm-devel
    cmake ninja-build file
    rpm-build ImageMagick bsdtar
    libzstd-devel
    # Multi-arch / 32-bit compatibility libraries (for Android SDK tools & builds)
    glibc.i686 libstdc++.i686 zlib-ng.i686
    # RustDesk Linux vcpkg + desktop packaging deps
    nasm yasm
    autoconf automake libtool
    pam-devel
    gtk3-devel libayatana-appindicator-gtk3-devel libxcb-devel libxdo-devel
    alsa-lib-devel pulseaudio-libs-devel gstreamer1-devel gstreamer1-plugins-base-devel
    libva-devel patchelf
    libffi-devel potrace
    fuse-libs
)

sudo mkdir -p /usr/include/glycin-2

# Optional 32-bit ncurses compat library
for ncurses_pkg in ncurses-compat-libs.i686 ncurses-libs.i686; do
    if dnf list --available "$ncurses_pkg" >/dev/null 2>&1 || rpm -q "$ncurses_pkg" >/dev/null 2>&1; then
        DEPS+=("$ncurses_pkg")
        break
    fi
done

NEEDED=()
for d in "${DEPS[@]}"; do
    if ! rpm -q "$d" >/dev/null 2>&1; then
        NEEDED+=("$d")
    fi
done

if [ ${#NEEDED[@]} -gt 0 ]; then
    sudo dnf install -y "${NEEDED[@]}"
    ok "Build dependencies installed"
else
    skip "All build dependencies already installed"
fi

# Fedora TLS CA certificate compatibility
log "Checking TLS CA certificate compatibility"

CA_SOURCE="/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
CA_LINK="/etc/pki/tls/certs/ca-bundle.crt"

if [ ! -f "${CA_SOURCE}" ]; then
    warn "Fedora CA bundle not found at ${CA_SOURCE}"
else
    CA_NEEDS_FIX=false

    if [ ! -e "${CA_LINK}" ] && [ ! -L "${CA_LINK}" ]; then
        CA_NEEDS_FIX=true
    elif [ ! -L "${CA_LINK}" ]; then
        warn "${CA_LINK} exists but is not a symlink; leaving it unchanged"
    elif [ "$(readlink -f "${CA_LINK}")" != "${CA_SOURCE}" ]; then
        CA_NEEDS_FIX=true
    fi

    if [ "${CA_NEEDS_FIX}" = true ]; then
        log "Creating Fedora CA bundle compatibility symlink"
        sudo mkdir -p "$(dirname "${CA_LINK}")"
        sudo ln -sfn "${CA_SOURCE}" "${CA_LINK}"
        ok "TLS CA bundle compatibility link created"
    else
        ok "TLS CA bundle compatibility link already configured"
    fi
fi

# Sanity checks
if have rpmbuild; then ok "rpmbuild present"; else warn "rpmbuild missing after dnf install"; fi
if have magick || have convert; then ok "ImageMagick present"; else warn "ImageMagick missing after dnf install"; fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

# Optional Virtual Environment & AppImage Packaging Setup
if [ "$INSTALL_APPIMAGE_DEPS" = true ]; then
    log "Setting up Python virtual environment & AppImage dependencies (--appimage enabled)"

    EXTRA_PKG=()
    for p in dpkg apt gnupg2; do
        if ! rpm -q "$p" >/dev/null 2>&1; then
            EXTRA_PKG+=("$p")
        fi
    done
    if [ ${#EXTRA_PKG[@]} -gt 0 ]; then
        sudo dnf install -y "${EXTRA_PKG[@]}" || true
    fi

    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating virtual environment at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
    else
        log "Virtual environment already exists at ${VENV_DIR}."
    fi

    log "Installing Python build packages..."
    "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
    "${VENV_DIR}/bin/pip" install "setuptools_scm<10"
    "${VENV_DIR}/bin/pip" install "git+https://github.com/rustdesk-org/appimage-builder.git"

    if ! command -v apt-key >/dev/null 2>&1; then
        log "'apt-key' not found on \$PATH. Creating compatibility shim at /usr/local/bin/apt-key..."

        sudo mkdir -p /etc/apt/trusted.gpg.d
        sudo tee /usr/local/bin/apt-key >/dev/null <<'EOF'
#!/bin/sh
case "$1" in
  add)
    shift
    if [ "$1" = "-" ] || [ -z "$1" ]; then
      gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/appimage-builder-shim.gpg >/dev/null
    else
      gpg --dearmor < "$1" | sudo tee "/etc/apt/trusted.gpg.d/$(basename "$1").gpg" >/dev/null
    fi
    ;;
  list|fingerprint)
    gpg --no-default-keyring --keyring /etc/apt/trusted.gpg.d/*.gpg --list-keys 2>/dev/null
    ;;
  *)
    exit 0
    ;;
esac
exit 0
EOF

        sudo chmod +x /usr/local/bin/apt-key
        log "apt-key shim created successfully."
    else
        log "'apt-key' already exists at $(command -v apt-key)."
    fi

    if [ -d "${PROJECT_DIR}/.git" ]; then
        EXCLUDE_FILE="${PROJECT_DIR}/.git/info/exclude"
        if ! grep -qs "^.venv/" "${EXCLUDE_FILE}" 2>/dev/null; then
            echo ".venv/" >> "${EXCLUDE_FILE}"
        fi
    fi
    ok "Virtual environment & AppImage tools configured"
else
    skip "Virtual environment and pip packages (--appimage omitted)"
fi

# Optional Toolchains Bootstrap
if [ "$INSTALL_TOOLCHAINS" = true ]; then
    log "Bootstrapping toolchains"

    if [ "$INSTALL_APPIMAGE_DEPS" = true ] && [ -x "${VENV_DIR}/bin/python3" ]; then
        PY_EXEC="${VENV_DIR}/bin/python3"
    else
        PY_EXEC="python3"
    fi

    TOOLCHAINS_PY=""
    for cand in "${PROJECT_DIR}/toolchains.py" "${PROJECT_DIR}/builder/toolchains.py"; do
        if [ -f "$cand" ]; then
            TOOLCHAINS_PY="$cand"
            break
        fi
    done

    export PATH="${HOME}/.cargo/bin:${PATH}"

    if [ -n "${TOOLCHAINS_PY}" ]; then
        log "Running toolchains.py via ${PY_EXEC}"
        "$PY_EXEC" "${TOOLCHAINS_PY}" \
            rust java android_sdk android_ndk flutter llvm vcpkg sccache
        ok "Toolchains installed and env.json generated"
    else
        warn "Could not locate toolchains.py; skipping automated SDK downloads."
    fi

    if ! command -v sccache >/dev/null 2>&1 && [ ! -f "${HOME}/.cargo/bin/sccache" ]; then
        log "Installing sccache 0.11.0 directly via cargo..."
        cargo install sccache --version 0.11.0 --locked
        ok "sccache installed"
    else
        ok "sccache is present"
    fi

    if have rustup || [ -x "${HOME}/.cargo/bin/rustup" ]; then
        rustup toolchain install 1.75 --profile minimal || true
        rustup default 1.75 || true
        rustup component add rustfmt --toolchain 1.75 || true
        ok "Rust 1.75 toolchain and rustfmt configured"
    fi

    NDK_DIR="${PROJECT_DIR}/.toolchains/android_ndk"
    if [ -d "${NDK_DIR}" ]; then
        log "Fixing Android NDK binary permissions..."
        find "${NDK_DIR}" \
            -type f \
            -path "*/toolchains/llvm/prebuilt/*/bin/*" \
            -exec chmod +x {} +
        find "${NDK_DIR}" \
            -maxdepth 3 \
            -type f \
            \( -name "ndk-build" -o -name "*.sh" \) \
            -exec chmod +x {} +
        ok "Android NDK permissions fixed"
    fi
else
    skip "Toolchains installation (--skip-toolchains passed)"
fi

log ""
log "Setup complete!"

TOOLBOX_NAME="dvforge-f41"

if [ "$USE_TOOLBOX" = "true" ]; then
    log "Environment running inside Fedora Toolbox container (${TOOLBOX_NAME})."
    log "To enter your development container manually later:"
    log "  toolbox enter ${TOOLBOX_NAME}"
    log ""
    if [ "$INSTALL_APPIMAGE_DEPS" = true ]; then
        log "To run the application inside the toolbox:"
        log "  toolbox enter ${TOOLBOX_NAME} bash -c 'cd $(pwd) && source .venv/bin/activate && ./run.sh'"
    else
        log "To run the application inside the toolbox:"
        log "  toolbox enter ${TOOLBOX_NAME} bash -c 'cd $(pwd) && ./run.sh'"
    fi
else
    if [ "$INSTALL_APPIMAGE_DEPS" = true ]; then
        log "To run the application with your venv:"
        log "  source .venv/bin/activate && ./run.sh"
    else
        log "To run the application:"
        log "  ./run.sh"
    fi
fi