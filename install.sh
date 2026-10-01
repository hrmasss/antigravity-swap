#!/bin/sh
# Install the aswap standalone binary from the latest GitHub release.
#   curl -fsSL https://raw.githubusercontent.com/hrmasss/antigravity-swap/main/install.sh | sh
# ASWAP_INSTALL_DIR (default ~/.local/bin) and ASWAP_VERSION (default latest) override.
set -eu

repo="hrmasss/antigravity-swap"
dir="${ASWAP_INSTALL_DIR:-$HOME/.local/bin}"
version="${ASWAP_VERSION:-latest}"

os=$(uname -s)
arch=$(uname -m)
case "$os/$arch" in
  Linux/x86_64|Linux/amd64) asset=aswap-linux-x86_64 ;;
  Linux/aarch64|Linux/arm64) asset=aswap-linux-aarch64 ;;
  Darwin/arm64) asset=aswap-macos-arm64 ;;
  *) echo "aswap: no prebuilt binary for $os/$arch; install with: uv tool install antigravity-swap" >&2; exit 1 ;;
esac

if [ "$version" = latest ]; then
  base="https://github.com/$repo/releases/latest/download"
else
  base="https://github.com/$repo/releases/download/$version"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$base/$asset" -o "$tmp/aswap"
curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"
want=$(grep " $asset\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)
if command -v sha256sum >/dev/null 2>&1; then got=$(sha256sum "$tmp/aswap" | cut -d' ' -f1)
else got=$(shasum -a 256 "$tmp/aswap" | cut -d' ' -f1); fi
[ -n "$want" ] && [ "$want" = "$got" ] || { echo "aswap: checksum mismatch, not installing" >&2; exit 1; }

mkdir -p "$dir"
chmod +x "$tmp/aswap"
mv "$tmp/aswap" "$dir/aswap"
echo "Installed aswap to $dir/aswap"
case ":$PATH:" in *":$dir:"*) ;; *) echo "Add $dir to your PATH." ;; esac
