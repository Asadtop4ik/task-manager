#!/usr/bin/env bash
# Run interactively on the owner's Mac. The token is never printed or passed in argv.
set +x
set -euo pipefail

if [ ! -t 0 ]; then
  echo 'Run this script interactively in Terminal.' >&2
  exit 1
fi
for command in gh ssh; do
  command -v "$command" >/dev/null || { echo "Missing $command" >&2; exit 1; }
done

read -r -s -p 'Paste the fine-grained PAT for Qurbot, Kans Shop and Ketoshop: ' agent_public_token
printf '\n'
trap 'unset agent_public_token' EXIT
if ! [[ "$agent_public_token" =~ ^github_pat_[A-Za-z0-9_]{20,}$ ]]; then
  echo 'Expected a GitHub fine-grained personal access token.' >&2
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
printf '%s\n' "$agent_public_token" \
  | ssh netcup /usr/bin/python3 /opt/task-manager/ops/update_public_agent_token.py
echo 'Publisher secret and server cancellation token saved; public flag is unchanged.'
