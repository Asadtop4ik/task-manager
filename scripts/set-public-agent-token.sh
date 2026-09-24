#!/usr/bin/env bash
# Provision only the three public project repositories for the trusted publisher.
set -euo pipefail

read -r -s -p 'Paste the fine-grained PAT for Qurbot, Kans Shop and Ketoshop: ' agent_public_token
printf '\n'
trap 'unset agent_public_token' EXIT
if [ "${#agent_public_token}" -lt 20 ]; then
  echo 'Token is empty or too short.' >&2
  exit 1
fi

for repo in qurbot kans-shop ketoshop; do
  permission=$(GH_TOKEN="$agent_public_token" gh api "repos/muradjanov-dev/$repo" --jq '.permissions.push')
  if [ "$permission" != true ]; then
    echo "Token cannot push to muradjanov-dev/$repo." >&2
    exit 1
  fi
done

# gh encrypts the value locally. Stdin avoids an argv or temporary file copy.
printf '%s' "$agent_public_token" \
  | gh secret set AGENT_PUBLIC_REPO_TOKEN -R Asadtop4ik/task-manager
echo 'Actions secret AGENT_PUBLIC_REPO_TOKEN saved for Task Manager.'
