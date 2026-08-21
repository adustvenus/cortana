#!/usr/bin/env bash
# Cortana secret distribution across machines.
#
#   ./secrets.sh push          encrypt this box's live secrets -> private repo
#   ./secrets.sh pull          fetch + decrypt them onto this box
#   ./secrets.sh add-machine   register this box so it is allowed to decrypt
#   ./secrets.sh status        show what is configured and what is present
#
# Secrets never enter THIS repo - it is public. They live encrypted in a
# separate PRIVATE repo and are readable only by machines whose SSH public key
# is listed in recipients.txt there. Each box decrypts with the SSH private key
# it already has for git, so onboarding a new box carries no extra key file.
#
# First-time setup is in SETUP.md ("Secrets across machines").
set -euo pipefail

# Files distributed. token.json and spotify_token.json are deliberately absent:
# they are per-machine OAuth state, regenerated locally by their own consent
# flow, and copying them around only spreads live session credentials.
FILES=(".env" "credentials.json")

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/cortana-secrets"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/cortana/secrets-repo"

die() { echo "error: $*" >&2; exit 1; }

need_age() {
  command -v age >/dev/null || die "age is not installed.
  Debian/Ubuntu : sudo apt install age
  Arch          : sudo pacman -S age
  macOS         : brew install age
  Windows       : winget install FiloSottile.age"
}

repo_url() {
  if [ -n "${SECRETS_REPO:-}" ]; then echo "$SECRETS_REPO"; return; fi
  [ -f "$CONF" ] || die "no secrets repo configured. Create a PRIVATE repo, then:
  mkdir -p \"$(dirname "$CONF")\"
  echo git@github.com:<you>/cortana-secrets.git > \"$CONF\""
  cat "$CONF"
}

ssh_key() {
  if [ -n "${SECRETS_SSH_KEY:-}" ]; then echo "$SECRETS_SSH_KEY"; return; fi
  for k in "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa"; do
    if [ -f "$k" ]; then echo "$k"; return; fi
  done
  die "no SSH key found. Generate one: ssh-keygen -t ed25519"
}

sync_repo() {
  local url
  url="$(repo_url)"
  if [ -d "$CACHE/.git" ]; then
    git -C "$CACHE" fetch --quiet origin
    # A brand-new private repo has no commits, so origin/HEAD does not exist
    # yet and there is nothing to reset onto.
    if git -C "$CACHE" rev-parse --quiet --verify origin/HEAD >/dev/null 2>&1; then
      git -C "$CACHE" reset --quiet --hard origin/HEAD
    fi
  else
    mkdir -p "$(dirname "$CACHE")"
    git clone --quiet "$url" "$CACHE" || die "cannot clone $url"
  fi
  # A freshly imaged box often has no git identity at all, which would make
  # every commit here fail. Only set one if nothing is configured already.
  git -C "$CACHE" config user.name >/dev/null 2>&1 ||
    git -C "$CACHE" config user.name "cortana-secrets"
  git -C "$CACHE" config user.email >/dev/null 2>&1 ||
    git -C "$CACHE" config user.email "cortana-secrets@$(hostname)"
  # Never let git translate line endings in this repo: a CR inside
  # recipients.txt breaks SSH key parsing, and a CR inside a .age file
  # corrupts the ciphertext outright.
  if [ ! -f "$CACHE/.gitattributes" ]; then
    echo '* -text' > "$CACHE/.gitattributes"
  fi
}

# The first push into an empty repo has no upstream branch to track yet.
push_repo() {
  git -C "$CACHE" push --quiet 2>/dev/null || git -C "$CACHE" push --quiet -u origin HEAD
}

cmd_push() {
  need_age
  sync_repo
  # Strip CR defensively for repos created before .gitattributes was pinned.
  local rcpt="$CACHE/.recipients.lf"
  tr -d '\015' < "$CACHE/recipients.txt" > "$rcpt" 2>/dev/null || true
  [ -s "$rcpt" ] || die "recipients.txt is missing or empty in the secrets repo.
  Run './secrets.sh add-machine' on each box that must decrypt, then push again."
  local n=0
  for f in "${FILES[@]}"; do
    if [ ! -s "$ROOT/$f" ]; then echo "skip   $f (not present here)"; continue; fi
    age -R "$rcpt" -o "$CACHE/$f.age" "$ROOT/$f"
    echo "sealed $f -> $f.age"
    n=$((n + 1))
  done
  rm -f "$rcpt"
  [ "$n" -gt 0 ] || die "nothing to push - none of ${FILES[*]} exist here."
  git -C "$CACHE" add -A
  if git -C "$CACHE" diff --cached --quiet; then echo "already up to date"; return; fi
  git -C "$CACHE" commit --quiet -m "secrets: update from $(hostname)"
  push_repo
  echo "pushed. On each Linux box: ./secrets.sh pull"
}

cmd_pull() {
  need_age
  sync_repo
  local key
  key="$(ssh_key)"
  local n=0
  for f in "${FILES[@]}"; do
    if [ ! -f "$CACHE/$f.age" ]; then echo "skip   $f (not in the secrets repo)"; continue; fi
    # Decrypt to a temp file first so a failure cannot truncate a working secret.
    if age -d -i "$key" -o "$ROOT/$f.tmp" "$CACHE/$f.age" 2>/dev/null; then
      mv "$ROOT/$f.tmp" "$ROOT/$f"
      chmod 600 "$ROOT/$f"
      echo "opened $f"
      n=$((n + 1))
    else
      rm -f "$ROOT/$f.tmp"
      die "cannot decrypt $f with $key.
  This machine is probably not a recipient yet. Run './secrets.sh add-machine'
  here, then re-run './secrets.sh push' on a box that already has the secrets."
    fi
  done
  [ "$n" -gt 0 ] || die "nothing decrypted."
  echo "done. Restart Cortana to load them: sudo systemctl restart cortana"
}

cmd_add_machine() {
  sync_repo
  local key pub
  key="$(ssh_key).pub"
  [ -f "$key" ] || die "no public key at $key"
  touch "$CACHE/recipients.txt"
  pub="$(awk '{print $1" "$2}' "$key")"   # type + base64, drop any comment
  if grep -qF "$pub" "$CACHE/recipients.txt"; then
    echo "$(hostname) is already a recipient"; return
  fi
  echo "$pub $(hostname)" >> "$CACHE/recipients.txt"
  git -C "$CACHE" add -A
  git -C "$CACHE" commit --quiet -m "recipients: add $(hostname)"
  push_repo
  echo "added $(hostname) as a recipient."
  echo "NOTE: existing .age files are still sealed to the OLD recipient list."
  echo "      Run './secrets.sh push' on a box that HAS the plaintext to reseal,"
  echo "      then './secrets.sh pull' here."
}

cmd_status() {
  local repo="NOT CONFIGURED" key="NONE" agev="NOT INSTALLED"
  if   [ -n "${SECRETS_REPO:-}" ]; then repo="$SECRETS_REPO (env)"
  elif [ -f "$CONF" ];             then repo="$(cat "$CONF")"; fi
  if [ -n "${SECRETS_SSH_KEY:-}" ] && [ -f "${SECRETS_SSH_KEY}" ]; then
    key="$SECRETS_SSH_KEY"
  else
    for k in "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa"; do
      if [ -f "$k" ]; then key="$k"; break; fi
    done
  fi
  if command -v age >/dev/null; then agev="$(age --version 2>/dev/null || echo installed)"; fi
  echo "secrets repo : $repo"
  echo "ssh identity : $key"
  echo "age          : $agev"
  echo "local files  :"
  for f in "${FILES[@]}"; do
    if [ -s "$ROOT/$f" ]; then echo "   present  $f"; else echo "   missing  $f"; fi
  done
}

case "${1:-}" in
  push)        cmd_push ;;
  pull)        cmd_pull ;;
  add-machine) cmd_add_machine ;;
  status)      cmd_status ;;
  *) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
