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
for pair in chronicle:chronicle arcadia:arcadia arcadia:townhall steward:steward; do
    dir="${pair%%:*}"; svc="${pair##*:}"
    if [ -f ~/docker/warren/$dir/DEPLOYED-$svc ]; then
        printf "%-10s %s\n" "$svc" "$(cat ~/docker/warren/$dir/DEPLOYED-$svc)"
    else
        printf "%-10s %s\n" "$svc" "(no marker: deployed by hand before deploy/deploy.sh)"
    fi
done
# The residents checkout (warren#351): the one thing on the burrow that IS git. Read
# through the API container, the process that writes it. (No apostrophes in here: this
# whole script is one single-quoted argument to ssh.)
if docker ps --format "{{.Names}}" | grep -qx steward-api; then
    branch="$(docker exec steward-api git -C /checkout rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")"
    head="$(docker exec steward-api git -C /checkout rev-parse --short HEAD 2>/dev/null || echo "?")"
    unpushed="$(docker exec steward-api git -C /checkout rev-list --count "origin/$branch..HEAD" 2>/dev/null || echo "?")"
    dirty="$(docker exec steward-api git -C /checkout status --porcelain 2>/dev/null | wc -l | tr -d " ")"
    printf "%-10s %s @ %s, %s unpushed, %s dirty\n" "checkout" "$branch" "$head" "$unpushed" "$dirty"
fi
echo
docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}" | grep -E "^(chronicle|arcadia|steward-)" || true
' 2>/dev/null | grep -v 'post-quantum\|store now\|openssh.com/pq' || true
