#!/bin/bash
# Ensure Node.js >= v22 is available for AppKit scaffolding.
#
# AppKit (@databricks/appkit) requires Node 22+. The Databricks Apps runtime
# image ships a Node that may be older, so this script installs a pinned Node
# 22 LTS into ~/.local (which is already first on PATH for setup steps and
# user terminals) ONLY when the present Node is missing or too old.
#
# Idempotent: a no-op when `node` already satisfies the minimum major version.
#
# Enterprise mode: honors the same proxy / CA-bundle env the other install
# scripts do (curl reads CURL_CA_BUNDLE / SSL_CERT_FILE / HTTPS_PROXY, which
# enterprise_config.bootstrap() pushes into the environment). The Node binary
# host is overridable for locked-down networks:
#
#   NODE_DIST_MIRROR   Replacement for https://nodejs.org/dist. Convention:
#                      mirror keeps the same /v{ver}/node-v{ver}-linux-x64.tar.xz
#                      tail, so it works against a generic-repo proxy with no
#                      path rewriting.
#   NODE_VERSION       Exact Node version to install (default: 22 LTS pin below).
#   NODE_MIN_MAJOR     Minimum acceptable major already on PATH (default: 22).

set -euo pipefail

INSTALL_DIR="$HOME/.local"
mkdir -p "$INSTALL_DIR/bin"

NODE_MIN_MAJOR="${NODE_MIN_MAJOR:-22}"
# Pinned Node 22 LTS ("Jod"). Bump deliberately during CoDA releases.
NODE_VERSION="${NODE_VERSION:-22.14.0}"
NODE_DIST_MIRROR="${NODE_DIST_MIRROR:-https://nodejs.org/dist}"
NODE_DIST_MIRROR="${NODE_DIST_MIRROR%/}"  # strip trailing slash

# --- Skip if a recent-enough Node is already on PATH ------------------------
current_major() {
  command -v node >/dev/null 2>&1 || return 1
  # node --version prints e.g. "v20.11.1"; strip the leading v and take major.
  node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1
}

if maj="$(current_major)"; then
  if [ "${maj:-0}" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null; then
    echo "Node v$(node --version | sed 's/^v//') already satisfies >= v${NODE_MIN_MAJOR}; skipping install."
    exit 0
  fi
  echo "Node major v${maj} is older than required v${NODE_MIN_MAJOR}; installing v${NODE_VERSION}."
else
  echo "Node not found on PATH; installing v${NODE_VERSION}."
fi

# --- Detect architecture ----------------------------------------------------
arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) node_arch="linux-x64" ;;
  aarch64|arm64) node_arch="linux-arm64" ;;
  *)
    echo "ERROR: unsupported architecture '${arch}' for Node install." >&2
    exit 1
    ;;
esac

tarball="node-v${NODE_VERSION}-${node_arch}.tar.xz"
url="${NODE_DIST_MIRROR}/v${NODE_VERSION}/${tarball}"

echo "Downloading ${url}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "$url" -o "$tmp/node.tar.xz"

# Extract into ~/.local, stripping the top-level node-vX-linux-x64/ directory so
# bin/node -> ~/.local/bin/node and lib/node_modules -> ~/.local/lib/node_modules
# (matching the npm --prefix=$HOME/.local convention used by the other setup
# scripts, so the bundled npm and globally-installed CLIs share one tree).
tar -xJf "$tmp/node.tar.xz" -C "$INSTALL_DIR" --strip-components=1

export PATH="$INSTALL_DIR/bin:$PATH"
echo "Installed Node $("$INSTALL_DIR/bin/node" --version) / npm $("$INSTALL_DIR/bin/npm" --version) to ${INSTALL_DIR}"
