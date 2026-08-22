#!/usr/bin/env bash
# Installs spotifyd, which makes this machine a Spotify Connect endpoint so the
# dashboard's Music module plays here instead of on your phone.
#
# It exists because spotifyd is NOT in the Ubuntu/Debian archives - "apt install
# spotifyd" fails - and the upstream advice is either a prebuilt tarball or a
# full Rust toolchain build. This takes the tarball and verifies it, because the
# binary ends up holding a live Spotify session.
#
#   bash Dashboard/install-spotifyd.sh
set -e
cd "$(dirname "$0")"

DEST="$HOME/.local/bin"
REPO="Spotifyd/spotifyd"

case "$(uname -m)" in
  x86_64|amd64)   ARCH=x86_64 ;;
  aarch64|arm64)  ARCH=aarch64 ;;
  armv7l|armv7)   ARCH=armv7 ;;
  *) echo "Unsupported architecture: $(uname -m)." >&2
     echo "Build from source instead: https://docs.spotifyd.rs/installation/" >&2
     exit 1 ;;
esac

# "full" rather than "slim": the dashboard's config uses the pulseaudio backend,
# which the slim build does not carry.
ASSET="spotifyd-linux-${ARCH}-full.tar.gz"

echo "[1/4] Finding the latest release..."
TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
       | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
[ -n "$TAG" ] || { echo "Could not reach the GitHub API." >&2; exit 1; }
BASE="https://github.com/$REPO/releases/download/$TAG"
echo "      $TAG  ($ARCH)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[2/4] Downloading $ASSET..."
curl -fsSL -o "$TMP/$ASSET" "$BASE/$ASSET"
curl -fsSL -o "$TMP/$ASSET.sha512" "$BASE/$ASSET.sha512"

echo "[3/4] Verifying checksum..."
# Upstream publishes "<hash>  <filename>", so sha512sum -c works directly once
# both files sit in the same directory.
( cd "$TMP" && sha512sum -c "$ASSET.sha512" >/dev/null ) || {
  echo "      ✗ CHECKSUM MISMATCH - refusing to install." >&2
  echo "      This binary would hold a live Spotify session; do not run it." >&2
  exit 1
}
echo "      ok"

echo "[4/4] Installing to $DEST/spotifyd..."
mkdir -p "$DEST"
tar xzf "$TMP/$ASSET" -C "$TMP"
install -m 0755 "$TMP/spotifyd" "$DEST/spotifyd"

echo ""
"$DEST/spotifyd" --version 2>/dev/null || true
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo ""
     echo "NOTE: $DEST is not on your PATH. The systemd unit uses an absolute"
     echo "      path so the service works regardless, but add this for shells:"
     echo '        echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.bashrc' ;;
esac
echo ""
echo "Next:"
echo "  cp Dashboard/spotifyd.conf.example Dashboard/spotifyd.conf   # set cache_path"
echo "  bash Dashboard/install-dash.sh                               # installs the unit"
echo "  systemctl --user start cortana-spotifyd"
echo "Then pick 'Cortana' once from Spotify Connect on any device."
