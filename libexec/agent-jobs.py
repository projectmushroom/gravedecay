#!/usr/bin/env python3
"""Owner-only persistent schedules and supervised, isolated headless agent runs."""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
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
STOP = False
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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


def read_job(name):
    path = job_dir(name)
    value = json.loads(read_private(path / "job.json"))
    if not isinstance(value, dict) or value.get("name") != name or value.get("agent") not in ("codex", "claude"):
        raise ValueError("invalid job metadata")
    repo_path(value.get("repo"))
    calendar(value.get("schedule"))
    if type(value.get("enabled")) is not bool or not isinstance(value.get("timeout"), int) or not 60 <= value["timeout"] <= 86400:
        raise ValueError("invalid job controls")
    if value.get("next_due") is not None and (type(value["next_due"]) not in (int, float) or not math.isfinite(value["next_due"]) or value["next_due"] < 0):
        raise ValueError("invalid due time")
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
    result = subprocess.run(["systemctl", "is-active", "--quiet", "t3code"], timeout=5)
    try:
        frozen = Path("/sys/fs/cgroup/grave-torpor/cgroup.freeze").read_text().strip() == "1"
    except OSError:
        frozen = False
    return result.returncode != 0 or frozen


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
                     timeout=args.timeout, enabled=True, created=now,
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
    command = ["git", *map(str, args)]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
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
    return directory, history, run_lock


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


def notify(record):
    try:
        subprocess.run([GRAVE, "notify", "--event", "agent-done", "--link", "/grave/?tab=work",
                        "Scheduled " + record["name"] + ": " + record["status"],
                        record.get("reason") or "Open the overnight report to review output and the isolated worktree."],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Results remain durable even if a notification channel is unavailable.


def worker(name):
    path = job_dir(name)
    with locked(path / ".run.lock", blocking=False):
        with locked(path / ".config.lock"):
            job = read_job(name)
            now = time.time()
            if not job["enabled"] or job["next_due"] is None or job["next_due"] > now:
                return
            due = job["next_due"]
            job["next_due"] = next_due(job["schedule"], now)
            run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
            target = path / "runs" / (run_id + ".json")
            record = dict(name=name, agent=job["agent"], repo=job["repo"], due=due,
                          started=now, status="running", run_id=run_id)
            write_json(target, record)
            write_json(path / "job.json", job)
        proc = None
        run_lock = None
        try:
            if gaming():
                record.update(status="skipped", reason="gaming mode or developer services unavailable")
            elif not read_job(name)["enabled"] or STOP:
                record.update(status="cancelled", reason="cancelled before launch")
            else:
                session = "job-" + name + "-" + run_id
                directory, history, run_lock = worktree(job, session)
                log = "session-" + time.strftime("%Y%m%d") + ".log"
                record.update(session=session, log=log, dir=str(directory))
                write_json(target, record)
                command = (["codex", "exec", "--sandbox", "workspace-write", "--color", "never", "-"]
                           if job["agent"] == "codex" else ["claude", "-p"])
                prompt = ("Follow this repository's instructions. Work in this isolated worktree, run relevant checks, "
                          "and report results. Do not merge or deploy.\n\n" + read_private(path / "prompt.txt"))
                with tempfile.TemporaryFile() as stdin, open(history / log, "xb") as output:
                    stdin.write(prompt.encode()); stdin.seek(0)
                    if STOP or not read_job(name)["enabled"]:
                        record.update(status="cancelled", reason="cancelled before provider launch")
                        return
                    if gaming():
                        record.update(status="skipped", reason="gaming mode entered before provider launch")
                        return
                    proc = subprocess.Popen(command, cwd=directory, stdin=stdin, stdout=output,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                    mode_check = time.monotonic()
                    while proc.poll() is None:
                        if STOP or not read_job(name)["enabled"]:
                            record.update(status="cancelled", reason="job or scheduler stopped")
                            break
                        if time.time() - now >= job["timeout"]:
                            record.update(status="timed-out", reason="configured runtime limit reached")
                            break
                        if time.monotonic() >= mode_check:
                            if gaming():
                                record.update(status="cancelled", reason="gaming mode entered during run")
                                break
                            mode_check = time.monotonic() + 2
                        time.sleep(.2)
                    terminate(proc)
                    record["exit_code"] = proc.returncode
                    if record["status"] == "running":
                        record["status"] = "succeeded" if proc.returncode == 0 else "failed"
        except Exception as error:
            record.update(status="failed", reason=str(error)[:500])
        finally:
            if proc is not None:
                terminate(proc)
            record["finished"] = time.time()
            write_json(target, record)
            if run_lock is not None:
                os.close(run_lock)
            notify(record)


def recover(name):
    path = job_dir(name)
    try:
        with locked(path / ".run.lock", blocking=False):
            for target in (path / "runs").glob("*.json"):
                value = json.loads(read_private(target))
                if value.get("status") == "running":
                    value.update(status="interrupted", reason="previous worker ended without a result", finished=time.time())
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


def check():
    for name in names():
        private_dir(job_dir(name), create=False)
        read_job(name)
        private_dir(job_dir(name) / "runs", create=False)
        try:
            with locked(job_dir(name) / ".run.lock", blocking=False):
                for path in (job_dir(name) / "runs").glob("*.json"):
                    record = json.loads(read_private(path))
                    if record.get("status") == "running":
                        raise ValueError(name + ": abandoned run; restart gravedecay-agents to reconcile it")
        except BlockingIOError:
            pass  # A live worker owns the run and will record its result.
    if names():
        subprocess.run(["systemctl", "is-enabled", "--quiet", "gravedecay-agents.service"], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", "gravedecay-agents.service"], check=True)


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
    jobs = sub.add_parser("jobs"); jobs.add_argument("command", nargs="?", choices=("cancel",)); jobs.add_argument("name", nargs="?"); jobs.add_argument("--json", action="store_true")
    sub.add_parser("scheduler"); sub.add_parser("check")
    sub.add_parser("worker").add_argument("name")
    args = parser.parse_args()
    if os.getuid() == 0 or os.environ.get("MULTI_USER", "0") == "1":
        raise ValueError("scheduled runs require the single appliance owner, never root or multi-user mode")
    owner = ROOT / "config/owner"
    if owner.exists() and owner.read_text().strip() != pwd.getpwuid(os.getuid()).pw_name:
        raise ValueError("run this command as the recorded appliance owner")
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
                job["last_status"] = json.loads(read_private(records[-1])).get("status") if records else None
            if args.json:
                print(json.dumps(values))
            else:
                for job in values:
                    due = dt.datetime.fromtimestamp(job["next_due"]).isoformat(" ", "minutes") if job["next_due"] else "—"
                    print(f'{job["name"]:20} {job["agent"]:6} {job["repo"]:20} next {due}  {"enabled" if job["enabled"] else "cancelled"}  last {job["last_status"] or "—"}')
                if not values:
                    print("No scheduled jobs. Use grave agents run <name> --prompt-file <file> --repo <repo>.")
    elif args.action == "scheduler":
        scheduler()
    elif args.action == "worker":
        worker(args.name)
    else:
        check()


if __name__ == "__main__":
    try:
        main()
    except BlockingIOError:
        sys.exit(0)  # Another scheduler/worker owns this job; never overlap it.
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("grave jobs: " + str(error), file=sys.stderr)
        sys.exit(1)
