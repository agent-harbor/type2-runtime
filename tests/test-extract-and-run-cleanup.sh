#! /bin/bash

# Regression test: --appimage-extract-and-run must remove its extraction.
#
# Started with --appimage-extract-and-run or APPIMAGE_EXTRACT_AND_RUN=1, the
# runtime extracts the image into $TMPDIR/appimage_extracted_<md5 of the image>,
# runs AppRun from there, waits for it and, unless NO_CLEANUP is set, removes
# that directory before it exits with AppRun's exit status. If the removal
# fails, it says "Failed to clean up cache directory" and exits 127 when AppRun
# exited 0.
#
# The test builds small AppImages from the given runtime and checks what is left
# in a private TMPDIR afterwards:
#
#   argument      --appimage-extract-and-run, AppRun exits 0: removed, status 0
#   variable      APPIMAGE_EXTRACT_AND_RUN=1, the same
#   exit-status   AppRun exits 3: removed, status 3
#   tree          nested directories, a hard link, symlinks (to a file, to a
#                 directory, dangling, and to a file and a directory outside the
#                 extraction), and files AppRun writes itself: removed, and the
#                 symlink targets outside the extraction are left alone
#   no-cleanup    NO_CLEANUP=1: kept (holds with and without the fix)
#   removal-fails AppRun leaves a directory without write permission behind:
#                 the failure is reported and the status is 127
#
# Usage: tests/test-extract-and-run-cleanup.sh <runtime binary>
#
#   e.g. tests/test-extract-and-run-cleanup.sh out/runtime-x86_64
#
# Needs mksquashfs (squashfs-tools) on PATH, or MKSQUASHFS set to it. It needs
# no FUSE, and must not run as root (root can remove entries of a directory
# without write permission, so removal-fails cannot fail).
#
# Exit status: 0 every case passed, 1 a case failed, 2 the test could not run.

set -uo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <runtime binary>" >&2
    exit 2
fi

runtime="$(readlink -f "$1")"
mksquashfs="${MKSQUASHFS:-$(command -v mksquashfs || true)}"

if [ ! -x "$runtime" ]; then
    echo "$0: cannot run: $1 is not an executable file" >&2
    exit 2
fi
if [ -z "$mksquashfs" ] || [ ! -x "$mksquashfs" ]; then
    echo "$0: cannot run: no mksquashfs (install squashfs-tools or set MKSQUASHFS)" >&2
    exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
    echo "$0: cannot run: run it as a regular user, not as root" >&2
    exit 2
fi

work="$(mktemp -d -t type2-runtime-cleanup-test-XXXXXX)"
cleanup() {
    # An extraction left behind may contain a directory without write permission.
    chmod -R u+rwX "$work" 2>/dev/null
    rm -rf "$work"
}
trap cleanup EXIT

failures=0
fail() {
    echo "FAIL $case_name: $*" >&2
    failures=$((failures + 1))
}

# The stand-in application: record where it ran, optionally change the
# extracted tree the way an application can, and exit with the requested
# status.
write_apprun() {
    cat > "$1/AppRun" <<'EOF'
#! /bin/sh
set -e
printf '%s\n' "$APPDIR" > "$TEST_RECORD/appdir"
case "${TEST_APP_WRITES-}" in
    tree)
        mkdir -p "$APPDIR/cache/a/b/c"
        echo x > "$APPDIR/cache/a/b/c/file"
        ln -s ../../a "$APPDIR/cache/a/b/up"
        echo y > "$APPDIR/written-by-app"
        ;;
    unwritable)
        mkdir "$APPDIR/locked"
        echo z > "$APPDIR/locked/file"
        chmod 0500 "$APPDIR/locked"
        ;;
esac
exit "${TEST_APP_EXIT:-0}"
EOF
    chmod 0755 "$1/AppRun"
}

populate_tree() {
    local appdir="$1" outside="$2" level="$1/deep" i
    for i in $(seq -w 0 47); do
        level="$level/d$i"
        mkdir -p "$level"
        echo "$i" > "$level/file"
    done
    mkdir -p "$appdir/usr/lib"
    printf 'library' > "$appdir/usr/lib/libdemo.so.1"
    ln "$appdir/usr/lib/libdemo.so.1" "$appdir/usr/lib/hardlink"
    ln -s libdemo.so.1 "$appdir/usr/lib/libdemo.so"
    ln -s lib "$appdir/usr/share"
    ln -s does-not-exist "$appdir/dangling"
    mkdir "$appdir/empty-dir"
    mkdir "$outside/target-dir"
    echo keep > "$outside/target-dir/keep"
    echo keep > "$outside/target-file"
    ln -s "$outside/target-dir" "$appdir/outside-dir"
    ln -s "$outside/target-file" "$appdir/outside-file"
}

# run_case <name> <mode: argument|variable> <app exit> <app writes> <tree: yes|no>
#          <NO_CLEANUP: yes|no> <expected status> <expected: removed|kept|reported>
run_case() {
    case_name="$1"
    local mode="$2" app_exit="$3" app_writes="$4" tree="$5" no_cleanup="$6"
    local expected_status="$7" expected="$8"
    local dir="$work/$case_name"
    local appdir="$dir/AppDir" tmp="$dir/tmp" record="$dir/record" outside="$dir/outside"
    mkdir -p "$appdir" "$tmp" "$record" "$outside"
    write_apprun "$appdir"
    if [ "$tree" = yes ]; then
        populate_tree "$appdir" "$outside"
    fi

    local image="$dir/test.AppImage"
    if ! "$mksquashfs" "$appdir" "$dir/test.squashfs" -root-owned -noappend -comp gzip -quiet > /dev/null; then
        echo "$0: cannot run: mksquashfs failed" >&2
        exit 2
    fi
    cat "$runtime" "$dir/test.squashfs" > "$image"
    chmod 0755 "$image"
    local extraction="$tmp/appimage_extracted_$(md5sum "$image" | cut -d' ' -f1)"

    local args=() vars=() status
    if [ "$mode" = argument ]; then
        args=(--appimage-extract-and-run)
    else
        vars+=(APPIMAGE_EXTRACT_AND_RUN=1)
    fi
    if [ "$no_cleanup" = yes ]; then
        vars+=(NO_CLEANUP=1)
    fi
    env -u APPIMAGE_EXTRACT_AND_RUN -u NO_CLEANUP \
        TMPDIR="$tmp" TEST_RECORD="$record" TEST_APP_EXIT="$app_exit" TEST_APP_WRITES="$app_writes" \
        "${vars[@]}" timeout 120 "$image" "${args[@]}" > "$dir/stdout" 2> "$dir/stderr" < /dev/null
    status=$?

    if [ "$(cat "$record/appdir" 2>/dev/null)" != "$extraction" ]; then
        fail "AppRun did not run from $extraction"
    fi
    if [ "$status" -ne "$expected_status" ]; then
        fail "exit status $status, expected $expected_status"
    fi
    case "$expected" in
        removed)
            if [ -e "$extraction" ] || [ -L "$extraction" ]; then
                fail "$extraction was left behind"
            fi
            if grep -q "Failed to clean up cache directory" "$dir/stderr"; then
                fail "the runtime reported a failed cleanup"
            fi
            ;;
        kept)
            if [ ! -f "$extraction/AppRun" ]; then
                fail "$extraction was removed although NO_CLEANUP was set"
            fi
            ;;
        reported)
            if ! grep -q "Failed to clean up cache directory" "$dir/stderr"; then
                fail "the failed cleanup was not reported"
            fi
            ;;
    esac
    if [ "$tree" = yes ] && { [ ! -f "$outside/target-dir/keep" ] || [ ! -f "$outside/target-file" ]; }; then
        fail "a symlink target outside the extraction was removed"
    fi
    local others
    others="$(find "$tmp" -mindepth 1 -maxdepth 1 ! -path "$extraction" -printf '%f ')"
    if [ -n "$others" ]; then
        fail "unexpected entries in TMPDIR: $others"
    fi
    echo "ran $case_name: exit status $status"
}

#        name          mode     exit writes     tree no_cleanup status expected
run_case argument      argument 0    ""         no   no         0      removed
run_case variable      variable 0    ""         no   no         0      removed
run_case exit-status   argument 3    ""         no   no         3      removed
run_case tree          argument 0    tree       yes  no         0      removed
run_case no-cleanup    argument 0    ""         no   yes        0      kept
run_case removal-fails argument 0    unwritable no   no         127    reported

if [ "$failures" -ne 0 ]; then
    echo "$0: $failures check(s) failed" >&2
    exit 1
fi
echo "$0: all cases passed"
exit 0
