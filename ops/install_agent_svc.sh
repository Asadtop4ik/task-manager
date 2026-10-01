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
# Node + Codex CLI, both downloaded fresh and hash-verified against ops/agent-svc-node.lock
# and ops/agent-svc-codex.lock — never copied from codex-runner's home, which any
# Actions job can write to. agent-svc only ever reaches agent-codex's files through
# `sudo -u agent-codex`, never as root.
#
# If agent-svc is already running, this script stops it once at the very start (so
# no step below mutates files out from under a live process) and restarts it once at
# the very end, after every file (code, node, codex-cli, tools, credentials, config,
# unit, sudoers, tmpfiles) is in place.
#
# Usage: ops/install_agent_svc.sh [--start]
#   (no args)  Install/update everything. If agent-svc was already active it ends
#              active again; otherwise it is left disabled and stopped.
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
  scripts/agent_ops_policy.py
  backend/app/services/agent_repos.py
  ops/agent-svc.service
  ops/agent-svc.sudoers
  ops/agent-svc.tmpfiles
  ops/agent-svc-tools.lock
  ops/agent-svc-node.lock
  ops/agent-svc-codex.lock
  ops/env_file_lock.py
  ops/sync_agent_svc_credentials.py
  ops/agent-ops-apply.service
  ops/ops-allowlist.example.json
)
for path in "${required_paths[@]}"; do
  test -e "$path" || {
    echo "Missing $path — merge every agent-svc work package before installing." >&2
    exit 1
  }
done

echo "Installing agent-svc for commit $commit"

# Stop once, at the very beginning, before anything on the server is touched: every
# step below (code, node, codex-cli, tools, credentials, config, unit, sudoers,
# tmpfiles) should apply to a quiescent install, not a live one. Restarted (only if
# it was running) once, at the very end, after all of it is in place.
was_active=$(ssh -o BatchMode=yes netcup '
  set -euo pipefail
  if sudo systemctl is-active --quiet agent-svc 2>/dev/null; then
    echo "agent-svc is active; stopping for the update" >&2
    sudo systemctl stop agent-svc
    echo true
  else
    echo false
  fi
')
echo "agent-svc was active before this update: $was_active"

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
  scripts/agent_ops_policy.py \
  backend/app/services/agent_repos.py \
  ops/env_file_lock.py \
  | tar -x -C "$raw_dir"

mkdir -p "$stage_dir/pkg/trusted"
mv "$raw_dir/agentsvc/agent_svc" "$stage_dir/pkg/agent_svc"
# agentsvc/libexec already carries env_apply.py (agent-ops root helper, WP-D) --
# no separate archive entry needed, it rides along with the whole directory.
mv "$raw_dir/agentsvc/libexec" "$stage_dir/pkg/libexec"
mv "$raw_dir/agentsvc/codex" "$stage_dir/pkg/codex"
mv "$raw_dir/scripts/"*.py "$stage_dir/pkg/trusted/"
mv "$raw_dir/backend/app/services/agent_repos.py" "$stage_dir/pkg/trusted/"
# env_apply.py loads this by path from trusted/ too (non-blocking lock retry
# reimplemented on top of its sibling-lock-file convention -- see env_apply.py's
# module docstring); ops/sync_agent_svc_credentials.py keeps using its own
# separately-scp'd copy below, unrelated to this trusted/ one.
mv "$raw_dir/ops/env_file_lock.py" "$stage_dir/pkg/trusted/"
# No macOS extended attributes in the archive (GNU tar on the server warns about them).
COPYFILE_DISABLE=1 tar --no-xattrs -C "$stage_dir/pkg" -czf "$stage_dir/code.tar.gz" \
  agent_svc libexec codex trusted

remote_dir=$(ssh -o BatchMode=yes netcup 'mktemp -d /tmp/agent-svc-install.XXXXXX')
[[ "$remote_dir" =~ ^/tmp/agent-svc-install\.[A-Za-z0-9]+$ ]] || exit 1

scp -q -o BatchMode=yes "$stage_dir/code.tar.gz" \
  ops/agent-svc.service ops/agent-svc.sudoers ops/agent-svc.tmpfiles \
  ops/agent-svc-tools.lock ops/agent-svc-node.lock ops/agent-svc-codex.lock \
  ops/env_file_lock.py ops/sync_agent_svc_credentials.py \
  ops/agent-ops-apply.service ops/ops-allowlist.example.json \
  agentsvc/config.example.json \
  "netcup:$remote_dir/"

echo "== a) system group, agent-svc user, agent-codex user =="
ssh -o BatchMode=yes netcup bash -s <<'REMOTE_A'
set -euo pipefail
getent group agentwork >/dev/null || sudo groupadd --system agentwork

ensure_system_user() {
  local name="$1" home="$2"
  if id -u "$name" >/dev/null 2>&1; then
    return 0
  fi
  if getent group "$name" >/dev/null; then
    sudo useradd --system --gid "$name" \
      --home-dir "$home" --shell /usr/sbin/nologin "$name"
  else
    sudo useradd --system --user-group \
      --home-dir "$home" --shell /usr/sbin/nologin "$name"
  fi
}

ensure_system_user agent-svc /nonexistent
sudo usermod -aG agentwork agent-svc

ensure_system_user agent-codex /home/agent-codex
sudo usermod -aG agentwork agent-codex
# Phase 3 (chat lane): the read-only Ketoshop diagnostics socket
# (/run/task-manager-diagnostics/diagnostics.sock, mode 0660) is owned by
# group `codex-runner` today -- ops/task-manager-diagnostics.service's own
# `Group=`, deliberately left unchanged so the OLD ops/discussion_appserver.py
# path (still live until Phase 5) keeps working with no ordering dependency
# on anything this installer does. Codex's app-server now connects to that
# socket as `agent-codex`, but `agent-codex` itself is deliberately NOT made
# a member of `codex-runner` (that account is the legacy GitHub Actions
# runner identity -- far broader than "may read one socket").
# Instead: a dedicated group, `task-diag-client`, owns ONLY this one socket
# (diagnostic_host.py chgrp's it there after bind, best-effort, if the group
# exists); `codex-runner` joins it too (so the legacy client keeps working
# once diagnostic_host.py's chgrp takes effect); `agent-codex` is never a
# permanent member of it at all -- its sudoers rule for the `discussion`
# subcommand only (see ops/agent-svc.sudoers) grants that group for the
# duration of that one sudo'd call (`sudo -g task-diag-client`), nothing else.
getent group task-diag-client >/dev/null || sudo groupadd --system task-diag-client
if getent group codex-runner >/dev/null; then
  sudo usermod -aG task-diag-client codex-runner
fi
# The diagnostics broker must itself be in the group to chgrp its socket (no
# CAP_CHOWN); takes effect when task-manager-diagnostics.service restarts.
if id -u task-diagnostics >/dev/null 2>&1; then
  sudo usermod -aG task-diag-client task-diagnostics
fi
if sudo test -L /home/agent-codex; then
  echo "/home/agent-codex is a symlink; refusing" >&2
  exit 1
fi
if ! sudo test -e /home/agent-codex; then
  sudo install -d -m 0700 -o agent-codex -g agent-codex /home/agent-codex
fi

echo "agent-svc and agent-codex users/groups ready"
REMOTE_A

echo "== b) code release (git-archived tree -> releases dir -> atomic symlink swap) =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" "$commit" <<'REMOTE_B'
set -euo pipefail
remote_dir="$1"
commit="$2"

sudo install -d -m 0755 -o root -g root /opt/agent-svc
sudo install -d -m 0755 -o root -g root /opt/agent-svc/releases

release_dir="/opt/agent-svc/releases/$commit"
if sudo test -d "$release_dir/agent_svc" && sudo test -d "$release_dir/libexec" \
  && sudo test -d "$release_dir/codex" && sudo test -d "$release_dir/trusted"; then
  echo "release $commit already present; reusing (never rebuilt or removed while current)"
else
  # Only reached when this release does not yet exist, or exists but is incomplete
  # (e.g. left behind by a crashed previous run) — never for a directory currently
  # in use, since a complete one is never rebuilt.
  sudo rm -rf "$release_dir"
  staging=$(sudo mktemp -d /opt/agent-svc/releases/.stage.XXXXXX)
  sudo tar -C "$staging" -xzf "$remote_dir/code.tar.gz"
  sudo chown -R root:root "$staging"
  sudo find "$staging" -type d -exec chmod 0755 {} +
  sudo find "$staging" -type f -exec chmod 0644 {} +
  sudo mv "$staging" "$release_dir"
  echo "release $commit built"
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

# Keep the 3 newest releases (by mtime) plus whichever release is current, even if
# current is older than those 3 (so a rollback target stays available).
current_target=$(basename "$(readlink -f /opt/agent-svc/current)")
kept=0
for old in $(ls -1t /opt/agent-svc/releases); do
  [ "$old" = "$current_target" ] && continue
  kept=$((kept + 1))
  if [ "$kept" -gt 3 ]; then
    sudo rm -rf "/opt/agent-svc/releases/$old"
  fi
done

echo "code installed: /opt/agent-svc/current -> releases/$commit"
REMOTE_B

echo "== c) pinned Node 24 + Codex CLI (downloaded + hash-verified, root-owned) =="
ssh -o BatchMode=yes netcup bash -s -- "$remote_dir" <<'REMOTE_C'
set -euo pipefail
remote_dir="$1"
# shellcheck disable=SC1090,SC1091
. "$remote_dir/agent-svc-node.lock"
# shellcheck disable=SC1090,SC1091
. "$remote_dir/agent-svc-codex.lock"

node_dir=/opt/agent-svc/node24
current_node_version=""
if [ -x "$node_dir/bin/node" ]; then
  current_node_version=$(sudo "$node_dir/bin/node" --version 2>/dev/null || true)
fi
if [ "$current_node_version" = "$NODE_VERSION" ]; then
  echo "node24 already installed: $current_node_version"
else
  # Downloaded fresh from nodejs.org and SHA-256 verified — never copied from
  # codex-runner's home, which any GitHub Actions job running there can write to.
  work=$(sudo mktemp -d /opt/agent-svc/.node24-download.XXXXXX)
  sudo curl -fsSL -o "$work/$NODE_TARBALL_NAME" "$NODE_TARBALL_URL"
  actual_sha256=$(sudo sha256sum "$work/$NODE_TARBALL_NAME" | awk '{print $1}')
  if [ "$actual_sha256" != "$NODE_TARBALL_SHA256" ]; then
    echo "node tarball sha256 mismatch: expected $NODE_TARBALL_SHA256, got $actual_sha256" >&2
    sudo rm -rf "$work"
    exit 1
  fi
  staging=$(sudo mktemp -d /opt/agent-svc/.node24-stage.XXXXXX)
  sudo tar -C "$staging" --strip-components=1 -xJf "$work/$NODE_TARBALL_NAME"
  sudo rm -rf "$work"
  sudo chown -R root:root "$staging"
  # Defense in depth only: the official tarball already ships correct modes
  # (binaries executable, everything else not group/other-writable).
  sudo find "$staging" -perm /go+w -exec chmod go-w {} +
  sudo rm -rf "$node_dir"
  sudo mv "$staging" "$node_dir"
  installed=$(sudo "$node_dir/bin/node" --version)
  test "$installed" = "$NODE_VERSION" || {
    echo "node --version mismatch after install: $installed" >&2
    exit 1
  }
  echo "node24 installed: $installed"
fi
# `mktemp -d` staging dirs are 0700; the top-level dir must stay traversable so
# agent-codex (and agent-svc) can run the pinned node. Enforced on every run.
sudo chmod 0755 "$node_dir"

codex_cli_dir=/opt/agent-svc/codex-cli
codex_version=""
if [ -x "$codex_cli_dir/bin/codex" ]; then
  codex_version=$(sudo -u agent-codex "$node_dir/bin/node" "$codex_cli_dir/bin/codex" --version 2>/dev/null || true)
fi
case "$codex_version" in
  *"$CODEX_VERSION"*)
    echo "codex-cli already installed: $codex_version"
    ;;
  *)
    work=$(sudo mktemp -d /opt/agent-svc/.codex-download.XXXXXX)
    sudo curl -fsSL -o "$work/$CODEX_MAIN_TARBALL_NAME" "$CODEX_MAIN_TARBALL_URL"
    sudo curl -fsSL -o "$work/$CODEX_LINUX_X64_TARBALL_NAME" "$CODEX_LINUX_X64_TARBALL_URL"
    for spec in \
      "$CODEX_MAIN_TARBALL_NAME=$CODEX_MAIN_INTEGRITY" \
      "$CODEX_LINUX_X64_TARBALL_NAME=$CODEX_LINUX_X64_INTEGRITY"
    do
      fname="${spec%%=*}"
      integrity="${spec#*=}"
      expected="${integrity#sha512-}"
      actual=$(sudo openssl dgst -sha512 -binary "$work/$fname" | sudo openssl base64 -A)
      if [ "$actual" != "$expected" ]; then
        echo "$fname sha512 mismatch (expected $expected, got $actual)" >&2
        sudo rm -rf "$work"
        exit 1
      fi
    done

    # `npm install --prefix DIR pkg` (no --global) would put the binary under
    # DIR/lib/node_modules/.bin, not DIR/bin — --global is required to get
    # DIR/bin/codex. Installing from local files means npm does not know the
    # registry's "name@npm:spec" alias for the native package (both tarballs'
    # own package.json say "name": "@openai/codex"), so the alias name is given
    # explicitly here (see ops/agent-svc-codex.lock for why).
    sudo rm -rf "$codex_cli_dir"
    sudo install -d -m 0755 -o root -g root "$codex_cli_dir"
    # npm is a `#!/usr/bin/env node` script and sudo's secure_path has no node,
    # so run it with the pinned node first on PATH.
    sudo env PATH="$node_dir/bin:/usr/bin:/bin" "$node_dir/bin/npm" install \
      --global --prefix "$codex_cli_dir" \
      --ignore-scripts --no-audit --no-fund \
      "$work/$CODEX_MAIN_TARBALL_NAME" \
      "${CODEX_LINUX_X64_ALIAS}@file:$work/$CODEX_LINUX_X64_TARBALL_NAME"
    sudo rm -rf "$work"
    sudo chown -R root:root "$codex_cli_dir"

    installed=$(sudo -u agent-codex "$node_dir/bin/node" "$codex_cli_dir/bin/codex" --version)
    case "$installed" in
      *"$CODEX_VERSION"*) echo "codex-cli installed: $installed" ;;
      *)
        echo "codex-cli --version did not report $CODEX_VERSION: $installed" >&2
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
      ruff) actual=$(printf '%s\n' "$raw" | awk 'NR==1{print $2}') ;;
      black) actual=$(printf '%s\n' "$raw" | awk 'NR==1{print $2}' | tr -d ',') ;;
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
sudo python3 -B "$remote_dir/sync_agent_svc_credentials.py"
REMOTE_F

echo "== g) config, unit, sudoers, tmpfiles =="
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

# `systemd-analyze verify` rejects a dot-prefixed filename outright ("Failed to
# prepare filename …: Invalid argument") — it is not the same "hidden from the
# directory scan" trick sudoers' #includedir uses, so it needs a *normally named*
# copy in a private, non-unit-search-path temp dir to check first. Only after that
# passes does the dot-prefixed atomic-install dance happen in the real unit
# directory (still invisible to systemd's directory scan; daemon-reload after the
# mv is what makes systemd forget it was ever there under the dotted name too).
verify_dir=$(sudo mktemp -d /tmp/agent-svc-unit-verify.XXXXXX)
sudo install -o root -g root -m 0644 "$remote_dir/agent-svc.service" "$verify_dir/agent-svc.service"
sudo systemd-analyze verify "$verify_dir/agent-svc.service"
sudo rm -rf "$verify_dir"

staged_unit=/etc/systemd/system/.agent-svc.service.tmp
sudo install -o root -g root -m 0644 "$remote_dir/agent-svc.service" "$staged_unit"
sudo mv -T "$staged_unit" /etc/systemd/system/agent-svc.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/agent-svc.service

# Same staged-verify-then-install shape for sudoers: sudo's #includedir skips any
# file with a "." in its name, so this staged copy is never active either, and
# visudo -cf (unlike systemd-analyze verify) tolerates the dotted filename directly.
staged_sudoers=/etc/sudoers.d/.60-agent-svc.tmp
sudo install -o root -g root -m 0440 "$remote_dir/agent-svc.sudoers" "$staged_sudoers"
sudo visudo -cf "$staged_sudoers"
sudo mv -T "$staged_sudoers" /etc/sudoers.d/60-agent-svc
sudo visudo -c

sudo install -o root -g root -m 0644 \
  "$remote_dir/agent-svc.tmpfiles" /etc/tmpfiles.d/agent-svc.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/agent-svc.conf

# agent-ops-apply.service (agent "ops requests", WP-D): same staged-verify-then-
# install shape as agent-svc.service above. Deliberately never `enable`d or
# `start`ed here (and it has no [Install] section to enable) -- it only ever
# runs on demand, via ops/agent-svc.sudoers' `systemctl start` rule or an
# operator's manual rollback invocation.
verify_dir=$(sudo mktemp -d /tmp/agent-svc-unit-verify.XXXXXX)
sudo install -o root -g root -m 0644 \
  "$remote_dir/agent-ops-apply.service" "$verify_dir/agent-ops-apply.service"
sudo systemd-analyze verify "$verify_dir/agent-ops-apply.service"
sudo rm -rf "$verify_dir"

staged_ops_unit=/etc/systemd/system/.agent-ops-apply.service.tmp
sudo install -o root -g root -m 0644 "$remote_dir/agent-ops-apply.service" "$staged_ops_unit"
sudo mv -T "$staged_ops_unit" /etc/systemd/system/agent-ops-apply.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/agent-ops-apply.service

# Root-owned allowlist of NON-secret env keys agent-ops-apply.service is ever
# allowed to touch (agent_ops_policy.py's `load_allowlist`/`parse_allowlist`
# independently re-check ownership/permissions/shape regardless of this
# install step). Left alone if already present, exactly like config.json above
# -- an operator's hand-edited allowlist (adding a project) must never be
# clobbered by a later re-install. The shipped example has an empty
# "projects" map, i.e. the feature is off until an operator adds an entry.
sudo install -d -m 0755 -o root -g root /etc/agent-svc
if [ -f /etc/agent-svc/ops-allowlist.json ]; then
  echo "/etc/agent-svc/ops-allowlist.json already exists; left unchanged"
else
  sudo install -o root -g root -m 0644 \
    "$remote_dir/ops-allowlist.example.json" /etc/agent-svc/ops-allowlist.json
  echo "Installed default (empty) /etc/agent-svc/ops-allowlist.json"
fi

echo "config, unit, sudoers and tmpfiles installed"
REMOTE_G

echo "== h) restart agent-svc if it was active before this update =="
ssh -o BatchMode=yes netcup bash -s -- "$was_active" <<'REMOTE_H'
set -euo pipefail
was_active="$1"
if [ "$was_active" = "true" ]; then
  sudo systemctl start agent-svc
  systemctl is-active agent-svc
else
  echo "agent-svc was not active before this update; leaving it stopped"
fi
REMOTE_H

if $start_service; then
  echo "== i) enable and start =="
  ssh -o BatchMode=yes netcup bash -s <<'REMOTE_I'
set -euo pipefail
sudo systemctl enable --now agent-svc
sleep 2
systemctl is-active agent-svc
REMOTE_I
else
  echo "agent-svc installed but not started (pass --start to enable it)."
fi

echo "Installed agent-svc for $commit"
