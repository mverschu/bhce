#!/usr/bin/env bash
set -Eeuo pipefail

# ==========================================
# BloodHound CE Installer
# Usage:
#   curl -fsSL URL | sudo bash
#   curl -fsSL URL | sudo bash -s -- --public
# ==========================================

BIND_ADDRESS="127.0.0.1"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
error() { echo -e "${RED}[-]${NC} $1" >&2; }
fail()  { error "$1"; exit 1; }

# Temporary files
WORKDIR="$(mktemp -d)"
LOG_FILE="${WORKDIR}/install.log"
trap 'rm -rf "$WORKDIR"' EXIT

# Parse arguments
case "${1:-}" in
    "")
        ;;
    --public)
        BIND_ADDRESS="0.0.0.0"
        ;;
    -h|--help)
        echo "Usage: $0 [--public]"
        exit 0
        ;;
    *)
        fail "Unknown argument: $1 (use --public)"
        ;;
esac

# Require root
[[ "$EUID" -eq 0 ]] || fail "Run this installer as root."

command -v apt-get >/dev/null 2>&1 \
    || fail "This installer supports apt-based systems (Debian, Ubuntu, Kali, etc.)."

echo
echo "=========================================="
echo "       BloodHound CE Installer"
echo "=========================================="
echo

if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    info "Detected: ${PRETTY_NAME:-${NAME:-Linux}}"
fi

info "Bind address: ${BIND_ADDRESS}"

# Detect architecture
ARCH="$(uname -m)"

case "$ARCH" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) fail "Unsupported architecture: ${ARCH}" ;;
esac

# --- Dependency helpers (Debian / Ubuntu / Kali / derivatives) ---

apt_pkg_installable() {
    local pkg="$1"
    local candidate
    candidate="$(apt-cache policy "$pkg" 2>/dev/null | awk '/^[[:space:]]*Candidate:/ { print $2; exit }')"
    [[ -n "$candidate" && "$candidate" != "(none)" ]]
}

docker_compose_available() {
    docker compose version >/dev/null 2>&1
}

ensure_docker_compose_cli() {
    docker_compose_available && return 0

    if command -v docker-compose >/dev/null 2>&1; then
        local bin plugin_dir="/usr/local/lib/docker/cli-plugins"
        mkdir -p "$plugin_dir"
        ln -sf "$(command -v docker-compose)" "${plugin_dir}/docker-compose"
    fi

    install_docker_compose_plugin_binary

    docker_compose_available \
        || fail "Docker Compose v2 is required (docker compose). Install docker-compose-plugin, docker-compose, or docker-compose-v2 from your distribution."
}

install_docker_compose_plugin_binary() {
    docker_compose_available && return 0

    local plugin_dir="/usr/local/lib/docker/cli-plugins"
    local arch uname_arch
    uname_arch="$(uname -m)"
    case "$uname_arch" in
        x86_64) arch="x86_64" ;;
        aarch64|arm64) arch="aarch64" ;;
        *) return 1 ;;
    esac

    local tag url dest="${plugin_dir}/docker-compose"
    tag="$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest \
        | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
    [[ -n "$tag" ]] || return 1

    url="https://github.com/docker/compose/releases/download/${tag}/docker-compose-linux-${arch}"
    mkdir -p "$plugin_dir"
    if curl -fsSL "$url" -o "$dest" >>"$LOG_FILE" 2>&1; then
        chmod +x "$dest"
        info "Installed Docker Compose plugin (${tag})"
        return 0
    fi
    rm -f "$dest"
    return 1
}

start_docker_daemon() {
    if docker info >/dev/null 2>&1; then
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1 \
        && systemctl list-unit-files docker.service &>/dev/null; then
        systemctl enable --now docker >>"$LOG_FILE" 2>&1 || true
    elif command -v service >/dev/null 2>&1; then
        service docker start >>"$LOG_FILE" 2>&1 || true
    fi

    docker info >/dev/null 2>&1 \
        || fail "Docker is installed but the daemon is not running. Start it (e.g. systemctl start docker) and run this installer again."
}

port_is_listening() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -tlnH | awk -v p=":${port}" '$4 ~ p { found=1 } END { exit !found }'
        return
    fi
    if command -v netstat >/dev/null 2>&1; then
        netstat -tln | grep -qE ":${port}([^0-9]|$)"
        return
    fi
    fail "Install iproute2 or net-tools so the installer can check port availability."
}

# Install dependencies
info "Installing dependencies..."

export DEBIAN_FRONTEND=noninteractive

if ! apt-get update -qq >"$LOG_FILE" 2>&1; then
    error "APT update failed."
    tail -n 20 "$LOG_FILE"
    exit 1
fi

DEPS=(curl ca-certificates)

if ! command -v docker >/dev/null 2>&1; then
    if apt_pkg_installable docker.io; then
        DEPS+=(docker.io)
    elif apt_pkg_installable docker-ce; then
        DEPS+=(docker-ce docker-ce-cli containerd.io)
    else
        fail "No Docker package found (expected docker.io or docker-ce in apt)."
    fi
fi

if ! command -v ss >/dev/null 2>&1 && apt_pkg_installable iproute2; then
    DEPS+=(iproute2)
fi

if ! apt-get install -y -qq "${DEPS[@]}" >"$LOG_FILE" 2>&1; then
    error "Dependency installation failed."
    tail -n 20 "$LOG_FILE"
    exit 1
fi

if ! docker_compose_available; then
    compose_pkg=""
    # docker-compose-v2 is Ubuntu archive naming; docker-compose is Debian/Kali (v2+).
    for candidate in docker-compose-plugin docker-compose docker-compose-v2; do
        if apt_pkg_installable "$candidate"; then
            compose_pkg="$candidate"
            break
        fi
    done
    if [[ -n "$compose_pkg" ]]; then
        if ! apt-get install -y -qq "$compose_pkg" >>"$LOG_FILE" 2>&1; then
            error "Failed to install ${compose_pkg}."
            tail -n 20 "$LOG_FILE"
            exit 1
        fi
    fi
fi

ensure_docker_compose_cli
start_docker_daemon

info "Dependencies installed"

# Ports required by BloodHound CE (Neo4j + UI)
check_port_free() {
    local port="$1" name="$2"
    if port_is_listening "$port"; then
        error "Port ${port} (${name}) is already in use."
        if command -v ss >/dev/null 2>&1; then
            ss -tlnp | grep -E ":${port}([^0-9]|$)" || true
        fi
        fail "Free port ${port} or stop the conflicting service before installing (e.g. stop host Neo4j on 7687)."
    fi
}

check_port_free 7687 "Neo4j"
check_port_free 8080 "BloodHound UI"

# Download BloodHound CLI
info "Downloading BloodHound CLI..."

DOWNLOAD_URL="https://github.com/SpecterOps/bloodhound-cli/releases/latest/download/bloodhound-cli-linux-${ARCH}.tar.gz"

if ! curl -fsSL "$DOWNLOAD_URL" -o "${WORKDIR}/bloodhound-cli.tar.gz"; then
    fail "Failed to download BloodHound CLI."
fi

# Extract
info "Extracting BloodHound CLI..."

if ! tar -xzf "${WORKDIR}/bloodhound-cli.tar.gz" -C "$WORKDIR" 2>/dev/null; then
    fail "Failed to extract BloodHound CLI."
fi

# Install binary
if [[ ! -f "${WORKDIR}/bloodhound-cli" ]]; then
    fail "BloodHound CLI binary not found in archive."
fi

install -m 0755 "${WORKDIR}/bloodhound-cli" /usr/local/bin/bloodhound-cli

info "BloodHound CLI installed"

# Install BloodHound CE
info "Installing BloodHound CE..."

# BLOODHOUND_HOST is the official Docker Compose
# environment variable controlling the published UI port.
if ! env BLOODHOUND_HOST="$BIND_ADDRESS" \
    /usr/local/bin/bloodhound-cli install >"$LOG_FILE" 2>&1; then
    error "BloodHound installation failed."
    tail -n 30 "$LOG_FILE"
    exit 1
fi

info "BloodHound installation completed"

# Apply requested binding
info "Applying network configuration..."

if ! env BLOODHOUND_HOST="$BIND_ADDRESS" \
    /usr/local/bin/bloodhound-cli up >"$LOG_FILE" 2>&1; then
    error "Failed to start BloodHound with requested binding."
    tail -n 30 "$LOG_FILE"
    exit 1
fi

info "BloodHound started"

# Retrieve admin credentials
info "Retrieving admin credentials..."

ADMIN_USER="admin"

ADMIN_PASSWORD="$(/usr/local/bin/bloodhound-cli config get default_password 2>/dev/null || true)"

if [[ -z "$ADMIN_PASSWORD" ]]; then
    fail "Could not retrieve admin password."
fi

# Wait for Docker port binding
info "Verifying network binding..."

PORT_BINDINGS=""

for attempt in $(seq 1 30); do
    PORT_BINDINGS="$(
        while IFS= read -r CONTAINER_ID; do
            docker inspect --format \
                '{{range (index .NetworkSettings.Ports "8080/tcp")}}{{.HostIp}}:{{.HostPort}} {{end}}' \
                "$CONTAINER_ID" 2>/dev/null || true
        done < <(docker ps -q --filter publish=8080)
    )"

    if [[ -n "$PORT_BINDINGS" ]]; then
        break
    fi

    sleep 2
done

if [[ -z "$PORT_BINDINGS" ]]; then
    error "BloodHound port 8080 is not published."
    docker ps
    exit 1
fi

# Verify exact binding
if [[ "$BIND_ADDRESS" == "0.0.0.0" ]]; then
    if ! grep -qF "0.0.0.0:8080" <<< "$PORT_BINDINGS"; then
        error "Expected 0.0.0.0:8080 but got: ${PORT_BINDINGS}"
        exit 1
    fi
else
    if ! grep -qF "127.0.0.1:8080" <<< "$PORT_BINDINGS"; then
        error "Expected 127.0.0.1:8080 but got: ${PORT_BINDINGS}"
        exit 1
    fi
fi

info "Network binding verified: ${PORT_BINDINGS}"

# Determine access IP
if [[ "$BIND_ADDRESS" == "0.0.0.0" ]]; then
    HOST_IP="$(hostname -I | awk '{print $1}')"
    HOST_IP="${HOST_IP:-YOUR-SERVER-IP}"
else
    HOST_IP="127.0.0.1"
fi

# Verify BloodHound services
info "Checking BloodHound services..."

if ! /usr/local/bin/bloodhound-cli running; then
    fail "BloodHound services are not running correctly."
fi

# Final output
echo
echo "=========================================="
echo "       BLOODHOUND CE INSTALLED"
echo "=========================================="
echo

info "Network:"
echo "    Bind: ${BIND_ADDRESS}"
echo "    Port: 8080"
echo "    Verified: ${PORT_BINDINGS}"

echo
info "Access URL:"
echo "    http://${HOST_IP}:8080/ui/login"

echo
info "Credentials:"
echo "    Username: ${ADMIN_USER}"
echo "    Password: ${ADMIN_PASSWORD}"

echo
echo "=========================================="
info "Installation completed!"
echo "=========================================="
echo
