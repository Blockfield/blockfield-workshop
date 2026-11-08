#!/bin/sh
# ci-same-tree.sh: answer whether a green pull_request / pull_request_review run
# already tested HEAD's tree, so main pushes skip pure re-verification (same
# lookup as blockfield-mod release.yml). Prints "true" or "false".
# Optional second argument limits the run lookup to one head branch
# (e.g. ci-same-tree.sh check.yml next for the next->main release merge).
set -eu
workflow="${1:?usage: ci-same-tree.sh <workflow-file> [branch]}"
branch="${2:-}"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
tree=$(git rev-parse 'HEAD^{tree}')
filter=""
if [ -n "$branch" ]; then
    filter="&branch=${branch}"
fi
tested=false
# Only the tested-tree-<tree> artifact counts: the build job uploads it after its
# gates pass, while a run whose build job was skipped still concludes success.
# PR runs test the merge ref, so the artifact (not head_sha) carries the tree.
# Fork runs execute their own workflow copy, so only same-repo heads count.
for event in pull_request pull_request_review; do
    if [ "$tested" = true ]; then
        break
    fi
    for id in $(gh api "repos/${repo}/actions/workflows/${workflow}/runs?event=${event}&status=success&per_page=20${filter}" --jq ".workflow_runs[] | select(.head_repository.full_name == \"${repo}\") | .id" || true); do
        if gh api "repos/${repo}/actions/runs/${id}/artifacts?per_page=100" --jq '.artifacts[].name' 2>/dev/null | grep -qx "tested-tree-${tree}"; then
            tested=true
            break
        fi
    done
done
echo "Same tree ${tree} already tested: ${tested}." >&2
echo "$tested"
