#!/usr/bin/env python3
"""Versioned, offline Git/Python development benchmark. Stdlib + Git only."""
import argparse
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

SUITE = "grave-dev-v1"
MODES = ("quick", "capacity", "sustained")


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f)
    os.replace(f.name, path)


def lock(store):
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = (store / "run.lock").open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def status(store):
    # Read under the same lock as start: a stale run cannot overwrite a new one.
    guard = lock(store)
    try:
        state = read(store / "status.json") or {"state": "idle"}
        if guard and state["state"] in ("running", "cancelling"):
            state.update(state="interrupted", message="Runner stopped; run again.")
            write(store / "status.json", state)
        return {**state, "latest": read(store / "latest.json") or None}
    finally:
        if guard:
            guard.close()


def start(store, mode):
    if mode not in MODES:
        raise ValueError("Unknown benchmark mode")
    guard = lock(store)
    if guard is None:
        raise ValueError("A benchmark is already running")
    try:
        (store / "cancel").unlink(missing_ok=True)
        state = {"state": "running", "mode": mode, "started_at": time.time(),
                 "progress": 0, "message": "Preparing fixed sample project…"}
        write(store / "status.json", state)
        # Inherit the held flock, so CLI/dashboard starts share one exclusion
        # boundary even during startup. The OS releases it after a crash.
        with (store / "runner.log").open("w") as log:
            subprocess.Popen([sys.executable, __file__, "--root", str(store.parent.parent),
                              "_run", "--mode", mode, "--lock-fd", str(guard.fileno())],
                             pass_fds=(guard.fileno(),), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        return state
    except Exception:
        write(store / "status.json", {"state": "failed", "message": "Could not start runner"})
        raise
    finally:
        guard.close()


def cancel(store):
    guard = lock(store)
    if guard:
        guard.close()
        return status(store)
    (store / "cancel").touch(mode=0o600)
    return {"state": "cancelling", "message": "Stopping benchmark…"}


def request(store, action, mode="quick"):
    # Serialize the short control operations too: a late cancel must never
    # cancel a different run started concurrently by another browser/CLI.
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store / "control.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        return start(store, mode) if action == "start" else {"status": status, "cancel": cancel}[action](store)


class Cancelled(Exception):
    pass


def command(args, cwd, store, deadline, env):
    if (store / "cancel").exists():
        raise Cancelled()
    started = time.monotonic()
    # A file avoids pipe-buffer deadlocks; output is never part of the score.
    with tempfile.TemporaryFile() as errors:
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                             stderr=errors, start_new_session=True)
        try:
            while p.poll() is None:
                if (store / "cancel").exists():
                    raise Cancelled()
                if time.monotonic() > min(deadline, started + 60):
                    raise RuntimeError("Workload timed out")
                time.sleep(0.02)
            if p.returncode:
                errors.seek(0)
                raise RuntimeError(f"{Path(args[0]).name} failed: " + errors.read(2000).decode(errors="replace"))
        finally:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            p.wait()
    return time.monotonic() - started


def fixture(root):
    root.mkdir()
    for i in range(128):
        (root / f"module_{i:03}.py").write_text("\n".join(
            f"def transform_{j}(values):\n    return sorted(value + {j} for value in values)\n"
            for j in range(48)))
    (root / "test_project.py").write_text(
        "import importlib, unittest\n"
        "class ProjectTest(unittest.TestCase):\n"
        "    def test_modules(self):\n"
        "        for i in range(128):\n"
        "            module = importlib.import_module(f'module_{i:03}')\n"
        "            for j in range(48):\n"
        "                self.assertEqual(getattr(module, f'transform_{j}')(range(511, -1, -1)), list(range(j, j + 512)))\n")
    (root / ".gitignore").write_text("__pycache__/\n")


def cycle(repo, store, deadline, env):
    run = lambda *args: command(list(args), repo, store, deadline, env)
    worktree = repo.parent / (repo.name + "-worktree")
    t = time.monotonic()
    run("git", "status", "--porcelain")
    run("git", "grep", "-l", "transform_47", "--", "*.py")
    run("git", "diff", "--no-ext-diff")
    run("git", "worktree", "add", "--detach", str(worktree), "HEAD")
    run("git", "worktree", "remove", str(worktree))
    repo_seconds = time.monotonic() - t
    build = run(sys.executable, "-I", "-m", "compileall", "-q", "-f", ".")
    tests = run(sys.executable, "-E", "-s", "-m", "unittest", "-q", "test_project")
    return {"repository_s": repo_seconds, "build_s": build, "tests_s": tests,
            "total_s": time.monotonic() - t}


def environment(env):
    info = {"host": platform.node(), "os": platform.system(), "os_version": platform.release(),
            "architecture": platform.machine(), "python": platform.python_version(),
            "git": subprocess.check_output(["git", "--version"], env=env, text=True, timeout=3).strip(),
            "logical_cpus": os.cpu_count(), "load_before": list(os.getloadavg()),
            "execution": os.environ.get("GRAVEDECAY_PLATFORM", platform.system().lower())}
    try:
        if sys.platform == "darwin":
            info["memory_bytes"] = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=3))
            info["power"] = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=3).strip()
        else:
            info["memory_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            info["power"] = {p.parent.name: p.read_text().strip()
                             for p in Path("/sys/class/power_supply").glob("*/online")}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return info


def run(store, mode):
    state = read(store / "status.json")
    started = time.monotonic()
    deadline = started + (900 if mode == "sustained" else 300)

    def progress(message, pct):
        state.update(message=message, progress=pct, elapsed_s=round(time.monotonic() - started, 1))
        write(store / "status.json", state)

    try:
        if not shutil.which("git"):
            raise RuntimeError("Git is required; install Git and run again")
        # Ignore user Git hooks/config and Python options: never execute project
        # or user-provided code. Fixture data stays on the workspace filesystem.
        with tempfile.TemporaryDirectory(prefix="work-", dir=store) as temp:
            base = Path(temp)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PYTHON"))}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TEMPLATE_DIR=str(base / "empty-template"), LC_ALL="C")
            (base / "empty-template").mkdir()
            info = environment(env)
            repo = base / "worker-0"
            fixture(repo)
            for args in (["git", "init", "-q"], ["git", "add", "."],
                         ["git", "-c", "user.name=Grave Bench", "-c", "user.email=bench@localhost",
                          "-c", "commit.gpgsign=false", "commit", "-qm", "Fixed benchmark fixture"]):
                command(args, repo, store, deadline, env)
            progress("Warming up repository, build and tests…", 10)
            cycle(repo, store, deadline, env)
            samples = []
            for i in range(3):
                progress(f"Single worker · sample {i + 1} of 3", 20 + i * 10)
                samples.append(cycle(repo, store, deadline, env))
            single = {k: statistics.median(s[k] for s in samples) for k in samples[0]}
            result = {"suite": SUITE, "mode": mode, "environment": info,
                      "measured_at": time.time(), "single": single, "samples": samples,
                      "score": round(1000 / single["total_s"]), "parallel": [],
                      "score_definition": "100 = one standard dev cycle in 10 seconds"}
            if mode == "capacity":
                # ponytail: cap at eight synthetic workers; larger fleet workloads
                # need a separate profile, not an unbounded stress test button.
                limit = min(8, os.cpu_count() or 1)
                repos = [repo]
                for n in range(1, limit):
                    dest = base / f"worker-{n}"
                    command(["git", "clone", "-q", "--no-hardlinks", str(repo), str(dest)], base, store, deadline, env)
                    repos.append(dest)
                for workers in (1, 2, 4, 8):
                    if workers > limit:
                        break
                    progress(f"Parallel workload · {workers} worker(s)", 50 + workers * 5)
                    batches = []
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                        for _ in range(3):
                            t = time.monotonic()
                            futures = [pool.submit(cycle, r, store, deadline, env) for r in repos[:workers]]
                            durations = [f.result()["total_s"] for f in futures]
                            batches.append({"cycles_per_min": 60 * workers / (time.monotonic() - t),
                                            "slowdown": max(durations) / single["total_s"]})
                    result["parallel"].append({"workers": workers, **{
                        k: statistics.median(b[k] for b in batches) for k in batches[0]}})
                eligible = [b for b in result["parallel"] if b["slowdown"] <= 2]
                result["best_workers"] = max(eligible, key=lambda b: b["cycles_per_min"])["workers"] if eligible else None
            if mode == "sustained":
                end = time.monotonic() + 600
                sustained = []
                while time.monotonic() < end:
                    progress("Sustained workload · " + str(max(0, round(end - time.monotonic()))) + "s remaining",
                             min(99, 50 + 50 * (1 - (end - time.monotonic()) / 600)))
                    sustained.append(cycle(repo, store, deadline, env)["total_s"])
                result["sustained"] = {"cycles": len(sustained),
                                       "cycles_per_min": 60 * len(sustained) / sum(sustained),
                                       "last_vs_first": statistics.median(sustained[-5:]) / statistics.median(sustained[:5])}
            result["duration_s"] = round(time.monotonic() - started, 1)
            result["environment"]["load_after"] = list(os.getloadavg())
            # Keep a bounded local history for comparing machines and reruns.
            history = store / "results"
            history.mkdir(exist_ok=True, mode=0o700)
            write(history / f"{time.time_ns()}.json", result)
            for old in sorted(history.glob("*.json"))[:-20]:
                old.unlink()
            write(store / "latest.json", result)
            state.update(state="completed", message="Benchmark complete", progress=100)
    except Cancelled:
        state.update(state="cancelled", message="Benchmark cancelled; previous result kept")
    except Exception as e:
        state.update(state="failed", message=str(e))
    finally:
        state["elapsed_s"] = round(time.monotonic() - started, 1)
        write(store / "status.json", state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("GRAVE_ROOT", "/srv/dev"))
    parser.add_argument("command", nargs="?", default="quick",
                        choices=(*MODES, "start", "status", "cancel", "check", "_run"))
    parser.add_argument("--mode", choices=MODES, default="quick")
    parser.add_argument("--json", action="store_true", help="print the complete result as JSON")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    store = Path(args.root).resolve() / "config" / "benchmark"
    if args.command == "check":
        if not shutil.which("git"):
            parser.exit(1, "benchmark: Git missing\n")
        print(f"benchmark: {SUITE} ready (Python + Git; no workload run)")
        return
    if args.command == "_run":
        if args.lock_fd is None:
            parser.error("internal runner requires lock")
        with os.fdopen(args.lock_fd) as guard:
            run(store, args.mode)
        return
    try:
        if args.command in ("start", "cancel", "status"):
            value = request(store, args.command, args.mode)
        else:
            request(store, "start", args.command)
            last = None
            try:
                while True:
                    value = request(store, "status")
                    if not args.json and value.get("message") != last:
                        last = value.get("message")
                        print(last, flush=True)
                    if value["state"] not in ("running", "cancelling"):
                        break
                    time.sleep(0.5)
            except KeyboardInterrupt:
                request(store, "cancel")
                parser.exit(130, "Benchmark cancellation requested\n")
            if value["state"] != "completed":
                parser.exit(1, value.get("message", "Benchmark failed") + "\n")
        if args.json or args.command not in MODES:
            print(json.dumps(value, indent=2))
        else:
            r = value["latest"]
            print(f"Dev score: {r['score']} · cycle: {r['single']['total_s']:.2f}s · {r['suite']}")
            for row in r["parallel"]:
                print(f"  {row['workers']} worker(s): {row['cycles_per_min']:.1f} cycles/min · {row['slowdown']:.2f}× slowdown")
            if r.get("sustained"):
                print(f"  Sustained: {r['sustained']['cycles_per_min']:.1f} cycles/min")
            print(f"Results: {store / 'latest.json'}")
    except (OSError, ValueError) as e:
        parser.exit(1, str(e) + "\n")


if __name__ == "__main__":
    main()
