#!/usr/bin/env bash
# Hot-patch a RUNNING unit without rebuilding the image.
#
# For iterating on the rig only. It copies working-tree files straight into the container and
# restarts just the affected process, turning a ~4 min build+deploy into a few seconds.
#
#   docker/hotpatch.sh <host> backend            # backend/scripts/** -> /app/scripts, restart audio
#   docker/hotpatch.sh <host> frontend           # vite build -> /app/www, reload nginx
#   docker/hotpatch.sh <host> both
#
# The container now DIVERGES from its image tag: `docker ps` still shows the old tag, and any
# redeploy or container recreate silently reverts everything. Always finish an accepted change with
# a real build + deploy — this is a debugging tool, not a delivery mechanism.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${1:?usage: hotpatch.sh <host> [backend|frontend|both]}"
WHAT="${2:-both}"
[[ -f docker/.deploy.env ]] && { set -a; . docker/.deploy.env; set +a; }
USER_="${PLUM_TEST_USER:-plum-admin}"
PW="${PLUM_TEST_PW:?set PLUM_TEST_PW (docker/.deploy.env)}"

ssh_() { sshpass -p "$PW" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR -o NumberOfPasswordPrompts=1 "$USER_@$HOST" "$@"; }
sudo_() { ssh_ "echo '$PW' | sudo -S -p '' $*"; }

# Stage a tar on the HOST, then feed it to `docker exec` with a REDIRECT. Piping the archive over
# ssh cannot work: `sudo -S` reads its password from stdin, so tar would receive the password as its
# first bytes ("This does not look like a tar archive"). The same trap is documented in deploy.sh.
push_tar() {  # push_tar <local-dir> <tar-args...> ; extracts into <container-dest> given as $2
    local src="$1" dest="$2"; shift 2
    COPYFILE_DISABLE=1 tar --no-xattrs -C "$src" -cf "$TMPTAR" "$@" 2>/dev/null \
        || tar -C "$src" -cf "$TMPTAR" "$@"   # --no-xattrs is BSD/GNU-tar dependent
    sshpass -p "$PW" scp -q -o StrictHostKeyChecking=no -o LogLevel=ERROR "$TMPTAR" "$USER_@$HOST:/tmp/plum-hotpatch.tar"
    sudo_ "sh -c 'docker exec -i plum-audio tar -C $dest -xf - < /tmp/plum-hotpatch.tar'"
    ssh_ "rm -f /tmp/plum-hotpatch.tar"
}
TMPTAR="$(mktemp -t plum-hotpatch).tar"
trap 'rm -f "$TMPTAR"' EXIT

if [[ "$WHAT" == backend || "$WHAT" == both ]]; then
    echo "==> backend/scripts -> $HOST:/app/scripts"
    push_tar backend /app scripts
    # Both audio processes, because scripts/ is shared: a stale player against a patched server is a
    # debugging session spent chasing a version skew that only exists on that one box.
    sudo_ "docker exec plum-audio supervisorctl -c /app/supervisord/supervisord.conf restart sendspin_server sendspin_player config_api" || true
fi

if [[ "$WHAT" == frontend || "$WHAT" == both ]]; then
    echo "==> building the frontend"
    ( cd frontend && npm run build >/dev/null )
    # nginx serves /app/www (see backend/nginx), NOT a dist/ directory — copy the CONTENTS.
    echo "==> frontend/dist/* -> $HOST:/app/www"
    push_tar frontend/dist /app/www .
    sudo_ "docker exec plum-audio supervisorctl -c /app/supervisord/supervisord.conf restart nginx" || true
fi

echo "==> patched $HOST ($WHAT) — NOTE: diverged from its image tag; redeploy to make it real"
