#!/usr/bin/env bash
# install.sh - Install kalico-flash as 'kflash' command
#
# Usage:
#   ./install.sh             Install kflash to ~/.local/bin
#   ./install.sh --yes       Auto-accept installer prompts
#   ./install.sh --uninstall Remove kflash symlink

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
COMMAND_NAME="kflash"
TARGET="${SCRIPT_DIR}/kflash.py"
AUTO_YES=0
DO_UNINSTALL=0

# Color support
if [[ -t 1 ]] && command -v tput &>/dev/null && [[ $(tput colors 2>/dev/null || echo 0) -ge 8 ]]; then
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    RESET=$(tput sgr0)
else
    GREEN=""
    YELLOW=""
    RESET=""
fi

success() { echo "${GREEN}$1${RESET}"; }
warn() { echo "${YELLOW}$1${RESET}"; }

prompt_yes_no() {
    local prompt="$1"
    local reply=""

    if [[ "${AUTO_YES}" -eq 1 ]]; then
        echo "${prompt}y (auto)"
        return 0
    fi

    if [[ -t 0 ]]; then
        read -p "${prompt}" -n 1 -r reply || true
        echo
    elif [[ -r /dev/tty ]]; then
        read -p "${prompt}" -n 1 -r reply < /dev/tty || true
        echo
    else
        return 1
    fi

    [[ "${reply}" =~ ^[Yy]$ ]]
}

_path_profile_candidates() {
    local shell_name="${SHELL##*/}"
    case "${shell_name}" in
        zsh)
            printf '%s\n' "${HOME}/.zshrc" "${HOME}/.zprofile" "${HOME}/.profile"
            ;;
        bash)
            printf '%s\n' "${HOME}/.bashrc" "${HOME}/.bash_profile" "${HOME}/.profile"
            ;;
        *)
            printf '%s\n' "${HOME}/.profile" "${HOME}/.bashrc" "${HOME}/.zshrc"
            ;;
    esac
}

_pick_path_rc_file() {
    local file
    for file in "$@"; do
        if [[ -f "${file}" ]]; then
            echo "${file}"
            return
        fi
    done
    echo "$1"
}

_path_line_exists_in_files() {
    local path_line="$1"
    shift
    local file
    for file in "$@"; do
        if [[ -f "${file}" ]] && grep -qF "${path_line}" "${file}" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

# Enable ccache in registry (best-effort)
enable_ccache_setting() {
    local registry_path="${XDG_CONFIG_HOME:-$HOME/.config}/kalico-flash/devices.json"

    if ! command -v python3 >/dev/null 2>&1; then
        warn "Warning: python3 not found; unable to enable ccache setting automatically"
        return
    fi

    if ! REGISTRY_PATH="${registry_path}" python3 - <<'PY'
import json
import os
from pathlib import Path

path = os.environ.get("REGISTRY_PATH")
if not path:
    raise SystemExit(0)

data = {}
try:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
except Exception:
    data = {}

data.setdefault("global", {})
data["global"]["use_ccache"] = True
data["global"]["ccache_install_declined"] = False
data.setdefault("devices", {})
data.setdefault("blocked_devices", [])

Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
    then
        warn "Warning: failed to update ccache setting in registry"
    fi
}

# Parse args
for arg in "$@"; do
    case "${arg}" in
        --yes|-y)
            AUTO_YES=1
            ;;
        --uninstall)
            DO_UNINSTALL=1
            ;;
        --help|-h)
            cat <<'USAGE'
Usage: ./install.sh [--yes|-y] [--uninstall]
  --yes, -y     Auto-accept installer prompts
  --uninstall   Remove kflash symlink
USAGE
            exit 0
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            echo "Use --help for usage." >&2
            exit 1
            ;;
    esac
done

# Handle --uninstall
if [[ "${DO_UNINSTALL}" -eq 1 ]]; then
    rm -f "${BIN_DIR}/${COMMAND_NAME}"
    success "Removed ${COMMAND_NAME}"
    echo "Config data preserved at: ${XDG_CONFIG_HOME:-$HOME/.config}/kalico-flash/"
    echo "To remove all data: rm -rf \"${XDG_CONFIG_HOME:-$HOME/.config}/kalico-flash/\""
    exit 0
fi

# Prerequisite checks (warn only, don't fail)

# Python 3.9+ check
if ! python3 -c "import sys; exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
    warn "Warning: Python 3.9+ recommended (current version may be older)"
fi

# Kalico directory check
if [[ ! -d "${HOME}/klipper" ]]; then
    warn "Warning: ~/klipper not found - install Kalico before using kflash"
fi
if ! command -v arm-none-eabi-gcc >/dev/null 2>&1; then
    warn "Warning: arm-none-eabi-gcc not found - install with: sudo apt install gcc-arm-none-eabi"
fi

# Serial access check (dialout group)
if ! groups 2>/dev/null | grep -q dialout; then
    warn "Warning: User not in 'dialout' group - may need: sudo usermod -aG dialout \$USER"
fi
if command -v sudo >/dev/null 2>&1; then
    if ! sudo -n true 2>/dev/null; then
        warn "Note: passwordless sudo not detected."
        warn "  kflash will prompt for your password when needed for service control."
    fi
else
    warn "Warning: sudo not found - klipper service control will fail"
fi

# Optional ccache install prompt
if ! command -v ccache >/dev/null 2>&1; then
    if prompt_yes_no "ccache not found. Install for faster builds? [y/N] "; then
        if command -v apt >/dev/null 2>&1; then
            if sudo -n true 2>/dev/null; then
                if sudo apt install -y ccache; then
                    success "ccache installed"
                    enable_ccache_setting
                else
                    warn "Warning: ccache install failed; continuing without ccache"
                fi
            else
                warn "Warning: passwordless sudo unavailable; install ccache manually:"
                warn "  sudo apt install ccache"
            fi
        else
            warn "Warning: apt not found; install ccache manually if desired"
        fi
    fi
fi

# Installation

# Create bin directory (idempotent)
mkdir -p "${BIN_DIR}"

# Make kflash.py executable
chmod +x "${TARGET}"

# Create symlink (idempotent with -sfn)
ln -sfn "${TARGET}" "${BIN_DIR}/${COMMAND_NAME}"

# PATH check and offer to fix
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
    warn "Warning: ${BIN_DIR} is not in your PATH"
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    mapfile -t PROFILE_FILES < <(_path_profile_candidates)

    if _path_line_exists_in_files "${PATH_LINE}" "${PROFILE_FILES[@]}"; then
        success "PATH export already present in shell profile"
    else
        RC_FILE="$(_pick_path_rc_file "${PROFILE_FILES[@]}")"
        if prompt_yes_no "Add to ${RC_FILE}? [y/N] "; then
            echo "" >> "${RC_FILE}"
            echo "# Added by kalico-flash installer" >> "${RC_FILE}"
            echo "${PATH_LINE}" >> "${RC_FILE}"
            success "Added to ${RC_FILE}"
            warn "Run 'source ${RC_FILE}' or open a new terminal"
        else
            warn "Skipped. Add manually: export PATH=\"\$HOME/.local/bin:\$PATH\""
        fi
    fi
fi

# Success message
success "Installed ${COMMAND_NAME} -> ${TARGET}"
echo "Run 'kflash' to start"
