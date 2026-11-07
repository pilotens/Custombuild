#!/usr/bin/env bash
# Exercise the previous broker volume and the exact image about to be scanned.
set -euo pipefail
candidate=${1:?Pass the locally built Redis image}
old='redis:7.2.16-alpine@sha256:ee061d86c25773048211e99f00d63b47b9ecfe28560113e16cce2c962d5bec1c'
volume=$(docker volume create)
name="bookcase-broker-${volume:0:16}"
password="local-upgrade-check"
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null
}
trap cleanup EXIT
start() {
    docker run -d --name "$name" --network none -v "$volume:/data" \
        -e REDIS_PASSWORD="$password" "$1" /bin/sh -ec "$2" >/dev/null
    for attempt in {1..30}; do
        if [[ $(cli ping 2>/dev/null) == PONG ]]; then return; fi
        sleep 1
    done
    docker logs "$name" >&2
    return 1
}
cli() { docker exec -e REDISCLI_AUTH="$password" "$name" redis-cli --raw "$@"; }
stop() { docker stop "$name" >/dev/null && docker rm "$name" >/dev/null; }

# Match the previous Compose invocation, including possible root-owned AOF files.
start "$old" 'exec redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"'
[[ $(cli SET custombuild:upgrade preserved) == OK ]]
stop
start "$candidate" 'exec docker-entrypoint.sh redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"'
[[ $(docker exec "$name" redis-cli PING) == *NOAUTH* ]]
docker exec "$name" sh -ec 'test "$(awk '\''/^Uid:/{print $2}'\'' /proc/1/status)" = 999'
[[ $(cli GET custombuild:upgrade) == preserved ]]
[[ $(cli SET custombuild:after-upgrade written) == OK ]]
stop
start "$candidate" 'exec docker-entrypoint.sh redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"'
[[ $(cli GET custombuild:upgrade) == preserved && $(cli GET custombuild:after-upgrade) == written ]]
