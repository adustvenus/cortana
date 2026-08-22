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

# Fail on a missing tool by name. Discovering curl is absent three steps
# later, as an opaque substitution failure, wastes everyone's time.
for tool in curl tar sha512sum install; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Missing required tool: $tool" >&2
    echo "  sudo apt install -y curl tar coreutils" >&2
    exit 1
  }
done

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
# Upstream names the checksum after the BUILD, not the tarball: it is
# spotifyd-linux-x86_64-full.sha512, not ...full.tar.gz.sha512.
SUMS="spotifyd-linux-${ARCH}-full.sha512"

WORK="$(mktemp -d)" || { echo "Cannot create a temp directory." >&2; exit 1; }
trap 'rm -rf "$WORK"' EXIT
ERRF="$WORK/curl.err"

# These pre-flight checks exist because curl reports "I could not write the
# output file" as error 23 - "client returned ERROR on write of N bytes" -
# which reads like a network fault and names neither the file nor the reason.
# They prove only that the SHELL can write here. A confined curl may still be
# unable to; that is fetch()'s problem, handled below.
if ! : > "$WORK/.probe" 2>/dev/null; then
  echo "Cannot write to $WORK - check permissions on the temp filesystem." >&2
  exit 1
fi
rm -f "$WORK/.probe"
FREE_KB="$(df -Pk "$WORK" | awk 'NR==2 {print $4}')"
if [ -n "$FREE_KB" ] && [ "$FREE_KB" -lt 20480 ]; then
  echo "Only ${FREE_KB}KB free on the filesystem holding $WORK." >&2
  echo "  spotifyd needs ~15MB to unpack. Free some space and retry:" >&2
  df -h "$WORK" | sed 's/^/    /' >&2
  exit 1
fi

# The body is redirected to a file the SHELL opens, rather than handed to curl
# as -o. That matters when curl is the snap build (/snap/bin/curl): a snap runs
# in its own mount namespace with a PRIVATE /tmp, so the directory mktemp -d
# just made is not there as far as curl is concerned. curl opens an -o file
# lazily, on the first write callback, so a transfer that is working fine dies
# on its first chunk with
#   curl: (23) client returned ERROR on write of 221 bytes
# and says nothing about the path. Redirecting means the unconfined shell does
# the open() and curl only ever writes to a descriptor it inherited, which
# neither the private /tmp nor the AppArmor profile mediates.
#
# -f (fail on HTTP >= 400) replaces -w '%{http_code}', which is unusable now:
# its output would land in the redirected body. The status code is recovered
# from curl's own message instead.
fetch() {   # fetch <url> <dest>
  local url="$1" dest="$2" code
  if curl -fsSL "$url" > "$dest" 2>"$ERRF"; then
    [ -s "$dest" ] && return 0
    echo "  x Empty response body from:" >&2
    echo "      $url" >&2
    return 1
  fi
  code="$(sed -n 's/.*returned error: \([0-9][0-9][0-9]\).*/\1/p' "$ERRF" | head -1)"
  if [ -n "$code" ]; then
    echo "  x HTTP $code fetching:" >&2
    echo "      $url" >&2
    [ "$code" = "403" ] && echo "      (GitHub API rate limit? 60/hour unauthenticated.)" >&2
    [ "$code" = "404" ] && echo "      (No such asset for this release/architecture.)" >&2
  else
    echo "  x Network error fetching:" >&2
    echo "      $url" >&2
    [ -s "$ERRF" ] && sed 's/^/      /' "$ERRF" >&2
    case "$(command -v curl)" in
      /snap/*) echo "      NOTE: curl here is the snap build, which runs confined." >&2
               echo "      If this persists: sudo apt install -y curl" >&2 ;;
    esac
  fi
  return 1
}

echo "[1/4] Finding the latest release..."
# SPOTIFYD_TAG=v0.4.2 skips the API entirely when it is rate-limited or down.
TAG="${SPOTIFYD_TAG:-}"
if [ -z "$TAG" ]; then
  fetch "https://api.github.com/repos/$REPO/releases/latest" "$WORK/release.json" || {
    echo "      Set the version explicitly to skip this lookup:" >&2
    echo "        SPOTIFYD_TAG=v0.4.2 bash Dashboard/install-spotifyd.sh" >&2
    exit 1
  }
  TAG="$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$WORK/release.json" | head -1)"
  rm -f "$WORK/release.json"
fi
[ -n "$TAG" ] || { echo "Could not determine the release tag from the API response." >&2
                   echo "  Retry with: SPOTIFYD_TAG=v0.4.2 bash Dashboard/install-spotifyd.sh" >&2
                   exit 1; }
BASE="https://github.com/$REPO/releases/download/$TAG"
echo "      $TAG  ($ARCH)"

TMP="$WORK"

echo "[2/4] Downloading $ASSET..."
fetch "$BASE/$ASSET" "$TMP/$ASSET" || exit 1
fetch "$BASE/$SUMS" "$TMP/$SUMS" || exit 1

echo "[3/4] Verifying checksum..."
# Upstream publishes "<hash>  <filename>", so sha512sum -c works directly once
# both files sit in the same directory.
( cd "$TMP" && sha512sum -c "$SUMS" >/dev/null ) || {
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
