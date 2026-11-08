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
for sha in $(gh api "repos/${repo}/actions/workflows/${workflow}/runs?status=success&per_page=30${filter}" --jq '.workflow_runs[].head_sha'); do
    t=$(gh api "repos/${repo}/git/commits/${sha}" --jq '.tree.sha')
    if [ "$t" = "$tree" ]; then
        tested=true
        break
    fi
done
# PR runs test the merge ref, whose tree differs from head_sha when the PR was
# behind its base; those runs record tested-tree-<tree> artifacts. Review runs
# (pull_request_review) carry the gate verdict, so both event kinds count.
if [ "$tested" != true ]; then
    for event in pull_request pull_request_review; do
        if [ "$tested" = true ]; then
            break
        fi
        for id in $(gh api "repos/${repo}/actions/workflows/${workflow}/runs?event=${event}&status=success&per_page=20${filter}" --jq '.workflow_runs[].id' || true); do
            if gh api "repos/${repo}/actions/runs/${id}/artifacts?per_page=100" --jq '.artifacts[].name' 2>/dev/null | grep -qx "tested-tree-${tree}"; then
                tested=true
                break
            fi
        done
    done
fi
echo "Same tree ${tree} already tested: ${tested}." >&2
echo "$tested"
