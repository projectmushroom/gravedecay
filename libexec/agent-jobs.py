#!/usr/bin/env python3
"""Owner-only persistent schedules and supervised, isolated headless agent runs."""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(os.environ.get("GRAVE_ROOT", "/srv/dev"))
STORE = ROOT / "config/secrets/agent-jobs"
GRAVE = os.environ.get("GRAVE_BIN", "/usr/local/bin/grave")
# Gaming mode is the agent freezer and nothing else: `grave gaming` freezes this
# cgroup. T3's unit state is not consulted, so a job runs to a verdict with
# t3code stopped or masked. Tests point the path at a file they control.
FREEZE = Path(os.environ.get("GRAVE_FREEZE_FILE", "/sys/fs/cgroup/grave-torpor/cgroup.freeze"))
STOP = False
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# `status` is the process lifecycle: how the provider ended. `result.verdict`
# is what the runner concluded about the work afterwards, with a command the
# agent did not choose. `unverified` is the fail-closed default: a succeeded
# run with no check executed, or a result that is missing or malformed.
# `skipped` is kept for records written before gaming mode deferred jobs instead of skipping them.
STATUSES = ("running", "succeeded", "failed", "skipped", "cancelled", "timed-out", "interrupted")
VERDICTS = ("ready-for-review", "no-changes", "checks-failed", "unverified", "provider-failed",
            "timed-out", "cancelled", "interrupted", "skipped")
CHECK_LIMIT = 8      # commands per job
CHECK_LENGTH = 500   # characters per command
TAIL = 4000          # bytes of one check's output read from its end
TAIL_BUDGET = 24 * 1024   # JSON bytes of check commands and tails per run record, shared across checks
RECORD_LIMIT = 48 * 1024  # bytes per run record on disk; doctor enforces it
DEFER_LIMIT = 8 * 86400   # a usage-limit reset further out than this is not waited for
SOURCE = re.compile(r"owner|base:[0-9a-f]{40}")
TEST_CHANGE_ROWS = 20     # informational rows per run record; their JSON bytes come out of TAIL_BUDGET
TEST_CHANGE_ROW = 160     # JSON bytes per row
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|specs?|e2e)/|(^|/)(test_[^/]*\.py|[^/]*_test\.(py|go|rb|rs|exs?)"
                       r"|[^/]*\.(test|spec)\.[cm]?[jt]sx?|[^/]*Tests?\.(java|kt|swift|cs|php)|[^/]*_spec\.rb|conftest\.py)$")
SKIP_MARK = re.compile(r"\.(skip|only|todo|fixme)\b|\b(xit|xdescribe|xtest|fit|fdescribe|fcontext|skipTest|t\.Skip|pytest\.skip|unittest\.skip)\b"
                       r"|\bmark\.(skip|skipif|xfail)\b|#\[ignore\]|@Ignore\b|@Disabled\b")
TEST_CONFIG = re.compile(r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|noxfile\.py|(jest|vitest|playwright|karma|mocha|ava)\.config\.[cm]?[jt]sx?|\.mocharc(\.[a-z]+)?)$")
CI_PATH = re.compile(r"^(\.github/workflows/|\.gitlab-ci\.yml$|\.circleci/|Jenkinsfile$|\.travis\.yml$|azure-pipelines\.yml$|\.buildkite/|bitbucket-pipelines\.yml$|\.drone\.yml$)")
LOCKFILE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|poetry\.lock|uv\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock|mix\.lock|flake\.lock)$")
SNAPSHOT = re.compile(r"(^|/)__snapshots__/|\.snap$|(^|/)snapshots?/")
LOG_COPY = 1 << 20   # bytes of check output copied into the session transcript
_SHARED = {}


def shared(name):
    """A sibling script: beside this file when installed, or in the repository layout."""
    if name not in _SHARED:
        here = Path(__file__).resolve().parent
        for candidate in (here / name, here.parent / "libexec" / name, here.parent / "dashboard" / name):
            if candidate.is_file():
                spec = importlib.util.spec_from_file_location("grave_" + candidate.stem.replace("-", "_"), candidate)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _SHARED[name] = module
                break
        else:
            raise ValueError(name + " is not installed beside the job runner; re-run raise.sh")
    return _SHARED[name]


def providers():
    return shared("providers.py")


def private_dir(path, create=True):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("job directories must be private and owned by the appliance owner")


def read_private(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError("invalid private job file: " + str(path))
        return stream.read()


def write_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream); stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked(path, blocking=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def job_dir(name):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", name):
        raise ValueError("job name must be 1–20 letters, digits, '-' or '_'")
    path = STORE / name
    if path.is_symlink():
        raise ValueError("job directory cannot be a symlink")
    return path


def repo_path(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}", name):
        raise ValueError("invalid repository name")
    path = ROOT / "repos" / name
    if path.is_symlink() or not (path / ".git").is_dir() or (path / ".git").is_symlink():
        raise ValueError("choose a primary Git checkout under GRAVE_ROOT/repos")
    return path


def calendar(value):
    if value is None:
        return None, None
    match = re.fullmatch(r"(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?([01][0-9]|2[0-3]):([0-5][0-9])", value)
    if not match:
        raise ValueError("use HH:MM or 'Mon HH:MM' in the appliance's local timezone")
    return DAYS.index(match[1]) if match[1] else None, dt.time(int(match[2]), int(match[3]))


def next_due(value, after):
    weekday, clock = calendar(value)
    if clock is None:
        return None
    today = dt.datetime.fromtimestamp(after).date()
    for offset in range(9):
        date = today + dt.timedelta(days=offset)
        if weekday is not None and date.weekday() != weekday:
            continue
        candidate = dt.datetime.combine(date, clock).timestamp()
        if candidate > after:
            return candidate
    raise ValueError("cannot calculate next run")


def valid_checks(value):
    """None means detect a check in the worktree; a list is the owner's literal commands."""
    if value is None:
        return True
    return (isinstance(value, list) and len(value) <= CHECK_LIMIT
            and all(isinstance(item, str) and 0 < len(item) <= CHECK_LENGTH and item.strip()
                    and item.isprintable() for item in value))


def read_job(name):
    path = job_dir(name)
    value = json.loads(read_private(path / "job.json"))
    required = {"name", "agent", "repo", "schedule", "timeout", "enabled", "next_due"}
    if not isinstance(value, dict) or not required <= value.keys() or value.get("name") != name or value.get("agent") not in providers().PROVIDERS:
        raise ValueError("invalid job metadata")
    repo_path(value.get("repo"))
    calendar(value.get("schedule"))
    if type(value.get("enabled")) is not bool or not isinstance(value.get("timeout"), int) or not 60 <= value["timeout"] <= 86400:
        raise ValueError("invalid job controls")
    if value.get("next_due") is not None and (type(value["next_due"]) not in (int, float) or not math.isfinite(value["next_due"]) or value["next_due"] < 0):
        raise ValueError("invalid due time")
    if not valid_checks(value.get("checks")):
        raise ValueError("invalid check commands")
    pending = value.get("requeue")
    if pending is not None and not (isinstance(pending, dict) and set(pending) == {"run_id", "of"}
                                    and all(isinstance(pending[key], str) and re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{6}", pending[key]) for key in pending)):
        raise ValueError("invalid requeue marker")
    prompt = read_private(path / "prompt.txt")
    if not prompt.strip() or "\x00" in prompt:
        raise ValueError("prompt must contain text without NUL bytes")
    return value


def names():
    if not STORE.exists():
        return []
    private_dir(STORE, create=False)
    return sorted(path.name for path in STORE.iterdir() if not path.name.startswith("."))


def gaming():
    try:
        return FREEZE.read_text().strip() == "1"
    except OSError:
        return False


def add(args):
    private_dir(STORE)
    path = job_dir(args.name)
    repo = args.repo
    if args.dir:
        directory = Path(args.dir).resolve()
        if directory.parent != (ROOT / "repos").resolve():
            raise ValueError("--dir must be a primary checkout under GRAVE_ROOT/repos")
        repo = directory.name
    if not repo:
        directory = Path.cwd().resolve()
        if directory.parent == (ROOT / "repos").resolve():
            repo = directory.name
    repo_path(repo)
    prompt = Path(args.prompt_file).read_bytes()
    if not 0 < len(prompt) <= 65536 or not prompt.decode().strip() or b"\0" in prompt:
        raise ValueError("prompt must be non-empty UTF-8 text, at most 64 KiB")
    if not valid_checks(args.check):
        raise ValueError(f"--check takes at most {CHECK_LIMIT} printable commands of up to {CHECK_LENGTH} characters")
    schedule = args.at or args.on
    calendar(schedule)
    if args.at and " " in args.at:
        raise ValueError("--at requires HH:MM; use --on for a weekday")
    now = time.time()
    # Names are never silently reused: old worktrees and results remain reviewable.
    path.mkdir(mode=0o700)
    try:
        with open(path / "prompt.txt", "xb") as stream:
            stream.write(prompt)
        private_dir(path / "runs")
        value = dict(name=args.name, repo=repo, agent=args.agent, schedule=schedule,
                     timeout=args.timeout, enabled=True, created=now, checks=args.check,
                     next_due=next_due(schedule, now) if schedule else now)
        write_json(path / "job.json", value)
        read_job(args.name)
    except Exception:
        # Retain a partial definition for inspection; never erase another job.
        raise
    print(json.dumps(value))


def cancel(name):
    path = job_dir(name)
    with locked(path / ".config.lock"):
        value = read_job(name)
        value.update(enabled=False, next_due=None)
        write_json(path / "job.json", value)
    print(name + ": cancelled; running work will stop and results are retained")


def git(*args):
    """Every git call ignores refs/replace: the worktree shares the source
    checkout's refs, so an agent could otherwise `git replace` the base commit
    and have its own files read as the base's."""
    command = ["git", *map(str, args)]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))
    try:
        output, _ = proc.communicate(timeout=30)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, command, output)
        return output.strip()
    finally:
        terminate(proc)


def worktree(job, session):
    source = repo_path(job["repo"])
    agents = ROOT / "agents"
    agents.mkdir(exist_ok=True)
    if agents.is_symlink():
        raise ValueError("agents directory must not be a symlink")
    with locked(agents / ".lifecycle.lock"):
        parent = ROOT / "worktrees" / job["repo"]
        for path in (ROOT / "worktrees", parent):
            if path.is_symlink():
                raise ValueError("worktree parents must not be symlinks")
            path.mkdir(mode=0o700, exist_ok=True)
        directory = parent / session
        branch = "agent/" + session
        base = git("-C", source, "rev-parse", "HEAD")
        history = agents / session
        history.mkdir(mode=0o700)
        git("-C", source, "worktree", "add", "-b", branch, directory, "HEAD")
        write_json(history / "meta.json", dict(dir=str(directory), repo=job["repo"],
            branch=branch, base=base, worktree=True, created=dt.datetime.now(dt.timezone.utc).isoformat(),
            scheduled_job=job["name"]))
        run_lock = os.open(history / ".run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(run_lock, fcntl.LOCK_EX)
    return directory, history, run_lock, base


def stopped(_signum, _frame):
    global STOP
    STOP = True


def terminate(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    # A CLI may finish while a background child still holds the process group.
    # A completed job must not leave that work running after releasing its lock.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


GAMING_REASON = "gaming mode entered during run"


def stopper(name):
    """The stop predicate for one run: None to keep going, else the (status,
    reason) that ends it. Gaming mode is polled every two seconds."""
    mode_check = time.monotonic()
    def stop():
        nonlocal mode_check
        if STOP or not read_job(name)["enabled"]:
            return "cancelled", "job or scheduler stopped"
        if time.monotonic() >= mode_check:
            mode_check = time.monotonic() + 2
            if gaming():
                return "cancelled", GAMING_REASON
        return None
    return stop


def supervise(proc, stop, started, timeout, record):
    """Wait for a child in its own process group. The stop predicate, or the
    runtime limit, ends the run: the record is updated and False returned."""
    while proc.poll() is None:
        if why := stop():
            record.update(status=why[0], reason=why[1])
            return False
        if time.time() - started >= timeout:
            record.update(status="timed-out", reason="configured runtime limit reached")
            return False
        time.sleep(.2)
    return True


# ---------------------------------------------------------------- result ----
def change_summary(directory, base):
    """Committed work since the run's base plus whether anything is left uncommitted."""
    commits = int(git("-C", directory, "rev-list", "--count", base + "..HEAD") or 0)
    shortstat = git("-C", directory, "diff", "--shortstat", base, "HEAD")
    def count(pattern):
        match = re.search(pattern, shortstat)
        return int(match.group(1)) if match else 0
    return {"commits": commits, "files": count(r"(\d+) files? changed"),
            "insertions": count(r"(\d+) insertions?\(\+\)"), "deletions": count(r"(\d+) deletions?\(-\)"),
            "dirty": bool(git("-C", directory, "status", "--porcelain"))}


def detect_checks(source, base):
    """The project's own test entry point as committed at the run's base, read with
    `git show` from the source checkout so the agent's worktree cannot supply it."""
    def at_base(name):
        try:
            return git("-C", source, "show", base + ":" + name)
        except subprocess.CalledProcessError:
            return None
    try:
        package = json.loads(at_base("package.json") or "null")
        script = (package.get("scripts") or {}).get("test") if isinstance(package, dict) else None
        # `npm init` writes a placeholder that exits 1; that is not a check.
        if isinstance(script, str) and script.strip() and "no test specified" not in script:
            return [script.strip()]
    except (ValueError, AttributeError):
        pass
    if at_base("pyproject.toml") is not None or at_base("pytest.ini") is not None:
        return ["pytest"]
    if re.search(r"^test\s*:", at_base("Makefile") or "", re.M):
        return ["make test"]
    return []


def test_changes(directory, base):
    """Informational only: ways the committed work since `base` could narrow what
    the checks verify. Deleted or renamed test files, lines removed from existing
    tests, added skip/only/xfail markers, a changed test script or runner config,
    and touched CI, lockfile or snapshot paths. Shown in amber; never a verdict."""
    removed, renamed, modified, touched, paths = [], [], [], [], []
    for line in git("-C", directory, "diff", "--name-status", "-M", base, "HEAD").splitlines():
        parts = line.split("\t")
        code, path = parts[0][:1], parts[-1]
        paths.append(path)
        if code == "D" and TEST_FILE.search(path):
            removed.append("deleted test " + path)
        elif code == "R" and (TEST_FILE.search(parts[1]) or TEST_FILE.search(path)):
            renamed.append("renamed test " + parts[1] + " → " + path)
        elif code in "MA" and TEST_FILE.search(path):
            modified.append((code, path))
        for label, pattern in (("test config", TEST_CONFIG), ("CI", CI_PATH), ("lockfile", LOCKFILE), ("snapshot", SNAPSHOT)):
            if pattern.search(path):
                touched.append(label + (" removed " if code == "D" else " changed ") + path)
    trimmed, skipped = [], []
    existing = [path for code, path in modified if code == "M"]
    if existing:
        for line in git("-C", directory, "diff", "--numstat", base, "HEAD", "--", *existing).splitlines():
            added, deleted, path = line.split("\t", 2)
            if deleted.isdigit() and int(deleted):
                trimmed.append("trimmed test " + path + " (−" + deleted + " lines)")
    if modified:
        counts, current = {}, None
        for line in git("-C", directory, "diff", "-U0", "--no-color", base, "HEAD", "--", *(path for _, path in modified)).splitlines():
            if line.startswith("+++ "):
                current = line[4:].removeprefix("b/")
            elif line.startswith("+") and SKIP_MARK.search(line):
                counts[current] = counts.get(current, 0) + 1
        skipped = ["skip/only/xfail added in " + path + " (+" + str(n) + ")" for path, n in counts.items()]
    script = []
    if "package.json" in paths:
        def test_script(rev):
            try:
                package = json.loads(git("-C", directory, "show", rev + ":package.json"))
            except (subprocess.CalledProcessError, ValueError):
                return None
            return (package.get("scripts") or {}).get("test") if isinstance(package, dict) else None
        if test_script(base) != test_script("HEAD"):
            script = ["test script changed in package.json"]
    rows = [fit(row, TEST_CHANGE_ROW, head=True) for row in removed + renamed + trimmed + skipped + script + touched]
    if len(rows) > TEST_CHANGE_ROWS:
        rows = rows[:TEST_CHANGE_ROWS - 1] + ["… " + str(len(rows) - TEST_CHANGE_ROWS + 1) + " more"]
    return rows


def fit(text, budget, head=False):
    """Drop leading (or, with head=True, trailing) characters until the JSON
    encoding fits the byte budget; json escapes control and non-ASCII
    characters to six bytes each, twelve for an emoji."""
    while text and (size := len(json.dumps(text))) > budget:
        keep = len(text) * budget // size
        text = text[:keep] if head else text[len(text) - keep:]
    return text


def run_check(cmd, source, directory, base, log, stop, timeout, record, started, budget=TAIL_BUDGET):
    """One check in the worktree: same directory, stop predicate and runtime
    budget as the provider, with GRAVE_BASE and the worktree's node_modules/.bin
    on PATH. Output goes to the transcript; the command (its head, up to half of
    `budget`) and a tail of the output together fit `budget` JSON bytes."""
    began = time.monotonic()
    env = dict(os.environ, GRAVE_BASE=base, PATH=str(directory / "node_modules/.bin") + ":" + os.environ.get("PATH", ""))
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            supervise(proc, stop, started, timeout, record)
        finally:
            terminate(proc)
        seconds = round(time.monotonic() - began, 1)
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - TAIL))
        tail = output.read().decode("utf-8", "replace")
        output.seek(0)
        with open(log, "ab") as transcript:
            transcript.write(("\n$ " + cmd + "\n").encode())
            copied = 0
            while copied < LOG_COPY:
                chunk = output.read(min(65536, LOG_COPY - copied))
                if not chunk:
                    break
                transcript.write(chunk)
                copied += len(chunk)
            if size > copied:
                transcript.write(b"\n[check output truncated in this transcript]\n")
            transcript.write(f"\n(exit {proc.returncode}, {seconds}s)\n".encode())
    if record["status"] != "succeeded":
        tail += "\n[check stopped: " + str(record.get("reason")) + "]"
    shown = fit(cmd, budget // 2, head=True)
    return {"cmd": shown, "exit_code": proc.returncode, "seconds": seconds, "tail": fit(tail, budget - len(json.dumps(shown))), "source": source}


def cost_for(directory, since):
    """Estimated spend and, from the run's own Codex rollout, the newest
    account-wide plan quota: the dashboard's transcript parser, filtered to
    sessions whose working directory was this run's worktree. Returns
    (cost, quota); either may be None."""
    try:
        value = shared("gravedecay.py").transcript_cost(str(directory), since)
    except Exception as error:  # a dashboard import problem must not lose the run record
        print("cost unavailable: " + str(error)[:200], file=sys.stderr, flush=True)
        return None, None
    if not value:
        return None, None
    return {key: value[key] for key in ("usd", "estimated", "model")}, value.get("quota")


def rate_limit_for(directory, since):
    """The reset time a Claude Code session in this worktree recorded when its
    last request was refused with a usage-limit 429, read from the transcript."""
    try:
        return shared("gravedecay.py").claude_rate_limit(str(directory), since)
    except Exception as error:
        print("rate limit unavailable: " + str(error)[:200], file=sys.stderr, flush=True)
        return None


def verdict_for(record, changes=None, checks=()):
    status = record.get("status")
    if status in ("timed-out", "cancelled", "interrupted", "skipped"):
        return status
    if status != "succeeded":
        return "provider-failed"
    if changes is not None and changes["commits"] == 0 and not changes["dirty"]:
        return "no-changes"
    if any(check["exit_code"] != 0 for check in checks):
        return "checks-failed"
    return "ready-for-review" if checks else "unverified"


def package(verdict, changes=None, checks=(), cost=None, test_changes=None, quota=None):
    value = {"verdict": verdict, "changes": changes, "checks": list(checks), "cost": cost}
    if test_changes is not None:
        value["test_changes"] = test_changes
    if quota is not None:
        value["quota"] = quota
    return value


def result_package(job, record, directory, base, log, started, stop=lambda: None):
    """Computed by the runner after the provider exits. The check command comes
    from the owner's --check or from the base commit, never from the worktree;
    it still executes agent-written code in the worktree, as the owner."""
    changes = None
    try:
        changes = change_summary(directory, base)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(job["name"] + ": could not summarise changes: " + str(error)[:200], file=sys.stderr, flush=True)
    tests = None
    if changes and changes["commits"]:
        try:
            tests = test_changes(directory, base)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(job["name"] + ": could not inspect test changes: " + str(error)[:200], file=sys.stderr, flush=True)
    budget = TAIL_BUDGET - (len(json.dumps(tests)) if tests else 0)
    checks = []
    if record["status"] == "succeeded" and verdict_for(record, changes) != "no-changes":
        commands, source = job.get("checks"), "owner"
        if commands is None:
            commands, source = detect_checks(repo_path(job["repo"]), base), "base:" + base
        for cmd in commands:
            checks.append(run_check(cmd, source, directory, base, log, stop, job["timeout"], record, started, budget // len(commands)))
            if record["status"] != "succeeded":  # cancelled or out of time while checking
                break
    cost, quota = cost_for(directory, started)
    return package(verdict_for(record, changes, checks), changes, checks, cost, tests, quota)


def valid_quota(value):
    """An account-wide quota snapshot: the provider, at least one window with
    a positive `window_minutes` (its label), a numeric `used_percent` and a
    positive epoch `resets_at`; never a primary/secondary slot name."""
    if not (isinstance(value, dict) and {"provider", "account_wide", "windows"} <= set(value) <= {"provider", "account_wide", "windows", "plan"}
            and value["provider"] in ("codex",) and value["account_wide"] is True
            and (value.get("plan") is None or isinstance(value["plan"], str))
            and isinstance(value["windows"], list) and value["windows"]):
        return False
    return all(isinstance(w, dict) and set(w) == {"window_minutes", "used_percent", "resets_at"}
               and type(w["window_minutes"]) is int and w["window_minutes"] > 0
               and type(w["used_percent"]) in (int, float) and math.isfinite(w["used_percent"]) and w["used_percent"] >= 0
               and type(w["resets_at"]) is int and w["resets_at"] > 0 for w in value["windows"])


def valid_result(value, status="succeeded"):
    """Required keys with their shapes, and a verdict consistent with the run's
    status and checks; `test_changes` is optional and informational (a short
    list of printable rows); `quota` is optional and must satisfy valid_quota;
    unknown extra keys are tolerated so a newer runner's record never reads as
    invalid, and `source` is optional for checks recorded before it existed."""
    if not isinstance(value, dict) or not {"verdict", "changes", "checks", "cost"} <= set(value) or value["verdict"] not in VERDICTS:
        return False
    if status == "succeeded":
        if value["verdict"] not in ("ready-for-review", "no-changes", "checks-failed", "unverified"):
            return False
    elif value["verdict"] != verdict_for({"status": status}):
        return False
    changes = value["changes"]
    if changes is not None and not (isinstance(changes, dict) and set(changes) == {"commits", "files", "insertions", "deletions", "dirty"}
                                     and all(type(changes[key]) is int and changes[key] >= 0 for key in ("commits", "files", "insertions", "deletions"))
                                     and type(changes["dirty"]) is bool):
        return False
    if not isinstance(value["checks"], list) or not all(
            isinstance(check, dict) and {"cmd", "exit_code", "seconds", "tail"} <= set(check)
            and isinstance(check["cmd"], str) and (check["exit_code"] is None or type(check["exit_code"]) is int)
            and type(check["seconds"]) in (int, float) and isinstance(check["tail"], str)
            and ("source" not in check or isinstance(check["source"], str) and SOURCE.fullmatch(check["source"]))
            for check in value["checks"]):
        return False
    if value["verdict"] == "ready-for-review" and not (value["checks"] and all(check["exit_code"] == 0 for check in value["checks"])):
        return False
    rows = value.get("test_changes", [])
    if not (isinstance(rows, list) and len(rows) <= TEST_CHANGE_ROWS
            and all(isinstance(row, str) and row.isprintable() and len(json.dumps(row)) <= TEST_CHANGE_ROW for row in rows)):
        return False
    cost = value["cost"]
    if cost is not None and not (isinstance(cost, dict) and set(cost) == {"usd", "estimated", "model"}
                                  and type(cost["usd"]) in (int, float) and cost["usd"] >= 0 and type(cost["estimated"]) is bool
                                  and (cost["model"] is None or isinstance(cost["model"], str))):
        return False
    if "quota" in value and not valid_quota(value["quota"]):
        return False
    return True


def legacy_result(record):
    """A verdict for a record whose result is missing or malformed. A live
    worktree can still show `no-changes`; anything else is `unverified`."""
    changes = None
    if record.get("status") == "succeeded" and isinstance(record.get("session"), str) and isinstance(record.get("dir"), str):
        try:
            meta = json.loads(read_private(ROOT / "agents" / record["session"] / "meta.json"))
            if Path(record["dir"]).is_dir() and isinstance(meta.get("base"), str):
                changes = change_summary(Path(record["dir"]), meta["base"])
        except (OSError, ValueError, subprocess.CalledProcessError):
            changes = None
    return package(verdict_for(record, changes), changes)


def notify(record):
    result = record.get("result") or {}
    verdict = result.get("verdict")
    title = "Scheduled " + record["name"] + ": " + record["status"] + ("" if verdict in (None, record["status"]) else ", " + verdict)
    body = record.get("reason") or ("No checks ran; nothing verified the work. " if verdict == "unverified" and not result.get("checks") else "") \
        + "Open the overnight report to review output and the isolated worktree."
    try:
        subprocess.run([GRAVE, "notify", "--event", "agent-done", "--link", "/grave/?tab=work", title, body],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Results remain durable even if a notification channel is unavailable.


def new_run_id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def requeue(path, record):
    """A run that gaming mode cancelled is queued again once: the job's next
    due time becomes now, the cancelled record names the run that replaces it,
    and that run carries `requeue_of` so a second gaming cancel is final."""
    if record.get("reason") != GAMING_REASON or "requeue_of" in record:
        return
    with locked(path / ".config.lock"):
        job = read_job(record["name"])
        if not job["enabled"] or STOP:
            return
        job.update(next_due=time.time(), requeue={"run_id": new_run_id(), "of": record["run_id"]})
        write_json(path / "job.json", job)
        record["requeued"] = job["requeue"]["run_id"]


def defer(path, record, now=None):
    """A Claude run that ended on a usage-limit 429 is tried again when the
    limit resets: the job's `next_due` moves to the transcript's reset time
    when that is sooner than the next scheduled occurrence (or the job has
    none), and the record carries `deferred_to`. Resets in the past or more
    than DEFER_LIMIT away are left alone."""
    if record.get("agent") != "claude" or record.get("status") not in ("failed", "succeeded") or not isinstance(record.get("dir"), str):
        return
    limit = rate_limit_for(Path(record["dir"]), record.get("started", 0))
    if not limit:
        return
    now = time.time() if now is None else now
    resets = limit["resets_at"]
    if not record.get("reason"):
        record["reason"] = "Claude usage limit reached" + (" (" + limit["window"] + ")" if limit["window"] else "") \
            + "; resets " + dt.datetime.fromtimestamp(resets).isoformat(" ", "minutes")
    if not now < resets <= now + DEFER_LIMIT:
        return
    with locked(path / ".config.lock"):
        job = read_job(record["name"])
        if not job["enabled"] or STOP or (job["next_due"] is not None and job["next_due"] <= resets):
            return
        job["next_due"] = resets
        write_json(path / "job.json", job)
        record["deferred_to"] = resets


def worker(name):
    path = job_dir(name)
    with locked(path / ".run.lock", blocking=False):
        with locked(path / ".config.lock"):
            job = read_job(name)
            now = time.time()
            # Due while gaming: keep next_due and start after the thaw. Nothing is recorded.
            if not job["enabled"] or job["next_due"] is None or job["next_due"] > now or gaming():
                return
            due = job["next_due"]
            job["next_due"] = next_due(job["schedule"], now)
            pending = job.pop("requeue", None)
            run_id = pending["run_id"] if pending else new_run_id()
            target = path / "runs" / (run_id + ".json")
            record = dict(name=name, agent=job["agent"], repo=job["repo"], due=due,
                          started=now, status="running", run_id=run_id)
            if pending:
                record["requeue_of"] = pending["of"]
            write_json(target, record)
            write_json(path / "job.json", job)
        proc = None
        run_lock = None
        stop = stopper(name)
        try:
            if why := stop():
                record.update(status=why[0], reason=why[1])
            else:
                session = "job-" + name + "-" + run_id
                directory, history, run_lock, base = worktree(job, session)
                log = "session-" + time.strftime("%Y%m%d") + ".log"
                record.update(session=session, log=log, dir=str(directory), base=base)
                write_json(target, record)
                command = providers().job_command(job["agent"])
                prompt = ("Follow this repository's instructions. Work in this isolated worktree, run relevant checks, "
                          "and report results. Do not merge or deploy.\n\n" + read_private(path / "prompt.txt"))
                with tempfile.TemporaryFile() as stdin, open(history / log, "xb") as output:
                    stdin.write(prompt.encode()); stdin.seek(0)
                    if why := stop():
                        record.update(status=why[0], reason=why[1])
                        return
                    proc = subprocess.Popen(command, cwd=directory, stdin=stdin, stdout=output,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                    supervise(proc, stop, now, job["timeout"], record)
                    terminate(proc)
                    record["exit_code"] = proc.returncode
                    if record["status"] == "running":
                        record["status"] = "succeeded" if proc.returncode == 0 else "failed"
                    proc = None  # Process group has already been reaped.
                record["result"] = result_package(job, record, directory, base, history / log, now, stop)
        except Exception as error:
            record.update(status="failed", reason=fit(str(error), 1024, head=True))
        finally:
            if proc is not None:
                terminate(proc)
            if not valid_result(record.get("result"), record["status"]):
                record["result"] = package(verdict_for(record))  # succeeded without a result is unverified
            try:
                requeue(path, record)
                defer(path, record)
            except (OSError, ValueError) as error:
                print(name + ": could not requeue: " + str(error)[:200], file=sys.stderr, flush=True)
            record["finished"] = time.time()
            write_json(target, record)
            if run_lock is not None:
                os.close(run_lock)
            notify(record)


def read_record(path):
    """A run record, or None with a warning when it is larger than doctor allows:
    an oversized record is doctor's to flag, never a reason to wedge the job."""
    if path.lstat().st_size > RECORD_LIMIT:
        print(path.parent.parent.name + ": run record " + path.name + " exceeds " + str(RECORD_LIMIT) + " bytes; run grave doctor",
              file=sys.stderr, flush=True)
        return None
    value = json.loads(read_private(path))
    if not isinstance(value, dict):
        raise ValueError("invalid run record")
    return value


def recover(name):
    path = job_dir(name)
    try:
        with locked(path / ".run.lock", blocking=False):
            for target in (path / "runs").glob("*.json"):
                value = read_record(target)
                if value is None:
                    continue
                if value.get("status") == "running":
                    value.update(status="interrupted", reason="previous worker ended without a result", finished=time.time())
                    value["result"] = package("interrupted")
                    write_json(target, value)
                elif not valid_result(value.get("result"), value.get("status")):  # a valid verdict is never rewritten
                    value["result"] = legacy_result(value)
                    write_json(target, value)
    except BlockingIOError:
        pass


def scheduler():
    private_dir(STORE)
    children = {}
    with locked(STORE / ".scheduler.lock", blocking=False):
        try:
            while not STOP:
                for name in names():
                    if name in children and children[name].poll() is None:
                        continue
                    try:
                        recover(name)
                        job = read_job(name)
                        if job["enabled"] and job["next_due"] is not None and job["next_due"] <= time.time():
                            children[name] = subprocess.Popen([sys.executable, __file__, "worker", name])
                    except (OSError, ValueError) as error:
                        print(name + ": " + str(error), file=sys.stderr, flush=True)
                for _ in range(50):
                    if STOP:
                        break
                    time.sleep(.2)
        finally:
            for proc in children.values():
                if proc.poll() is None:
                    proc.terminate()
            for proc in children.values():
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()


def check(what=None):
    """Doctor: `check` covers definitions, storage, gaming-cancelled runs being
    requeued and the service (which must not wait on T3); `check results`
    requires every finished run record to carry a valid result and verdict,
    and any quota snapshot a window label and reset time."""
    for name in names():
        private_dir(job_dir(name), create=False)
        read_job(name)
        private_dir(job_dir(name) / "runs", create=False)
        try:
            with locked(job_dir(name) / ".run.lock", blocking=False):
                for path in (job_dir(name) / "runs").glob("*.json"):
                    if path.lstat().st_size > RECORD_LIMIT:
                        raise ValueError(name + ": run record " + path.name + " exceeds " + str(RECORD_LIMIT) + " bytes")
                    record = json.loads(read_private(path))
                    if not isinstance(record, dict) or record.get("status") not in STATUSES:
                        raise ValueError(name + ": invalid run record " + path.name)
                    if record.get("status") == "running":
                        raise ValueError(name + ": abandoned run; restart gravedecay-agents to reconcile it")
                    if record.get("reason") == GAMING_REASON and not (record.get("requeued") or record.get("requeue_of")):
                        raise ValueError(name + ": run " + path.name + " was cancelled by gaming mode and never requeued")
                    if what == "results":
                        if isinstance(record.get("result"), dict) and "quota" in record["result"] and not valid_quota(record["result"]["quota"]):
                            raise ValueError(name + ": run " + path.name + " carries a quota snapshot without a valid window_minutes and reset time")
                        if not valid_result(record.get("result"), record["status"]):
                            raise ValueError(name + ": run " + path.name + " has no valid verdict; restart gravedecay-agents to reconcile it")
                        for check_ in record["result"]["checks"]:
                            if check_.get("source", "owner") not in ("owner", "base:" + str(record.get("base"))):
                                raise ValueError(name + ": run " + path.name + " ran a check from " + check_["source"] + ", not the owner or its base commit")
        except BlockingIOError:
            pass  # A live worker owns the run and will record its result.
    if what == "results":
        table = providers()
        for provider in table.PROVIDERS:
            if not table.policy_ok(table.job_command(provider)):
                raise ValueError("job command for " + provider + " carries a bypass flag")
    elif names():
        subprocess.run(["systemctl", "is-enabled", "--quiet", "gravedecay-agents.service"], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", "gravedecay-agents.service"], check=True)
        shown = subprocess.run(["systemctl", "show", "gravedecay-agents.service", "-p", "After", "-p", "Requires", "-p", "Wants",
                                "-p", "BindsTo", "-p", "Requisite", "-p", "PartOf"], check=True, capture_output=True, text=True).stdout
        if "t3code.service" in shown:
            raise ValueError("gravedecay-agents.service orders after or depends on t3code.service; re-run raise.sh")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("name"); run.add_argument("--prompt-file", required=True)
    place = run.add_mutually_exclusive_group(); place.add_argument("--repo"); place.add_argument("--dir")
    when = run.add_mutually_exclusive_group(); when.add_argument("--at"); when.add_argument("--on")
    run.add_argument("--agent", choices=("codex", "claude"), default="codex")
    run.add_argument("--timeout", type=int, default=7200)
    run.add_argument("--check", action="append", metavar="CMD",
                     help="verification command run in the worktree after the agent; repeatable. "
                          "Without it the runner detects the base commit's package.json test script, pytest or make test.")
    jobs = sub.add_parser("jobs"); jobs.add_argument("command", nargs="?", choices=("cancel",)); jobs.add_argument("name", nargs="?"); jobs.add_argument("--json", action="store_true")
    sub.add_parser("scheduler")
    sub.add_parser("check").add_argument("what", nargs="?", choices=("results",))
    sub.add_parser("worker").add_argument("name")
    args = parser.parse_args()
    if os.getuid() == 0 or os.environ.get("MULTI_USER", "0") == "1":
        raise ValueError("scheduled runs require the single appliance owner, never root or multi-user mode")
    owner = ROOT / "config/owner"
    if owner.exists() and owner.read_text().strip() != pwd.getpwuid(os.getuid()).pw_name:
        raise ValueError("run this command as the recorded appliance owner")
    if args.action in ("worker", "scheduler"):
        signal.signal(signal.SIGTERM, stopped); signal.signal(signal.SIGINT, stopped)
    if args.action == "run":
        if not 60 <= args.timeout <= 86400:
            raise ValueError("--timeout must be 60–86400 seconds")
        add(args)
    elif args.action == "jobs":
        if args.command == "cancel":
            if not args.name:
                raise ValueError("use jobs cancel <name>")
            cancel(args.name)
        else:
            values = [read_job(name) for name in names()]
            for job in values:
                records = sorted((job_dir(job["name"]) / "runs").glob("*.json"))
                last = (read_record(records[-1]) if records else None) or {}
                job["last_status"] = last.get("status")
                job["last_verdict"] = (last.get("result") or {}).get("verdict")
            if args.json:
                print(json.dumps(values))
            else:
                for job in values:
                    due = dt.datetime.fromtimestamp(job["next_due"]).isoformat(" ", "minutes") if job["next_due"] else "—"
                    last = (job["last_status"] or "—") + ("/" + job["last_verdict"] if job["last_verdict"] else "")
                    print(f'{job["name"]:20} {job["agent"]:6} {job["repo"]:20} next {due}  {"enabled" if job["enabled"] else "cancelled"}  last {last}')
                if not values:
                    print("No scheduled jobs. Use grave agents run <name> --prompt-file <file> --repo <repo>.")
    elif args.action == "scheduler":
        scheduler()
    elif args.action == "worker":
        worker(args.name)
    else:
        check(args.what)


if __name__ == "__main__":
    try:
        main()
    except BlockingIOError:
        sys.exit(0)  # Another scheduler/worker owns this job; never overlap it.
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("grave jobs: " + str(error), file=sys.stderr)
        sys.exit(1)
