#!/usr/bin/env bash
#
# EMEC-AMS Raspberry Pi provisioning. Safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/zacjcsu/emec-ams-client/main/emecamssetup.sh | bash
#
# Unattended: pass MACHINE_ID, MACHINE_NAME, MACHINE_TYPE, DB_* and --yes.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_USER="${APP_USER:-emec}"
APP_DIR="${APP_DIR:-/home/${APP_USER}/emec-ams}"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="emec-ams"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# "git" (SOURCE_REPO/SOURCE_BRANCH) or "drive" (SOURCE_DRIVE_ID).
SOURCE_MODE="${SOURCE_MODE:-git}"

# Blank on purpose: the zip contains a .env, so the id is a pointer to secrets.
SOURCE_DRIVE_ID="${SOURCE_DRIVE_ID:-}"

SOURCE_REPO="${SOURCE_REPO:-https://github.com/zacjcsu/emec-ams-client.git}"
SOURCE_BRANCH="${SOURCE_BRANCH:-main}"

# Locale / regional settings applied non-interactively (replaces raspi-config).
TIMEZONE="${TIMEZONE:-America/Denver}"
KEYBOARD_LAYOUT="${KEYBOARD_LAYOUT:-us}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"

# Written to ${APP_DIR}/.env at mode 600. Prompted for, or passed as env vars.
# This file is public, so the host, user and password have no default.
# Every Pi shares the ams_pi password. If a Pi is lost, change it on the server, then on each Pi set DB_PASS
# in ${APP_DIR}/.env and restart emec-ams.
DEF_DB_PORT="5432"
DEF_DB_NAME="emec_access"

DB_HOST="${DB_HOST:-}"
DB_PORT="${DB_PORT:-}"
DB_USER="${DB_USER:-}"
DB_PASS="${DB_PASS:-}"
DB_NAME="${DB_NAME:-}"

ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'
    C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
else
    C_RESET=""; C_BOLD=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi

step() { printf '\n%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s[ok]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '    %s[warn]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '\n%s[error]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# Read prompts from the tty, not stdin, so `curl | bash` still works.
# /dev/tty can exist but not open, so test opening it rather than -r.
TTY=""
if [[ -e /dev/tty ]] && ( : </dev/tty ) 2>/dev/null; then
    TTY=/dev/tty
elif [[ -t 0 ]]; then
    TTY=/dev/stdin
fi
CAN_PROMPT=0
[[ -n "$TTY" ]] && CAN_PROMPT=1

no_prompt_die() {
    die "Cannot prompt for '$1': no terminal available.
      Run from an interactive shell, or re-run non-interactively with --yes and
      supply the values as environment variables, e.g.
        MACHINE_ID=lathe-001 MACHINE_NAME='Manual Lathe 1' \\
        MACHINE_TYPE='Manual Lathe' DB_PASS='...' bash -s -- --yes"
}

ask() {
    # ask <variable-name> <prompt> <default>
    local __var="$1" __prompt="$2" __default="$3" __reply=""
    if (( ASSUME_YES )); then
        printf -v "$__var" '%s' "$__default"
        info "${__prompt}: ${__default}"
        return
    fi
    (( CAN_PROMPT )) || no_prompt_die "$__prompt"
    read -r -p "    ${__prompt} [${__default}]: " __reply <"$TTY" || true
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

ask_required() {
    # ask_required <variable-name> <prompt>   (no default, must not be empty)
    local __var="$1" __prompt="$2" __reply="" __try=0
    if (( ASSUME_YES )); then
        die "${__prompt} is required. With --yes, pass it as an environment variable: ${__var}=..."
    fi
    (( CAN_PROMPT )) || no_prompt_die "$__prompt"
    while (( __try < 3 )); do
        read -r -p "    ${__prompt}: " __reply <"$TTY" || true
        if [[ -n "$__reply" ]]; then
            printf -v "$__var" '%s' "$__reply"
            return
        fi
        warn "Cannot be empty."
        __try=$(( __try + 1 ))
    done
    die "Too many failed attempts."
}

ask_secret() {
    # ask_secret <variable-name> <prompt>   (no echo, asks twice to catch typos)
    local __var="$1" __prompt="$2" __a="" __b="" __try=0
    if (( ASSUME_YES )); then
        die "${__prompt} is required. With --yes, pass it as an environment variable: ${__var}=..."
    fi
    (( CAN_PROMPT )) || no_prompt_die "$__prompt"
    while (( __try < 3 )); do
        read -rs -p "    ${__prompt}: " __a <"$TTY" || true; printf '\n'
        read -rs -p "    ${__prompt} (again): " __b <"$TTY" || true; printf '\n'
        if [[ -z "$__a" ]]; then
            warn "Cannot be empty."
        elif [[ "$__a" != "$__b" ]]; then
            warn "Entries did not match."
        else
            printf -v "$__var" '%s' "$__a"
            return
        fi
        __try=$(( __try + 1 ))
    done
    die "Too many failed attempts."
}

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------

step "Preflight checks"

[[ "$(id -u)" -eq 0 ]] && die "Run this as the '${APP_USER}' user, not as root. It will call sudo where needed."

if [[ "$(id -un)" != "$APP_USER" ]]; then
    warn "Running as '$(id -un)' but installing for '${APP_USER}'."
    warn "The service runs as '${APP_USER}', so file ownership will be fixed at the end."
fi

id "$APP_USER" >/dev/null 2>&1 || die "User '${APP_USER}' does not exist. Create it first: sudo adduser ${APP_USER}"

command -v sudo >/dev/null || die "sudo is not installed."
command -v systemctl >/dev/null || die "systemd not found. This script targets Raspberry Pi OS."

PI_MODEL="unknown"
if [[ -r /proc/device-tree/model ]]; then
    PI_MODEL="$(tr -d '\0' </proc/device-tree/model)"
fi
info "Device: ${PI_MODEL}"

IS_PI5=0
[[ "$PI_MODEL" == *"Raspberry Pi 5"* ]] && IS_PI5=1
(( IS_PI5 )) && info "Pi 5 detected: will substitute rpi-lgpio for RPi.GPIO."

step "Requesting sudo access up front"
if ! sudo -n true 2>/dev/null; then
    (( CAN_PROMPT )) || die "sudo needs a password but there is no terminal to ask on. Run from an interactive shell, or configure passwordless sudo for ${APP_USER}."
fi
sudo -v || die "sudo authentication failed."
# Keep the sudo timestamp alive for the length of the run.
( while true; do sudo -n true 2>/dev/null || exit; sleep 50; done ) &
SUDO_KEEPALIVE=$!
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT
ok "sudo ready"

# ---------------------------------------------------------------------------
# 1. Machine identity
# ---------------------------------------------------------------------------

step "Machine identity (written to config/config.json)"

DEF_ID="lathe-001"; DEF_NAME="Manual Lathe 1"; DEF_TYPE="Manual Lathe"
if [[ -f "${APP_DIR}/config/config.json" ]] && command -v python3 >/dev/null; then
    # Re-run: offer the values already on this Pi as the defaults.
    eval "$(python3 - "${APP_DIR}/config/config.json" <<'PY' 2>/dev/null || true
import json, shlex, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for k, v in (("DEF_ID", "machine_id"), ("DEF_NAME", "machine_name"), ("DEF_TYPE", "machine_type")):
    if d.get(v):
        print(f"{k}={shlex.quote(str(d[v]))}")
PY
)"
    info "Found an existing config on this Pi; its values are the defaults below."
fi

MACHINE_ID="${MACHINE_ID:-}"; MACHINE_NAME="${MACHINE_NAME:-}"; MACHINE_TYPE="${MACHINE_TYPE:-}"
[[ -z "$MACHINE_ID"   ]] && ask MACHINE_ID   "Machine ID"   "$DEF_ID"
[[ -z "$MACHINE_NAME" ]] && ask MACHINE_NAME "Machine name" "$DEF_NAME"
[[ -z "$MACHINE_TYPE" ]] && ask MACHINE_TYPE "Machine type" "$DEF_TYPE"

[[ -n "$MACHINE_ID" ]] || die "Machine ID cannot be empty."
ok "${MACHINE_ID} / ${MACHINE_NAME} / ${MACHINE_TYPE}"

# ---------------------------------------------------------------------------
# 1b. Database credentials
# ---------------------------------------------------------------------------

step "Database credentials (written to .env)"

ENV_FILE="${APP_DIR}/.env"
KEEP_ENV=0

if [[ -f "$ENV_FILE" ]] && [[ -z "$DB_PASS" ]]; then
    KEEP_ENV=1
    ok "Existing .env found; keeping it."
    info "To replace it, re-run with DB_PASS=... or delete ${ENV_FILE} first."
else
    [[ -z "$DB_HOST" ]] && ask_required DB_HOST "PostgreSQL host (dashboard VM address)"
    [[ -z "$DB_PORT" ]] && ask DB_PORT "PostgreSQL port" "$DEF_DB_PORT"
    [[ -z "$DB_USER" ]] && ask_required DB_USER "PostgreSQL user"
    [[ -z "$DB_NAME" ]] && ask DB_NAME "Database name" "$DEF_DB_NAME"
    [[ -z "$DB_PASS" ]] && ask_secret DB_PASS "PostgreSQL password"
    ok "Credentials collected for ${DB_USER}@${DB_HOST}"
fi

# ---------------------------------------------------------------------------
# 2. Regional settings (the raspi-config steps, done non-interactively)
# ---------------------------------------------------------------------------

step "Regional settings"

if sudo timedatectl set-timezone "$TIMEZONE" 2>/dev/null; then
    ok "Timezone: ${TIMEZONE}"
else
    warn "Could not set timezone to ${TIMEZONE}."
fi

if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_configure_keyboard "$KEYBOARD_LAYOUT" >/dev/null 2>&1 \
        && ok "Keyboard layout: ${KEYBOARD_LAYOUT}" \
        || warn "Could not set keyboard layout."
    sudo raspi-config nonint do_wifi_country "$WIFI_COUNTRY" >/dev/null 2>&1 \
        && ok "Wi-Fi country: ${WIFI_COUNTRY}" \
        || warn "Could not set Wi-Fi country (harmless on wired-only units)."
else
    warn "raspi-config not found; skipping keyboard and Wi-Fi country."
fi

# ---------------------------------------------------------------------------
# 3. Hardware interfaces
# ---------------------------------------------------------------------------

step "Enabling SPI (RFID reader) and I2C (LCD)"

REBOOT_NEEDED=0
if command -v raspi-config >/dev/null; then
    # raspi-config nonint uses 0 = enable.
    sudo raspi-config nonint do_spi 0 && ok "SPI enabled" || warn "Could not enable SPI."
    sudo raspi-config nonint do_i2c 0 && ok "I2C enabled" || warn "Could not enable I2C."
else
    CONFIG_TXT=/boot/firmware/config.txt
    [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt
    for param in spi i2c_arm; do
        if ! grep -qE "^dtparam=${param}=on" "$CONFIG_TXT" 2>/dev/null; then
            echo "dtparam=${param}=on" | sudo tee -a "$CONFIG_TXT" >/dev/null
            ok "Added dtparam=${param}=on to ${CONFIG_TXT}"
            REBOOT_NEEDED=1
        fi
    done
fi

[[ -e /dev/spidev0.0 ]] || REBOOT_NEEDED=1
[[ -e /dev/i2c-1 ]]     || REBOOT_NEEDED=1

HW_GROUPS=""
for g in spi i2c gpio dialout; do
    getent group "$g" >/dev/null && HW_GROUPS="${HW_GROUPS:+${HW_GROUPS},}${g}"
done
if [[ -n "$HW_GROUPS" ]]; then
    sudo usermod -aG "$HW_GROUPS" "$APP_USER" \
        && ok "Added ${APP_USER} to: ${HW_GROUPS}" \
        || warn "Could not add ${APP_USER} to hardware groups."
fi

# ---------------------------------------------------------------------------
# 4. System packages
# ---------------------------------------------------------------------------

step "Installing system packages"


export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git curl unzip rsync ca-certificates \
    python3 python3-venv python3-dev python3-pip \
    python3-rpi-lgpio python3-spidev \
    build-essential i2c-tools >/dev/null
ok "Base packages installed"

# ---------------------------------------------------------------------------
# 5. Fetch the application code
# ---------------------------------------------------------------------------

step "Fetching application code (source: ${SOURCE_MODE})"

sudo mkdir -p "$APP_DIR"
sudo chown "${APP_USER}:${APP_USER}" "$APP_DIR"

STAGE="$(mktemp -d)"
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true; rm -rf "$STAGE"' EXIT

# In place, leaving a real repo. reset --hard ignores untracked files, so
# .env, config.json, .venv/, logs/ and data/ survive.
fetch_from_git() {
    if [[ ! -d "${APP_DIR}/.git" ]]; then
        info "Initialising a git repo in ${APP_DIR}..."
        git -C "$APP_DIR" init -q -b "$SOURCE_BRANCH"
    fi
    if git -C "$APP_DIR" remote get-url origin >/dev/null 2>&1; then
        git -C "$APP_DIR" remote set-url origin "$SOURCE_REPO"
    else
        git -C "$APP_DIR" remote add origin "$SOURCE_REPO"
    fi

    info "Fetching ${SOURCE_REPO} (branch ${SOURCE_BRANCH})..."
    git -C "$APP_DIR" fetch --quiet origin "$SOURCE_BRANCH" || die "Fetch failed."
    git -C "$APP_DIR" reset --hard --quiet FETCH_HEAD || die "Checkout failed."
    # So a plain `git pull` / `git status` works when someone SSHes in.
    git -C "$APP_DIR" branch --set-upstream-to="origin/${SOURCE_BRANCH}" \
        "$SOURCE_BRANCH" >/dev/null 2>&1 || true
    ok "At $(git -C "$APP_DIR" log -1 --pretty='%h %s')"

    # hardware/ is KiCad and gerbers. skip-worktree stops reset restoring it.
    if [[ -d "${APP_DIR}/hardware" ]]; then
        git -C "$APP_DIR" ls-files -z hardware \
            | xargs -0 -r git -C "$APP_DIR" update-index --skip-worktree 2>/dev/null || true
        rm -rf "${APP_DIR}/hardware"
        ok "Pruned hardware/ (not needed at runtime)"
    fi
}

# Fallback. A zip has no history, so this stages and rsyncs instead.
fetch_from_drive() {
    [[ -n "$SOURCE_DRIVE_ID" ]] \
        || die "SOURCE_DRIVE_ID is not set. Pass SOURCE_DRIVE_ID=<id>."

    local zip="${STAGE}/emec-ams.zip"
    info "Downloading zip from Google Drive..."
    curl -fsSL --retry 3 \
        "https://drive.usercontent.google.com/download?id=${SOURCE_DRIVE_ID}&export=download&confirm=t" \
        -o "$zip" || die "Download failed. Check the file ID and that sharing is 'Anyone with the link'."

    # A permission error comes back as HTML, not a zip.
    unzip -tq "$zip" >/dev/null 2>&1 \
        || die "Downloaded file is not a valid zip (Drive probably returned an error page)."

    unzip -q -o "$zip" -d "${STAGE}/unpacked"

    local main_py src_root
    main_py="$(find "${STAGE}/unpacked" -name main.py -not -path '*/.git/*' -print -quit)"
    [[ -n "$main_py" ]] || die "Could not find main.py in the zip."
    src_root="$(dirname "$main_py")"
    info "Source root: ${src_root}"

    rsync -a --delete \
        --exclude '.venv/' --exclude 'logs/' --exclude 'data/' \
        --exclude '.env' --exclude '.gitignore' --exclude 'config/config.json' \
        --exclude '.git/' --exclude 'hardware/' \
        --exclude '__pycache__/' --exclude '*.pyc' \
        "${src_root}/" "${APP_DIR}/" \
        || die "Failed to copy the code into ${APP_DIR}."
    ok "Code in place (no git repo: Drive zips have no history)"
    warn "emecamsupdate.sh will convert this to a real repo on its first run."
}

step "Installing code into ${APP_DIR}"
case "$SOURCE_MODE" in
    git)   fetch_from_git ;;
    drive) fetch_from_drive ;;
    *)     die "SOURCE_MODE must be 'git' or 'drive', got '${SOURCE_MODE}'." ;;
esac

mkdir -p "${APP_DIR}/logs" "${APP_DIR}/data" "${APP_DIR}/config"
touch "${APP_DIR}/logs/errors.log" "${APP_DIR}/logs/sync.log"

# ---------------------------------------------------------------------------
# 6. Config files
# ---------------------------------------------------------------------------

step "Writing configuration"

if (( KEEP_ENV )); then
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    ok ".env left untouched (mode 600)"
else
    umask 077
    cat >"$ENV_FILE" <<EOF
DB_HOST=${DB_HOST}
DB_PORT=${DB_PORT}
DB_USER=${DB_USER}
DB_PASS=${DB_PASS}
DB_NAME=${DB_NAME}
EOF
    umask 022
    chmod 600 "$ENV_FILE"
    ok ".env written (mode 600)"
fi

cat >"${APP_DIR}/config/config.json" <<EOF
{
  "machine_id": "$(json_escape "$MACHINE_ID")",
  "machine_name": "$(json_escape "$MACHINE_NAME")",
  "machine_type": "$(json_escape "$MACHINE_TYPE")"
}
EOF
ok "config/config.json written"

# Keep secrets and per-Pi state out of git if this is a clone.
if [[ ! -f "${APP_DIR}/.gitignore" ]]; then
    cat >"${APP_DIR}/.gitignore" <<'EOF'
.env
.venv/
data/
logs/
__pycache__/
*.pyc
config/config.json
EOF
fi

# ---------------------------------------------------------------------------
# 7. Python environment
# ---------------------------------------------------------------------------

step "Setting up the virtual environment"

# --system-site-packages so the venv can see apt's rpi-lgpio and spidev, which
# have no wheel for python 3.13 and would otherwise compile from source.
if [[ -x "${VENV_DIR}/bin/python" ]] \
   && ! grep -q 'include-system-site-packages = true' "${VENV_DIR}/pyvenv.cfg" 2>/dev/null; then
    warn "Existing venv cannot see system packages; recreating it."
    rm -rf "$VENV_DIR"
fi
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
    ok "Created ${VENV_DIR}"
else
    ok "Reusing existing venv"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel -q

# mfrc522 depends on RPi.GPIO; we want rpi-lgpio providing that module.
# --no-deps skips it, then we supply the real needs ourselves.
REQ="${APP_DIR}/requirements.txt"
if [[ ! -f "$REQ" ]]; then
    warn "No requirements.txt found; skipping."
elif grep -qiE '^[[:space:]]*mfrc522([[:space:]]|;|$|[<>=!])' "$REQ"; then
    REQ_TMP="$(mktemp)"
    grep -viE '^[[:space:]]*mfrc522([[:space:]]|;|$|[<>=!])' "$REQ" >"$REQ_TMP" || true
    "${VENV_DIR}/bin/pip" install -q -r "$REQ_TMP" \
        || die "pip install failed. Scroll up for the failing package."
    rm -f "$REQ_TMP"
    "${VENV_DIR}/bin/pip" install -q --no-deps mfrc522 \
        || die "pip install of mfrc522 failed."
    ok "Requirements installed (mfrc522 --no-deps, GPIO via apt rpi-lgpio)"
    info "pip's 'mfrc522 requires RPi.GPIO' ERROR above is expected and harmless:"
    info "rpi-lgpio provides that module under a different distribution name,"
    info "which pip has no way to know. Nothing is actually missing."
else
    # mfrc522 absent: plain install.
    "${VENV_DIR}/bin/pip" install -q -r "$REQ" \
        || die "pip install failed. Scroll up for the failing package."
    ok "Requirements installed"
fi

# Swap out the real RPi.GPIO if something reinstalled it. Both must be
# uninstalled first: they share RPi/, so removing one orphans the other.
if "${VENV_DIR}/bin/python" -c 'import RPi,pathlib,sys; sys.exit(0 if list(pathlib.Path(RPi.__file__).parent.glob("_GPIO*.so")) else 1)' 2>/dev/null; then
    warn "Real RPi.GPIO is shadowing rpi-lgpio; removing it from the venv."
    "${VENV_DIR}/bin/pip" uninstall -y -q RPi.GPIO rpi-gpio 2>/dev/null || true
fi

chmod +x "${APP_DIR}/main.py" "${APP_DIR}/emecamsupdate.sh" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 7b. Known compatibility fixes
# ---------------------------------------------------------------------------

step "Verifying the install"

VERIFY_OUT="$(cd "$APP_DIR" && "${VENV_DIR}/bin/python" - <<'PYCHECK'
import importlib

# Import check.
FAKEABLE = ("RPi.GPIO", "smbus2", "mfrc522", "spidev")
OTHER = ("dotenv", "psycopg")

faked, missing, hardware = [], [], []
for group, dest in ((FAKEABLE, faked), (OTHER, missing)):
    for mod in group:
        try:
            importlib.import_module(mod)
        except ImportError as e:
            dest.append(f"{mod}: {e}")
        except Exception as e:
            # Present but the hardware is not responding yet. Not a packaging problem.
            hardware.append(f"{mod}: {type(e).__name__}: {e}")

impl = "none"
try:
    import RPi, pathlib
    d = pathlib.Path(RPi.__file__).parent
    impl = "RPi.GPIO (real C extension)" if list(d.glob("_GPIO*.so")) else "rpi-lgpio"
except Exception:
    pass

print("FAKED:" + "|".join(faked))
print("MISSING:" + "|".join(missing))
print("HARDWARE:" + "|".join(hardware))
print("IMPL:" + impl)
PYCHECK
)" || true

GPIO_IMPL="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^IMPL://p')"
FAKED_MODS="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^FAKED://p')"
MISSING_MODS="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^MISSING://p')"
HARDWARE_MODS="$(printf '%s\n' "$VERIFY_OUT" | sed -n 's/^HARDWARE://p')"

if [[ -n "$FAKED_MODS" ]]; then
    warn "MISSING HARDWARE MODULES. The service will not start:"
    printf '%s\n' "$FAKED_MODS" | tr '|' '\n' | sed 's/^/      /'
    warn "Install with: sudo apt install python3-rpi-lgpio python3-spidev"
elif [[ -z "$MISSING_MODS" ]]; then
    ok "All Python imports resolve, with real hardware modules (no stubs)"
fi

info "GPIO implementation: ${GPIO_IMPL}"
if (( IS_PI5 )) && [[ "$GPIO_IMPL" == "RPi.GPIO"* ]]; then
    warn "This is a Pi 5 running the real RPi.GPIO, which fails at runtime."
    warn "Fix with: sudo apt install python3-rpi-lgpio"
fi
if [[ -n "$MISSING_MODS" ]]; then
    warn "Missing Python modules (these will stop the service):"
    printf '%s\n' "$MISSING_MODS" | tr '|' '\n' | sed 's/^/      /'
fi
if [[ -n "$HARDWARE_MODS" ]]; then
    info "Hardware not responding yet (normal before the SPI/I2C reboot):"
    printf '%s\n' "$HARDWARE_MODS" | tr '|' '\n' | sed 's/^/      /'
fi

# ---------------------------------------------------------------------------
# 8. systemd service
# ---------------------------------------------------------------------------

step "Installing the systemd service"

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=EMEC Access Management System
After=network-online.target
Wants=network-online.target

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
ok "${SERVICE_FILE} written"

sudo chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1
ok "Service enabled"

if (( REBOOT_NEEDED )); then
    # SPI/I2C do not exist until the reboot, so starting now would just fail.
    warn "Not starting the service yet: SPI/I2C need a reboot first."
    info "It is enabled and will start automatically on boot."
else
    sudo systemctl restart "${SERVICE_NAME}.service"
    sleep 4
    step "Service status"
    sudo systemctl status "${SERVICE_NAME}.service" --no-pager --lines=15 || true
    if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        warn "Service is not running. Last 20 journal lines:"
        journalctl -u "${SERVICE_NAME}.service" -n 20 --no-pager 2>/dev/null | sed 's/^/      /' || true
        printf '\n    Reproduce in the foreground with:\n'
        printf '      cd %s && ./.venv/bin/python main.py\n' "$APP_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

# The updater's units are part of a provisioned Pi, so install them here rather
# than leaving it as a step to remember. INSTALL_UPDATER=0 skips it.
if [[ "${INSTALL_UPDATER:-1}" == "1" && -f "${APP_DIR}/emecamsupdate.sh" ]]; then
    step "Installing the updater's systemd units"
    sudo bash "${APP_DIR}/emecamsupdate.sh" --install >/dev/null \
        && ok "emec-ams-update timer and path unit enabled" \
        || warn "Could not install the updater units; run: sudo ${APP_DIR}/emecamsupdate.sh --install"
fi

if (( REBOOT_NEEDED )); then
    HEADLINE="${C_YELLOW}${C_BOLD}Install complete. Reboot required.${C_RESET}"
elif systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    HEADLINE="${C_GREEN}${C_BOLD}Setup complete. Service is running.${C_RESET}"
else
    HEADLINE="${C_YELLOW}${C_BOLD}Install complete, but the service is not running.${C_RESET}"
fi

cat <<EOF

${HEADLINE}

  Machine     ${MACHINE_ID} (${MACHINE_NAME})
  Install dir ${APP_DIR}
  Service     ${SERVICE_NAME}.service

  Service log    journalctl -u ${SERVICE_NAME}.service -f   <- tracebacks go here
  App log        tail -f ${APP_DIR}/logs/sync.log
  Run by hand    cd ${APP_DIR} && ./.venv/bin/python main.py
  Restart        sudo systemctl restart ${SERVICE_NAME}.service
  I2C check      i2cdetect -y 1      (expect 3e and 60)
  SPI check      ls -l /dev/spidev*
EOF

if (( REBOOT_NEEDED )); then
    printf '\n%s[action needed]%s SPI and I2C were just enabled and do not exist yet.\n' "$C_YELLOW" "$C_RESET"
    printf '    The service cannot start until you reboot. It will come up on its own after:\n\n      sudo reboot\n\n'
fi
