#!/usr/bin/env bash
#
# EMEC-AMS Raspberry Pi provisioning. Safe to re-run.
#
#   curl -O https://raw.githubusercontent.com/zacjcsu/emec-ams-client/refs/heads/main/emecamssetup.sh
#   bash emecamssetup.sh
#
# It asks everything first, then runs on its own. It connects to the dashboard's database before asking about the
# machine, so it can list the machines and categories there.
#
# Unattended: pass DB_PASS, MACHINE_ID and --yes. Anything else not passed takes its default:
#   DB_HOST DB_PORT DB_USER DB_NAME MACHINE_NAME MACHINE_TYPE (the category ID) MACHINE_KIND (attended, cnc)
#   SCREEN READER (yes, no) CONTACTOR_SWITCH (none, true, inverted) CONTACTOR_PIN
#   CONTROL (none, haas-ngc, haas-classic) HAAS_HOST HAAS_PORT SERIAL_DEVICE SERIAL_BAUD
#   SHARE_PROGRAMS (yes, no) HAAS_SMB_PASS

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
# This file is public, so the password has no default.
# Every Pi shares the ams_pi password. If a Pi is lost, change it on the server, then on each Pi set DB_PASS
# in ${APP_DIR}/.env and restart emec-ams.
DEF_DB_HOST="10.1.226.72"
DEF_DB_PORT="5432"
DEF_DB_USER="ams_pi"
DEF_DB_NAME="emec_access"

# A CNC's control is on the Pi's second network port, eth1, with only the Pi and the control on it.
HAAS_LINK_ADDR="${HAAS_LINK_ADDR:-192.168.50.1/24}"
PROGRAM_DIR="/home/${APP_USER}/haas-programs"

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
        MACHINE_ID=lathe-001 DB_PASS='...' bash -s -- --yes"
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

is_one_of() {
    local __x="$1" __o
    shift
    for __o; do
        [[ "$__x" == "$__o" ]] && return 0
    done
    return 1
}

ask_choice() {
    # ask_choice <variable-name> <prompt> <default> <option>...   (a value passed as an env var is checked, not asked)
    local __cv="$1" __cp="$2" __cd="$3" __choice __list __try=0
    shift 3
    __list="$(printf '%s, ' "$@")"; __list="${__list%, }"
    __choice="${!__cv:-}"
    while (( __try < 3 )); do
        local __preset=0
        if [[ -n "$__choice" ]]; then
            __preset=1
        else
            ask __choice "${__cp} (${__list})" "$__cd"
        fi
        __choice="${__choice,,}"
        case "$__choice" in y|true|1) is_one_of yes "$@" && __choice=yes ;; n|false|0) is_one_of no "$@" && __choice=no ;; esac
        if is_one_of "$__choice" "$@"; then
            printf -v "$__cv" '%s' "$__choice"
            (( __preset )) && info "${__cp}: ${__choice}"
            return 0
        fi
        (( __preset || ASSUME_YES )) && die "${__cp}: '${__choice}' isn't one of ${__list}. Set ${__cv} to one of them."
        warn "Pick one of ${__list}."
        __choice=""
        __try=$(( __try + 1 ))
    done
    die "Too many failed attempts."
}

ask_number() {
    # ask_number <variable-name> <prompt> <default>
    local __nv="$1" __np="$2" __nd="$3" __number __try=0
    __number="${!__nv:-}"
    while (( __try < 3 )); do
        [[ -n "$__number" ]] || ask __number "$__np" "$__nd"
        if [[ "$__number" =~ ^[0-9]+$ ]]; then
            printf -v "$__nv" '%s' "$__number"
            return 0
        fi
        (( ASSUME_YES )) && die "${__np}: '${__number}' isn't a number. Set ${__nv}."
        warn "Enter a number."
        __number=""
        __try=$(( __try + 1 ))
    done
    die "Too many failed attempts."
}

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
# 1. Database credentials
# ---------------------------------------------------------------------------

step "Database credentials (written to .env)"

ENV_FILE="${APP_DIR}/.env"
KEEP_ENV=0

if [[ -f "$ENV_FILE" ]] && [[ -z "$DB_PASS" ]]; then
    KEEP_ENV=1
    while IFS='=' read -r __k __v; do
        case "$__k" in
            DB_HOST|DB_PORT|DB_USER|DB_PASS|DB_NAME)
                if [[ -z "${!__k}" ]]; then printf -v "$__k" '%s' "$__v"; fi ;;
        esac
    done <"$ENV_FILE"
    ok "Existing .env found; using it."
    info "To replace it, re-run with DB_PASS=... or delete ${ENV_FILE} first."
else
    [[ -z "$DB_HOST" ]] && ask DB_HOST "PostgreSQL host (dashboard VM address)" "$DEF_DB_HOST"
    [[ -z "$DB_PORT" ]] && ask DB_PORT "PostgreSQL port" "$DEF_DB_PORT"
    [[ -z "$DB_USER" ]] && ask DB_USER "PostgreSQL user" "$DEF_DB_USER"
    [[ -z "$DB_NAME" ]] && ask DB_NAME "Database name" "$DEF_DB_NAME"
    [[ -z "$DB_PASS" ]] && ask_secret DB_PASS "PostgreSQL password"
fi
DB_PORT="${DB_PORT:-$DEF_DB_PORT}"
DB_NAME="${DB_NAME:-$DEF_DB_NAME}"

# ---------------------------------------------------------------------------
# 2. System packages
# ---------------------------------------------------------------------------

step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git curl unzip rsync ca-certificates \
    python3 python3-venv python3-dev python3-pip \
    python3-rpi-lgpio python3-spidev \
    build-essential i2c-tools postgresql-client >/dev/null
ok "Base packages installed"

# ---------------------------------------------------------------------------
# 3. Connect to the dashboard's database
# ---------------------------------------------------------------------------

step "Connecting to the dashboard's database"

db_query() {
    PGPASSWORD="$DB_PASS" PGCONNECT_TIMEOUT=10 psql -X -q -A -t -F $'\x1f' -v ON_ERROR_STOP=1 \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
}

__try=0
until DB_ERR="$(db_query 'SELECT 1' 2>&1 >/dev/null)"; do
    warn "Cannot connect to ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}:"
    printf '%s\n' "$DB_ERR" | sed 's/^/      /' >&2
    if (( ASSUME_YES || ! CAN_PROMPT || __try >= 2 )); then
        die "Check the database settings and run this again."
    fi
    info "Enter the settings again."
    ask DB_HOST "PostgreSQL host (dashboard VM address)" "$DB_HOST"
    ask DB_PORT "PostgreSQL port" "$DB_PORT"
    ask DB_USER "PostgreSQL user" "$DB_USER"
    ask DB_NAME "Database name" "$DB_NAME"
    DB_PASS=""
    ask_secret DB_PASS "PostgreSQL password"
    KEEP_ENV=0
    __try=$(( __try + 1 ))
done
ok "Connected as ${DB_USER}@${DB_HOST}"

CAT_IDS=(); CAT_NAMES=()
while IFS=$'\x1f' read -r __id __name; do
    CAT_IDS+=("$__id"); CAT_NAMES+=("$__name")
done < <(db_query "SELECT machine_type, name FROM machine_categories ORDER BY name")
(( ${#CAT_IDS[@]} )) || die "The dashboard has no machine categories. Add one on the Machines page first."

M_IDS=(); M_NAMES=(); M_TYPES=(); M_DEVICES=()
while IFS=$'\x1f' read -r __id __name __type __device; do
    M_IDS+=("$__id"); M_NAMES+=("$__name"); M_TYPES+=("$__type"); M_DEVICES+=("$__device")
done < <(db_query "SELECT machine_id, COALESCE(machine_name, ''), COALESCE(machine_type, ''), COALESCE(device_id, '')
                   FROM machine ORDER BY machine_id")
ok "${#M_IDS[@]} machines and ${#CAT_IDS[@]} categories on the dashboard"

machine_index() {
    local __i
    for __i in "${!M_IDS[@]}"; do
        if [[ "${M_IDS[$__i]}" == "$1" ]]; then echo "$__i"; return 0; fi
    done
    return 0
}

# A number from the list, an ID or a name gives the ID. Anything else gives nothing.
category_id() {
    local __x="${1,,}" __i
    if [[ "$__x" =~ ^[0-9]+$ ]] && (( __x >= 1 && __x <= ${#CAT_IDS[@]} )); then
        echo "${CAT_IDS[$(( __x - 1 ))]}"; return 0
    fi
    for __i in "${!CAT_IDS[@]}"; do
        if [[ "$__x" == "${CAT_IDS[$__i],,}" || "$__x" == "${CAT_NAMES[$__i],,}" ]]; then
            echo "${CAT_IDS[$__i]}"; return 0
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# 4. The machine (written to config/config.json)
# ---------------------------------------------------------------------------

step "Machine (written to config/config.json)"

CONFIG_FILE="${APP_DIR}/config/config.json"
CPU_SERIAL="$(sed -n 's/^Serial[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | head -1)"

# Re-run: what's already in this Pi's config.json is the default for each question.
CFG_ID=""; CFG_NAME=""; CFG_TYPE=""; CFG_KIND=""; CFG_SCREEN=""; CFG_READER=""; CFG_CONTACTOR=""; CFG_PIN=""
CFG_CONTROL=""; CFG_HAAS_HOST=""; CFG_HAAS_PORT=""; CFG_SERIAL_DEVICE=""; CFG_SERIAL_BAUD=""
if [[ -f "$CONFIG_FILE" ]]; then
    eval "$(python3 - "$CONFIG_FILE" <<'PY' 2>/dev/null || true
import json, shlex, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
md = d.get("machine_data") if isinstance(d.get("machine_data"), dict) else {}
switch = d.get("contactor_switch", False)
values = {
    "CFG_ID": d.get("machine_id"), "CFG_NAME": d.get("machine_name"), "CFG_TYPE": d.get("machine_type"),
    "CFG_KIND": d.get("kind"),
    "CFG_SCREEN": "no" if d.get("screen", True) is False else "yes",
    "CFG_READER": "no" if d.get("reader", True) is False else "yes",
    "CFG_CONTACTOR": "inverted" if switch == "inverted" else "true" if switch else "none",
    "CFG_PIN": d.get("contactor_pin"), "CFG_CONTROL": md.get("control"),
    "CFG_HAAS_HOST": md.get("host"), "CFG_HAAS_PORT": md.get("port"),
    "CFG_SERIAL_DEVICE": md.get("device"), "CFG_SERIAL_BAUD": md.get("baud"),
}
for k, v in values.items():
    if v not in (None, ""):
        print(f"{k}={shlex.quote(str(v))}")
PY
)"
    info "Found an existing config on this Pi; its values are the defaults below."
fi

# The machine this Pi last ran as, from its CPU serial.
SERIAL_ID=""
for __i in "${!M_IDS[@]}"; do
    if [[ -n "$CPU_SERIAL" && "${M_DEVICES[$__i]}" == "$CPU_SERIAL" ]]; then SERIAL_ID="${M_IDS[$__i]}"; fi
done

info "Machines on the dashboard:"
for __i in "${!M_IDS[@]}"; do
    printf '      %-20s %-34s %s\n' "${M_IDS[$__i]}" "${M_NAMES[$__i]}" "${M_TYPES[$__i]}"
done
[[ -n "$SERIAL_ID" ]] && info "This Pi last ran as ${SERIAL_ID}."

MACHINE_ID="${MACHINE_ID:-}"; MACHINE_NAME="${MACHINE_NAME:-}"; MACHINE_TYPE="${MACHINE_TYPE:-}"
DEF_ID="${CFG_ID:-$SERIAL_ID}"
if [[ -z "$MACHINE_ID" ]]; then
    if [[ -n "$DEF_ID" ]]; then ask MACHINE_ID "Machine ID" "$DEF_ID"; else ask_required MACHINE_ID "Machine ID"; fi
fi
MACHINE_ID="${MACHINE_ID,,}"
[[ "$MACHINE_ID" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Machine ID '${MACHINE_ID}' can only have letters, numbers, - and _."

IDX="$(machine_index "$MACHINE_ID")"
DEF_NAME=""; DEF_TYPE=""
if [[ -n "$IDX" ]]; then
    ok "On the dashboard as ${M_NAMES[$IDX]}"
    DEF_NAME="${M_NAMES[$IDX]}"; DEF_TYPE="${M_TYPES[$IDX]}"
    if [[ -n "${M_DEVICES[$IDX]}" && -n "$CPU_SERIAL" && "${M_DEVICES[$IDX]}" != "$CPU_SERIAL" ]]; then
        warn "Another Pi (${M_DEVICES[$IDX]}) last ran as ${MACHINE_ID}. This one takes over when it starts."
    fi
else
    info "${MACHINE_ID} isn't on the dashboard yet. The Pi adds it when it starts."
fi
if [[ "$CFG_ID" == "$MACHINE_ID" ]]; then
    DEF_NAME="${DEF_NAME:-$CFG_NAME}"; DEF_TYPE="${DEF_TYPE:-$CFG_TYPE}"
fi

if [[ -z "$MACHINE_NAME" ]]; then
    if [[ -n "$DEF_NAME" ]]; then ask MACHINE_NAME "Machine name" "$DEF_NAME"; else ask_required MACHINE_NAME "Machine name"; fi
fi

info "Categories:"
for __i in "${!CAT_IDS[@]}"; do
    printf '      %2d) %-24s %s\n' "$(( __i + 1 ))" "${CAT_IDS[$__i]}" "${CAT_NAMES[$__i]}"
done
DEF_TYPE="$(category_id "$DEF_TYPE")"
[[ -n "$DEF_TYPE" ]] || DEF_TYPE="$(category_id standard-manual-lathe)"
[[ -n "$DEF_TYPE" ]] || DEF_TYPE="${CAT_IDS[0]}"
__try=0
until [[ -n "$MACHINE_TYPE" ]] && [[ -n "$(category_id "$MACHINE_TYPE")" ]]; do
    if [[ -n "$MACHINE_TYPE" ]]; then
        (( ASSUME_YES || __try >= 3 )) && die "'${MACHINE_TYPE}' isn't a category on the dashboard."
        warn "'${MACHINE_TYPE}' isn't a category on the dashboard. Pick a number or an ID from the list."
        __try=$(( __try + 1 ))
    fi
    MACHINE_TYPE=""
    ask MACHINE_TYPE "Category (number or ID)" "$DEF_TYPE"
done
MACHINE_TYPE="$(category_id "$MACHINE_TYPE")"

DEF_KIND="${CFG_KIND:-attended}"
[[ -z "$CFG_KIND" && "$MACHINE_TYPE" == cnc* ]] && DEF_KIND="cnc"
MACHINE_KIND="${MACHINE_KIND:-}"
ask_choice MACHINE_KIND "Kind" "$DEF_KIND" attended cnc

SCREEN="${SCREEN:-}"; READER="${READER:-}"
ask_choice SCREEN "Screen fitted" "${CFG_SCREEN:-yes}" yes no
ask_choice READER "Card reader fitted" "${CFG_READER:-yes}" yes no

# A CNC's relay is in series with cycle start, so it has no contactor switch unless this Pi already had one.
CONTACTOR_SWITCH="${CONTACTOR_SWITCH:-}"; CONTACTOR_PIN="${CONTACTOR_PIN:-}"
if [[ "$MACHINE_KIND" == cnc && "${CFG_CONTACTOR:-none}" == none && -z "$CONTACTOR_SWITCH" ]]; then
    CONTACTOR_SWITCH="none"
else
    info "Contactor switch: true reads an open circuit as on. inverted reads a closed circuit as on."
    ask_choice CONTACTOR_SWITCH "Contactor switch" "${CFG_CONTACTOR:-none}" none true inverted
fi
if [[ "$CONTACTOR_SWITCH" != none ]]; then
    ask_number CONTACTOR_PIN "Contactor header pin (ground is pin 39)" "${CFG_PIN:-37}"
fi

# A control this script doesn't know, like one from a branch, can be kept as it is.
CONTROLS=(none haas-ngc haas-classic)
if [[ -n "$CFG_CONTROL" ]] && ! is_one_of "$CFG_CONTROL" "${CONTROLS[@]}"; then CONTROLS+=("$CFG_CONTROL"); fi
DEF_CONTROL="${CFG_CONTROL:-none}"
[[ -z "$CFG_CONTROL" && "$MACHINE_KIND" == cnc ]] && DEF_CONTROL="haas-ngc"
CONTROL="${CONTROL:-}"
ask_choice CONTROL "Machine control" "$DEF_CONTROL" "${CONTROLS[@]}"
HAAS_HOST="${HAAS_HOST:-}"; HAAS_PORT="${HAAS_PORT:-}"; SERIAL_DEVICE="${SERIAL_DEVICE:-}"; SERIAL_BAUD="${SERIAL_BAUD:-}"
case "$CONTROL" in
    haas-ngc)
        [[ -z "$HAAS_HOST" ]] && ask HAAS_HOST "Haas address" "${CFG_HAAS_HOST:-192.168.50.2}"
        ask_number HAAS_PORT "Haas port" "${CFG_HAAS_PORT:-5051}" ;;
    haas-classic)
        # The adapter's name under /dev/serial/by-id stays the same when other USB devices come and go.
        DEF_SERIAL="/dev/ttyUSB0"
        SERIAL_IDS=(/dev/serial/by-id/*)
        if [[ -e "${SERIAL_IDS[0]}" ]]; then
            info "Serial adapters:"
            printf '      %s\n' "${SERIAL_IDS[@]}"
            (( ${#SERIAL_IDS[@]} == 1 )) && DEF_SERIAL="${SERIAL_IDS[0]}"
        else
            info "No serial adapter is plugged in. Its name is under /dev/serial/by-id once it is."
        fi
        [[ -z "$SERIAL_DEVICE" ]] && ask SERIAL_DEVICE "Serial device" "${CFG_SERIAL_DEVICE:-$DEF_SERIAL}"
        ask_number SERIAL_BAUD "Serial baud" "${CFG_SERIAL_BAUD:-9600}" ;;
esac

SHARE_PROGRAMS="${SHARE_PROGRAMS:-}"
HAAS_SMB_PASS="${HAAS_SMB_PASS:-}"
# Only an NGC control reads programs from a network share. A classic one has only its RS-232 port here.
if [[ "$CONTROL" == haas-ngc ]]; then
    ask_choice SHARE_PROGRAMS "Share programs with the control" yes yes no
else
    SHARE_PROGRAMS="${SHARE_PROGRAMS:-no}"
fi
if [[ "$SHARE_PROGRAMS" == yes ]] && [[ -z "$HAAS_SMB_PASS" ]] \
   && ! { command -v pdbedit >/dev/null && sudo pdbedit -L 2>/dev/null | grep -q '^haas:'; }; then
    ask_secret HAAS_SMB_PASS "Password the control signs in with, as user haas"
fi

ok "${MACHINE_ID} / ${MACHINE_NAME} / ${MACHINE_TYPE} / ${MACHINE_KIND}"
info "Everything else runs on its own."

# ---------------------------------------------------------------------------
# 5. Regional settings (the raspi-config steps, done non-interactively)
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
# 6. Hardware interfaces
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
# 7. Fetch the application code
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
# 8. Config files
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

PROGRAM_FOLDER=""
[[ "$SHARE_PROGRAMS" == yes ]] && PROGRAM_FOLDER="$PROGRAM_DIR"
# Keys this script doesn't ask about are kept. So are a control's other settings, like poll_seconds.
CFG_ID="$MACHINE_ID" CFG_NAME="$MACHINE_NAME" CFG_TYPE="$MACHINE_TYPE" CFG_KIND="$MACHINE_KIND" \
CFG_SCREEN="$SCREEN" CFG_READER="$READER" CFG_CONTACTOR="$CONTACTOR_SWITCH" CFG_PIN="$CONTACTOR_PIN" \
CFG_CONTROL="$CONTROL" CFG_HAAS_HOST="$HAAS_HOST" CFG_HAAS_PORT="$HAAS_PORT" \
CFG_SERIAL_DEVICE="$SERIAL_DEVICE" CFG_SERIAL_BAUD="$SERIAL_BAUD" \
CFG_PROGRAM_DIR="$PROGRAM_FOLDER" \
python3 - "$CONFIG_FILE" <<'PY'
import json, os, sys
path, e = sys.argv[1], os.environ
try:
    data = json.load(open(path))
except Exception:
    data = {}
data.update(machine_id=e["CFG_ID"], machine_name=e["CFG_NAME"], machine_type=e["CFG_TYPE"], kind=e["CFG_KIND"])
for key in ("screen", "reader"):
    if e["CFG_" + key.upper()] == "no":
        data[key] = False
    else:
        data.pop(key, None)
if e["CFG_CONTACTOR"] == "none":
    data.pop("contactor_switch", None)
    data.pop("contactor_pin", None)
else:
    data["contactor_switch"] = True if e["CFG_CONTACTOR"] == "true" else "inverted"
    data["contactor_pin"] = int(e["CFG_PIN"])
control = e["CFG_CONTROL"]
old = data.get("machine_data") if isinstance(data.get("machine_data"), dict) else {}
machine_data = dict(old) if old.get("control") == control else {"control": control}
if control == "none":
    data.pop("machine_data", None)
elif control == "haas-ngc":
    machine_data.update(host=e["CFG_HAAS_HOST"], port=int(e["CFG_HAAS_PORT"]))
    data["machine_data"] = machine_data
elif control == "haas-classic":
    machine_data.update(device=e["CFG_SERIAL_DEVICE"], baud=int(e["CFG_SERIAL_BAUD"]))
    data["machine_data"] = machine_data
if e["CFG_PROGRAM_DIR"]:
    data["program_folder"] = e["CFG_PROGRAM_DIR"]
else:
    data.pop("program_folder", None)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
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
# 9. Program share for the control
# ---------------------------------------------------------------------------

# The control reads programs from a Samba share on eth1. Old Haas controls only speak SMB1 and NTLMv1, and find the
# Pi by its name, so nmbd runs too.
SMB_NAME="$(hostname | cut -c1-15 | tr '[:lower:]' '[:upper:]')"
if [[ "$SHARE_PROGRAMS" == yes ]]; then
    step "Sharing programs with the control"

    sudo apt-get install -y -qq samba >/dev/null
    ok "Samba installed"

    mkdir -p "$PROGRAM_DIR"
    getent passwd haas >/dev/null \
        || sudo useradd --system --no-create-home --shell /usr/sbin/nologin haas
    [[ -f /etc/samba/smb.conf.orig ]] || sudo cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
    sudo tee /etc/samba/smb.conf >/dev/null <<EOF
[global]
   interfaces = lo eth1
   bind interfaces only = yes
   server min protocol = NT1
   ntlm auth = ntlmv1-permitted
   map to guest = never

[programs]
   path = ${PROGRAM_DIR}
   valid users = haas
   force user = ${APP_USER}
   read only = no
EOF
    ok "/etc/samba/smb.conf written (the original is smb.conf.orig)"

    if [[ -n "$HAAS_SMB_PASS" ]]; then
        printf '%s\n%s\n' "$HAAS_SMB_PASS" "$HAAS_SMB_PASS" | sudo smbpasswd -a -s haas >/dev/null \
            && ok "Samba user haas set" || warn "Could not set the Samba password for haas."
    else
        ok "Samba user haas kept"
    fi

    if command -v nmcli >/dev/null; then
        NM_ARGS=(connection.interface-name eth1 ipv4.method manual ipv4.addresses "$HAAS_LINK_ADDR"
                 ipv4.never-default yes ipv6.method disabled connection.autoconnect yes)
        if nmcli -t -f NAME connection show | grep -qx haas; then
            sudo nmcli connection modify haas "${NM_ARGS[@]}"
        else
            sudo nmcli connection add type ethernet con-name haas "${NM_ARGS[@]}" >/dev/null
        fi
        ok "eth1 set to ${HAAS_LINK_ADDR}"
        sudo nmcli connection up haas >/dev/null 2>&1 \
            || warn "eth1 isn't up yet. It comes up once the control's cable is in."
    else
        warn "NetworkManager isn't here. Give eth1 the address ${HAAS_LINK_ADDR} by hand."
    fi

    sudo systemctl enable smbd nmbd >/dev/null 2>&1 || true
    sudo systemctl restart smbd nmbd || warn "Samba didn't start. Check: journalctl -u smbd"
    ok "Sharing ${PROGRAM_DIR} as \\\\${SMB_NAME}\\programs"
fi

# ---------------------------------------------------------------------------
# 10. Python environment
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
    MFRC_SPEC="$(grep -iE '^[[:space:]]*mfrc522([[:space:]]|;|$|[<>=!])' "$REQ" | head -1 | cut -d';' -f1 | tr -d '[:space:]')"
    "${VENV_DIR}/bin/pip" install -q --no-deps "$MFRC_SPEC" \
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
# 10b. Known compatibility fixes
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
# 11. systemd service
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
  Category    ${MACHINE_TYPE}, ${MACHINE_KIND}
  Control     ${CONTROL}
  Install dir ${APP_DIR}
  Service     ${SERVICE_NAME}.service

  Service log    journalctl -u ${SERVICE_NAME}.service -f   <- tracebacks go here
  App log        tail -f ${APP_DIR}/logs/sync.log
  Run by hand    cd ${APP_DIR} && ./.venv/bin/python main.py
  Restart        sudo systemctl restart ${SERVICE_NAME}.service
  I2C check      i2cdetect -y 1      (expect 3e and 60)
  SPI check      ls -l /dev/spidev*
EOF

if [[ "$SHARE_PROGRAMS" == yes ]]; then
    printf '\n  On the control, set the Net Share to server name %s, share name programs,\n' "$SMB_NAME"
    printf '  user name haas and Domain/Workgroup WORKGROUP. Use the name, not an IP.\n'
fi

if (( REBOOT_NEEDED )); then
    printf '\n%s[action needed]%s SPI and I2C were just enabled and do not exist yet.\n' "$C_YELLOW" "$C_RESET"
    printf '    The service cannot start until you reboot. It will come up on its own after:\n\n      sudo reboot\n\n'
fi
