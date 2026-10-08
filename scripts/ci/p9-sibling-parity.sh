#!/usr/bin/env bash
# CI helper for the cross-repo P9 hook parity check (terms-analysis#175, Copilot C3).
# Byte-identical in terms-analysis and legal-corpus-ingester.
#
# Usage:
#   p9-sibling-parity.sh resolve-ref <owner/repo> [<head-ref>]
#       Prints two lines, ready to append to GITHUB_OUTPUT: `ref=<name>`, the
#       sibling branch the parity test compares against, and `sha=<commit>`,
#       that branch's current commit. The test fetches the sibling's files at
#       the sha, which is immutable, never at the branch name, which a CDN can
#       serve stale. The branch is <head-ref> when the sibling repo has a
#       branch of that name, otherwise `main` (no head ref: a push to main; or
#       a PR that has no sibling branch). Prints nothing and exits 1 if GitHub
#       cannot be asked, has no such branch, or answers something unexpected.
#   p9-sibling-parity.sh check <pytest-node-id>
#       Runs that one test with `${PYTHON:-python} -m pytest -q` and exits 0
#       only if the final summary line says exactly one test passed: a
#       skip, a deselect, an error or a failure all exit 1.
#
# Exit codes: 0 done and passed; 1 refused or failed; 2 usage error.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

REPO_RE='^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$'

usage() {
    echo "usage: p9-sibling-parity.sh resolve-ref <owner/repo> [<head-ref>] | check <pytest-node-id>" >&2
    exit 2
}

# branch_sha <owner/repo> <branch>: prints the branch's commit sha on GitHub,
# or nothing if the repo has no such branch. Exits 1 if GitHub cannot be
# asked or answers something unexpected; the answer is never echoed.
branch_sha() {
    local repo="$1" branch="$2" listing expected
    if ! listing="$(git ls-remote --heads -- "https://github.com/${repo}" "refs/heads/${branch}" </dev/null)"; then
        echo "p9-sibling-parity: cannot ask github.com/${repo} whether branch '${branch}' exists" >&2
        exit 1
    fi
    if [ -z "${listing}" ]; then
        return 0
    fi
    expected=$'^([0-9a-f]{40}|[0-9a-f]{64})\trefs/heads/'
    if [[ "${listing}" =~ ${expected}(.*)$ ]] && [ "${BASH_REMATCH[2]}" = "${branch}" ]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi
    echo "p9-sibling-parity: unexpected answer from github.com/${repo} for branch '${branch}'" >&2
    exit 1
}

resolve_ref() {
    local repo="$1" head="${2:-}" ref="" sha=""
    if ! [[ "${repo}" =~ ${REPO_RE} ]]; then
        echo "p9-sibling-parity: the repo argument is not an owner/repo name" >&2
        exit 1
    fi
    if [ -n "${head}" ]; then
        if ! git check-ref-format --branch "${head}" >/dev/null 2>&1; then
            echo "p9-sibling-parity: the head ref is not a valid branch name" >&2
            exit 1
        fi
        sha="$(branch_sha "${repo}" "${head}")" || exit 1
        ref="${head}"
        if [ -z "${sha}" ]; then
            echo "p9-sibling-parity: ${repo} has no branch '${head}'; comparing against main" >&2
        fi
    fi
    if [ -z "${sha}" ]; then
        sha="$(branch_sha "${repo}" main)" || exit 1
        ref=main
        if [ -z "${sha}" ]; then
            echo "p9-sibling-parity: ${repo} has no main branch to compare against" >&2
            exit 1
        fi
    fi
    printf 'ref=%s\nsha=%s\n' "${ref}" "${sha}"
}

check() {
    local node="$1" out summary
    if out="$("${PYTHON:-python}" -m pytest "${node}" -q -p no:cacheprovider 2>&1)"; then
        printf '%s\n' "${out}"
    else
        printf '%s\n' "${out}"
        echo "p9-sibling-parity: the parity test failed" >&2
        exit 1
    fi
    summary="$(printf '%s\n' "${out}" | tail -n 1)"
    if [[ "${summary}" =~ ^1\ passed(,\ [0-9]+\ warnings?)?\ in\  ]]; then
        echo "p9-sibling-parity: parity test ran and passed"
        return 0
    fi
    echo "p9-sibling-parity: the parity test did not run and pass exactly once: '${summary}'" >&2
    exit 1
}

[ "$#" -ge 1 ] || usage
case "$1" in
    resolve-ref)
        [ "$#" -ge 2 ] && [ "$#" -le 3 ] || usage
        resolve_ref "$2" "${3:-}"
        ;;
    check)
        [ "$#" -eq 2 ] || usage
        check "$2"
        ;;
    *) usage ;;
esac
