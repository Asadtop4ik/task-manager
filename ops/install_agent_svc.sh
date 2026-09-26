#!/usr/bin/env bash
# Install agent-svc (the local Codex executor) on the Task Manager production host.
#
# Run from the owner's Mac, from a clean, reviewed local `main` checkout, once CI has
# passed and the exact commit is deployed — same gating as ops/install_project_catalog.sh.
# One-time steps, users/paths/credentials and rollback are documented in
# docs/AGENT_SVC.md; read that first.
#
# Usage: ops/install_agent_svc.sh [--start]
#   (no args)  Install/update code, users, sudoers, tmpfiles and the systemd unit.
#              The unit is left disabled and stopped.
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

# Build the code tree that gets swapped into /opt/agent-svc/{agent_svc,libexec,codex,trusted}
# as one unit per subdirectory (never a half-copied tree observed as live).
mkdir -p "$stage_dir/tree/agent_svc" "$stage_dir/tree/libexec" \
  "$stage_dir/tree/codex" "$stage_dir/tree/trusted"
cp -R agentsvc/agent_svc/. "$stage_dir/tree/agent_svc/"
cp -R agentsvc/libexec/. "$stage_dir/tree/libexec/"
cp -R agentsvc/codex/. "$stage_dir/tree/codex/"
cp scripts/agent_task.py scripts/public_agent_task.py scripts/agent_preflight.py \
  scripts/agent_pr_review.py scripts/agent_release.py scripts/agent_images.py \
  backend/app/services/agent_repos.py \
  "$stage_dir/tree/trusted/"
tar -C "$stage_dir/tree" -czf "$stage_dir/code.tar.gz" agent_svc libexec codex trusted

remote_dir=$(ssh -o BatchMode=yes netcup 'mktemp -d /tmp/agent-svc-install.XXXXXX')
[[ "$remote_dir" =~ ^/tmp/agent-svc-install\.[A-Za-z0-9]+$ ]] || exit 1

scp -q "$stage_dir/code.tar.gz" \
  ops/agent-svc.service ops/agent-svc.sudoers ops/agent-svc.tmpfiles \
  agentsvc/config.example.json ops/sync_agent_svc_credentials.py \
  "netcup:$remote_dir/"

echo "== a) system group and user =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_A'
set -euo pipefail
getent group agentwork >/dev/null || sudo groupadd --system agentwork
id -u agent-svc >/dev/null 2>&1 || sudo useradd --system --user-group \
  --home-dir /nonexistent --shell /usr/sbin/nologin agent-svc
sudo usermod -aG agentwork agent-svc
sudo usermod -aG agentwork codex-runner
echo "agent-svc user/group ready; codex-runner is in agentwork"
REMOTE_A

echo "== b) code (staging dir + atomic per-directory swap) =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_B'
set -euo pipefail
remote_dir="$1"
sudo install -d -m 0755 -o root -g root /opt/agent-svc
staging=$(sudo mktemp -d /opt/agent-svc/.stage.XXXXXX)
sudo tar -C "$staging" -xzf "$remote_dir/code.tar.gz"
sudo chown -R root:root "$staging"
sudo find "$staging" -type d -exec chmod 0755 {} +
sudo find "$staging" -type f -exec chmod 0644 {} +
for name in agent_svc libexec codex trusted; do
  if [ -d "/opt/agent-svc/$name" ]; then
    sudo rm -rf "/opt/agent-svc/${name}.prev"
    sudo mv "/opt/agent-svc/$name" "/opt/agent-svc/${name}.prev"
  fi
  sudo mv "$staging/$name" "/opt/agent-svc/$name"
  sudo rm -rf "/opt/agent-svc/${name}.prev"
done
sudo rmdir "$staging"
echo "code installed under /opt/agent-svc"
REMOTE_B

echo "== c) pinned tool venvs =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_C'
set -euo pipefail
sudo install -d -m 0755 -o root -g root /opt/agent-svc/tools

ensure_tool() {
  pkg="$1"
  version="$2"
  venv_dir="/opt/agent-svc/tools/${pkg}-${version}"
  if [ -x "$venv_dir/bin/$pkg" ]; then
    installed=$(sudo "$venv_dir/bin/$pkg" --version 2>/dev/null || true)
    case "$installed" in
      *"$version"*)
        echo "$pkg $version already installed"
        return 0
        ;;
    esac
    echo "Recreating $venv_dir (found: $installed)"
    sudo rm -rf "$venv_dir"
  fi
  sudo python3 -m venv "$venv_dir"
  sudo "$venv_dir/bin/pip" install --quiet "${pkg}==${version}"
  sudo chown -R root:root "$venv_dir"
  printf '%s %s: ' "$pkg" "$version"
  sudo "$venv_dir/bin/$pkg" --version
}

ensure_tool ruff 0.7.4
ensure_tool ruff 0.16.0
ensure_tool black 26.5.1
REMOTE_C

echo "== d) Codex homes and luna_worker agent =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_D'
set -euo pipefail
for home in /home/codex-runner/.codex-code /home/codex-runner/.codex-chat; do
  if [ ! -d "$home" ]; then
    sudo install -d -m 0700 -o codex-runner -g codex-runner "$home"
  fi
  sudo install -d -m 0755 -o root -g root "$home/agents"
  sudo install -o root -g root -m 0644 \
    /opt/agent-svc/codex/agents/luna_worker.toml "$home/agents/luna_worker.toml"
  if sudo test -f "$home/auth.json"; then
    echo "$home auth.json: yes"
  else
    echo "$home auth.json: no"
    echo "  -> run manually: sudo -u codex-runner env CODEX_HOME=$home" \
      "/home/codex-runner/.local/bin/codex login --device-auth"
  fi
done
REMOTE_D

echo "== e) credentials =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_E'
set -euo pipefail
remote_dir="$1"
sudo install -d -m 0755 -o root -g root /etc/agent-svc
sudo install -d -m 0700 -o root -g root /etc/agent-svc/credentials
sudo python3 "$remote_dir/sync_agent_svc_credentials.py"
REMOTE_E

echo "== f) config, unit, sudoers, tmpfiles =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_F'
set -euo pipefail
remote_dir="$1"

if [ -f /etc/agent-svc/config.json ]; then
  echo "/etc/agent-svc/config.json already exists; left unchanged"
else
  sudo install -o root -g root -m 0644 \
    "$remote_dir/config.example.json" /etc/agent-svc/config.json
  echo "Installed default /etc/agent-svc/config.json"
fi

sudo install -o root -g root -m 0644 \
  "$remote_dir/agent-svc.service" /etc/systemd/system/agent-svc.service

sudo cp "$remote_dir/agent-svc.sudoers" /tmp/agent-svc.sudoers.check
sudo visudo -cf /tmp/agent-svc.sudoers.check
sudo rm -f /tmp/agent-svc.sudoers.check
sudo install -o root -g root -m 0440 \
  "$remote_dir/agent-svc.sudoers" /etc/sudoers.d/60-agent-svc

sudo install -o root -g root -m 0644 \
  "$remote_dir/agent-svc.tmpfiles" /etc/tmpfiles.d/agent-svc.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/agent-svc.conf

sudo systemd-analyze verify /etc/systemd/system/agent-svc.service
sudo systemctl daemon-reload
echo "unit, sudoers and tmpfiles installed"
REMOTE_F

if $start_service; then
  echo "== g) enable and start =="
  ssh -o BatchMode=yes netcup bash -s <<'REMOTE_G'
set -euo pipefail
sudo systemctl enable --now agent-svc
sleep 2
systemctl is-active agent-svc
REMOTE_G
else
  echo "agent-svc installed but not started (pass --start to enable it)."
fi

echo "Installed agent-svc for $commit"
