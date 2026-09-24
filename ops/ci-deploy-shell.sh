#!/usr/bin/env bash
# Forced command for the GitHub Actions deploy key. Keep the deployment key
# limited to an allowlisted stack and verify Task Manager's versioned helper.
set -euo pipefail

log() { logger -t ci-deploy -- "$*"; echo "$*" >&2; }

cmd="${SSH_ORIGINAL_COMMAND:-}"
if [ -z "$cmd" ]; then
	log "refused: interactive login attempt from ${SSH_CONNECTION%% *}"
	exit 1
fi

# shellcheck disable=SC2086  # every split field is validated below
set -- $cmd
if [ "$#" -lt 2 ] || [ "$#" -gt 4 ] || [ "$1" != "/srv/stack/scripts/deploy.sh" ]; then
	log "refused: invalid deploy command"
	exit 1
fi

stack="$2"
tag="${3:-latest}"
case "$stack" in
	kans-shop|ketoshop|qurbot|task-manager|kitob-challenge) ;;
	*) log "refused: unknown stack '$stack'"; exit 1 ;;
esac
[[ "$tag" =~ ^(latest|[0-9a-f]{7,40})$ ]] || { log "refused: bad tag '$tag'"; exit 1; }

if [ "$stack" = "task-manager" ]; then
	if [ "$#" -ne 4 ] || ! [[ "$4" =~ ^[0-9a-f]{64}$ ]]; then
		log "refused: task-manager requires a deploy-helper SHA-256"
		exit 1
	fi
	actual_hash=$(sha256sum /srv/stack/scripts/deploy.sh | cut -d' ' -f1)
	if [ "$actual_hash" != "$4" ]; then
		log "refused: deployed helper differs from CI's reviewed helper"
		exit 1
	fi
elif [ "$#" -eq 4 ]; then
	log "refused: unexpected helper hash for $stack"
	exit 1
fi

# GHCR credentials arrive only on stdin and expire with this deploy job.
ghcr_token=""
if IFS= read -r -t 10 ghcr_token && [ -n "$ghcr_token" ]; then
	printf '%s' "$ghcr_token" \
		| docker login ghcr.io --username x-access-token --password-stdin >/dev/null \
		&& log "ghcr login ok" \
		|| log "ghcr login FAILED — continuing, the pull may still work"
	unset ghcr_token
else
	log "no ghcr token on stdin — pulling anonymously"
fi

log "deploy $stack @ $tag"
exec /srv/stack/scripts/deploy.sh "$stack" "$tag"
