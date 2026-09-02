#!/bin/sh
# deploy/status.sh — what is running on the burrow, against what main is.
#
# The NAS has no git, so the only thing there that can name a revision is the marker
# deploy/deploy.sh leaves beside each deploy directory. A service with no marker was last
# deployed by hand, before the script existed — which is itself the answer.
set -eu

NAS="${NAS:-Miha@dxp2800}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

main="$(git -C "$ROOT" rev-parse origin/main 2>/dev/null || git -C "$ROOT" rev-parse HEAD)"
printf '%-10s %s\n' 'main' "$main"

ssh -o BatchMode=yes -o ConnectTimeout=15 "$NAS" '
for pair in burrow:chronicle arcadia:arcadia arcadia:townhall steward:steward; do
    dir="${pair%%:*}"; svc="${pair##*:}"
    if [ -f ~/docker/$dir/DEPLOYED-$svc ]; then
        printf "%-10s %s\n" "$svc" "$(cat ~/docker/$dir/DEPLOYED-$svc)"
    else
        printf "%-10s %s\n" "$svc" "(no marker: deployed by hand before deploy/deploy.sh)"
    fi
done
echo
docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}" | grep -E "^(burrow|arcadia|steward-)" || true
' 2>/dev/null | grep -v 'post-quantum\|store now\|openssh.com/pq' || true
