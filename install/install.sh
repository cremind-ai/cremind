#!/usr/bin/env bash
# Cremind installer — Linux / macOS.
#
# Usage:
#   curl -fsSL https://cremind.io/install.sh | bash
#   curl -fsSL https://cremind.io/install.sh | bash -s -- [flags]
#
# Flags:
#   --deployment local|server|custom
#                               Skip the deployment-type prompt. ``custom``
#                               exposes advanced fields (listen host, public
#                               URL, allowed origins, wizard preset) so you
#                               can configure unusual setups (running inside
#                               a container, behind a reverse proxy, etc.).
#                               ``container`` is accepted as a deprecated
#                               alias for ``custom`` with container defaults.
#   --host HOST                 Public IP/domain (server deployment only).
#   --desktop / --no-desktop    (Docker mode) Include or skip the VNC Desktop
#                               UI. --desktop pulls cremind/cremind-desktop
#                               (XFCE + VNC); --no-desktop pulls the smaller
#                               headless cremind/cremind image (no noVNC/VNC
#                               ports). Default: desktop, including
#                               --unattended; a re-install keeps the previous
#                               choice unless you pass a flag.
#   --listen-host HOST          (custom deployment) Override HOST in .env.
#   --public-url URL            (custom deployment) Override APP_URL in .env.
#   --allowed-origins LIST      (custom deployment) Override CORS_ALLOWED_ORIGINS.
#   --wizard-preset ID          (custom deployment) Override SETUP_WIZARD_ENV.
#   --no-launch                 Skip opening the setup wizard at the end.
#   --unattended                Use defaults; never prompt. Implies --no-launch
#                               unless deployment+host are also provided.
#   --reinstall                 Wipe any existing $CREMIND_SYSTEM_DIR/venv before installing.
#   --auto-install-python       Auto-install isolated Python 3.13 if missing
#                               (default: prompt; --unattended installs silently).
#   --no-auto-install-python    Never auto-install; print manual hints and exit.
#   --no-modify-path            Don't modify shell rc; print the export line instead.
#   --channel production|test|dev
#                               Install source. 'production' (default) pulls
#                               from PyPI. 'test' pulls the latest rcN wheel
#                               from Test PyPI for release-candidate
#                               validation. 'dev' pip-installs the local
#                               checkout in editable mode — requires running
#                               this script from a clone of the repo; rejected
#                               when piped via curl. 'dev' works with both
#                               --mode native and --mode docker (the compose
#                               override bind-mounts the checkout at /src).
#   --version SPEC              Explicit cremind version to install (e.g.
#                               ``0.2.1`` for production, ``0.2.1rc3`` for
#                               test). Validated against the channel's shape
#                               (production = X.Y.Z, test = X.Y.ZrcN). When
#                               --electron-version is also given, the
#                               version must additionally match that line
#                               (production = exact, test = same X.Y.Z).
#   --electron-version VER      Build version of the Electron app driving
#                               this install (e.g. ``0.2.1``). Forwarded by
#                               the Cremind desktop app so we pin the cremind
#                               package to the Electron build's line; CLI
#                               users typically don't set this.
#   --uninstall                 Run the uninstaller instead of installing.
#                               Combine with --keep or --purge; omit both
#                               for an interactive k/p/c prompt. Honours
#                               CREMIND_SYSTEM_DIR / CREMIND_INSTALL_DIR.
#   --keep                      (with --uninstall) Remove binaries + install
#                               scratch; preserve .env, bootstrap.toml,
#                               storage/, tokens/, profile dirs.
#   --purge                     (with --uninstall) Wipe both System Dir and
#                               Install Dir. For docker installs, also runs
#                               ``docker compose down -v`` to drop volumes.
#   --help                      Show this message.
#
# Service selection (database, vector store, …) is no longer made here —
# the Setup Wizard now lets you pick each backing service's deployment
# mode (Docker / Native / External) per-service, independent of how
# Cremind itself is installed.
#
# This is the Phase 2 installer: native install only. Docker mode is
# detected and recommended, but the actual containerized bundle ships in
# Phase 3 — for now we explain that and fall back to native.

set -euo pipefail

# ── colors / logging ──────────────────────────────────────────────────────

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    BLUE=$(printf '\033[34m')
    RESET=$(printf '\033[0m')
else
    BOLD= DIM= RED= GREEN= YELLOW= BLUE= RESET=
fi

info()  { printf '%s==>%s %s\n' "$BLUE$BOLD" "$RESET" "$1"; }
warn()  { printf '%s!!!%s %s\n' "$YELLOW$BOLD" "$RESET" "$1" >&2; }
err()   { printf '%sERR%s %s\n' "$RED$BOLD" "$RESET" "$1" >&2; }
ok()    { printf '%s ✓%s  %s\n' "$GREEN$BOLD" "$RESET" "$1"; }
step()  { printf '\n%s── %s ──%s\n' "$BOLD" "$1" "$RESET"; }

# ── channel ───────────────────────────────────────────────────────────────
#
# Install source. See --help for the user-facing description.
#   production (default) — pip install cremind from PyPI
#   test                 — install latest rcN wheel from Test PyPI
#   dev                  — pip install -e <repo_root> (local checkout)
CHANNEL="production"

# REPO_ROOT is the repo containing this script. Required for dev mode (we
# install from there and read templates from there). Empty when piped via
# curl — the dev-mode check below uses that to reject `curl | bash -s -- --channel dev`.
REPO_ROOT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
fi

# ── flags ─────────────────────────────────────────────────────────────────

DEPLOYMENT=""
APP_HOST=""
MODE=""           # docker | native (default: prompt if Docker available)
# Docker mode only: include the VNC Desktop UI? "" = ask (default yes);
# "1" = desktop image (cremind/cremind-desktop); "0" = basic headless image
# (cremind/cremind). Ignored for native installs.
DESKTOP_UI=""
NO_LAUNCH=0
UNATTENDED=0
REINSTALL=0
# When 1, skip the prompt_toolkit TUI bootstrap and fall back to the
# numbered ``read -p`` prompts. CI/debugging only — interactive users
# benefit from the TUI's keyboard navigation and version picker.
NO_TUI="${CREMIND_NO_TUI:-0}"
# Uninstall switch: when set, this script runs the uninstall flow inline
# below (after flag parsing) and exits before any install-side setup.
# UNINSTALL_MODE is read from --keep / --purge; empty = interactive prompt.
UNINSTALL=0
UNINSTALL_MODE=""        # keep | purge | empty (interactive)
AUTO_INSTALL_PYTHON=""   # "" = ask interactively; "1" = yes; "0" = no
MODIFY_PATH=""           # "" = auto (1 for canonical home, 0 otherwise); 0/1 explicit
# Explicit cremind version to install. Empty = resolve channel default
# (production → latest from PyPI / pin to ELECTRON_VERSION when set;
# test → highest matching ``rcN``). Validated against the channel
# shape, and against ELECTRON_VERSION when present.
VERSION_SPEC=""
# Build version of the Electron app driving the install (e.g. 0.2.1).
# Set only when the script is spawned by the Cremind desktop app — its
# main.ts always forwards ``app.getVersion()`` so the install pins to
# the same line. Empty for direct curl|bash invocations.
ELECTRON_VERSION=""
# Custom-deployment advanced fields. Empty = prompt interactively (or use
# the catalog default in --unattended). The keys mirror the catalog's
# deployments.custom.advanced_fields[].key entries.
CUSTOM_listen_host=""
CUSTOM_public_url=""
CUSTOM_allowed_origins=""
CUSTOM_wizard_preset=""

while [ $# -gt 0 ]; do
    case "$1" in
        --deployment)             DEPLOYMENT="$2"; shift 2 ;;
        --deployment=*)           DEPLOYMENT="${1#*=}"; shift ;;
        --host)                   APP_HOST="$2"; shift 2 ;;
        --host=*)                 APP_HOST="${1#*=}"; shift ;;
        --mode)                   MODE="$2"; shift 2 ;;
        --mode=*)                 MODE="${1#*=}"; shift ;;
        --docker)                 MODE="docker"; shift ;;
        --native)                 MODE="native"; shift ;;
        --desktop)                DESKTOP_UI=1; shift ;;
        --no-desktop)             DESKTOP_UI=0; shift ;;
        --listen-host)            CUSTOM_listen_host="$2"; shift 2 ;;
        --listen-host=*)          CUSTOM_listen_host="${1#*=}"; shift ;;
        --public-url)             CUSTOM_public_url="$2"; shift 2 ;;
        --public-url=*)           CUSTOM_public_url="${1#*=}"; shift ;;
        --allowed-origins)        CUSTOM_allowed_origins="$2"; shift 2 ;;
        --allowed-origins=*)      CUSTOM_allowed_origins="${1#*=}"; shift ;;
        --wizard-preset)          CUSTOM_wizard_preset="$2"; shift 2 ;;
        --wizard-preset=*)        CUSTOM_wizard_preset="${1#*=}"; shift ;;
        --no-launch)              NO_LAUNCH=1; shift ;;
        --unattended)             UNATTENDED=1; shift ;;
        --reinstall)              REINSTALL=1; shift ;;
        --auto-install-python)    AUTO_INSTALL_PYTHON=1; shift ;;
        --no-auto-install-python) AUTO_INSTALL_PYTHON=0; shift ;;
        --modify-path)            MODIFY_PATH=1; shift ;;
        --no-modify-path)         MODIFY_PATH=0; shift ;;
        --channel)                CHANNEL="$2"; shift 2 ;;
        --channel=*)              CHANNEL="${1#*=}"; shift ;;
        --version)                VERSION_SPEC="$2"; shift 2 ;;
        --version=*)              VERSION_SPEC="${1#*=}"; shift ;;
        --electron-version)       ELECTRON_VERSION="$2"; shift 2 ;;
        --electron-version=*)     ELECTRON_VERSION="${1#*=}"; shift ;;
        --uninstall)              UNINSTALL=1; shift ;;
        --keep)                   UNINSTALL_MODE="keep"; shift ;;
        --purge)                  UNINSTALL_MODE="purge"; shift ;;
        --no-tui)                 NO_TUI=1; shift ;;
        --help|-h)
            sed -n '1,/^set -e/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^set -e/d'
            exit 0
            ;;
        *)
            err "Unknown flag: $1"
            exit 2
            ;;
    esac
done

# ── --uninstall flow ──────────────────────────────────────────────────────
# Inline uninstaller. Runs before banner / catalog / TUI setup so the
# install path's interactive scaffolding never touches an uninstall run.
#
# Removes Cremind from this machine. Operates on two directories:
#
#   System Dir  ($CREMIND_SYSTEM_DIR / default ~/.cremind)
#       Native installs: runtime + user content (.env, bootstrap.toml,
#       storage/, tokens/, venv/, bin/, per-profile dirs, server.log,
#       backups/). Docker installs leave the host's ~/.cremind untouched —
#       runtime state lives in the ``cremind-data`` named Docker volume.
#
#   Install Dir ($CREMIND_INSTALL_DIR / platform-conventional)
#       Install-time scratch: install.log, install.pid, docker/ (compose
#       bundle), pip-cache/, uv-cache/, python/.
#
# Detection: $INSTALL_DIR/docker/docker-compose.yml -> Docker; else
#            $SYSTEM_DIR/venv -> Native; else partial-install -> Native.
#
# The User Working Directory (server_config.user_working_dir, picked in
# the Setup Wizard) is NEVER touched directly. When it resolves inside
# the System Dir, --purge takes it along with the rest.
if [ "$UNINSTALL" -eq 1 ]; then
    # Resolve System / Install Dir before the install path's own resolution
    # block runs. Defaults must match the install side below.
    UNINSTALL_SYSTEM_DIR="${CREMIND_SYSTEM_DIR:-$HOME/.cremind}"
    if [ -n "${CREMIND_INSTALL_DIR:-}" ]; then
        UNINSTALL_INSTALL_DIR="$CREMIND_INSTALL_DIR"
    else
        case "$(uname -s)" in
            Darwin) UNINSTALL_INSTALL_DIR="$HOME/Library/Application Support/Cremind" ;;
            *)      UNINSTALL_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cremind" ;;
        esac
    fi

    if [ ! -d "$UNINSTALL_SYSTEM_DIR" ] && [ ! -d "$UNINSTALL_INSTALL_DIR" ]; then
        echo "Nothing to uninstall — neither $UNINSTALL_SYSTEM_DIR nor $UNINSTALL_INSTALL_DIR exists."
        exit 0
    fi

    # Detect install kind from on-disk markers.
    KIND=""
    if [ -f "$UNINSTALL_INSTALL_DIR/docker/docker-compose.yml" ]; then
        KIND=docker
    elif [ -d "$UNINSTALL_SYSTEM_DIR/venv" ]; then
        KIND=native
    elif [ -d "$UNINSTALL_INSTALL_DIR" ]; then
        # Install Dir exists but no compose file and no venv — partial install
        # (script crashed mid-run, or already partly uninstalled). Treat as
        # native so we still clean up whatever's there.
        KIND=native
    else
        echo "Unrecognized install layout." >&2
        echo "Expected $UNINSTALL_SYSTEM_DIR/venv (native) or $UNINSTALL_INSTALL_DIR/docker/docker-compose.yml (docker)." >&2
        exit 1
    fi

    # Interactive prompt if no mode flag passed.
    if [ -z "$UNINSTALL_MODE" ]; then
        printf 'Uninstall Cremind (%s):\n' "$KIND"
        printf '  System Dir:  %s\n' "$UNINSTALL_SYSTEM_DIR"
        printf '  Install Dir: %s\n\n' "$UNINSTALL_INSTALL_DIR"
        printf '  [k] Keep data    — remove the binaries + install scratch; preserve System Dir contents\n'
        printf '                     (.env, bootstrap.toml, storage/, tokens/, profile dirs)\n'
        printf '  [p] Purge all    — delete everything Cremind installed'
        if [ "$KIND" = "docker" ]; then
            printf ' (incl. Docker volumes)'
        fi
        printf '\n'
        printf '  [c] Cancel\n\n> '
        read -r ans
        case "$ans" in
            k|K) UNINSTALL_MODE=keep ;;
            p|P) UNINSTALL_MODE=purge ;;
            *)
                echo "Cancelled."
                exit 0
                ;;
        esac
    fi

    # Pre-purge guard: probe user_working_dir for the post-purge note.
    # Best-effort — only runs when sqlite3 is on PATH.
    USER_DIR=""
    if [ "$UNINSTALL_MODE" = "purge" ] && command -v sqlite3 >/dev/null 2>&1; then
        USER_DIR=$(sqlite3 "$UNINSTALL_SYSTEM_DIR/storage/cremind.db" \
            "select value from server_config where key='user_working_dir'" 2>/dev/null || true)
    fi

    # ── Docker pre-check (Docker-mode installs only) ─────────────────────
    #
    # Runs BEFORE process-kill / file removal so the user can Cancel without
    # leaving the install half-torn-down. Returns 0 (daemon up), 1 (daemon
    # down), or 2 (docker not installed).
    docker_daemon_status() {
        command -v docker >/dev/null 2>&1 || return 2
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        return 1
    }

    # TTY: interactive r/f/c prompt. Non-TTY (Electron IPC, NSIS-style spawn):
    # write a structured marker to stderr and exit 3. The renderer's existing
    # onUninstallDone listener surfaces the exit code so the user knows to
    # start Docker and re-run.
    resolve_docker_offline() {
        if [ -t 0 ]; then
            while :; do
                printf '\nDocker daemon is not running.\n'
                printf '  [r] Retry after starting Docker\n'
                printf '  [f] Force-remove (orphans containers/volumes; clean up later with: docker compose -p cremind down -v)\n'
                printf '  [c] Cancel uninstall\n> '
                read -r ans
                case "$ans" in
                    r|R)
                        if docker_daemon_status; then
                            return 0
                        fi
                        ;;
                    f|F)
                        FORCE_REMOVE=1
                        echo "Force-removing Cremind without Docker cleanup."
                        echo "Run 'docker compose -p cremind down -v' later to remove orphaned containers and volumes." >&2
                        return 0
                        ;;
                    c|C)
                        echo "Uninstall cancelled - Docker is required for Docker-mode cleanup." >&2
                        exit 3
                        ;;
                    *)
                        echo "Please answer r, f, or c."
                        ;;
                esac
            done
        else
            echo "ERROR: Docker daemon is not running. Start Docker and re-run the uninstall," >&2
            echo "       or run 'bash install/install.sh --uninstall --purge' from a terminal to force-remove." >&2
            exit 3
        fi
    }

    FORCE_REMOVE=0
    DOCKER_INSTALLED=1
    if [ "$KIND" = "docker" ]; then
        docker_daemon_status
        case $? in
            0)
                : # daemon up - proceed
                ;;
            2)
                echo "Docker not installed on PATH; skipping container cleanup."
                DOCKER_INSTALLED=0
                ;;
            *)
                resolve_docker_offline
                ;;
        esac
    fi

    # Stop any backend tracked via install.pid. Best-effort — a stale PID
    # file from a crashed install just no-ops here.
    if [ -f "$UNINSTALL_INSTALL_DIR/install.pid" ]; then
        pid=$(cat "$UNINSTALL_INSTALL_DIR/install.pid" 2>/dev/null || true)
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    # Also stop the long-running server (writes server.pid in System Dir
    # after Setup Wizard completes).
    if [ -f "$UNINSTALL_SYSTEM_DIR/server.pid" ]; then
        pid=$(cat "$UNINSTALL_SYSTEM_DIR/server.pid" 2>/dev/null || true)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    fi

    # Docker container/volume cleanup. Only runs when the daemon is reachable
    # AND the user didn't pick force-remove at the pre-check above.
    if [ "$KIND" = "docker" ] && [ "$DOCKER_INSTALLED" -eq 1 ] && [ "$FORCE_REMOVE" -eq 0 ]; then
        if [ -d "$UNINSTALL_INSTALL_DIR/docker" ]; then
            if [ "$UNINSTALL_MODE" = "purge" ]; then
                echo "Stopping containers and removing volumes..."
                (cd "$UNINSTALL_INSTALL_DIR/docker" && docker compose -p cremind down -v --remove-orphans) || true
            else
                echo "Stopping containers (volumes preserved)..."
                (cd "$UNINSTALL_INSTALL_DIR/docker" && docker compose -p cremind down --remove-orphans) || true
            fi
        fi
    fi

    # Remove PATH-marker blocks from shell rc files. The install script writes
    # a `# >>> cremind installer >>>` / `# <<< cremind installer <<<` marker pair
    # around the PATH export. sed -i.bak then rm -f .bak works on both GNU
    # sed and BSD sed (macOS).
    PATH_MARKER_BEGIN='# >>> cremind installer >>>'
    PATH_MARKER_END='# <<< cremind installer <<<'
    for rc in \
        "$HOME/.bashrc" \
        "$HOME/.zshrc" \
        "$HOME/.bash_profile" \
        "$HOME/.profile" \
        "$HOME/.config/fish/config.fish"
    do
        if [ -f "$rc" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rc" 2>/dev/null; then
            sed -i.bak "/^${PATH_MARKER_BEGIN}\$/,/^${PATH_MARKER_END}\$/d" "$rc" 2>/dev/null || true
            rm -f "$rc.bak"
            echo "Removed PATH block from $rc"
        fi
    done

    # Apply the chosen mode.
    if [ "$UNINSTALL_MODE" = "purge" ]; then
        # Belt-and-suspenders: refuse to purge a stray empty or root path.
        case "$UNINSTALL_SYSTEM_DIR" in
            /|"")
                echo "Refusing to purge a root-like System Dir: '$UNINSTALL_SYSTEM_DIR'" >&2
                exit 1
                ;;
        esac
        case "$UNINSTALL_INSTALL_DIR" in
            /|"")
                echo "Refusing to purge a root-like Install Dir: '$UNINSTALL_INSTALL_DIR'" >&2
                exit 1
                ;;
        esac
        [ -d "$UNINSTALL_SYSTEM_DIR" ]  && rm -rf "$UNINSTALL_SYSTEM_DIR"  && echo "Removed $UNINSTALL_SYSTEM_DIR."
        [ -d "$UNINSTALL_INSTALL_DIR" ] && rm -rf "$UNINSTALL_INSTALL_DIR" && echo "Removed $UNINSTALL_INSTALL_DIR."
        if [ -n "$USER_DIR" ]; then
            case "$USER_DIR" in
                "$UNINSTALL_SYSTEM_DIR"|"$UNINSTALL_SYSTEM_DIR"/*) ;;
                *) echo "User Working Directory preserved at: $USER_DIR" ;;
            esac
        fi
    else
        # Keep mode: remove install scratch + Native binaries; preserve System
        # Dir contents (.env, bootstrap.toml, storage/, tokens/, profile dirs).
        rm -rf \
            "$UNINSTALL_SYSTEM_DIR/venv" \
            "$UNINSTALL_SYSTEM_DIR/bin" \
            "$UNINSTALL_SYSTEM_DIR/server.pid"
        # Wipe the Install Dir wholesale — it's all install scratch.
        [ -d "$UNINSTALL_INSTALL_DIR" ] && rm -rf "$UNINSTALL_INSTALL_DIR" && echo "Removed install scratch at $UNINSTALL_INSTALL_DIR."
        echo "Kept data in $UNINSTALL_SYSTEM_DIR (.env, bootstrap.toml, storage/, tokens/, profile dirs)."
    fi

    echo "Uninstall complete."
    exit 0
fi

# ``container`` was a separate deployment in earlier installs; it's now a
# narrow case of ``custom`` (listen on 0.0.0.0, URLs at localhost). Accept
# the old name for one release as an alias with sensible defaults so
# existing one-liners don't break, and warn so users update their scripts.
if [ "$DEPLOYMENT" = "container" ]; then
    warn "--deployment container is deprecated; using --deployment custom with container defaults."
    DEPLOYMENT="custom"
    : "${CUSTOM_listen_host:=0.0.0.0}"
    : "${CUSTOM_public_url:=http://localhost:1515}"
    : "${CUSTOM_wizard_preset:=local}"
fi

# ── channel validation / dev-mode guards ──────────────────────────────────

case "$CHANNEL" in
    production|test|dev) ;;
    *) err "Invalid --channel: $CHANNEL (must be production, test, or dev)"; exit 2 ;;
esac

if [ "$CHANNEL" = "dev" ]; then
    if [ -z "$REPO_ROOT" ] || [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
        err "--channel dev requires running install.sh from a checkout (not piped via curl)."
        err "Usage: bash <repo>/install/install.sh --channel dev"
        exit 2
    fi
fi

# ── --version validation ──────────────────────────────────────────────────
#
# Two layers of check:
#   1. Channel-shape — production = X.Y.Z, test = X.Y.ZrcN. Dev ignores
#      --version entirely (editable install).
#   2. Electron-line — only when --electron-version is also provided.
#      Production: exact match. Test: same X.Y.Z, rcN suffix.
#
# The error message is the one the Cremind desktop app surfaces in its
# install log, so it carries the user-visible context (which Electron
# build is rejecting the spec).
if [ -n "$VERSION_SPEC" ] && [ "$CHANNEL" = "dev" ]; then
    warn "--version is ignored on dev channel (editable install)."
    VERSION_SPEC=""
fi
if [ -n "$VERSION_SPEC" ]; then
    case "$CHANNEL" in
        production)
            if ! printf '%s' "$VERSION_SPEC" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
                err "Invalid version: '$VERSION_SPEC' does not look like a production release (expected X.Y.Z)."
                exit 2
            fi
            ;;
        test)
            # Fast fail-on-bad-arg shape check. The per-PR dev form
            # X.Y.ZrcN.devM is valid on test. The authoritative rules +
            # ordering live in app/upgrade/channel.py (invoked by the
            # resolver below); a parity test guards this against drift.
            if ! printf '%s' "$VERSION_SPEC" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+rc[0-9]+(\.dev[0-9]+)?$'; then
                err "Invalid version: '$VERSION_SPEC' does not look like a test prerelease (expected X.Y.ZrcN[.devM])."
                exit 2
            fi
            ;;
    esac
fi
if [ -n "$VERSION_SPEC" ] && [ -n "$ELECTRON_VERSION" ]; then
    case "$CHANNEL" in
        production)
            if [ "$VERSION_SPEC" != "$ELECTRON_VERSION" ]; then
                err "Invalid version: '$VERSION_SPEC' is not a valid production release for this Electron build (v$ELECTRON_VERSION). Production requires an exact version match — use the in-app update flow to install a different version."
                exit 2
            fi
            ;;
        test)
            # No line lock on test: any published RC of any line is
            # installable on a test build (the shell auto-updates
            # independently). The shape check above already validated the
            # X.Y.ZrcN[.devM] form; nothing line-specific to enforce here.
            :
            ;;
    esac
fi

# Detect a containerized host (Docker / Podman / k8s pod). When the
# installer runs inside a container, ``local`` deployment binds to
# 127.0.0.1 inside the container — unreachable from the docker host's
# browser even with ``-p 1515:1515``. We use this to default unattended
# installs to ``custom`` (with container-friendly field values) and to
# steer the interactive prompt at ``custom`` instead of ``local``.
IN_CONTAINER=0
if [ -f /.dockerenv ] || [ -f /run/.containerenv ] \
        || (grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null); then
    IN_CONTAINER=1
fi

if [ "$UNATTENDED" -eq 1 ] && [ -z "$DEPLOYMENT" ]; then
    if [ "$IN_CONTAINER" -eq 1 ]; then
        DEPLOYMENT="custom"
        : "${CUSTOM_listen_host:=0.0.0.0}"
        : "${CUSTOM_public_url:=http://localhost:1515}"
        : "${CUSTOM_wizard_preset:=local}"
    else
        DEPLOYMENT="local"
    fi
fi
if [ "$UNATTENDED" -eq 1 ] && [ -z "$APP_HOST" ] && [ "$DEPLOYMENT" = "server" ]; then
    err "--unattended with --deployment=server requires --host"
    exit 2
fi
# --unattended implies "yes" for the auto-install prompt unless the
# operator explicitly said otherwise.
if [ "$UNATTENDED" -eq 1 ] && [ -z "$AUTO_INSTALL_PYTHON" ]; then
    AUTO_INSTALL_PYTHON=1
fi

# ── paths ─────────────────────────────────────────────────────────────────

# Cremind System Directory — runtime state + user content root. Defaults to
# ~/.cremind so the layout users see hasn't changed since v0.1.9-test55.
# Override via CREMIND_SYSTEM_DIR so power users can install side-by-side
# (e.g., a staging copy at ~/.cremind-staging).
CREMIND_SYSTEM_DIR="${CREMIND_SYSTEM_DIR:-$HOME/.cremind}"
_DEFAULT_CREMIND_SYSTEM_DIR="$HOME/.cremind"

# Cremind Install Directory — install-time scratch (install.log, install.pid,
# docker compose bundle, pip/uv caches, downloaded Python). Platform-
# conventional default so install artifacts never pollute ~/.cremind.
if [ -z "${CREMIND_INSTALL_DIR:-}" ]; then
    case "$(uname -s)" in
        Darwin) CREMIND_INSTALL_DIR="$HOME/Library/Application Support/Cremind" ;;
        *)      CREMIND_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cremind" ;;
    esac
fi

# Paths in the System Dir (preserved across uninstall --keep)
VENV_DIR="$CREMIND_SYSTEM_DIR/venv"
ENV_FILE="$CREMIND_SYSTEM_DIR/.env"
BOOTSTRAP_FILE="$CREMIND_SYSTEM_DIR/bootstrap.toml"
BIN_DIR="$CREMIND_SYSTEM_DIR/bin"
UV_BIN="$BIN_DIR/uv"

# Paths in the Install Dir (scratch — safely deletable on uninstall --keep)
LOG_FILE="$CREMIND_INSTALL_DIR/install.log"

# Default MODIFY_PATH: only modify shell rc for the canonical System Dir,
# so a staging/test install at a custom CREMIND_SYSTEM_DIR doesn't clobber
# the prod PATH entry. --modify-path / --no-modify-path override.
if [ -z "$MODIFY_PATH" ]; then
    if [ "$CREMIND_SYSTEM_DIR" = "$_DEFAULT_CREMIND_SYSTEM_DIR" ]; then
        MODIFY_PATH=1
    else
        MODIFY_PATH=0
    fi
fi

# Scope pip's HTTP + wheel cache under the Install Dir so a user-driven
# wipe of the System Dir (or --reinstall) doesn't lose the cache, and a
# wipe of the Install Dir clears stale index responses cleanly. Without
# this, pip uses ~/.cache/pip/, which persists across cremind reinstalls
# and has bitten us when re-resolving the latest pre-release.
export PIP_CACHE_DIR="$CREMIND_INSTALL_DIR/pip-cache"

# The Install Dir is always needed (install.log, docker compose bundle,
# uv/pip caches). The System Dir is only created for native installs —
# Docker installs keep runtime state inside the ``cremind-data`` named
# volume and never touch the host's ~/.cremind.
mkdir -p "$CREMIND_INSTALL_DIR"

# Forward both to the Python backend so its settings layer agrees with the
# install script on path resolution (subprocess inherits these).
export CREMIND_SYSTEM_DIR CREMIND_INSTALL_DIR

# Templates are fetched at install time so we don't need to ship them
# alongside the script. Production/test fetch from GitHub; dev reads from
# the checkout's install/templates/ directly (treat them as files, not
# URLs). CREMIND_TEMPLATE_BASE overrides the remote URL for testing.
if [ "$CHANNEL" = "dev" ]; then
    TEMPLATE_BASE="file://$REPO_ROOT/install/templates"
else
    TEMPLATE_BASE="${CREMIND_TEMPLATE_BASE:-https://raw.githubusercontent.com/cremind-ai/cremind/main/install/templates}"
fi

# Helper: emit a template to stdout, branching on file:// vs http(s)://.
# Centralizes the curl-vs-cat plumbing so every caller reads from one verb.
fetch_template() {
    local name="$1"
    case "$TEMPLATE_BASE" in
        file://*) cat "${TEMPLATE_BASE#file://}/$name" ;;
        *)        curl -fsSL "$TEMPLATE_BASE/$name" ;;
    esac
}

# The catalog (deployments / modes / mode rules / labels) lives next to
# install.sh in the repo and is fetched alongside templates at install
# time. Source it now so every prompt and rendered .env can read its
# variables. ``CREMIND_CATALOG_BASE`` overrides the URL for testing.
# Prefer the checkout's own catalog + TUI when running from a clone (dev,
# or test/prod from source): they're the exact assets being tested and
# avoid fetching a possibly-stale copy from the default ref. curl|bash runs
# have no REPO_ROOT and fall through to the remote download.
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/install/_catalog.sh" ]; then
    CATALOG_BASE="$REPO_ROOT/install"
    # shellcheck disable=SC1090
    . "$CATALOG_BASE/_catalog.sh"
    CATALOG_JSON="$CATALOG_BASE/_catalog.json"
    INSTALLER_TUI_PYZ="$CATALOG_BASE/installer_tui.pyz"
    [ -f "$CATALOG_JSON" ] || CATALOG_JSON=""
    [ -f "$INSTALLER_TUI_PYZ" ] || INSTALLER_TUI_PYZ=""
else
    CATALOG_BASE="${CREMIND_CATALOG_BASE:-https://raw.githubusercontent.com/cremind-ai/cremind/main/install}"
    CATALOG_TMP="$(mktemp 2>/dev/null || echo "$CREMIND_INSTALL_DIR/_catalog.sh")"
    if ! curl -fsSL "$CATALOG_BASE/_catalog.sh" -o "$CATALOG_TMP"; then
        err "Failed to fetch install catalog from $CATALOG_BASE/_catalog.sh"
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$CATALOG_TMP"
    # Fetch the JSON catalog and TUI bundle alongside the shell catalog.
    # Both are optional: if the fetch fails we silently fall back to the
    # legacy numbered prompts.
    CATALOG_JSON="$CREMIND_INSTALL_DIR/_catalog.json"
    INSTALLER_TUI_PYZ="$CREMIND_INSTALL_DIR/installer_tui.pyz"
    if ! curl -fsSL "$CATALOG_BASE/_catalog.json" -o "$CATALOG_JSON" 2>/dev/null; then
        CATALOG_JSON=""
    fi
    if ! curl -fsSL "$CATALOG_BASE/installer_tui.pyz" -o "$INSTALLER_TUI_PYZ" 2>/dev/null; then
        INSTALLER_TUI_PYZ=""
    fi
fi

# ── version resolver (single source of truth) ──────────────────────────────
#
# Version / RC-tag parsing + PEP 440 ordering live in ONE place:
# app/upgrade/channel.py. bash can't import it, so we run that exact file
# standalone via ``python <channel.py> resolve``. This replaces the old
# version-sort pipeline, which wasn't PEP 440-aware and mis-ordered the
# per-PR dev form X.Y.ZrcN.devM (so dev wheels were dropped and the newest
# *legacy* rc wheel wrongly won).
CHANNEL_PY=""
ensure_resolver() {
    if [ -n "$CHANNEL_PY" ]; then printf '%s' "$CHANNEL_PY"; return 0; fi
    # Prefer the checkout's own channel.py when running from a clone (dev, or
    # ``--channel test`` from source): it's the exact version being tested and
    # avoids fetching a possibly-older copy from the default ref.
    if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/app/upgrade/channel.py" ]; then
        CHANNEL_PY="$REPO_ROOT/app/upgrade/channel.py"
        printf '%s' "$CHANNEL_PY"; return 0
    fi
    local src dest
    src="${CREMIND_CHANNEL_PY:-https://raw.githubusercontent.com/cremind-ai/cremind/main/app/upgrade/channel.py}"
    dest="$CREMIND_INSTALL_DIR/channel.py"
    if ! curl -fsSL "$src" -o "$dest"; then
        err "Failed to fetch the version resolver from $src"
        exit 1
    fi
    CHANNEL_PY="$dest"
    printf '%s' "$CHANNEL_PY"
}

# resolve_cremind_wheel <channel> <emit:url|version> [line]
# Reads a simple-index HTML page on stdin; prints the chosen wheel URL or
# PEP 440 version (empty + non-zero exit when nothing matches). Ordering and
# channel/line filtering are done by the canonical resolver, not here.
resolve_cremind_wheel() {
    local rchan="$1" emit="$2" line="${3:-}" py resolver
    py="${PYTHON:-python3}"
    resolver="$(ensure_resolver)"
    if [ -n "$line" ]; then
        "$py" "$resolver" resolve --channel "$rchan" --print "$emit" --line "$line"
    else
        "$py" "$resolver" resolve --channel "$rchan" --print "$emit"
    fi
}

# ── banner ────────────────────────────────────────────────────────────────

# Single-quoted heredoc: every char ($, \, backtick, apostrophe) is literal.
cat <<'LOGO'
                            xrjjjjjjjjjrrxc
                      xrjfffjrx        jxjjfffjx
                   rjffj1                     jjfjr:
                rjfjj         *W8%%%%%%8&*       /ffjx
              rffj        M%%%BBBBBB@@@B%%%BB&      jjjj1
            jjjr       o%%%BB@@@@@@@@@@@BBB%%%B@8     rjjr
           jfx       C8%%B@@@@@@@@@@@BBBBBB%%%%%B@&     cjjr
         jfj      LJOB%B@@@@@@@@@BBBB%%%%%%88888%8BBQ     rjj
        jjx     xUYpBBB@@@@a|lllI;:,"^',tb&88888888%8C     ujj\
      :rjn     UYXq%%BB@v;;;:::,""^`'..      \M&&&&8%&YU    njjx
      jjj    cYXzZB%BB/,,""""^^``''.            CWW&8BmYU    xjr1
   $8%WU    YXzzcW%B8:^^`````'''..               ;*MW&*czY    :jj\
   &8&8%%M UzccvQ%%%>'''';UQ0Q].          ...     .*MWBXczXO   xjn
   &8&&8%B8cvvvu*%%k ...fX:```xu'      .-QZZ0U^    f#M%QvccX    rjn
   r8&W&&8%Wnuunp%8k                   11....iX;   ?*#8muvvvc   rjr
   fL&WWW&88%vxxn&%8>                              ]o*%Jnnunu    rj
  xfx&WMMWW&8%mrju888Y                             Ja*Wrxxxxn    ujn
  jjOM&MMMMMW&8*fff0WWWWv                        ;dka%zrrrrrrn   0jr
  rj  MW###MMWW&8ntt/j*M######Xl             ILbbbbo#ffjjjjjjr    jf
  fj   MM***##MMM8\\\(|00Um#*aaaaooooaahhhkkkbbbk#Q|//tttttfff    jj
  jj    M#*o****##ZoMbqmOLJYzuxjftjYwkaooabZYzYLmoQ|\\///////t    jj
  rfz    0#ooooooob@%%BB*bdpwmO0LCUYXzcccczzXUJLW%BQ||\\\\\||t   Ojj
  nfr    1)ooaaahhq@%%%%BBBBBBB@@BBBBBBBBBB%&WM*oaWhBM|((((((Y   nfn
   jjJ   )11roaahhx@%%%BBBBBB%%888&&&WWWMM##*oohdpZMW8Bn))))(    jj
   rjr    (1111111|@%%%%BBB%888&&&WWWMM##X0Uhahbw0hoMW8%f111|   rjr
   jjj     11111111B%%BBBB%%88&&&WWMM##*L0wZbhkwZQka#M&8B(1(    rfn
    zfr    |{{1{{{{uB%%%%%88&&&WWMMM##****M*akpmmQbho#W&%J1    jjn
     rfx    ){{{{{{1W%%%%88&&&WWWM###**oooahhdwwqLQha*M&88    nfr
      jjr    |{{{{{{Z%8%%8&&WWWMM###**oooaahkpqph){hho#W&%d  rfj
       rfr    :1{{{{}#%88&&WWWMM##**oooaaahkdqphY{[qha*M&%* jjj:
        rjr:    1{{{{f%88&WWMMM##***oaaaaahbddh#{{}XahoM&%orfr
         rfjx     {{{}f%8&WMM##***ooaaahhhkddao1{{{c*ha*W%rfr
           ffj\     ){}Z8&WM##***oaaaahhhkbbaM{{1(  aah*WMtx
            fffr       {0&WM#**ooahhhhhkkbkoW)1     a#hoW&
              fjfj\      o&W#*ooaahhhhhkkk*#b       fu*#&a
                :fffr     hWW#oaahhhhkkkoMa      nffffZh$
                   \jffjj   kMMoahhhhho#k    ujffjj
                       rjffffjjJoM#M*d rxjfjfjj1
                            Onjjjffffjjjr\
LOGO

cat <<EOF
${BOLD}Cremind installer${RESET}
${DIM}Logs: $LOG_FILE${RESET}

EOF

# Channel stamp: visible ONLY when non-production. End users never see it;
# CI / maintainers / devs do, and it tells them at a glance which path the
# installer is taking.
if [ "$CHANNEL" != "production" ]; then
    if [ "$CHANNEL" = "dev" ]; then
        printf '%s==>%s %schannel: dev (source: %s)%s\n\n' \
            "$BLUE$BOLD" "$RESET" "$DIM" "$REPO_ROOT" "$RESET"
    else
        printf '%s==>%s %schannel: %s%s\n\n' \
            "$BLUE$BOLD" "$RESET" "$DIM" "$CHANNEL" "$RESET"
    fi
fi

# ── detection ─────────────────────────────────────────────────────────────

step "Environment"

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Linux*)  OS=linux ;;
    Darwin*) OS=macos ;;
    *)
        err "Unsupported OS: $OS_NAME (this script handles Linux and macOS; use install.ps1 on Windows)"
        exit 1
        ;;
esac
ARCH="$(uname -m)"
ok "OS:   $OS ($ARCH)"

# curl is used for fetching templates, downloading uv, and the post-install
# health check. Fail fast with a clear hint if it's missing — the alternative
# is a confusing error several screens later.
if ! command -v curl >/dev/null 2>&1; then
    err "curl is required but was not found on PATH."
    cat <<EOF >&2

Install curl, then re-run this script:
  ${BOLD}macOS${RESET}: brew install curl   (usually preinstalled)
  ${BOLD}Ubuntu/Debian${RESET}: sudo apt install curl
  ${BOLD}Fedora/RHEL${RESET}: sudo dnf install curl
  ${BOLD}Alpine${RESET}: apk add curl

EOF
    exit 1
fi

# Find a Python 3.13+ interpreter. Try the most-specific name first so we
# don't accidentally pick up a system 3.10 named just `python3`.
find_system_python() {
    for candidate in python3.13 python3.14 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
            case "$ver" in
                3.13|3.14|3.15|3.16|3.17|3.18|3.19)
                    command -v "$candidate"
                    return 0
                    ;;
            esac
        fi
    done
    return 1
}
PYTHON="$(find_system_python || true)"

if [ -n "$PYTHON" ]; then
    ok "Python: $("$PYTHON" --version) at $PYTHON"
else
    info "Python: 3.13+ not found (will auto-install in native mode)"
fi

# Docker is detected so we can RECOMMEND it. The actual container bundle
# ships in Phase 3 — for now we explain and fall back.
HAS_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    HAS_DOCKER=1
    ok "Docker: detected (recommended in a future release)"
else
    info "Docker: not detected (or not running)"
fi

# ── TUI bootstrap ─────────────────────────────────────────────────────────

# Run the prompt_toolkit-based installer to gather channel, version,
# deployment, mode, and custom-field selections in one keyboard-driven
# flow — replacing the legacy ``read -p`` prompts that follow.
#
# The TUI is OPTIONAL: any failure (missing bundle, no /dev/tty, --no-tui,
# --unattended, uv unavailable) drops through to the legacy prompts so the
# installer never blocks on a UX upgrade.
#
# Communication is one-way via a temp file the TUI writes as KEY=VALUE.
# Sourcing that file pre-populates DEPLOYMENT / MODE / VERSION_SPEC etc.
# so the existing ``if [ -z "$X" ]`` guards in the prompt blocks below
# skip cleanly.
tui_run_bootstrap() {
    [ "$UNATTENDED" -eq 1 ] && return 0
    [ "$NO_TUI" = "1" ] && return 0
    [ ! -e /dev/tty ] && return 0
    [ -z "$INSTALLER_TUI_PYZ" ] && return 0
    [ ! -f "$INSTALLER_TUI_PYZ" ] && return 0
    [ -z "$CATALOG_JSON" ] && return 0
    [ ! -f "$CATALOG_JSON" ] && return 0

    # Decide how to launch the TUI:
    #   1. Dev channel from a checkout — reuse the project's .venv. It
    #      already has prompt_toolkit + rich (core deps in pyproject.toml),
    #      so we don't need uv at all.
    #   2. Otherwise — install a private uv and let it manage Python + the
    #      two TUI deps in an ephemeral env.
    local dev_python=""
    if [ "$CHANNEL" = "dev" ] && [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/.venv/bin/python" ]; then
        dev_python="$REPO_ROOT/.venv/bin/python"
    fi

    if [ -z "$dev_python" ] && [ ! -x "$UV_BIN" ]; then
        mkdir -p "$BIN_DIR"
        if ! curl -LsSf https://astral.sh/uv/install.sh \
                | env UV_INSTALL_DIR="$BIN_DIR" UV_UNMANAGED_INSTALL="$BIN_DIR" \
                      INSTALLER_NO_MODIFY_PATH=1 sh \
                >>"$LOG_FILE" 2>&1; then
            warn "Could not download uv for TUI; falling back to text prompts."
            return 0
        fi
        if [ ! -x "$UV_BIN" ]; then
            warn "uv installer ran but $UV_BIN is missing; falling back to text prompts."
            return 0
        fi
    fi

    local tui_out
    tui_out="$(mktemp 2>/dev/null || echo "$CREMIND_INSTALL_DIR/.tui-out")"
    # shellcheck disable=SC2064
    trap "rm -f \"$tui_out\"" RETURN

    info "Launching installer TUI (Enter continues, Esc cancels, Ctrl+C force-quits)"
    local rc=0
    if [ -n "$dev_python" ]; then
        "$dev_python" "$INSTALLER_TUI_PYZ" \
            --output "$tui_out" \
            --catalog "$CATALOG_JSON" \
            --channel "$CHANNEL" \
            --deployment "$DEPLOYMENT" \
            --mode "$MODE" \
            --desktop "$DESKTOP_UI" \
            --version "$VERSION_SPEC" \
            --host "$APP_HOST" \
            --listen-host "$CUSTOM_listen_host" \
            --public-url "$CUSTOM_public_url" \
            --allowed-origins "$CUSTOM_allowed_origins" \
            --wizard-preset "$CUSTOM_wizard_preset" \
            --electron-version "$ELECTRON_VERSION" \
            --in-container "$IN_CONTAINER" \
            --has-docker "$HAS_DOCKER" \
            </dev/tty >/dev/tty || rc=$?
    else
        "$UV_BIN" run --quiet --python 3.13 \
            --with prompt_toolkit --with rich \
            python "$INSTALLER_TUI_PYZ" \
            --output "$tui_out" \
            --catalog "$CATALOG_JSON" \
            --channel "$CHANNEL" \
            --deployment "$DEPLOYMENT" \
            --mode "$MODE" \
            --desktop "$DESKTOP_UI" \
            --version "$VERSION_SPEC" \
            --host "$APP_HOST" \
            --listen-host "$CUSTOM_listen_host" \
            --public-url "$CUSTOM_public_url" \
            --allowed-origins "$CUSTOM_allowed_origins" \
            --wizard-preset "$CUSTOM_wizard_preset" \
            --electron-version "$ELECTRON_VERSION" \
            --in-container "$IN_CONTAINER" \
            --has-docker "$HAS_DOCKER" \
            </dev/tty >/dev/tty || rc=$?
    fi

    if [ "$rc" -eq 0 ]; then
        # shellcheck disable=SC1090
        . "$tui_out"
        ok "TUI selections applied."
    elif [ "$rc" -eq 1 ]; then
        err "Installer cancelled."
        exit 1
    else
        warn "TUI exited with code $rc; falling back to text prompts."
    fi
}

tui_run_bootstrap

# ── deployment type ───────────────────────────────────────────────────────

step "Deployment"

# Render the deployment options from the catalog so install.sh,
# install.ps1, and the Setup Wizard share the same prompts.
# DEPLOYMENT_IDS is set in _catalog.sh; the indirect ``${!varname}``
# lookups read DEPLOYMENT_LABEL_<id> / DEPLOYMENT_DESC_<id> for each.
if [ -z "$DEPLOYMENT" ]; then
    if [ "$IN_CONTAINER" -eq 1 ]; then
        cat <<EOF
${YELLOW}${BOLD}Detected: this installer is running inside a container.${RESET}
${DIM}Pick ${BOLD}custom${RESET}${DIM} — ${BOLD}local${RESET}${DIM} would bind to 127.0.0.1 inside the
container, which is unreachable from the docker host's browser even
with -p forwarding.${RESET}

EOF
        default_choice="custom"
    else
        default_choice="local"
    fi
    echo "How will you run Cremind?"
    idx=0
    default_idx=1
    for d_id in $DEPLOYMENT_IDS; do
        idx=$((idx + 1))
        [ "$d_id" = "$default_choice" ] && default_idx=$idx
        label_var="DEPLOYMENT_LABEL_$d_id"; label="${!label_var}"
        desc_var="DEPLOYMENT_DESC_$d_id";   desc="${!desc_var}"
        printf '  %s%d)%s %s%s%s — %s\n' \
            "$BOLD" "$idx" "$RESET" "$BOLD" "$label" "$RESET" "$desc"
    done
    while :; do
        read -r -p "Choice [$default_idx]: " choice </dev/tty || choice=""
        choice="${choice:-$default_idx}"
        # Resolve numeric or name choice against DEPLOYMENT_IDS.
        DEPLOYMENT=""
        idx=0
        for d_id in $DEPLOYMENT_IDS; do
            idx=$((idx + 1))
            if [ "$choice" = "$idx" ] || [ "$choice" = "$d_id" ]; then
                DEPLOYMENT="$d_id"; break
            fi
        done
        [ -n "$DEPLOYMENT" ] && break
        warn "Pick a number 1-$idx or a deployment id."
    done
fi
ok "Deployment: $DEPLOYMENT"

# Guard against ``--deployment <unknown>`` flags slipping through.
DEPLOYMENT_KNOWN=0
for d_id in $DEPLOYMENT_IDS; do
    [ "$d_id" = "$DEPLOYMENT" ] && DEPLOYMENT_KNOWN=1
done
if [ "$DEPLOYMENT_KNOWN" -ne 1 ]; then
    err "Unknown deployment: $DEPLOYMENT (must be one of: $DEPLOYMENT_IDS)"
    exit 2
fi

if [ "$DEPLOYMENT" = "server" ] && [ -z "$APP_HOST" ]; then
    while :; do
        read -r -p "Public IP or domain (e.g. 100.120.175.90 or cremind.example.com): " APP_HOST </dev/tty || APP_HOST=""
        case "$APP_HOST" in
            "" ) warn "Required for server deployment." ;;
            *[![:alnum:].:-]* ) warn "Invalid characters; use letters, digits, dot, colon, hyphen." ;;
            *) break ;;
        esac
    done
fi
[ -n "$APP_HOST" ] && ok "Host: $APP_HOST"

# ── custom deployment fields ─────────────────────────────────────────────

# When the user picks ``custom``, walk the advanced-field array from the
# catalog and prompt for each one with its plain-English question + hint.
# Already-set values (from --listen-host etc., or from the deprecated
# container-deployment alias above) skip the prompt. In --unattended the
# catalog default is used silently.
if [ "$DEPLOYMENT" = "custom" ]; then
    echo
    info "Custom deployment — answer a few questions about how Cremind should be reached."
    for field in $CUSTOM_FIELD_IDS; do
        var_name="CUSTOM_$field"
        current="${!var_name}"
        if [ -n "$current" ]; then
            ok "$field: $current"
            continue
        fi
        prompt_var="CUSTOM_FIELD_PROMPT_$field"; prompt="${!prompt_var}"
        hint_var="CUSTOM_FIELD_HINT_$field";     hint="${!hint_var}"
        default_var="CUSTOM_FIELD_DEFAULT_$field"; default="${!default_var}"
        choices_var="CUSTOM_FIELD_CHOICES_$field"; choices="${!choices_var}"
        if [ "$UNATTENDED" -eq 1 ]; then
            eval "CUSTOM_$field=\"\$default\""
            ok "$field: $default (default)"
            continue
        fi
        echo
        printf '%s%s%s\n' "$BOLD" "$prompt" "$RESET"
        printf '%s%s%s\n' "$DIM" "$hint" "$RESET"
        if [ -n "$choices" ]; then
            printf '%sChoices: %s%s\n' "$DIM" "$choices" "$RESET"
        fi
        while :; do
            read -r -p "  [$default]: " answer </dev/tty || answer=""
            answer="${answer:-$default}"
            if [ -n "$choices" ]; then
                ok_choice=0
                for c in $choices; do
                    [ "$c" = "$answer" ] && ok_choice=1
                done
                if [ "$ok_choice" -ne 1 ]; then
                    warn "Pick one of: $choices"
                    continue
                fi
            fi
            eval "CUSTOM_$field=\"\$answer\""
            break
        done
    done
    # Fall back to a sensible value for allowed_origins when the user
    # left it blank — defaulting to "public URL + localhost variants"
    # keeps a copy-paste-the-URL-into-the-browser flow working without
    # asking the user to construct a CORS list by hand.
    if [ -z "$CUSTOM_allowed_origins" ]; then
        CUSTOM_allowed_origins="$CUSTOM_public_url,http://localhost:1515,http://127.0.0.1:1515"
    fi
fi

# ── mode (docker vs native) ──────────────────────────────────────────────

# Default: docker if available, native otherwise. We sandbox the agent in
# a desktop container by default — the user opts out explicitly if they
# want a native install. Both labels + descriptions come from the
# catalog ($MODE_IDS / MODE_LABEL_<id> / MODE_DESC_<id>).
if [ -z "$MODE" ]; then
    if [ "$HAS_DOCKER" -eq 1 ]; then
        if [ "$UNATTENDED" -eq 1 ]; then
            MODE="docker"
        else
            echo
            printf '%sHow do you want to run Cremind?%s\n' "$BOLD" "$RESET"
            idx=0
            for m_id in $MODE_IDS; do
                idx=$((idx + 1))
                label_var="MODE_LABEL_$m_id"; label="${!label_var}"
                desc_var="MODE_DESC_$m_id";   desc="${!desc_var}"
                hint_var="MODE_HINT_$m_id";   hint="${!hint_var}"
                printf '  %s%d)%s %s%s%s — %s\n' \
                    "$BOLD" "$idx" "$RESET" "$BOLD" "$label" "$RESET" "$desc"
                [ -n "$hint" ] && printf '                  %s%s%s\n' "$DIM" "$hint" "$RESET"
            done
            while :; do
                read -r -p "Choice [1]: " choice </dev/tty || choice=""
                choice="${choice:-1}"
                MODE=""
                idx=0
                for m_id in $MODE_IDS; do
                    idx=$((idx + 1))
                    if [ "$choice" = "$idx" ] || [ "$choice" = "$m_id" ]; then
                        MODE="$m_id"; break
                    fi
                done
                [ -n "$MODE" ] && break
                warn "Pick a number 1-$idx or a mode id."
            done
        fi
    else
        MODE="native"
    fi
fi
ok "Mode: $MODE"

if [ "$MODE" = "docker" ] && [ "$HAS_DOCKER" -eq 0 ]; then
    err "Docker mode requested but Docker is not available."
    exit 1
fi

# ── desktop UI (docker mode only) ─────────────────────────────────────────
#
# Docker installs choose an image flavor: the desktop image
# (cremind/cremind-desktop, XFCE + VNC) or the basic headless image
# (cremind/cremind). Default is desktop everywhere, including --unattended.
# A re-install reads the previous choice from the existing docker/.env
# (CREMIND_IMAGE) so an unattended re-run preserves the flavor.
if [ "$MODE" = "docker" ] && [ -z "$DESKTOP_UI" ]; then
    # Sticky default from a prior install: cremind/cremind → basic (0),
    # anything else / no prior install → desktop (1).
    desktop_default=1
    prev_env="$CREMIND_INSTALL_DIR/docker/.env"
    if [ -f "$prev_env" ]; then
        prev_image="$(sed -n 's/^CREMIND_IMAGE=//p' "$prev_env" | head -n 1)"
        [ "$prev_image" = "cremind/cremind" ] && desktop_default=0
    fi

    if [ "$UNATTENDED" -eq 1 ]; then
        DESKTOP_UI="$desktop_default"
    else
        echo
        printf '%s%s%s\n' "$BOLD" "$DOCKER_DESKTOP_PROMPT" "$RESET"
        [ -n "$DOCKER_DESKTOP_HINT" ] && printf '  %s%s%s\n' "$DIM" "$DOCKER_DESKTOP_HINT" "$RESET"
        if [ "$desktop_default" -eq 1 ]; then
            yn_hint="[Y/n]"
        else
            yn_hint="[y/N]"
        fi
        while :; do
            read -r -p "Install the VNC Desktop UI? $yn_hint: " ans </dev/tty || ans=""
            if [ -z "$ans" ]; then
                DESKTOP_UI="$desktop_default"; break
            fi
            case "$ans" in
                [Yy]|[Yy][Ee][Ss]) DESKTOP_UI=1; break ;;
                [Nn]|[Nn][Oo])     DESKTOP_UI=0; break ;;
                *) warn "Please answer yes or no." ;;
            esac
        done
    fi
fi
if [ "$MODE" = "docker" ]; then
    if [ "$DESKTOP_UI" = "0" ]; then
        ok "Desktop UI: no (basic headless image)"
    else
        ok "Desktop UI: yes (VNC desktop image)"
    fi
fi

# ── docker install ────────────────────────────────────────────────────────

# Random secret generator. /dev/urandom + tr keeps us within the printable
# alnum set so passwords paste cleanly through web forms and shells.
gen_secret() {
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true
    echo
}

# Filter a rendered template on stdin: pass through unchanged for a desktop
# install, or strip the ``# >>> desktop-only >>>`` … ``# <<< desktop-only <<<``
# marker blocks (noVNC/VNC ports + VNC env) for a basic install. Uses POSIX
# character classes so it works with both GNU and BSD sed (macOS).
maybe_strip_desktop() {
    if [ "$DESKTOP_UI" = "0" ]; then
        sed '/^[[:space:]]*# >>> desktop-only >>>/,/^[[:space:]]*# <<< desktop-only <<</d'
    else
        cat
    fi
}

# Pick the CREMIND_VERSION tag to bake into the rendered docker .env.
# Channel-driven:
#   production → latest stable from PyPI JSON
#   test       → latest pre-release from Test PyPI's simple index
#                (same regex the native test installer uses below)
#   dev        → literal 'dev'; the docker-compose.override.yml
#                rebuilds locally and re-tags so the value is only
#                a cosmetic image label
# Hard-fails on network errors for prod / test — the previous
# 'main' fallback silently mis-tagged the image and masked the
# failure behind ``docker compose up --build``.
resolve_version() {
    # Explicit --version short-circuits all lookups. Already validated
    # against the channel shape (and Electron line, when applicable) by
    # the top-level guards.
    if [ -n "$VERSION_SPEC" ] && [ "$CHANNEL" != "dev" ]; then
        ok "Using cremind==$VERSION_SPEC (--version)" >&2
        echo "$VERSION_SPEC"
        return
    fi

    case "$CHANNEL" in
        production)
            # When the Electron app drives the install, pin to its
            # build version. The Electron + cremind wheel are released
            # together under the same tag, and the tray-menu / taskbar
            # code lives in the Electron main process — drifting the
            # backend off the Electron line silently desyncs the UI.
            if [ -n "$ELECTRON_VERSION" ]; then
                ok "Pinning cremind==$ELECTRON_VERSION (Electron build version)" >&2
                echo "$ELECTRON_VERSION"
                return
            fi
            info "Resolving latest cremind version from PyPI" >&2
            local body v py
            body="$(curl -fsSL https://pypi.org/pypi/cremind/json)" || {
                err "Failed to fetch https://pypi.org/pypi/cremind/json"
                exit 1
            }
            # Python is the only reliable JSON parser we can count on
            # cross-distro. Prefer the discovered $PYTHON (3.10+), fall
            # back to the system ``python3`` (3.x is sufficient for
            # ``json.load``).
            py="${PYTHON:-python3}"
            v="$(printf '%s' "$body" | "$py" -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)" || {
                err "Failed to parse PyPI JSON for cremind (need python3 in PATH)"
                exit 1
            }
            if [ -z "$v" ]; then
                err "PyPI JSON returned no version for cremind"
                exit 1
            fi
            ok "Resolved cremind==$v from PyPI" >&2
            echo "$v"
            ;;
        test)
            info "Resolving latest cremind pre-release from Test PyPI" >&2
            local index v
            # Pick the newest matching wheel via the canonical resolver
            # (PEP 440 ordering incl. the rcN.devM dev form). When
            # --electron-version is set, only that dev line is considered;
            # cross-line picks like 0.1.10rc1 would silently desync UI
            # features from the backend on an Electron v0.1.9 build.
            index="$(curl -fsSL https://test.pypi.org/simple/cremind/)" || {
                err "Failed to fetch https://test.pypi.org/simple/cremind/"
                exit 1
            }
            v="$(printf '%s' "$index" | resolve_cremind_wheel test version "$ELECTRON_VERSION")"
            if [ -z "$v" ]; then
                if [ -n "$ELECTRON_VERSION" ]; then
                    err "No cremind wheel matching ${ELECTRON_VERSION}rcN[.devM] found at https://test.pypi.org/simple/cremind/ — has a test prerelease been published for this Electron build?"
                else
                    err "No cremind wheel found at https://test.pypi.org/simple/cremind/"
                fi
                exit 1
            fi
            ok "Resolved cremind==$v from Test PyPI" >&2
            echo "$v"
            ;;
        dev)
            # Dev mode rebuilds via docker-compose.override.yml; the tag is
            # only the local image label.
            echo "dev"
            ;;
        *)
            err "Unknown channel: $CHANNEL"
            exit 1
            ;;
    esac
}

if [ "$MODE" = "docker" ]; then
    step "Docker install"

    DOCKER_DIR="$CREMIND_INSTALL_DIR/docker"
    mkdir -p "$DOCKER_DIR"

    # Sidecar services (postgres / qdrant / chroma) are no longer
    # provisioned here — the Setup Wizard activates each one on demand
    # via its own per-service deployment-mode picker.
    #
    # Bundle regeneration is unconditional. Channel-dependent fields
    # (CREMIND_VERSION, CREMIND_UPGRADE_CHANNEL, CREMIND_PIP_INDEX_URL) all
    # drift if the .env is reused across runs, and a previous-channel
    # docker-compose.override.yml silently keeps a stale ``build:``
    # context alive on dev→test/prod switches. Regenerating from
    # templates on every run is the only way to keep state honest;
    # VNC_PASSWORD is the only true secret here and the installer
    # surfaces it at the end of the run, so re-rolling it is cheap.
    if [ -f "$DOCKER_DIR/docker-compose.yml" ]; then
        info "Regenerating $DOCKER_DIR config (templates re-render every run)"
    fi

    # Image flavor from the desktop-UI choice (default desktop). BUILD_TARGET
    # selects the Dockerfile stage for the dev-channel local build; IMAGE_REPO
    # is the Docker Hub repo written into .env as CREMIND_IMAGE.
    if [ "$DESKTOP_UI" = "0" ]; then
        IMAGE_REPO="cremind/cremind"
        BUILD_TARGET="basic"
        VNC_PASSWORD=""
    else
        IMAGE_REPO="cremind/cremind-desktop"
        BUILD_TARGET="desktop"
        VNC_PASSWORD="$(gen_secret)"
    fi
    CREMIND_VERSION="$(resolve_version)"

    case "$DEPLOYMENT" in
        local)
            DOCKER_APP_URL="http://localhost:1515"
            DOCKER_CORS="http://localhost:1515,http://127.0.0.1:1515"
            DOCKER_WIZARD_ENV="local"
            ;;
        server)
            DOCKER_APP_URL="http://$APP_HOST:1515"
            DOCKER_CORS="http://$APP_HOST:1515,http://localhost:1515"
            DOCKER_WIZARD_ENV="server"
            ;;
        custom)
            DOCKER_APP_URL="$CUSTOM_public_url"
            DOCKER_CORS="$CUSTOM_allowed_origins"
            DOCKER_WIZARD_ENV="$CUSTOM_wizard_preset"
            ;;
    esac

    # Dev channel: open CORS so ``npm run dev`` (Vite at localhost:5173)
    # and other ad-hoc dev origins can hit the API without preflight
    # failures. Production and test installs keep the locked-down list.
    if [ "$CHANNEL" = "dev" ]; then
        DOCKER_CORS="*"
    fi

    info "Fetching docker-compose template"
    # The cremind container's /root/.cremind lives in the ``cremind-data``
    # named Docker volume — isolated from the host. No path substitution
    # needed at render time; the volume is declared in the template's
    # ``volumes:`` block.
    fetch_template "docker-compose.yml.tmpl" | maybe_strip_desktop > "$DOCKER_DIR/docker-compose.yml"

    info "Writing $DOCKER_DIR/.env (secrets, do not commit)"
    fetch_template "docker.env.tmpl" \
        | sed \
            -e "s|__CREMIND_IMAGE__|$IMAGE_REPO|g" \
            -e "s|__CREMIND_VERSION__|$CREMIND_VERSION|g" \
            -e "s|__APP_URL__|$DOCKER_APP_URL|g" \
            -e "s|__CORS_ALLOWED_ORIGINS__|$DOCKER_CORS|g" \
            -e "s|__SETUP_WIZARD_ENV__|$DOCKER_WIZARD_ENV|g" \
            -e "s|__INSTALL_MODE__|$MODE|g" \
            -e "s|__VNC_PASSWORD__|$VNC_PASSWORD|g" \
        | maybe_strip_desktop \
        > "$DOCKER_DIR/.env"

    # Test channel: forward Test PyPI indices into the Dockerfile build
    # via the compose .env. Prod leaves both unset (Dockerfile treats
    # empty as default PyPI). Dev installs use ``-e /src`` via the
    # override file below, so they don't need pip index overrides.
    if [ "$CHANNEL" = "test" ]; then
        cat >>"$DOCKER_DIR/.env" <<EOF
CREMIND_PIP_INDEX_URL=https://test.pypi.org/simple/
CREMIND_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/
EOF
    fi

    # Stamp the channel into docker.env so the running container's
    # upgrader and feature installer see it. Without this, dev images
    # fall back to ``production`` semantics at runtime — which makes
    # ``pip_spec()`` pin to ``cremind==<version>`` and look up PyPI
    # for a release that may not be published yet during release prep.
    printf 'CREMIND_UPGRADE_CHANNEL=%s\n' "$CHANNEL" >>"$DOCKER_DIR/.env"
    chmod 600 "$DOCKER_DIR/.env"

    # Dev channel: emit a docker-compose.override.yml that points the
    # build context at the local checkout, switches the pip install
    # to ``-e /src``, and bind-mounts the checkout for runtime
    # imports. Compose auto-merges this when running from $DOCKER_DIR.
    #
    # Non-dev channels must remove any previously-written override:
    # a stale dev override silently re-adds a local ``build:`` context
    # that wins over the pulled image.
    if [ "$CHANNEL" = "dev" ]; then
        info "Writing $DOCKER_DIR/docker-compose.override.yml (bind-mounts $REPO_ROOT at /src)"
        fetch_template "docker-compose.override.yml.tmpl" \
            | sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
                  -e "s|__CREMIND_BUILD_TARGET__|$BUILD_TARGET|g" \
            > "$DOCKER_DIR/docker-compose.override.yml"
    elif [ -f "$DOCKER_DIR/docker-compose.override.yml" ]; then
        info "Removing stale docker-compose.override.yml (dev-only)"
        rm -f "$DOCKER_DIR/docker-compose.override.yml"
    fi

    ok "Wrote $DOCKER_DIR/docker-compose.yml + .env"

    # Per-channel pull / build strategy:
    #   production / test → pull the pre-built image from Docker Hub
    #                       and refuse to fall back to a local build.
    #                       The main compose template has no ``build:``
    #                       section, so a missing tag fails hard at
    #                       ``compose pull`` with a clear error rather
    #                       than silently rebuilding off the user's
    #                       checkout.
    #   dev               → the docker-compose.override.yml re-adds a
    #                       ``build:`` section pointing at the local
    #                       checkout. ``compose pull`` is best-effort
    #                       (the dev image isn't published), and
    #                       ``compose up --build`` forces a rebuild
    #                       so host-side edits land in the image.
    if [ "$CHANNEL" = "dev" ]; then
        info "Pulling sidecar images (cremind image is built locally for dev)"
        (cd "$DOCKER_DIR" && docker compose pull --ignore-pull-failures \
            >>"$LOG_FILE" 2>&1) || warn "Some images couldn't be pulled; will build locally."

        info "Building cremind image and starting bundle"
        (cd "$DOCKER_DIR" && docker compose up -d --build >>"$LOG_FILE" 2>&1)
    else
        info "Pulling $IMAGE_REPO:$CREMIND_VERSION and sidecar images from Docker Hub"
        # Docker Hub's CDN drops large layer downloads mid-transfer with an
        # EOF more often than you'd like. Docker caches completed layers, so
        # a retry resumes where the last attempt died — a bounded loop turns
        # a transient CDN hiccup into a non-event. A genuinely-missing tag
        # (manifest unknown) can't be fixed by retrying, so bail early then.
        pulled=0
        for attempt in 1 2 3; do
            if [ "$attempt" -gt 1 ]; then
                info "Retrying image pull (attempt $attempt/3)…"
                sleep 5
            fi
            if (cd "$DOCKER_DIR" && docker compose pull >>"$LOG_FILE" 2>&1); then
                pulled=1
                break
            fi
            if tail -n 40 "$LOG_FILE" | grep -qE 'manifest unknown|not found|repository does not exist'; then
                err "docker compose pull failed — $IMAGE_REPO:$CREMIND_VERSION is not on Docker Hub. Check the version/tag, or wait for the release to finish publishing (see $LOG_FILE)."
                exit 1
            fi
        done
        if [ "$pulled" -ne 1 ]; then
            err "docker compose pull failed after 3 attempts."
            err "The image is published, but layer downloads from Docker Hub's CDN kept"
            err "dropping (look for 'EOF' / 'failed to copy' in $LOG_FILE) — a network"
            err "issue, not a missing image. Things that help:"
            err "  - Re-run this installer; Docker caches completed layers and resumes."
            err "  - Pull directly, repeating until it finishes:"
            err "      docker pull $IMAGE_REPO:$CREMIND_VERSION"
            err "  - Reduce parallelism: add '\"max-concurrent-downloads\": 1' to the Docker"
            err "    daemon config (Docker Desktop -> Settings -> Docker Engine; or"
            err "    /etc/docker/daemon.json), restart Docker, and re-run."
            err "  - If you're on a VPN or corporate proxy, try again off it."
            exit 1
        fi
        info "Starting bundle"
        (cd "$DOCKER_DIR" && docker compose up -d >>"$LOG_FILE" 2>&1)
    fi

    # Health gate: don't open the wizard until the backend is reachable.
    # Docker publishes only the single public port (1515), so probe
    # :1515/health on the host. Custom installs already picked the public URL —
    # strip the scheme and trailing path to get its hostname.
    if [ "$DEPLOYMENT" = "local" ]; then
        HEALTH_HOST="localhost"
    elif [ "$DEPLOYMENT" = "custom" ]; then
        # http://foo.bar:1515 → foo.bar
        HEALTH_HOST="${CUSTOM_public_url#*://}"
        HEALTH_HOST="${HEALTH_HOST%%/*}"
        HEALTH_HOST="${HEALTH_HOST%%:*}"
        [ -z "$HEALTH_HOST" ] && HEALTH_HOST="localhost"
    else
        HEALTH_HOST="$APP_HOST"
    fi
    HEALTH_URL="http://${HEALTH_HOST}:1515/health"
    info "Waiting for backend at $HEALTH_URL ..."
    for i in $(seq 1 60); do
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            ok "Backend is up"
            break
        fi
        sleep 2
    done

    # The Electron app drives navigation to the wizard inside its own
    # window — printing a "Wizard URL: http://localhost:1515/#/setup"
    # block in that context misleads the user into thinking they need
    # to open a browser. Suppress the whole human-handoff section when
    # invoked with CREMIND_INSTALLER_FRONTEND=electron.
    WIZARD_URL="http://${HEALTH_HOST}:1515/#/setup"
    if [ "${CREMIND_INSTALLER_FRONTEND:-}" != "electron" ]; then
        step "Setup wizard"

        cat <<EOF
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

${BOLD}Open this URL in your browser to continue setup:${RESET}

    ${BOLD}$WIZARD_URL${RESET}

  ${BOLD}Cremind${RESET}:    http://${HEALTH_HOST}:1515
EOF
        # Desktop image only: surface the noVNC viewer + VNC password.
        if [ "$DESKTOP_UI" != "0" ]; then
            NOVNC_URL="http://${HEALTH_HOST}:6080/vnc.html"
            cat <<EOF
  ${BOLD}Desktop${RESET}:    $NOVNC_URL
  ${BOLD}VNC password${RESET} (saved to $DOCKER_DIR/.env):
    $(grep '^VNC_PASSWORD=' "$DOCKER_DIR/.env" | cut -d= -f2-)
EOF
        fi
        cat <<EOF

  Stop:    cd $DOCKER_DIR && docker compose down
  Logs:    cd $DOCKER_DIR && docker compose logs -f cremind
  Restart: cd $DOCKER_DIR && docker compose restart cremind

EOF

        # Auto-open only when we're on the user's actual desktop — skip
        # if we're inside a container (xdg-open would target the
        # container's display, not the host where the port is exposed).
        if [ "$NO_LAUNCH" -eq 0 ] && [ "$UNATTENDED" -eq 0 ] \
            && [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$WIZARD_URL" >/dev/null 2>&1 || true
            elif command -v open >/dev/null 2>&1; then
                open "$WIZARD_URL" >/dev/null 2>&1 || true
            fi
        fi
    fi

    # The Electron app's reconcileInstallStateWithDisk() detects a Docker
    # install via $DOCKER_DIR/.env (the compose env file written above),
    # so no host-side marker in $CREMIND_SYSTEM_DIR is required. Docker
    # installs leave the host's ~/.cremind untouched — runtime state
    # lives in the cremind-data named volume.

    # Consolidated post-install credentials file. Users open this to find
    # "how do I connect to service X?" without hunting across docker/.env
    # + bootstrap.toml + the install dir. The Setup Wizard re-emits this
    # file via app/config/credentials_file.py when it provisions Postgres
    # so the [postgres] block lands here too. Schema must stay in sync
    # with that writer — see tests/config/test_credentials_file.py.
    CREDS_FILE="$CREMIND_INSTALL_DIR/credentials.toml"
    GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    _docker_env_val() {
        grep "^$1=" "$DOCKER_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true
    }
    CREDS_API_PORT="$(_docker_env_val API_PORT)"
    CREDS_SPA_PORT="$(_docker_env_val SPA_PORT)"
    CREDS_NOVNC_PORT="$(_docker_env_val NOVNC_PORT)"
    CREDS_VNC_PORT="$(_docker_env_val VNC_PORT)"
    CREDS_RESOLUTION="$(_docker_env_val RESOLUTION)"
    [ -z "$CREDS_API_PORT" ] && CREDS_API_PORT=1112
    [ -z "$CREDS_SPA_PORT" ] && CREDS_SPA_PORT=1515
    [ -z "$CREDS_NOVNC_PORT" ] && CREDS_NOVNC_PORT=6080
    [ -z "$CREDS_VNC_PORT" ] && CREDS_VNC_PORT=5900
    [ -z "$CREDS_RESOLUTION" ] && CREDS_RESOLUTION="1280x720"
    SPA_URL="${DOCKER_APP_URL%:*}:$CREDS_SPA_PORT"
    info "Writing $CREDS_FILE (consolidated credentials)"
    cat > "$CREDS_FILE.tmp" <<EOF
# Cremind service credentials and connection info.
# Auto-generated by the installer and the Setup Wizard.
# Postgres credentials reflect the last successful wizard
# setup; re-running the installer does not rotate them.
# To change a value, edit the source of truth (the docker
# bundle .env or bootstrap.toml) and re-run the installer
# or the wizard.

generated_at = "$GENERATED_AT"
install_mode = "docker"
cremind_version = "$CREMIND_VERSION"
system_dir = "$CREMIND_SYSTEM_DIR"
install_dir = "$CREMIND_INSTALL_DIR"

[app]
api_url = "$DOCKER_APP_URL"
spa_url = "$SPA_URL"
api_port = $CREDS_API_PORT
spa_port = $CREDS_SPA_PORT
cors_allowed_origins = "$DOCKER_CORS"
setup_wizard_env = "$DOCKER_WIZARD_ENV"
EOF
    # Desktop image only: the basic image has no noVNC/VNC. Keep this in
    # sync with app/config/credentials_file.py, which gates [desktop] the
    # same way (see tests/config/test_credentials_file.py).
    if [ "$DESKTOP_UI" != "0" ]; then
        cat >> "$CREDS_FILE.tmp" <<EOF

[desktop]
novnc_url = "http://$HEALTH_HOST:$CREDS_NOVNC_PORT/vnc.html"
novnc_port = $CREDS_NOVNC_PORT
vnc_port = $CREDS_VNC_PORT
vnc_password = "$VNC_PASSWORD"
resolution = "$CREDS_RESOLUTION"
EOF
    fi
    mv "$CREDS_FILE.tmp" "$CREDS_FILE"
    chmod 600 "$CREDS_FILE"

    case "$CHANNEL" in
        test) ok "Done. Welcome to Cremind (test build)." ;;
        *)    ok "Done. Welcome to Cremind." ;;
    esac
    exit 0
fi

# ── native install ────────────────────────────────────────────────────────

# (Reaching here implies MODE=native. The Docker path exited above.)

# Native installs write everything (venv/, .env, bootstrap.toml, storage/,
# tokens/, profile dirs) under the System Dir, so create it now.
mkdir -p "$CREMIND_SYSTEM_DIR"

# Print the manual-install hint shown when auto-install is declined or fails.
print_python_manual_hint() {
    cat <<EOF >&2

Install options:
  ${BOLD}macOS${RESET}: brew install python@3.13
  ${BOLD}Ubuntu/Debian${RESET}: sudo apt install python3.13 python3.13-venv
  ${BOLD}Fedora/RHEL${RESET}: sudo dnf install python3.13
  ${BOLD}Any${RESET}: https://www.python.org/downloads/

Re-run this script after Python is on your PATH (or pass --mode docker).
EOF
}

# Decide whether to auto-install Python. Honors --auto-install-python /
# --no-auto-install-python / --unattended; otherwise prompts (default Yes).
prompt_for_python_install() {
    if [ "$AUTO_INSTALL_PYTHON" = "0" ]; then
        err "Python 3.13 or newer is required for native mode but was not found."
        print_python_manual_hint
        exit 1
    fi
    if [ "$AUTO_INSTALL_PYTHON" = "1" ]; then
        return 0
    fi
    cat <<EOF

Cremind can install an isolated Python 3.13 just for itself
(~70 MB downloaded into $CREMIND_INSTALL_DIR/python, no admin needed; system
Python is left untouched).

EOF
    while :; do
        read -r -p "Install isolated Python 3.13 now? [Y/n]: " choice </dev/tty || choice=""
        case "${choice:-y}" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO)
                err "Aborted: Python 3.13 is required for native mode."
                print_python_manual_hint
                exit 1
                ;;
            *) warn "Please answer y or n." ;;
        esac
    done
}

# Download a private copy of `uv` into $CREMIND_SYSTEM_DIR/bin so we can manage
# Python and venv installs without touching system tools. Pinned to a
# tested major.minor.
install_uv_locally() {
    if [ -x "$UV_BIN" ]; then
        info "uv already installed at $UV_BIN"
        return 0
    fi
    info "Installing uv into $BIN_DIR"
    mkdir -p "$BIN_DIR"
    if ! curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR="$BIN_DIR" UV_UNMANAGED_INSTALL="$BIN_DIR" \
                  INSTALLER_NO_MODIFY_PATH=1 sh \
            >>"$LOG_FILE" 2>&1; then
        err "Failed to download uv (the Python installer)."
        cat <<EOF >&2

Possible causes: no internet, corporate TLS interception, or astral.sh
is blocked. Set HTTPS_PROXY / SSL_CERT_FILE if you're behind a proxy,
or install Python manually (see hints below).
EOF
        print_python_manual_hint
        exit 1
    fi
    if [ ! -x "$UV_BIN" ]; then
        err "uv installer ran but $UV_BIN is missing — see $LOG_FILE."
        exit 1
    fi
    ok "Installed uv at $UV_BIN"
}

# Use uv to download an isolated Python 3.13 into $CREMIND_INSTALL_DIR/python
# and return its absolute path via $PYTHON. The cache and install dir are
# scoped to CREMIND_INSTALL_DIR so an Install Dir wipe removes everything
# (the interpreter is install-time scratch, not part of the user's data).
install_python_via_uv() {
    export UV_PYTHON_INSTALL_DIR="$CREMIND_INSTALL_DIR/python"
    export UV_CACHE_DIR="$CREMIND_INSTALL_DIR/uv-cache"
    info "Downloading isolated Python 3.13 (this may take a minute)"
    if ! "$UV_BIN" python install 3.13 >>"$LOG_FILE" 2>&1; then
        err "uv failed to install Python 3.13 — see $LOG_FILE."
        exit 1
    fi
    PYTHON="$("$UV_BIN" python find 3.13 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
        err "Python install completed but the interpreter could not be located."
        exit 1
    fi
    ok "Python: $("$PYTHON" --version) at $PYTHON (isolated)"
}

# Dev channel reuses the developer's local .venv (managed by uv) for the
# install, so we don't need a separate Python here. Skip the prompt and
# the isolated-Python bootstrap entirely.
if [ -z "$PYTHON" ] && [ "$CHANNEL" != "dev" ]; then
    prompt_for_python_install
    install_uv_locally
    install_python_via_uv
fi

# ── existing install detection ────────────────────────────────────────────

step "Install"

if [ "$CHANNEL" = "dev" ]; then
    # Dev channel: reuse the developer's local .venv (managed by ``uv
    # sync`` from <repo>/pyproject.toml) instead of building a parallel
    # venv at $CREMIND_SYSTEM_DIR/venv. The dev already has cremind + every
    # transitive dep installed in editable mode; reinstalling them into
    # a separate venv takes minutes for no benefit.
    if [ "$REINSTALL" -eq 1 ]; then
        warn "--reinstall has no effect in dev mode (the dev .venv is shared; refusing to wipe)."
    fi
    if [ ! -d "$REPO_ROOT/app/static/ui" ]; then
        warn "Dev: $REPO_ROOT/app/static/ui is empty."
        warn "Run scripts/build_ui.sh once so the SPA listener can start."
    fi
    VENV_DIR="$REPO_ROOT/.venv"
    if [ ! -x "$VENV_DIR/bin/cremind" ]; then
        err "Dev mode expects $VENV_DIR/bin/cremind to exist."
        err "Run 'uv sync' from $REPO_ROOT first, then re-run this installer."
        exit 1
    fi
    info "Reusing dev .venv at $VENV_DIR (no pip install)"
    INSTALLED_VERSION="$("$VENV_DIR/bin/cremind" version 2>/dev/null | awk '{print $2}' || echo "?")"
    ok "Using cremind $INSTALLED_VERSION from dev .venv"
else
    if [ "$REINSTALL" -eq 1 ] && [ -d "$VENV_DIR" ]; then
        info "Removing existing venv (--reinstall): $VENV_DIR"
        rm -rf "$VENV_DIR"
    fi

    # Resolve the spec passed to ``pip install``. Test channel pins a
    # direct wheel URL (see Test PyPI rationale below); production uses
    # the bare package name.
    #
    # The bare ``cremind`` install is intentionally thin — it ships only
    # the core deps the server + Setup Wizard + SQLite need. Optional
    # feature groups (vector embedding, vector stores, browser, channels,
    # LLM SDKs, postgres) are installed on demand by ``app/features/``
    # when the user enables them in the wizard. The Docker desktop image
    # pre-bakes ``cremind[all]`` instead (see ``Dockerfile``).
    INSTALL_SPEC=""
    INSTALL_SOURCE_LABEL=""
    case "$CHANNEL" in
        production)
            # Pin order (highest priority first):
            #   --version           — explicit, already validated.
            #   --electron-version  — the Electron build's version (the
            #                         Cremind desktop app always sets this).
            #   (unset)             — bare ``cremind`` lets pip pull the
            #                         latest from PyPI; preserved for
            #                         CLI users running install.sh directly.
            if [ -n "$VERSION_SPEC" ]; then
                INSTALL_SPEC="cremind==$VERSION_SPEC"
            elif [ -n "$ELECTRON_VERSION" ]; then
                INSTALL_SPEC="cremind==$ELECTRON_VERSION"
            else
                INSTALL_SPEC="cremind"
            fi
            INSTALL_SOURCE_LABEL="PyPI"
            ;;
        test)
            # Resolve the cremind test wheel directly from Test
            # PyPI's simple index, then install that URL. Production
            # PyPI is the only resolver for transitive deps.
            #
            # We deliberately do NOT use ``--index-url <test_pypi>
            # --extra-index-url <prod_pypi> --pre`` to install cremind,
            # because Test PyPI is a public free-for-all polluted with
            # broken stubs and stale pre-releases that match cremind's
            # transitive constraints. Two examples we hit on
            # v0.1.6.dev4:
            #
            #   - ``FASTAPI-1.0.tar.gz`` (uppercase, 2.5 KB stub with a
            #     missing DESCRIPTION.txt) satisfies ``fastapi>=0.115.2``
            #     and crashes the setuptools build with FileNotFoundError.
            #   - ``httpx-1.0.dev3`` outranks prod PyPI's stable
            #     ``httpx 0.28`` once ``--pre`` is on, even though we
            #     only wanted pre-release semantics for cremind itself.
            #
            # Pinning cremind to a direct wheel URL sidesteps both: pip
            # never asks Test PyPI for transitive deps, so the pollution
            # can't reach us.
            test_index="$(curl -fsSL https://test.pypi.org/simple/cremind/)" || {
                err "Failed to fetch https://test.pypi.org/simple/cremind/"
                exit 1
            }
            if [ -n "$VERSION_SPEC" ]; then
                # Exact pinned version: literal filename match between
                # ``cremind-`` and ``-py3-none-any.whl`` (version-agnostic,
                # so the rcN.devM form works without special-casing).
                info "Locating cremind==$VERSION_SPEC wheel"
                INSTALL_SPEC="$(printf '%s' "$test_index" \
                    | grep -oE "https://[^\"]*cremind-${VERSION_SPEC//./\\.}-py3-none-any\\.whl" \
                    | head -n 1)"
            else
                # Newest matching wheel via the canonical resolver (PEP 440
                # ordering incl. the rcN.devM dev form).
                if [ -n "$ELECTRON_VERSION" ]; then
                    info "Locating latest cremind test wheel for line ${ELECTRON_VERSION}rc*"
                else
                    info "Locating latest cremind test wheel"
                fi
                INSTALL_SPEC="$(printf '%s' "$test_index" | resolve_cremind_wheel test url "$ELECTRON_VERSION")"
            fi
            if [ -z "$INSTALL_SPEC" ]; then
                if [ -n "$VERSION_SPEC" ]; then
                    err "No cremind-${VERSION_SPEC}-py3-none-any.whl found at https://test.pypi.org/simple/cremind/ — has this version been published?"
                elif [ -n "$ELECTRON_VERSION" ]; then
                    err "No cremind-${ELECTRON_VERSION}rc*-py3-none-any.whl found at https://test.pypi.org/simple/cremind/ — has a test prerelease been published for this Electron build?"
                else
                    err "No cremind wheel found at https://test.pypi.org/simple/cremind/"
                fi
                exit 1
            fi
            ok "Test wheel: $(basename "$INSTALL_SPEC")"
            INSTALL_SOURCE_LABEL="Test PyPI"
            ;;
    esac

    if [ -d "$VENV_DIR" ]; then
        info "Existing install detected at $VENV_DIR — upgrading in place."
        "$VENV_DIR/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1
        "$VENV_DIR/bin/pip" install --upgrade "$INSTALL_SPEC" >>"$LOG_FILE" 2>&1
    else
        info "Creating venv at $VENV_DIR"
        "$PYTHON" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1
        info "Installing cremind from $INSTALL_SOURCE_LABEL (this may take a few minutes)"
        "$VENV_DIR/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1
        "$VENV_DIR/bin/pip" install "$INSTALL_SPEC" >>"$LOG_FILE" 2>&1
    fi

    INSTALLED_VERSION="$("$VENV_DIR/bin/cremind" version 2>/dev/null | awk '{print $2}' || echo "?")"
    ok "Installed cremind $INSTALLED_VERSION"
fi

# ── shim & PATH ───────────────────────────────────────────────────────────

# Create a symlink in $BIN_DIR so a single, stable path on PATH points at
# the venv's `cremind`. This means re-installs (which can rebuild the venv)
# don't change what the user has on PATH.
mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/cremind" "$BIN_DIR/cremind"
ok "Linked $BIN_DIR/cremind -> $VENV_DIR/bin/cremind"

# Append a marker block to the user's shell rc so $BIN_DIR is on PATH for
# every new shell. The block is idempotent (re-runs are no-ops) and uses a
# runtime guard so $PATH doesn't accumulate duplicates if the rc gets
# sourced more than once.
PATH_MARKER_BEGIN="# >>> cremind installer >>>"
PATH_MARKER_END="# <<< cremind installer <<<"

write_path_block_posix() {
    local rcfile="$1"
    if [ -f "$rcfile" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rcfile" 2>/dev/null; then
        return 0   # already present
    fi
    {
        printf '\n%s\n' "$PATH_MARKER_BEGIN"
        printf 'case ":$PATH:" in\n'
        printf '    *":%s:"*) ;;\n' "$BIN_DIR"
        printf '    *) export PATH="%s:$PATH" ;;\n' "$BIN_DIR"
        printf 'esac\n'
        printf '%s\n' "$PATH_MARKER_END"
    } >> "$rcfile" 2>/dev/null
}

write_path_block_fish() {
    local rcfile="$1"
    if [ -f "$rcfile" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rcfile" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$(dirname "$rcfile")"
    {
        printf '\n%s\n' "$PATH_MARKER_BEGIN"
        printf 'if not contains %s $fish_user_paths\n' "$BIN_DIR"
        printf '    set -Ux fish_user_paths %s $fish_user_paths\n' "$BIN_DIR"
        printf 'end\n'
        printf '%s\n' "$PATH_MARKER_END"
    } >> "$rcfile" 2>/dev/null
}

install_path_entry() {
    local shellname rcfile written=""
    shellname="$(basename "${SHELL:-}")"
    case "$shellname" in
        zsh)
            rcfile="$HOME/.zshrc"
            ;;
        fish)
            rcfile="$HOME/.config/fish/config.fish"
            write_path_block_fish "$rcfile" && written="$rcfile"
            ;;
        bash)
            if [ -f "$HOME/.bashrc" ] || [ ! -f "$HOME/.bash_profile" ]; then
                rcfile="$HOME/.bashrc"
            else
                rcfile="$HOME/.bash_profile"
            fi
            ;;
        *)
            rcfile="$HOME/.profile"
            ;;
    esac
    if [ -z "$written" ] && [ -n "$rcfile" ]; then
        write_path_block_posix "$rcfile"
        written="$rcfile"
    fi
    # Also drop a copy into ~/.profile if it exists and we haven't already
    # touched it — covers GUI-launched terminals and `sh` subshells.
    if [ -f "$HOME/.profile" ] && [ "$written" != "$HOME/.profile" ]; then
        write_path_block_posix "$HOME/.profile"
    fi
    if [ -n "$written" ] && [ -f "$written" ] \
            && grep -q "^${PATH_MARKER_BEGIN}\$" "$written" 2>/dev/null; then
        ok "Added $BIN_DIR to PATH via $written"
    else
        warn "Couldn't write PATH entry (permission denied?). Add manually:"
        printf '    export PATH="%s:$PATH"\n' "$BIN_DIR" >&2
    fi
}

if [ "$MODIFY_PATH" -eq 1 ]; then
    install_path_entry
else
    info "Skipping PATH modification (--no-modify-path). Add manually:"
    printf '    export PATH="%s:$PATH"\n' "$BIN_DIR"
fi

# ── env file ──────────────────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
    info "Generating $ENV_FILE"
    case "$DEPLOYMENT" in
        local)
            fetch_template "local.env" > "$ENV_FILE"
            ;;
        server)
            # __APP_HOST__ is the only placeholder; the user-provided host
            # gets substituted as-is (validated against [a-zA-Z0-9.:-]+ above).
            fetch_template "server.env.tmpl" | sed "s|__APP_HOST__|$APP_HOST|g" > "$ENV_FILE"
            ;;
        custom)
            # All four advanced fields come from the user (or the catalog
            # defaults in --unattended); render them straight into the
            # template. They're already validated by the catalog's
            # choice list (wizard_preset) or by being copy-pasted by the
            # operator, so no further sanitisation is applied.
            fetch_template "custom.env.tmpl" \
                | sed \
                    -e "s|__LISTEN_HOST__|$CUSTOM_listen_host|g" \
                    -e "s|__PUBLIC_URL__|$CUSTOM_public_url|g" \
                    -e "s|__CORS_ALLOWED_ORIGINS__|$CUSTOM_allowed_origins|g" \
                    -e "s|__SETUP_WIZARD_ENV__|$CUSTOM_wizard_preset|g" \
                > "$ENV_FILE"
            ;;
    esac
    # Stamp the install mode so the backend's mode-rule filter knows which
    # service modes to expose in the wizard. Native installs land here too;
    # docker installs stamp INSTALL_MODE into docker.env above.
    if ! grep -q '^INSTALL_MODE=' "$ENV_FILE" 2>/dev/null; then
        printf 'INSTALL_MODE=%s\n' "$MODE" >> "$ENV_FILE"
    fi
    ok "Wrote $ENV_FILE"
else
    info ".env already exists — keeping it. Edit $ENV_FILE if you need to."
fi

# Stamp the channel-specific keys into .env so the running app's upgrader
# reads them via the .env loader. Each write is idempotent (skipped if the
# key is already present) so re-runs don't accumulate duplicates and a
# user-customized value is preserved.
case "$CHANNEL" in
    production)
        if ! grep -q '^CREMIND_UPGRADE_CHANNEL=' "$ENV_FILE" 2>/dev/null; then
            printf '\nCREMIND_UPGRADE_CHANNEL=production\n' >> "$ENV_FILE"
        fi
        ;;
    test)
        if ! grep -q '^CREMIND_UPGRADE_CHANNEL=' "$ENV_FILE" 2>/dev/null; then
            printf '\nCREMIND_UPGRADE_CHANNEL=test\n' >> "$ENV_FILE"
        fi
        if ! grep -q '^CREMIND_PIP_INDEX_URL=' "$ENV_FILE" 2>/dev/null; then
            printf 'CREMIND_PIP_INDEX_URL=https://test.pypi.org/simple/\n' >> "$ENV_FILE"
        fi
        if ! grep -q '^CREMIND_PIP_EXTRA_INDEX_URL=' "$ENV_FILE" 2>/dev/null; then
            printf 'CREMIND_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/\n' >> "$ENV_FILE"
        fi
        ;;
    dev)
        # Stamp the channel so the feature installer's ``pip_spec()`` knows
        # to skip the ``==<version>`` pin (the editable install in /src or
        # the developer's checkout already satisfies the requirement, and
        # pinning to a release that hasn't been published to PyPI yet
        # fails at install time). ``cremind upgrade`` itself is still a
        # footgun in dev — the dev path is ``git pull`` — but the
        # upgrader's no-op behavior on dev is preferable to it silently
        # treating dev as production.
        if ! grep -q '^CREMIND_UPGRADE_CHANNEL=' "$ENV_FILE" 2>/dev/null; then
            printf '\nCREMIND_UPGRADE_CHANNEL=dev\n' >> "$ENV_FILE"
        fi
        # Replace the static template's locked-down CORS list with ``*``
        # so ``npm run dev`` (Vite at localhost:5173) and other ad-hoc dev
        # origins work without preflight failures. Idempotent: skip if
        # already wildcarded so re-runs don't churn the file.
        if ! grep -q '^CORS_ALLOWED_ORIGINS=\*$' "$ENV_FILE" 2>/dev/null; then
            sed -i.bak '/^CORS_ALLOWED_ORIGINS=/d' "$ENV_FILE"
            rm -f "${ENV_FILE}.bak"
            printf 'CORS_ALLOWED_ORIGINS=*\n' >> "$ENV_FILE"
        fi
        ;;
esac

# ── bootstrap.toml (DB selection) ─────────────────────────────────────────

# Skip the default-SQLite bootstrap.toml when the Electron app is driving
# — the Setup Wizard will write the file once the user picks a backend,
# and the backend boots in deferred-storage mode until then so no DB is
# materialised under $CREMIND_SYSTEM_DIR/storage before the user has chosen.
# Native installs (curl | sh, no Electron) get the SQLite default here,
# matching the legacy behavior; the wizard can still flip them to
# Postgres on first setup.
if [ "${CREMIND_INSTALLER_FRONTEND:-}" != "electron" ]; then
    if [ ! -f "$BOOTSTRAP_FILE" ]; then
        info "Generating $BOOTSTRAP_FILE (SQLite, the recommended default)"
        cat > "$BOOTSTRAP_FILE" <<'EOF'
# Database selection. SQLite is the recommended default for native
# installs; switch to "postgres" via the setup wizard if you want a
# multi-process or networked DB.
db_provider = "sqlite"
EOF
        ok "Wrote $BOOTSTRAP_FILE"
    fi
fi

# ── credentials.toml (consolidated connect info) ──────────────────────────
#
# Native installs have no VNC desktop — only [app] (and optionally
# [postgres] once the wizard provisions it). The Python writer at
# app/config/credentials_file.py re-emits this file on every wizard
# service provision, so the [postgres] block lands here later.
CREDS_FILE="$CREMIND_INSTALL_DIR/credentials.toml"
GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
_env_val() {
    grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}
CREDS_APP_URL="$(_env_val APP_URL)"
CREDS_API_PORT="$(_env_val PORT)"
CREDS_CORS="$(_env_val CORS_ALLOWED_ORIGINS)"
CREDS_WIZARD_ENV="$(_env_val SETUP_WIZARD_ENV)"
[ -z "$CREDS_API_PORT" ] && CREDS_API_PORT=1112
CREDS_SPA_PORT=1515
SPA_URL="${CREDS_APP_URL%:*}:$CREDS_SPA_PORT"
info "Writing $CREDS_FILE (consolidated credentials)"
cat > "$CREDS_FILE.tmp" <<EOF
# Cremind service credentials and connection info.
# Auto-generated by the installer and the Setup Wizard.
# Postgres credentials reflect the last successful wizard
# setup; re-running the installer does not rotate them.
# To change a value, edit the source of truth (the docker
# bundle .env or bootstrap.toml) and re-run the installer
# or the wizard.

generated_at = "$GENERATED_AT"
install_mode = "native"
cremind_version = "${INSTALLED_VERSION:-}"
system_dir = "$CREMIND_SYSTEM_DIR"
install_dir = "$CREMIND_INSTALL_DIR"

[app]
api_url = "$CREDS_APP_URL"
spa_url = "$SPA_URL"
api_port = $CREDS_API_PORT
spa_port = $CREDS_SPA_PORT
cors_allowed_origins = "$CREDS_CORS"
setup_wizard_env = "$CREDS_WIZARD_ENV"
EOF
mv "$CREDS_FILE.tmp" "$CREDS_FILE"
chmod 600 "$CREDS_FILE"

# ── migrate ───────────────────────────────────────────────────────────────

# Skip Alembic's ``upgrade head`` (and therefore creating the SQLite DB
# file) when the Electron app is driving — the app starts the backend
# only after the user clicks "Continue to Setup Wizard", and the backend
# is what eventually creates the DB. Keeping this here means a stray
# $CREMIND_SYSTEM_DIR/storage/cremind.db never shows up between the installer
# finishing and the user choosing to continue.
if [ "${CREMIND_INSTALLER_FRONTEND:-}" != "electron" ]; then
    info "Migrating database to current schema"
    "$VENV_DIR/bin/cremind" db upgrade >>"$LOG_FILE" 2>&1
    REVISION="$("$VENV_DIR/bin/cremind" db current 2>/dev/null || echo "?")"
    ok "Database at revision $REVISION"
fi

# ── start the server (foreground-detached for the install session) ───────

# Skip starting ``cremind serve`` when the Electron app is driving —
# the app spawns the backend itself once the user clicks "Continue to
# Setup Wizard", and that's also the point at which the SQLite DB is
# created (via the backend's own initialize() call).
SERVER_PID_FILE="$CREMIND_INSTALL_DIR/install.pid"
if [ "${CREMIND_INSTALLER_FRONTEND:-}" != "electron" ]; then
    step "Starting Cremind"

    # We start the server in the background so the wizard URL works as
    # soon as we open the browser. The PID is recorded so the user can
    # stop it with `kill $(cat $CREMIND_INSTALL_DIR/install.pid)`. Future Phase:
    # install a real service unit (systemd / launchd) so it survives
    # logout.

    # Source the .env early so HOST/PORT are honored both by the health
    # probe below and by the spawned server.
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a

    SERVER_RUNNING=0
    if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
        info "Cremind is already running (pid $(cat "$SERVER_PID_FILE"))."
        SERVER_RUNNING=1
    fi

    # Detect a server bound to the port without going through this
    # installer — typical dev case: ``uv run cremind serve`` in a separate
    # terminal. Starting a second cremind would collide on the bind, so
    # treat it as already-running and skip the spawn.
    if [ "$SERVER_RUNNING" -eq 0 ] && curl -fsS "http://${HOST:-127.0.0.1}:${PORT:-1112}/health" >/dev/null 2>&1; then
        info "Cremind is already responding at http://${HOST:-127.0.0.1}:${PORT:-1112} — skipping server start."
        SERVER_RUNNING=1
    fi

    if [ "$SERVER_RUNNING" -eq 0 ]; then
        nohup "$VENV_DIR/bin/cremind" serve >>"$CREMIND_SYSTEM_DIR/server.log" 2>&1 &
        echo $! > "$SERVER_PID_FILE"
        ok "Cremind started (pid $(cat "$SERVER_PID_FILE"), logs: $CREMIND_SYSTEM_DIR/server.log)"
    fi
fi

# Wait briefly for the HTTP listener — if it doesn't come up the wizard
# will be a dead link, which is the worst end-user experience. Skipped
# under the Electron front-end since the app starts the backend later
# (on the Continue click) and waits for its own health probe there.
if [ "${CREMIND_INSTALLER_FRONTEND:-}" != "electron" ]; then
    HEALTH_URL="http://${HOST:-127.0.0.1}:${PORT:-1112}/health"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
fi

# ── wizard handoff ────────────────────────────────────────────────────────

# ``custom`` installs honour the public URL the user provided; ``server``
# uses the host from --host; ``local`` is loopback. Custom URLs may
# include a path/scheme; normalise to the single public port (1515) while
# preserving the hostname.
if [ "$DEPLOYMENT" = "server" ]; then
    WIZARD_URL="http://$APP_HOST:1515/#/setup"
elif [ "$DEPLOYMENT" = "custom" ]; then
    # http://foo.bar:1515 → http://foo.bar:1515/#/setup
    CUSTOM_HOSTPART="${CUSTOM_public_url#*://}"
    CUSTOM_HOSTPART="${CUSTOM_HOSTPART%%/*}"
    CUSTOM_HOSTPART="${CUSTOM_HOSTPART%%:*}"
    [ -z "$CUSTOM_HOSTPART" ] && CUSTOM_HOSTPART="localhost"
    WIZARD_URL="http://${CUSTOM_HOSTPART}:1515/#/setup"
else
    WIZARD_URL="http://localhost:1515/#/setup"
fi

# Suppress the human-handoff block when the Electron app is driving —
# the app navigates to the in-window wizard via vue-router and showing
# a "Wizard URL: ..." instruction in that context misleads the user.
if [ "${CREMIND_INSTALLER_FRONTEND:-}" != "electron" ]; then
    step "Setup wizard"

    cat <<EOF
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

${BOLD}Open this URL in your browser to continue setup:${RESET}

    ${BOLD}$WIZARD_URL${RESET}

  Cremind:    ${WIZARD_URL%/#/setup}
  Stop:       kill \$(cat $SERVER_PID_FILE)

EOF

    # Activation guidance — front-and-center because bash can't update
    # the parent shell's PATH for us, and "command not found" right
    # after install is the most common user pitfall.
    if [ "$MODIFY_PATH" -eq 1 ]; then
        cat <<EOF
${BOLD}One last step — activate \`cremind\` in this shell:${RESET}

    ${BOLD}export PATH="$BIN_DIR:\$PATH"${RESET}

(New terminals pick this up automatically from your shell rc.)
Then run: ${BOLD}cremind --help${RESET}

EOF
    else
        cat <<EOF
${BOLD}One last step — put \`cremind\` on your PATH:${RESET}

    ${BOLD}export PATH="$BIN_DIR:\$PATH"${RESET}

To make this permanent, add the line above to your shell rc
(e.g. ~/.bashrc, ~/.zshrc).

EOF
    fi

    # Auto-open only when we're on the user's actual desktop. Inside a
    # container, xdg-open would launch a browser on the container's
    # display (e.g., a VNC desktop) rather than on the host where the
    # user has exposed the port — see /.dockerenv and /run/.containerenv
    # (Podman) for the standard "I'm in a container" markers.
    if [ "$NO_LAUNCH" -eq 0 ] && [ "$UNATTENDED" -eq 0 ] \
        && [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
        if command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$WIZARD_URL" >/dev/null 2>&1 || true
        elif command -v open >/dev/null 2>&1; then
            open "$WIZARD_URL" >/dev/null 2>&1 || true
        fi
    fi
fi

case "$CHANNEL" in
    test) ok "Done. Welcome to Cremind (test build)." ;;
    dev)  ok "Done. Welcome to Cremind (dev install from $REPO_ROOT)." ;;
    *)    ok "Done. Welcome to Cremind." ;;
esac
