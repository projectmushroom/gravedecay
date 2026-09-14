#!/usr/bin/env python3
"""Read personal service credentials only after switching to the workspace UID.

This is deliberately not a shell or a systemd EnvironmentFile parser. Personal
files may supply only the named integration values, never service controls.
"""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import sys

LIMIT = 8192
FIELDS = {
    "linear": {"LINEAR_API_KEY"},
    "activity": {"T3_ACTIVITY_URL", "T3_ACTIVITY_TOKEN", "T3_ACTIVITY_ENVIRONMENT",
                 "T3_ACTIVITY_ENVIRONMENT_ID"},
}
FILES = {"linear": "linear.env", "activity": "t3-activity.env"}


def linear_value(value):
    return len(value) <= LIMIT - 32 and re.fullmatch(r"lin_api_[A-Za-z0-9_-]{12,}", value) is not None


@contextmanager
def secret_dir(home):
    """Anchor each component; never resolve a link in a workspace path."""
    home = Path(home)
    if not home.is_absolute() or ".." in home.parts:
        raise ValueError("workspace home must be an absolute path without traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*home.parts[1:], "config", "secrets"):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("credential directory must be private and workspace-owned")
        yield fd
    finally:
        os.close(fd)


def check_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("credential file must be a private, singly linked workspace-owned regular file")


def read_values(home, kind):
    try:
        with secret_dir(home) as directory:
            fd = os.open(FILES[kind], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                check_file(os.fstat(source.fileno()))
                text = source.read(LIMIT + 1)
    except FileNotFoundError:
        return {}
    if len(text) > LIMIT:
        raise ValueError("credential file exceeds size limit")
    values = {}
    for line in text.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in FIELDS[kind] or name in values:
            raise ValueError("unexpected or duplicate credential field")
        # Accept existing simple quoted assignments, without shell expansion.
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid credential value")
        if kind == "linear" and not linear_value(value):
            raise ValueError("invalid Linear API key")
        values[name] = value
    if set(values) != FIELDS[kind]:
        raise ValueError("incomplete credential file")
    return values


def write_linear(home, value):
    if not linear_value(value):
        raise ValueError("invalid Linear API key")
    with secret_dir(home) as directory:
        # Reject existing links/devices without opening or chmod/chown of targets.
        try:
            check_file(os.stat(FILES["linear"], dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            pass
        name = ".linear-" + os.urandom(16).hex()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w") as out:
                out.write("LINEAR_API_KEY=" + value + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, FILES["linear"], src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("exec", "check", "write-linear", "delete-linear"))
    parser.add_argument("home")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if os.geteuid() == 0:
            raise ValueError("personal credentials must never be accessed as root")
        if args.action == "write-linear":
            write_linear(args.home, sys.stdin.read(LIMIT + 1).strip())
        elif args.action == "delete-linear":
            with secret_dir(args.home) as directory:
                try:
                    os.unlink(FILES["linear"], dir_fd=directory)
                except FileNotFoundError:
                    pass
        else:
            kind, *command = args.args
            values = read_values(args.home, kind)
            if args.action == "check":
                print("configured" if values else "onboarding")
            else:
                env = {key: value for key, value in os.environ.items() if key not in FIELDS[kind]}
                os.execvpe(command[0], command, {**env, **values})
    except (OSError, ValueError, KeyError, IndexError):
        # Never include file contents, credential values or command arguments.
        print("workspace credentials unsafe or unavailable; check private paths and allowed fields", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
