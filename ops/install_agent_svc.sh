#!/usr/bin/env bash
# Install agent-svc (the local Codex executor) on the Task Manager production host.
#
# Run from the owner's Mac, from a clean, reviewed local `main` checkout, once CI has
# passed and the exact commit is deployed — same gating as ops/install_project_catalog.sh.
# One-time steps, users/paths/credentials and rollback are documented in
# docs/AGENT_SVC.md; read that first.
#
# Code lands under /opt/agent-svc/releases/<commit sha>, with /opt/agent-svc/current
# and the stable /opt/agent-svc/{agent_svc,libexec,codex,trusted} symlinks atomically
# repointed at it. Codex itself runs as the dedicated `agent-codex` account (never
# `codex-runner`, which is the live GitHub Actions runner) via a root-owned pinned
# Codex CLI + Node install; agent-svc only ever reaches agent-codex's files through
# `sudo -u agent-codex`, never as root.
#
# Usage: ops/install_agent_svc.sh [--start]
#   (no args)  Install/update code, users, sudoers, tmpfiles and the systemd unit.
#              The unit is left disabled and stopped (unless it was already running,
#              in which case it is stopped for the code swap and restarted after).
#   --start    Also `systemctl enable --now agent-svc` and print its status. Only pass
#              this once every project you want to run through it is DISABLED in
#              config.json/lane flags, so the first boot does no work.
#
# Never enables a project lane and never prints a secret value.
set -euo pipefail

start_service=false
for arg in "$@"; do
  case "$arg" in
    --start) start_service=true ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--start]" >&2
      exit 1
      ;;
  esac
done

repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

test "$(git branch --show-current)" = main || {
  echo 'Run this from the reviewed main branch.' >&2
  exit 1
}
test -z "$(git status --porcelain)" || {
  echo 'The checkout must be clean.' >&2
  exit 1
}
commit=$(git rev-parse HEAD)
remote_main=$(git ls-remote origin refs/heads/main | cut -f1)
test "$commit" = "$remote_main" || {
  echo 'Local main does not match origin/main.' >&2
  exit 1
}
live_image=$(ssh -o BatchMode=yes netcup \
  'docker inspect --format "{{.Config.Image}}" task-api')
test "$live_image" = "ghcr.io/asadtop4ik/task-manager-api:$commit" || {
  echo 'Deploy the exact main commit before installing agent-svc.' >&2
  exit 1
}

required_paths=(
  agentsvc/agent_svc
  agentsvc/libexec
  agentsvc/codex/agents/luna_worker.toml
  agentsvc/config.example.json
  scripts/agent_task.py
  scripts/public_agent_task.py
  scripts/agent_preflight.py
  scripts/agent_pr_review.py
  scripts/agent_release.py
  scripts/agent_images.py
  backend/app/services/agent_repos.py
  ops/agent-svc.service
  ops/agent-svc.sudoers
  ops/agent-svc.tmpfiles
  ops/agent-svc-tools.lock
  ops/sync_agent_svc_credentials.py
)
for path in "${required_paths[@]}"; do
  test -e "$path" || {
    echo "Missing $path — merge every agent-svc work package before installing." >&2
    exit 1
  }
done

echo "Installing agent-svc for commit $commit"

stage_dir=$(mktemp -d /tmp/agent-svc-install.XXXXXX)
remote_dir=""
cleanup() {
  rm -rf -- "$stage_dir"
  if [ -n "$remote_dir" ]; then
    ssh -o BatchMode=yes netcup "rm -rf -- '$remote_dir'" || true
  fi
}
trap cleanup EXIT

# Build the release tree from exactly what is committed at $commit (git archive),
# never from the working tree, so nothing untracked or .gitignore'd can ride along
# even though the tree is already required to be clean and match origin/main.
raw_dir="$stage_dir/raw"
mkdir -p "$raw_dir"
git archive "$commit" -- \
  agentsvc/agent_svc agentsvc/libexec agentsvc/codex \
  scripts/agent_task.py scripts/public_agent_task.py scripts/agent_preflight.py \
  scripts/agent_pr_review.py scripts/agent_release.py scripts/agent_images.py \
  backend/app/services/agent_repos.py \
  | tar -x -C "$raw_dir"

mkdir -p "$stage_dir/pkg/trusted"
mv "$raw_dir/agentsvc/agent_svc" "$stage_dir/pkg/agent_svc"
mv "$raw_dir/agentsvc/libexec" "$stage_dir/pkg/libexec"
mv "$raw_dir/agentsvc/codex" "$stage_dir/pkg/codex"
mv "$raw_dir/scripts/"*.py "$stage_dir/pkg/trusted/"
mv "$raw_dir/backend/app/services/agent_repos.py" "$stage_dir/pkg/trusted/"
tar -C "$stage_dir/pkg" -czf "$stage_dir/code.tar.gz" agent_svc libexec codex trusted

remote_dir=$(ssh -o BatchMode=yes netcup 'mktemp -d /tmp/agent-svc-install.XXXXXX')
[[ "$remote_dir" =~ ^/tmp/agent-svc-install\.[A-Za-z0-9]+$ ]] || exit 1

scp -q -o BatchMode=yes "$stage_dir/code.tar.gz" \
  ops/agent-svc.service ops/agent-svc.sudoers ops/agent-svc.tmpfiles \
  ops/agent-svc-tools.lock agentsvc/config.example.json \
  ops/sync_agent_svc_credentials.py \
  "netcup:$remote_dir/"

echo "== a) system group and agent-svc user =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_A'
set -euo pipefail
getent group agentwork >/dev/null || sudo groupadd --system agentwork

if ! id -u agent-svc >/dev/null 2>&1; then
  if getent group agent-svc >/dev/null; then
    sudo useradd --system --gid agent-svc \
      --home-dir /nonexistent --shell /usr/sbin/nologin agent-svc
  else
    sudo useradd --system --user-group \
      --home-dir /nonexistent --shell /usr/sbin/nologin agent-svc
  fi
fi
sudo usermod -aG agentwork agent-svc
echo "agent-svc user/group ready"
REMOTE_A

echo "== b) code release (git-archived tree -> releases dir -> atomic symlink swap) =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" "$commit" <<'REMOTE_B'
set -euo pipefail
remote_dir="$1"
commit="$2"

sudo install -d -m 0755 -o root -g root /opt/agent-svc
sudo install -d -m 0755 -o root -g root /opt/agent-svc/releases

release_dir="/opt/agent-svc/releases/$commit"
staging=$(sudo mktemp -d /opt/agent-svc/releases/.stage.XXXXXX)
sudo tar -C "$staging" -xzf "$remote_dir/code.tar.gz"
sudo chown -R root:root "$staging"
sudo find "$staging" -type d -exec chmod 0755 {} +
sudo find "$staging" -type f -exec chmod 0644 {} +
sudo rm -rf "$release_dir"
sudo mv "$staging" "$release_dir"

was_active=false
if sudo systemctl is-active --quiet agent-svc 2>/dev/null; then
  was_active=true
  echo "agent-svc is active; stopping before code swap"
  sudo systemctl stop agent-svc
fi

# Atomic swap: build the new symlink under a temp name, then rename it over
# `current` in one syscall (mv -T: never treat `current`, itself a symlink to a
# directory, as a directory to move *into*).
sudo ln -sfn "$release_dir" /opt/agent-svc/current.new
sudo mv -T /opt/agent-svc/current.new /opt/agent-svc/current

# These stable names never change value across releases (always "current/<name>"),
# so recreating them is not part of the atomic swap above; sudoers and the unit
# stay pinned to these paths regardless of which release is current.
for name in agent_svc libexec codex trusted; do
  sudo ln -sfn "current/$name" "/opt/agent-svc/$name"
done

if $was_active; then
  echo "restarting agent-svc after code swap"
  sudo systemctl start agent-svc
fi

# Keep the 3 newest releases plus whichever is current (even if it is older).
current_target=$(basename "$(readlink -f /opt/agent-svc/current)")
kept=0
for old in $(ls -1t /opt/agent-svc/releases); do
  [ "$old" = "$current_target" ] && continue
  kept=$((kept + 1))
  if [ "$kept" -gt 2 ]; then
    sudo rm -rf "/opt/agent-svc/releases/$old"
  fi
done

echo "code installed: /opt/agent-svc/current -> releases/$commit"
REMOTE_B

echo "== c) pinned Node 24 + Codex CLI (root-owned) =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_C'
set -euo pipefail

node_dir=/opt/agent-svc/node24
current_node_version=""
if [ -x "$node_dir/bin/node" ]; then
  current_node_version=$(sudo "$node_dir/bin/node" --version 2>/dev/null || true)
fi
case "$current_node_version" in
  v24*)
    echo "node24 already installed: $current_node_version"
    ;;
  *)
    source_node=/home/codex-runner/actions-runner/externals/node24
    sudo test -x "$source_node/bin/node"
    source_version=$(sudo "$source_node/bin/node" --version)
    case "$source_version" in
      v24*) ;;
      *)
        echo "runner node24 is not a v24.x build: $source_version" >&2
        exit 1
        ;;
    esac
    staging=$(sudo mktemp -d /opt/agent-svc/.node24-stage.XXXXXX)
    sudo cp -a "$source_node/." "$staging/"
    sudo chown -R root:root "$staging"
    sudo rm -rf "$node_dir"
    sudo mv "$staging" "$node_dir"
    echo "node24 installed: $(sudo "$node_dir/bin/node" --version)"
    ;;
esac

codex_cli_dir=/opt/agent-svc/codex-cli
codex_version=""
if [ -x "$codex_cli_dir/bin/codex" ]; then
  codex_version=$(sudo "$node_dir/bin/node" "$codex_cli_dir/bin/codex" --version 2>/dev/null || true)
fi
case "$codex_version" in
  *0.156.1*)
    echo "codex-cli already installed: $codex_version"
    ;;
  *)
    sudo rm -rf "$codex_cli_dir"
    sudo install -d -m 0755 -o root -g root "$codex_cli_dir"
    sudo "$node_dir/bin/npm" install --prefix "$codex_cli_dir" \
      --no-audit --no-fund @openai/codex@0.156.1
    sudo chown -R root:root "$codex_cli_dir"
    installed=$(sudo "$node_dir/bin/node" "$codex_cli_dir/bin/codex" --version)
    case "$installed" in
      *0.156.1*) echo "codex-cli installed: $installed" ;;
      *)
        echo "codex-cli --version did not report 0.156.1: $installed" >&2
        exit 1
        ;;
    esac
    ;;
esac
REMOTE_C

echo "== d) pinned tool venvs (hash-locked) =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_D'
set -euo pipefail
remote_dir="$1"
sudo install -d -m 0755 -o root -g root /opt/agent-svc/tools

ensure_tool() {
  local pkg="$1" version="$2"
  local venv_dir="/opt/agent-svc/tools/${pkg}-${version}"
  if [ -x "$venv_dir/bin/$pkg" ]; then
    local raw actual
    raw=$(sudo "$venv_dir/bin/$pkg" --version 2>/dev/null || true)
    case "$pkg" in
      ruff) actual=$(printf '%s' "$raw" | awk '{print $2}') ;;
      black) actual=$(printf '%s' "$raw" | awk '{print $2}' | tr -d ',') ;;
      *) actual="" ;;
    esac
    if [ "$actual" = "$version" ]; then
      echo "$pkg $version already installed"
      return 0
    fi
    echo "Recreating $venv_dir (found: $raw)"
    sudo rm -rf "$venv_dir"
  fi

  local section
  section=$(sudo mktemp /tmp/agent-svc-tool-lock.XXXXXX)
  sudo sed -n "/^# BEGIN ${pkg}-${version}\$/,/^# END ${pkg}-${version}\$/p" \
    "$remote_dir/agent-svc-tools.lock" | sudo tee "$section" >/dev/null
  sudo sed -i '/^# BEGIN /d;/^# END /d' "$section"
  if [ ! -s "$section" ]; then
    echo "no lock section for ${pkg}-${version} in agent-svc-tools.lock" >&2
    sudo rm -f "$section"
    exit 1
  fi
  sudo python3 -m venv "$venv_dir"
  sudo "$venv_dir/bin/pip" install --quiet --require-hashes --only-binary=:all: \
    --no-deps --no-cache-dir -r "$section"
  sudo rm -f "$section"
  sudo chown -R root:root "$venv_dir"
  printf '%s %s: ' "$pkg" "$version"
  sudo "$venv_dir/bin/$pkg" --version
}

ensure_tool ruff 0.7.4
ensure_tool ruff 0.16.0
ensure_tool black 26.5.1
REMOTE_D

echo "== e) Codex homes and luna_worker agent (written AS agent-codex, never as root) =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_E'
set -euo pipefail

if ! id -u agent-codex >/dev/null 2>&1; then
  if getent group agent-codex >/dev/null; then
    sudo useradd --system --gid agent-codex \
      --home-dir /home/agent-codex --shell /usr/sbin/nologin agent-codex
  else
    sudo useradd --system --user-group \
      --home-dir /home/agent-codex --shell /usr/sbin/nologin agent-codex
  fi
fi
sudo usermod -aG agentwork agent-codex

if sudo test -L /home/agent-codex; then
  echo "/home/agent-codex is a symlink; refusing" >&2
  exit 1
fi
if ! sudo test -e /home/agent-codex; then
  sudo install -d -m 0700 -o agent-codex -g agent-codex /home/agent-codex
fi

for home in /home/agent-codex/.codex-code /home/agent-codex/.codex-chat; do
  if sudo test -L "$home"; then
    echo "$home is a symlink; refusing" >&2
    exit 1
  fi
  if ! sudo test -e "$home"; then
    sudo -u agent-codex install -d -m 0700 "$home"
  fi
  if sudo test -L "$home/agents"; then
    echo "$home/agents is a symlink; refusing" >&2
    exit 1
  fi
  if sudo test -L "$home/agents/luna_worker.toml"; then
    echo "$home/agents/luna_worker.toml is a symlink; refusing" >&2
    exit 1
  fi
  sudo -u agent-codex install -D -m 0644 \
    /opt/agent-svc/codex/agents/luna_worker.toml "$home/agents/luna_worker.toml"
  if sudo test -f "$home/auth.json"; then
    echo "$home auth.json: yes"
  else
    echo "$home auth.json: no"
    echo "  -> run manually: sudo -u agent-codex env CODEX_HOME=$home" \
      "/opt/agent-svc/node24/bin/node /opt/agent-svc/codex-cli/bin/codex login --device-auth"
  fi
done
REMOTE_E

echo "== f) credentials =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_F'
set -euo pipefail
remote_dir="$1"
sudo install -d -m 0755 -o root -g root /etc/agent-svc
sudo install -d -m 0700 -o root -g root /etc/agent-svc/credentials
sudo python3 "$remote_dir/sync_agent_svc_credentials.py"
REMOTE_F

echo "== g) config, unit, sudoers, tmpfiles (each staged and verified before install) =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_G'
set -euo pipefail
remote_dir="$1"

if [ -f /etc/agent-svc/config.json ]; then
  echo "/etc/agent-svc/config.json already exists; left unchanged"
else
  sudo install -o root -g root -m 0644 \
    "$remote_dir/config.example.json" /etc/agent-svc/config.json
  echo "Installed default /etc/agent-svc/config.json"
fi

# Dot-prefixed name: systemd's unit loader ignores hidden files, so this staged
# copy is never live. Verify it there first, move it into place, then verify
# the installed unit too.
staged_unit=/etc/systemd/system/.agent-svc.service.stage
sudo install -o root -g root -m 0644 "$remote_dir/agent-svc.service" "$staged_unit"
sudo systemd-analyze verify "$staged_unit"
sudo mv "$staged_unit" /etc/systemd/system/agent-svc.service
sudo systemd-analyze verify /etc/systemd/system/agent-svc.service
sudo systemctl daemon-reload

# Same staged-verify-then-install shape for sudoers: sudo's #includedir skips any
# file with a "." in its name, so this staged copy is never active either.
staged_sudoers=/etc/sudoers.d/.60-agent-svc.tmp
sudo install -o root -g root -m 0440 "$remote_dir/agent-svc.sudoers" "$staged_sudoers"
sudo visudo -cf "$staged_sudoers"
sudo mv "$staged_sudoers" /etc/sudoers.d/60-agent-svc
sudo visudo -c

sudo install -o root -g root -m 0644 \
  "$remote_dir/agent-svc.tmpfiles" /etc/tmpfiles.d/agent-svc.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/agent-svc.conf

echo "config, unit, sudoers and tmpfiles installed"
REMOTE_G

if $start_service; then
  echo "== h) enable and start =="
  ssh -o BatchMode=yes netcup bash -s <<'REMOTE_H'
set -euo pipefail
sudo systemctl enable --now agent-svc
sleep 2
systemctl is-active agent-svc
REMOTE_H
else
  echo "agent-svc installed but not started (pass --start to enable it)."
fi

echo "Installed agent-svc for $commit"
