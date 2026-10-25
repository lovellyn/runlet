"""Linux job supervisor: adopt and reap descendants, including detached sessions."""

import ctypes
import fcntl
import os
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

TERM_GRACE = 0.2
CLEANUP_TIMEOUT = 2.0


def children(pid: int) -> set[int]:
    result = set()
    try:
        tasks = list(Path(f"/proc/{pid}/task").iterdir())
    except OSError:
        return result
    for task in tasks:
        try:
            result.update(int(value) for value in (task / "children").read_text().split())
        except (OSError, ValueError):
            continue
    return result


def descendants(pid: int) -> set[int]:
    found = set()
    pending = [pid]
    while pending:
        for child in children(pending.pop()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def signal_children(pid: int, sig: int) -> None:
    for child in descendants(pid):
        try:
            os.kill(child, sig)
        except ProcessLookupError:
            pass


def main() -> int:
    # This separate process is single-threaded. Making it a subreaper keeps
    # orphaned grandchildren assigned to this job even after setsid/double-fork.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot enable job child reaping")
    pid = os.getpid()
    Path(f"/proc/{pid}/task/{pid}/children").read_text()
    stopping = False

    def request_stop(_sig, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, request_stop)
    tty = sys.argv[1] == "tty"
    if tty:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    child = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", sys.argv[2]],
        process_group=0,
    )
    if tty:
        # The command has its own foreground group. Resume it if it reached
        # its first terminal read before the foreground assignment.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(0, child.pid)
        try:
            os.killpg(child.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass

    def reap() -> None:
        while True:
            try:
                reaped, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if not reaped:
                return
            if reaped == child.pid:
                child.returncode = os.waitstatus_to_exitcode(status)

    while child.returncode is None and not stopping:
        reap()
        if child.returncode is None and not stopping:
            time.sleep(0.02)

    # Repeat discovery as parents exit: the kernel reparents their remaining
    # children to this supervisor, independent of terminal/session membership.
    started = time.monotonic()
    signalled = set()
    while True:
        reap()
        remaining = descendants(pid)
        if not remaining:
            break
        elapsed = time.monotonic() - started
        sig = signal.SIGKILL if elapsed >= TERM_GRACE else signal.SIGTERM
        for target in remaining:
            key = (target, sig)
            if key not in signalled:
                try:
                    os.kill(target, sig)
                except ProcessLookupError:
                    pass
                signalled.add(key)
        if elapsed >= CLEANUP_TIMEOUT:
            print("Job descendant cleanup exceeded its deadline", file=sys.stderr, flush=True)
            return 125
        time.sleep(0.01)
    reap()
    return child.returncode if child.returncode is not None else 125


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:
        print(f"Job supervisor failed: {error}", file=sys.stderr, flush=True)
        code = 125
    if code < 0:
        sig = -code
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
    sys.exit(code)
