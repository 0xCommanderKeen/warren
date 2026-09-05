# Living work queue (#466)

The Queue page in Townhall reads `GET /queue`: open issues and labels, explicit blocker
states and chains, provably stale blocked labels, open PR mergeability, and issues closed
since a UTC timestamp. Issue order is numeric; only Karen's note expresses a ranking.
Unknown references, cross-repository blockers and ambiguous dependency prose prevent
stale-label certification. Dependency chains retain cycles and report display truncation.

Decision recorded before implementation in
[#466](https://github.com/0xCommanderKeen/warren/issues/466#issuecomment-5554618511):
Townhall is the surface because facts can remain computed beside attributed judgement.
#441 remains the manifest-derived org projection. Karen owns daily queue review and the
future weekly staff review (#444); this adds no competing reporter or staff-data model.

## Configuration

Set `STEWARD_QUEUE_REPOSITORY=owner/repository` on the API; without it `/queue` answers
503 with a configuration explanation. `STEWARD_QUEUE_REPORTER` defaults to `karen`.
The deployment Compose file supplies the Warren repository and Karen. GitHub reads
are cached for five minutes and include the observation timestamp. Failed refreshes
answer 503, rather than passing old data off as a fresh or empty queue. Retries back off
for a minute. Unknown PR mergeability stays UNKNOWN until a later refresh; MERGEABLE
means the GitHub mergeability field, not that CI or branch-protection checks passed.

Public repositories can be read without a GitHub credential, but the unauthenticated
rate limit can be exhausted. Configure an optional read-only `STEWARD_QUEUE_GITHUB_TOKEN`
through the existing secret directory or environment for sustained/private access. Only
issue and pull-request reads are used; no tracker writes are implemented. The API reads
this credential on startup, so restart it after rotation. Requests have response-size,
pagination and refresh-time limits. A partial issue inventory is rejected.

For local evidence: `steward queue --repository owner/repository --since
2026-09-05T00:00:00Z --ranked 466` prints the same mechanical projection without the
resident note. Repeat `--ranked` to obtain each recommendation's current issue state.
The reader uses GitHub REST issue pagination (which includes PRs), and individual PR
reads for the nullable mergeability field:
[issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues),
[pulls](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request).

## Activate the reporting routine

Karen is managed on the burrow's declarations branch, not among main's seed residents.
After #415 completes her runtime and model-login setup, grant the non-default `queue-review` skill through the existing
operator approval/declaration flow. Add the routine from `queue-review-routine.yaml` to
her declaration; review her model budget and runtime access, then enable it. This routine
needs a writable memory directory, git/HTTPS to the configured repository and access to
the existing Steward read API with its scoped session credential. It does not need a
mount of the deployment checkout or GitHub write permissions.

`queue-review.json` lives directly in Karen's memory. The API opens only that fixed name,
rejects symlinks and invalid/oversized documents, and requires its run ID and repository
to match the latest successful `queue-review` receipt. The prose is resident-authored;
structural evidence validation does not certify that its conclusions are correct.
Published reasons and evidence use Steward’s shared credential redaction policy.
A failed latest review suppresses the note and displays the failed receipt. Live states
of the recommended numbers come from the tracker observation, even if the note is older.

## Acceptance still requiring a live resident

No resident review has been run by this implementation. After runtime/login setup, trigger the
routine once, read the actual run receipt and verify Townhall's note and every cited
claim. Record that receipt on #466 before closing it. Unit fixtures are not this evidence.
