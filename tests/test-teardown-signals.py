#!/usr/bin/env python3
"""Regression test: a termination signal during the FUSE daemon's teardown must
not leave a dead mount behind.

When the FUSE session loop returns, the daemon removes libfuse's signal handlers
and only then unmounts the image. A SIGTERM (from a service manager that
signals every process of the application, or the one write_pipe_thread() sends
once the application has exited), or the SIGPIPE write_pipe_thread() raises,
arriving in between used to kill the daemon with the image still mounted. The
mount point was then left behind without a server: stat() on it fails with
"Transport endpoint is not connected" until someone runs fusermount -u.

The test builds a small AppImage from the given runtime. Its AppRun records
$APPDIR, waits until the test has found the FUSE daemon, and then becomes
`sleep`: one process holding the keepalive pipe, as an application does. It
runs three kinds of round, ROUNDS of each:

  both         SIGTERM to the application and to the FUSE daemon together, as
               systemd's default KillMode=control-group does when it stops a
               service or a session
  storm        the same, then SIGTERM to the daemon every millisecond until it
               exits (at most 2 s), which covers the teardown window whenever
               it opens
  client-exit  the application exits by itself, 0 to 30 ms after the daemon's
               storm begins (the SIGPIPE path)

After each round it waits for the daemon to exit, counts a dead mount (the
mount point still in /proc/self/mountinfo and stat() failing with ENOTCONN)
and removes it with fusermount -u -z.

Usage: tests/test-teardown-signals.py [--rounds N] <runtime binary>

  e.g. tests/test-teardown-signals.py out/runtime-x86_64

Needs Linux, Python 3.8 or later (standard library only), /dev/fuse, a
setuid-root fusermount3 or fusermount on PATH (what the runtime itself needs
to mount), and mksquashfs (squashfs-tools) on PATH or in $MKSQUASHFS.

The test makes itself a child subreaper (prctl PR_SET_CHILD_SUBREAPER), so
the FUSE daemon, which daemonizes, becomes its child. It signals only the
AppImage process it starts and the child that runs this test's own image file
with /dev/fuse open.

Exit status: 0 no dead mount, 1 a dead mount or a daemon that did not exit,
2 the test could not run.

It is written in Python rather than shell because the second signal has to
land within a fraction of a millisecond, between the end of the session loop
and the fork of fusermount. A shell version of this test did not reproduce the
bug; this one does in most rounds of the "storm" kind.
"""

import argparse
import ctypes
import errno
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PR_SET_CHILD_SUBREAPER = 36
KINDS = ("both", "storm", "client-exit")
STORM_INTERVAL_S = 0.001
STORM_LIMIT_S = 2.0
READY_TIMEOUT_S = 60.0
EXIT_TIMEOUT_S = 30.0

APP_RUN = """#!/bin/sh
printf '%s\\n' "$APPDIR" > "$TEST_READY.tmp"
mv "$TEST_READY.tmp" "$TEST_READY"
read -r _ < "$TEST_GO"
exec sleep "$TEST_SLEEP"
"""


class CannotRun(Exception):
    """The test could not run (exit status 2)."""


def find_setuid_fusermount():
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        for name in ("fusermount3", "fusermount"):
            candidate = os.path.join(directory, name)
            try:
                st = os.stat(candidate)
            except OSError:
                continue
            if st.st_uid == 0 and st.st_mode & 0o4000:
                return candidate
    raise CannotRun("no setuid fusermount3 or fusermount on PATH")


def build_image(runtime, mksquashfs, work):
    appdir = os.path.join(work, "AppDir")
    os.mkdir(appdir)
    with open(os.path.join(appdir, "AppRun"), "w") as f:
        f.write(APP_RUN)
    os.chmod(os.path.join(appdir, "AppRun"), 0o755)
    squashfs = os.path.join(work, "test.squashfs")
    result = subprocess.run(
        [mksquashfs, appdir, squashfs, "-root-owned", "-noappend", "-comp", "gzip", "-quiet"],
        stdout=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise CannotRun("mksquashfs failed")
    image = os.path.join(work, "test.AppImage")
    with open(image, "wb") as out:
        for part in (runtime, squashfs):
            with open(part, "rb") as f:
                shutil.copyfileobj(f, out)
    os.chmod(image, 0o755)
    return image


def children():
    result = []
    for task in os.listdir("/proc/self/task"):
        try:
            with open("/proc/self/task/%s/children" % task) as f:
                result.extend(int(pid) for pid in f.read().split())
        except OSError:
            pass
    return result


def find_daemon(image, client_pid):
    """Our child (as subreaper) that runs the image and holds /dev/fuse."""
    for pid in children():
        if pid == client_pid:
            continue
        try:
            if os.readlink("/proc/%d/exe" % pid) != image:
                continue
            for fd in os.listdir("/proc/%d/fd" % pid):
                if os.readlink("/proc/%d/fd/%s" % (pid, fd)) == "/dev/fuse":
                    return pid
        except OSError:
            continue
    raise CannotRun("found no FUSE daemon among the test's children")


def gone(pid):
    """True once pid has exited (it is our child, so it is reaped here)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except ChildProcessError:
        return True
    return False


def wait_gone(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gone(pid):
            return True
        time.sleep(0.005)
    return False


def terminate(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def storm(pid):
    """SIGTERM to pid every millisecond until it exits; True if it did."""
    deadline = time.monotonic() + STORM_LIMIT_S
    while time.monotonic() < deadline:
        if gone(pid):
            return True
        terminate(pid)
        time.sleep(STORM_INTERVAL_S)
    return gone(pid)


def mount_points():
    with open("/proc/self/mountinfo") as f:
        return {line.split()[4] for line in f if len(line.split()) > 4}


def reap_others():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def run_round(kind, image, tmpdir, fusermount, rng):
    """Returns (dead mount left, daemon did not exit)."""
    ready = os.path.join(tmpdir, "ready")
    go = os.path.join(tmpdir, "go")
    for path in (ready, go):
        if os.path.exists(path):
            os.unlink(path)
    os.mkfifo(go, 0o600)
    env = dict(os.environ)
    env.update(
        TMPDIR=tmpdir,
        TEST_READY=ready,
        TEST_GO=go,
        TEST_SLEEP="%.3f" % rng.uniform(0.0, 0.03) if kind == "client-exit" else "600",
    )
    client = subprocess.Popen(
        [image], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + READY_TIMEOUT_S
    while not os.path.exists(ready):
        if client.poll() is not None:
            raise CannotRun("the AppImage exited with %d before it mounted" % client.returncode)
        if time.monotonic() > deadline:
            client.kill()
            raise CannotRun("the AppImage did not mount within %d s" % READY_TIMEOUT_S)
        time.sleep(0.001)
    with open(ready) as f:
        mountpoint = f.read().strip()
    daemon = find_daemon(image, client.pid)

    # Let AppRun past its wait: it is reading the FIFO, or about to open it.
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            fd = os.open(go, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or client.poll() is not None or time.monotonic() > deadline:
                raise CannotRun("the stand-in AppRun did not wait for the test")
            time.sleep(0.001)
    try:
        os.write(fd, b"go\n")
    finally:
        os.close(fd)

    if kind == "client-exit":
        exited = storm(daemon)
    else:
        terminate(client.pid)
        terminate(daemon)
        exited = storm(daemon) if kind == "storm" else wait_gone(daemon, EXIT_TIMEOUT_S)
    if not exited:
        exited = wait_gone(daemon, EXIT_TIMEOUT_S)
    if not exited:
        os.kill(daemon, signal.SIGKILL)
        wait_gone(daemon, EXIT_TIMEOUT_S)
    if client.poll() is None:
        terminate(client.pid)
    client.wait(timeout=EXIT_TIMEOUT_S)
    reap_others()

    dead = False
    if mountpoint in mount_points():
        try:
            os.stat(mountpoint)
        except OSError as error:
            dead = error.errno == errno.ENOTCONN
        subprocess.run([fusermount, "-u", "-z", mountpoint], stderr=subprocess.DEVNULL)
    try:
        os.rmdir(mountpoint)
    except OSError:
        pass
    return dead, not exited


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("runtime", help="the runtime binary under test")
    parser.add_argument("--rounds", type=int, default=100, help="rounds of each kind (default 100)")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    work = tempfile.mkdtemp(prefix="type2-runtime-teardown-test-")
    status = 0
    fusermount = None
    try:
        runtime = os.path.realpath(args.runtime)
        if not os.access(runtime, os.X_OK):
            raise CannotRun("%s is not an executable file" % args.runtime)
        if args.rounds < 1:
            raise CannotRun("--rounds must be at least 1")
        mksquashfs = os.environ.get("MKSQUASHFS") or shutil.which("mksquashfs")
        if not mksquashfs:
            raise CannotRun("no mksquashfs (install squashfs-tools or set MKSQUASHFS)")
        if not os.path.exists("/dev/fuse"):
            raise CannotRun("no /dev/fuse")
        fusermount = find_setuid_fusermount()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            raise CannotRun("prctl(PR_SET_CHILD_SUBREAPER) failed: %s" % os.strerror(ctypes.get_errno()))

        image = build_image(runtime, mksquashfs, work)
        tmpdir = os.path.join(work, "tmp")
        os.mkdir(tmpdir)
        rng = random.Random(args.seed)
        dead_total = hung_total = 0
        for kind in KINDS:
            dead = hung = 0
            for _ in range(args.rounds):
                round_dead, round_hung = run_round(kind, image, tmpdir, fusermount, rng)
                dead += round_dead
                hung += round_hung
            print("%s: %d dead mount(s) in %d rounds%s"
                  % (kind, dead, args.rounds, ", %d daemon(s) did not exit" % hung if hung else ""))
            dead_total += dead
            hung_total += hung
        if dead_total or hung_total:
            status = 1
    except CannotRun as error:
        print("%s: cannot run: %s" % (sys.argv[0], error), file=sys.stderr)
        status = 2
    finally:
        # Mounts a failed round left behind, all of them under this test's directory.
        if fusermount:
            for mountpoint in mount_points():
                if mountpoint.startswith(work + "/"):
                    subprocess.run([fusermount, "-u", "-z", mountpoint], stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)
    if status == 0:
        print("%s: no dead mount" % sys.argv[0])
    return status


if __name__ == "__main__":
    sys.exit(main())
