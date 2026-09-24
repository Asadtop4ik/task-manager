#!/usr/bin/env bash
# Sync the reviewed Task Manager catalog and its two local consumers to netcup.
set -euo pipefail

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
  echo 'Deploy the exact main commit before updating the server catalog.' >&2
  exit 1
}

remote_dir=$(ssh -o BatchMode=yes netcup 'mktemp -d /tmp/taskmgr-catalog.XXXXXX')
[[ "$remote_dir" =~ ^/tmp/taskmgr-catalog\.[A-Za-z0-9]+$ ]] || exit 1
cleanup() { ssh -o BatchMode=yes netcup "rm -rf -- '$remote_dir'"; }
trap cleanup EXIT

scp -q backend/app/services/agent_repos.py ops/project_catalog.py \
  ops/intake_worker.py ops/discussion_appserver.py \
  ops/agent_deploy_monitor.py "netcup:$remote_dir/"
ssh -o BatchMode=yes netcup "
  sudo install -o root -g root -m 0644 '$remote_dir/agent_repos.py' /opt/task-manager/ops/agent_repos.py &&
  sudo install -o root -g root -m 0644 '$remote_dir/project_catalog.py' /opt/task-manager/ops/project_catalog.py &&
  sudo install -o root -g root -m 0644 '$remote_dir/intake_worker.py' /opt/task-manager/ops/intake_worker.py &&
  sudo install -o root -g root -m 0644 '$remote_dir/discussion_appserver.py' /opt/task-manager/ops/discussion_appserver.py &&
  sudo install -o root -g root -m 0644 '$remote_dir/agent_deploy_monitor.py' /opt/task-manager/ops/agent_deploy_monitor.py &&
  sudo systemctl restart task-manager-intake.service &&
  sudo systemctl start task-manager-external-monitor.service &&
  systemctl is-active task-manager-intake.service task-manager-external-monitor.timer
"
echo "Installed agent project catalog for $commit"
