#!/usr/bin/env bash
# check-no-stale-worktrees.sh
# Worktree hygiene (terms-analysis#123): agent worktrees live under the roots
# listed in .claude/governance/worktree-roots.txt. For every root this checks
#   1. the root is ignored by a TRACKED ignore file (.gitignore), not only by
#      .git/info/exclude or a global excludes file a fresh CI clone lacks; and
#   2. nothing under the root is tracked (file, symlink or gitlink).
#
# Exit codes (documented in scripts/governance/README.md):
#   0 - clean; stdout carries the sentinel "CLEAN <n>", n = roots checked
#   1 - violation: a root is tracked-into, or not ignored by a tracked file
#   2 - config missing, unreadable, empty, a symlink, or a root is invalid
#   3 - git unavailable, not a work tree, a git query failed, or its output
#       could not be parsed; never reported as clean
#
# Output safety: the only repository data ever printed are the configured
# roots, which load validation restricts to [A-Za-z0-9._/-]. Tracked path
# names, ignore patterns, ignore-file sources and git's own stderr (which can
# carry absolute paths) are never echoed; violations are reported as counts
# per root plus the command that lists them.
#
# Git cost is fixed, not per root: one rev-parse, one ls-files and one
# check-ignore whose roots arrive on stdin (no argv growth).
#
# Written for bash 3.2 (macOS /bin/bash) and later: no associative arrays.

set -u -o pipefail
export LC_ALL=C

readonly EXIT_CLEAN=0
readonly EXIT_VIOLATION=1
readonly EXIT_CONFIG=2
readonly EXIT_GIT=3

# The one pointer to the config; every root comes from that file.
readonly CONFIG_REL=".claude/governance/worktree-roots.txt"
readonly ME="check-no-stale-worktrees"

die() {
    printf '%s: %s\n' "$ME" "$2" >&2
    exit "$1"
}

tmp=""
cleanup() {
    if [ -n "$tmp" ]; then
        rm -rf -- "$tmp"
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 1. Locate the work-tree top level (F7: check the repo, not the cwd) ---
rp_out="$(git rev-parse --is-inside-work-tree --show-cdup 2>/dev/null)"
rc=$?
if [ "$rc" -ne 0 ]; then
    die "$EXIT_GIT" "git rev-parse failed (exit $rc): git is unavailable or this is not a git work tree. Run from inside a checkout."
fi
inside="${rp_out%%$'\n'*}"
if [ "$inside" != "true" ]; then
    die "$EXIT_GIT" "not inside a git work tree. Run from inside a checkout."
fi
cdup=""
case "$rp_out" in
    *$'\n'*) cdup="${rp_out#*$'\n'}" ;;
esac
# --show-cdup is empty or a run of "../"; anything else is unexpected output.
case "$cdup" in
    "") ;;
    *[!./]*) die "$EXIT_GIT" "git rev-parse --show-cdup returned unexpected output." ;;
    *)
        rest="$cdup"
        while [ -n "$rest" ]; do
            case "$rest" in
                ../*) rest="${rest#../}" ;;
                *) die "$EXIT_GIT" "git rev-parse --show-cdup returned unexpected output." ;;
            esac
        done
        cd "./$cdup" || die "$EXIT_GIT" "cannot change to the work-tree top level."
        ;;
esac

# --- 2. Load and validate the config (F1, F5, F13: fail closed) ---
cfg_err() {
    die "$EXIT_CONFIG" "$CONFIG_REL: $1"
}

# Refuse a symlinked config or a symlinked directory on the way to it (F7).
for p in .claude .claude/governance "$CONFIG_REL"; do
    if [ -L "$p" ]; then
        cfg_err "$p is a symlink; the config must be a regular file inside the repository."
    fi
done
if [ ! -e "$CONFIG_REL" ]; then
    cfg_err "missing. Create it with one repo-relative worktree root per line."
fi
if [ ! -f "$CONFIG_REL" ]; then
    cfg_err "not a regular file."
fi
if [ ! -r "$CONFIG_REL" ]; then
    cfg_err "not readable."
fi

path_max="$(getconf PATH_MAX . 2>/dev/null)" || path_max=""
case "$path_max" in
    "" | *[!0-9]*) cfg_err "cannot determine PATH_MAX (getconf PATH_MAX .), so root length cannot be validated." ;;
esac

tmp="$(mktemp -d "${TMPDIR:-/tmp}/$ME.XXXXXX" 2>/dev/null)" || tmp=""
if [ -z "$tmp" ]; then
    die "$EXIT_GIT" "cannot create a temporary directory (check TMPDIR)."
fi

# A NUL byte is invalid anywhere in the file; line tools cannot see it.
total_bytes="$(wc -c < "$CONFIG_REL" 2>/dev/null)" || cfg_err "not readable."
nonnul_bytes="$(tr -d '\000' < "$CONFIG_REL" 2>/dev/null | wc -c)" || cfg_err "not readable."
if [ "$((total_bytes))" -ne "$((nonnul_bytes))" ]; then
    cfg_err "contains a NUL byte."
fi

# Validate every line. Comment ("#...") and empty lines are skipped; every
# other line, after one optional trailing "/", must be a relative path whose
# segments use only [A-Za-z0-9._-], are not "." or "..", do not start with
# "-", and whose length is at most PATH_MAX. Length is checked before the
# regex; the regex is a single character class (no nested quantifiers).
# Summary: "<valid roots> <invalid lines> <first invalid line number>".
if ! awk -v pathmax="$path_max" -v out="$tmp/roots" -v summary="$tmp/summary" '
    BEGIN { n = 0; bad = 0; first = 0; printf "" > out }
    {
        line = $0
        if (line == "" || substr(line, 1, 1) == "#") next
        if (substr(line, length(line), 1) == "/") line = substr(line, 1, length(line) - 1)
        ok = (length(line) > 0 && length(line) <= pathmax && line ~ /^[-A-Za-z0-9._\/]+$/)
        if (ok) {
            k = split(line, seg, "/")
            for (i = 1; i <= k; i++) {
                if (seg[i] == "" || seg[i] == "." || seg[i] == ".." || substr(seg[i], 1, 1) == "-") { ok = 0; break }
            }
        }
        if (!ok) { bad++; if (!first) first = NR; next }
        n++
        print line > out
    }
    END { printf "%d %d %d\n", n, bad, first > summary }
' "$CONFIG_REL" 2>/dev/null; then
    cfg_err "could not be read or parsed."
fi
read -r n_roots n_bad first_bad < "$tmp/summary" || cfg_err "could not be parsed."
if [ "$n_bad" -ne 0 ]; then
    cfg_err "$n_bad invalid line(s), first at line $first_bad. A root is a relative path of [A-Za-z0-9._-] segments separated by '/', with no '.', '..' or empty segment, no segment starting with '-', at most PATH_MAX ($path_max) bytes."
fi
if [ "$n_roots" -eq 0 ]; then
    cfg_err "lists no roots. Add at least one repo-relative worktree root."
fi

# --- 3. Query git: a fixed number of calls, whatever the root count (F6) ---
# Probe each root as a directory ("root/"), NUL-separated, on stdin.
sed 's|$|/|' "$tmp/roots" | tr '\n' '\000' > "$tmp/probe" \
    || die "$EXIT_GIT" "cannot prepare the check-ignore input."

# core.quotePath=true overrides user config: every tracked name is one line,
# C-quoted when it holds control, quote, backslash or non-ASCII bytes.
git -c core.quotePath=true ls-files --cached > "$tmp/tracked" 2>/dev/null
rc=$?
if [ "$rc" -ne 0 ]; then
    die "$EXIT_GIT" "git ls-files failed (exit $rc); cannot tell whether anything is tracked under the roots."
fi

# --no-index: match patterns even for tracked roots. -n -v: one record per
# root (source, line number, pattern, path), empty fields when unmatched.
git check-ignore --no-index --stdin -z -v -n < "$tmp/probe" > "$tmp/ignore" 2>/dev/null
rc=$?
# 0 = some path ignored, 1 = none ignored; anything else is a failure.
if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
    die "$EXIT_GIT" "git check-ignore failed (exit $rc); cannot tell whether the roots are ignored (a root reached through a symlink fails here)."
fi

# --- 4. Make the check-ignore records line-safe, losslessly for our needs ---
# Records are NUL-separated fields: source, line number, pattern, path. Map
# NUL to LF and any LF inside a field to 0x01, so each field is one line. A
# mapped field can never equal an accepted source (validated ASCII), and the
# decisions below read only a pattern's emptiness and first byte.
tr '\n\000' '\001\n' < "$tmp/ignore" > "$tmp/ignore.lines" \
    || die "$EXIT_GIT" "cannot read the git check-ignore output."

# --- 5. Evaluate both rules in one awk pass ---
# Rule "ignored": exactly one record per root, in input order. The root is
# ignored only if the deciding pattern is not a negation and comes from a
# TRACKED .gitignore at the top level or in one of the root's ancestor
# directories (never .git/info/exclude or a global excludes file).
# Rule "tracked": no tracked name equals the root or starts with "root/".
# Only the first (longest root + 1) bytes of a name are scanned for "/"
# boundaries, so a hostile 2 MB name costs O(PATH_MAX), not O(size squared).
# Messages name only validated roots and counts, never raw repository bytes.
# awk exits 0 and writes "OK <n>" when clean, exits 1 with the violation
# report when not, and exits 7 when the git output cannot be trusted.
awk -v roots="$tmp/roots" -v ign="$tmp/ignore.lines" -v report="$tmp/report" \
    -v me="$ME" -v cfg="$CONFIG_REL" '
    function rd(file,    l) {
        if ((getline l < file) <= 0) { bad_ignore = 1; return "" }
        return l
    }
    BEGIN {
        n = 0; maxlen = 0; bad_ignore = 0; fatal = 0
        while ((getline r < roots) > 0) {
            n++; order[n] = r; isroot[r] = 1
            if (length(r) > maxlen) maxlen = length(r)
        }
        close(roots)
        for (i = 1; i <= n; i++) {
            r = order[i]
            s = rd(ign); lnum = rd(ign); pat = rd(ign); got = rd(ign)
            if (bad_ignore || got != r "/") { fatal = 1; exit 7 }
            ok = ""
            if (substr(pat, 1, 1) != "!") {
                if (s == ".gitignore") ok = s
                else if (length(s) > 11 && substr(s, length(s) - 10) == "/.gitignore") {
                    d = substr(s, 1, length(s) - 11)
                    if (substr(r, 1, length(d) + 1) == d "/") ok = s
                }
            }
            src[r] = ok
            if (ok != "") need[ok] = 1
        }
        if ((getline extra < ign) > 0) { fatal = 1; exit 7 }
        close(ign)
        total = 0
    }
    {
        if ($0 in need) tracked_src[$0] = 1
        line = $0
        quoted = (substr(line, 1, 1) == "\"")
        if (quoted) line = substr(line, 2)
        hit = 0
        if (!quoted && (line in isroot)) { cnt[line]++; hit = 1 }
        off = 0; s = substr(line, 1, maxlen + 1)
        while ((p = index(s, "/")) > 0) {
            off += p
            pre = substr(line, 1, off - 1)
            if (pre in isroot) { cnt[pre]++; hit = 1 }
            s = substr(s, p + 1)
        }
        if (hit) total++
    }
    END {
        # POSIX awk runs END after an exit in BEGIN; keep the failure.
        if (fatal) exit 7
        v = 0; bad_roots = 0
        for (i = 1; i <= n; i++) {
            r = order[i]; this = 0
            if (cnt[r] > 0) {
                v++; this = 1
                printf "%s: VIOLATION tracked: %d path(s) under %s/ are tracked in the index; an ignore rule has no effect on tracked files. Fix: git rm --cached -r -- %s and commit (list them with: git ls-files -- %s).\n", me, cnt[r], r, r, r > report
            }
            if (src[r] == "" || !(src[r] in tracked_src)) {
                v++; this = 1
                printf "%s: VIOLATION not ignored: %s/ is not ignored by a tracked ignore file (.git/info/exclude and global excludes do not reach a CI checkout). Fix: add \"/%s/\" to the tracked .gitignore and commit it.\n", me, r, r > report
            }
            bad_roots += this
        }
        if (v > 0) {
            printf "%s: FAIL %d violation(s) on %d of %d root(s) in %s; %d tracked path(s) in total.\n", me, v, bad_roots, n, cfg, total > report
            exit 1
        }
        printf "OK %d\n", n > report
        exit 0
    }
' "$tmp/tracked" 2>/dev/null
rc=$?

# --- 6. Report ---
case "$rc" in
    0)
        # Positive attestation (F12): awk must confirm every loaded root.
        if [ "$(cat "$tmp/report" 2>/dev/null)" != "OK $n_roots" ]; then
            die "$EXIT_GIT" "evaluation did not confirm all $n_roots root(s); result not trusted."
        fi
        printf 'CLEAN %d\n' "$n_roots"
        exit "$EXIT_CLEAN"
        ;;
    1)
        cat "$tmp/report" >&2
        exit "$EXIT_VIOLATION"
        ;;
    7)
        die "$EXIT_GIT" "git check-ignore output did not match the configured roots; result not trusted."
        ;;
    *)
        die "$EXIT_GIT" "could not evaluate the git query results (awk exit $rc)."
        ;;
esac
