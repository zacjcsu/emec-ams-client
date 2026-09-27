#!/usr/bin/env bash
#
# EMEC-AMS updater / first-boot provisioner. Install its units once:
#     sudo emecamsupdate.sh --install
#
# Triggered by the timer (boot + daily 01:00) and by the .path unit.
# Modes: blank config -> wait; filled + service disabled -> provision;
# filled + enabled -> update.

set -euo pipefail

APP_USER="${APP_USER:-emec}"
APP_DIR="${APP_DIR:-/home/${APP_USER}/emec-ams}"
VENV_DIR="${APP_DIR}/.venv"
SERVICE="emec-ams.service"
CONFIG="${APP_DIR}/config/config.json"
FLAG="${APP_DIR}/.update-requested"
LOCK="/run/emec-ams-update.lock"
LOGFILE="${APP_DIR}/logs/update.log"

REPO="${REPO:-https://github.com/zacjcsu/emec-ams-client.git}"
BRANCH="${BRANCH:-main}"

# Provisioning ends with a reboot so the new hostname and machine-id take
# effect cleanly. Set PROVISION_REBOOT=0 to skip it.
PROVISION_REBOOT="${PROVISION_REBOOT:-1}"

UNIT_DIR=/etc/systemd/system

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# --install: write the systemd units and enable them
# ---------------------------------------------------------------------------

install_units() {
    [[ "$(id -u)" -eq 0 ]] || die "--install must run as root (use sudo)."

    cat >"${UNIT_DIR}/emec-ams-update.service" <<EOF
[Unit]
Description=EMEC-AMS update and first-boot provisioning
After=network-online.target
Wants=network-online.target
# Caps a restart loop if the flag is ever left behind.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=oneshot
ExecStart=/bin/bash ${APP_DIR}/emecamsupdate.sh
# Room for a pip install on a slow link.
TimeoutStartSec=900
EOF

    cat >"${UNIT_DIR}/emec-ams-update.timer" <<EOF
[Unit]
Description=EMEC-AMS update schedule

[Timer]
# After boot, once the network is likely up.
OnBootSec=3min
# System timezone, so DST is handled.
OnCalendar=*-*-* 01:00:00
# Catch up if the Pi was off at 01:00.
Persistent=true
# Stagger the fleet.
RandomizedDelaySec=300
Unit=emec-ams-update.service

[Install]
WantedBy=timers.target
EOF

    cat >"${UNIT_DIR}/emec-ams-update.path" <<EOF
[Unit]
Description=EMEC-AMS update requested by the application

[Path]
# Touch ${FLAG} to update now. Running as its own unit is what lets the
# updater stop emec-ams without killing itself.
PathExists=${FLAG}
Unit=emec-ams-update.service

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now emec-ams-update.timer >/dev/null
    systemctl enable --now emec-ams-update.path >/dev/null

    log "Installed and enabled:"
    log "  ${UNIT_DIR}/emec-ams-update.service"
    log "  ${UNIT_DIR}/emec-ams-update.timer   (boot + daily 01:00 local)"
    log "  ${UNIT_DIR}/emec-ams-update.path    (watches ${FLAG})"
    exit 0
}

[[ "${1:-}" == "--install" ]] && install_units

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if [[ "$(id -u)" -ne 0 ]]; then
    die "Must run as root.
      Units installed:     sudo systemctl start emec-ams-update.service
      Units not installed: sudo ${APP_DIR}/emecamsupdate.sh --install"
fi
[[ -d "$APP_DIR" ]] || die "${APP_DIR} does not exist."

mkdir -p "${APP_DIR}/logs"
exec > >(tee -a "$LOGFILE") 2>&1

# Must precede any early exit: the .path unit re-triggers the instant this
# service ends, so a leftover flag means a restart loop.
rm -f "$FLAG"

# Covers a manual run overlapping the timer.
exec 9>"$LOCK"
flock -n 9 || { log "Another update is already running; exiting."; exit 0; }

as_app() { runuser -u "$APP_USER" -- "$@"; }

# ---------------------------------------------------------------------------
# Decide state
# ---------------------------------------------------------------------------

read_config() {
    python3 - "$CONFIG" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
for k in ("machine_id", "machine_name", "machine_type"):
    v = d.get(k)
    print(str(v).strip() if v is not None else "")
PY
}

MACHINE_ID=""; MACHINE_NAME=""; MACHINE_TYPE=""
{ read -r MACHINE_ID; read -r MACHINE_NAME; read -r MACHINE_TYPE; } < <(read_config) || true

if [[ -z "$MACHINE_ID" || -z "$MACHINE_NAME" || -z "$MACHINE_TYPE" ]]; then
    log "config.json is not filled in yet (machine_id='${MACHINE_ID}'). Waiting."
    log "Nothing to do without an identity. Fill in ${CONFIG} and this will"
    log "provision on the next trigger, or force one now with:"
    log "  sudo systemctl start emec-ams-update.service"
    exit 0
fi

if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
    MODE=update
else
    MODE=provision
fi
log "Machine ${MACHINE_ID} (${MACHINE_NAME}) — mode: ${MODE}"

# ---------------------------------------------------------------------------
# Code sync
# ---------------------------------------------------------------------------

sync_code() {
    # Adopt a non-git directory (drive-mode installs).
    if [[ ! -d "${APP_DIR}/.git" ]]; then
        log "No git repo here yet; adopting ${APP_DIR} in place."
        as_app git -C "$APP_DIR" init -q -b "$BRANCH"
    fi

    # set-url so REPO wins over whatever the checkout was created with.
    if as_app git -C "$APP_DIR" remote get-url origin >/dev/null 2>&1; then
        as_app git -C "$APP_DIR" remote set-url origin "$REPO"
    else
        as_app git -C "$APP_DIR" remote add origin "$REPO"
    fi

    local before after
    before="$(sha256sum "${APP_DIR}/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)"

    log "Fetching ${REPO} (${BRANCH})..."
    if ! as_app git -C "$APP_DIR" fetch --quiet origin "$BRANCH"; then
        log "WARNING: fetch failed (offline?). Keeping the code that is here."
        return 1
    fi

    # reset, not pull: no merge conflicts, and untracked files are left alone.
    as_app git -C "$APP_DIR" reset --hard --quiet FETCH_HEAD
    # A power cut soon after an update left lathe-001 with empty code files.
    sync
    as_app git -C "$APP_DIR" branch --set-upstream-to="origin/${BRANCH}" \
        "$BRANCH" >/dev/null 2>&1 || true
    log "Now at $(as_app git -C "$APP_DIR" log -1 --pretty='%h %s')"

    after="$(sha256sum "${APP_DIR}/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)"
    if [[ "$before" != "$after" || ! -x "${VENV_DIR}/bin/python" ]]; then
        log "requirements.txt changed (or no venv); installing dependencies."
        [[ -x "${VENV_DIR}/bin/python" ]] || as_app python3 -m venv --system-site-packages "$VENV_DIR"
        install_python_deps
    fi
    ensure_gpio

    verify_hardware_modules

    prune_hardware
    chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
    return 0
}

# Import check.
HW_CHECK_PY='
import importlib
bad = []
for mod in ("RPi.GPIO", "smbus2", "mfrc522", "spidev", "dotenv", "psycopg"):
    try:
        importlib.import_module(mod)
    except ImportError as e:
        bad.append("%s: %s" % (mod, e))
    except Exception:
        pass
impl = "?"
try:
    import RPi, pathlib
    d = pathlib.Path(RPi.__file__).parent
    impl = "RPi.GPIO (real)" if list(d.glob("_GPIO*.so")) else "rpi-lgpio"
except Exception:
    impl = "none"
print("|".join(bad))
print("IMPL:" + impl)
'

hw_check() {
    (cd "$APP_DIR" && printf '%s' "$HW_CHECK_PY" | "${VENV_DIR}/bin/python" - 2>/dev/null) || true
}

verify_hardware_modules() {
    local out
    local raw impl
    raw="$(hw_check)"
    impl="$(sed -n 's/^IMPL://p' <<<"$raw")"
    out="$(grep -v '^IMPL:' <<<"$raw" | head -1)"
    [[ -z "$out" ]] && { log "Hardware modules present. GPIO implementation: ${impl}"; return 0; }

    log "Hardware modules missing, attempting repair:"
    printf '%s\n' "$out" | tr '|' '\n' | sed 's/^/      /'
    install_python_deps
    ensure_gpio

    out="$(hw_check | grep -v '^IMPL:' | head -1)"
    if [[ -z "$out" ]]; then
        log "Repaired; all hardware modules now present."
    else
        log "WARNING: STILL MISSING after repair. The service will not start:"
        printf '%s\n' "$out" | tr '|' '\n' | sed 's/^/      /'
    fi
}

install_python_deps() {
    # mfrc522 depends on RPi.GPIO; we want rpi-lgpio providing that module.
    # --no-deps skips it, then we supply the real needs ourselves.
    local req="${APP_DIR}/requirements.txt" tmp
    [[ -f "$req" ]] || { log "No requirements.txt; skipping dependencies."; return 0; }

    if grep -qiE '^[[:space:]]*mfrc522([[:space:]]|;|$|[<>=!])' "$req"; then
        tmp="$(mktemp)"
        # pip runs as APP_USER; mktemp creates this 0600 root-owned.
        chmod 0644 "$tmp"
        grep -viE '^[[:space:]]*mfrc522([[:space:]]|;|$|[<>=!])' "$req" >"$tmp" || true
        as_app "${VENV_DIR}/bin/pip" install -q -r "$tmp" || log "WARNING: pip install failed."
        rm -f "$tmp"
        as_app "${VENV_DIR}/bin/pip" install -q --no-deps mfrc522 || log "WARNING: mfrc522 install failed."
        # pip's "mfrc522 requires RPi.GPIO" notice is expected here.
        log "(pip's 'mfrc522 requires RPi.GPIO' notice is expected; apt's rpi-lgpio provides it.)"
    else
        as_app "${VENV_DIR}/bin/pip" install -q -r "$req" || log "WARNING: pip install failed."
    fi
}

ensure_gpio() {
    # Swap out the real RPi.GPIO if something reinstalled it. Both must be
    # uninstalled first: they share RPi/, so removing one orphans the other.
    "${VENV_DIR}/bin/python" -c 'import RPi,pathlib,sys; sys.exit(0 if list(pathlib.Path(RPi.__file__).parent.glob("_GPIO*.so")) else 1)' 2>/dev/null || return 0
    log "Real RPi.GPIO is shadowing rpi-lgpio; removing it from the venv."
    as_app "${VENV_DIR}/bin/pip" uninstall -y -q RPi.GPIO rpi-gpio 2>/dev/null || true
}

prune_hardware() {
    # hardware/ is KiCad and gerbers. skip-worktree stops reset restoring it.
    if [[ -d "${APP_DIR}/hardware" ]]; then
        as_app git -C "$APP_DIR" ls-files -z hardware \
            | as_app xargs -0 -r git -C "$APP_DIR" update-index --skip-worktree 2>/dev/null || true
        rm -rf "${APP_DIR}/hardware"
        log "Pruned hardware/ (KiCad and gerbers, not needed at runtime)."
    fi
}

# ---------------------------------------------------------------------------
# PROVISION
# ---------------------------------------------------------------------------

provision() {
    # Hostname from machine_id: lowercase, only [a-z0-9-], no leading/trailing
    # dash, max 63 chars.
    local host
    host="$(printf '%s' "$MACHINE_ID" | tr '[:upper:]' '[:lower:]' \
            | tr -c 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-63)"
    [[ -n "$host" ]] || die "Could not derive a hostname from machine_id '${MACHINE_ID}'."

    log "Setting hostname to '${host}'."
    hostnamectl set-hostname "$host"
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${host}/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$host" >>/etc/hosts
    fi

    log "Regenerating SSH host keys."
    rm -f /etc/ssh/ssh_host_*
    ssh-keygen -A >/dev/null
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true

    log "Regenerating machine-id."
    truncate -s 0 /etc/machine-id
    rm -f /var/lib/dbus/machine-id
    systemd-machine-id-setup >/dev/null
    command -v dbus-uuidgen >/dev/null && dbus-uuidgen --ensure 2>/dev/null || true

    log "Clearing state carried over from the golden image."
    rm -f "${APP_DIR}/data/local.db"
    rm -f "${APP_DIR}"/logs/*.log "${APP_DIR}"/logs/*.log.*
    find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"

    sync_code || log "Continuing with the code already present."

    log "Enabling ${SERVICE}."
    systemctl enable "$SERVICE" >/dev/null

    chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
    log "Provisioning complete for ${MACHINE_ID}."

    if (( PROVISION_REBOOT )); then
        log "Rebooting so the hostname and machine-id take effect."
        sleep 2
        systemctl reboot
    else
        log "PROVISION_REBOOT=0, starting the service without rebooting."
        systemctl start "$SERVICE"
    fi
}

# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------

update() {
    log "Stopping ${SERVICE}."
    systemctl stop "$SERVICE" || true

    # Never leave the app down.
    trap 'log "Starting ${SERVICE}."; systemctl start "$SERVICE" || log "ERROR: could not start ${SERVICE}."' EXIT

    sync_code || log "Continuing with the code already present."

    log "Update complete."
}

case "$MODE" in
    provision) provision ;;
    update)    update ;;
esac
