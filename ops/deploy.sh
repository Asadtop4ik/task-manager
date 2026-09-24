#!/usr/bin/env bash
# Pull a new image for one stack and restart it. Called by GitHub Actions over
# SSH; also the thing to run by hand for a rollback.
#
#   ./deploy.sh qurbot            # deploy whatever :latest points at
#   ./deploy.sh qurbot 4ab0b16    # deploy/roll back to a specific commit sha
set -euo pipefail

STACK="${1:?usage: deploy.sh <kans-shop|ketoshop|qurbot|task-manager|kitob-challenge> [image-tag]}"
export IMAGE_TAG="${2:-latest}"

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f stacks/${STACK}.yml"

echo "==> $STACK @ $IMAGE_TAG"

previous_tag=""
if [ "$STACK" = "task-manager" ]; then
	previous_image=$(docker inspect --format '{{.Config.Image}}' task-api 2>/dev/null || true)
	previous_tag="${previous_image##*:}"
	[[ "$previous_tag" =~ ^[0-9a-f]{40}$ ]] || previous_tag=""
	echo "PREVIOUS_SHA=$previous_tag"
fi

verify_task_manager_images() {
	local tag="$1" service image actual
	for service in task-api task-bot task-worker task-frontend; do
		case "$service" in
			task-api) image="ghcr.io/asadtop4ik/task-manager-api:$tag" ;;
			task-bot|task-worker) image="ghcr.io/asadtop4ik/task-manager-bot:$tag" ;;
			task-frontend) image="ghcr.io/asadtop4ik/task-manager-frontend:$tag" ;;
		esac
		actual=$(docker inspect --format '{{.Config.Image}}' "$service") || return 1
		if [ "$actual" != "$image" ]; then
			echo "!! $service is running $actual; expected $image" >&2
			return 1
		fi
	done
}

verify_task_manager_ready() {
	curl --fail --silent --show-error --retry 5 --retry-delay 2 --max-time 10 \
		https://tasks.standart-eko.uz/ready \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"] == "ok" and d["checks"] == {"postgres":"ok","redis":"ok"}'
}

rollback_task_manager() {
	if [ -z "$previous_tag" ] || [ "$previous_tag" = "$IMAGE_TAG" ]; then
		echo "!! no prior task-manager SHA available for rollback" >&2
		return 1
	fi
	echo "!! rolling task-manager back to $previous_tag" >&2
	export IMAGE_TAG="$previous_tag"
	$COMPOSE pull || echo "!! rollback pull failed; using local image" >&2
	$COMPOSE up -d --remove-orphans --wait --wait-timeout 180 || return 1
	verify_task_manager_images "$previous_tag" || return 1
	verify_task_manager_ready || return 1
	echo "==> rollback verified at $previous_tag"
}

fail_task_manager() {
	echo "!! task-manager deployment failed: $1" >&2
	rollback_task_manager || echo "!! automatic rollback also failed" >&2
	exit 1
}

# A failed pull is not fatal on its own. CI hands us a fresh GHCR token, but a
# rollback run by hand has no token, and the image for a sha we deployed before
# is normally still on this box. So warn, carry on, and let `up -d` be the thing
# that fails if the image genuinely isn't here.
$COMPOSE pull || echo "!! pull failed — falling back to whatever is already on this box" >&2

if [ "$STACK" = "task-manager" ]; then
	$COMPOSE up -d --remove-orphans --wait --wait-timeout 180 \
		|| fail_task_manager "Compose health"
else
	$COMPOSE up -d --remove-orphans --wait --wait-timeout 180
fi

# A green deploy must be the requested image, not an old container left behind
# after a pull failure. Task Manager is the first stack using this assertion;
# extend the map to other stacks after their image layouts are audited.
if [ "$STACK" = "task-manager" ]; then
	verify_task_manager_images "$IMAGE_TAG" || fail_task_manager "image SHA mismatch"
	verify_task_manager_ready || fail_task_manager "public readiness"
	echo "==> verified task-manager image tag $IMAGE_TAG on all services"
fi

# `--wait` exits non-zero if a service never becomes running/healthy, so CI can
# no longer report success while a migration is crash-looping in production.
$COMPOSE ps
echo "--- last 40 log lines ---"
$COMPOSE logs --tail=40

# Reclaim disk from the image we just replaced. Without this, /var/lib/docker
# grows by an image per deploy until the disk fills.
docker image prune -af --filter "until=168h" >/dev/null || true
