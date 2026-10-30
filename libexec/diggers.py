#!/usr/bin/env python3
"""Diggers: focused agents bound to one job, one repository and the channels you bind them to.

A validated definition, an identity tier, the digger's own provider login, a
deny-by-default policy compiled through the one provider table, the doctor
probe and, from M2, the conversation runner (docs/DIGGERS.md): ``serve`` is the
digger unit's loop. It reads requests from an inbox directory the owner writes
(``ask``, ``cancel``, ``finish``, ``kill``), runs one provider turn at a time
through the Keeper's runner in a per-conversation checkout, and ends each
conversation with the job runner's result package. It never listens on a socket.

``grave digger …`` runs this file as the appliance owner. Work that needs the
``dedicated`` tier's own Unix identity goes through ``grave __digger …``: the
root entry the owner's scoped grant already covers. Root creates the account,
its HOME and a root-owned copy of the policy, and acts *as* the digger through
``runuser`` for everything inside that HOME. No identity ever runs Git in a
repository another identity owns: the clone arrives as a bundle. Stdlib only.
"""
import argparse
import getpass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

ROOT = Path(os.environ.get("GRAVE_ROOT", "/srv/dev"))
STORE = ROOT / "config/diggers"
HOMES = ROOT / "diggers"
TOOLS = HOMES / ".tools"
POLICIES = HOMES / ".policy"
GRAVE = os.environ.get("GRAVE_BIN", "/usr/local/bin/grave")
# Tests run every identity as the current user and skip useradd/chown/runuser/systemctl.
TEST = os.environ.get("GRAVE_DIGGER_TEST") == "1"
NAME = re.compile(r"[a-z][a-z0-9-]{1,19}")
RESERVED = ("keeper",)
IDENTITIES = ("shared", "separate-login", "dedicated")
WORK = {"shared": "worktree", "separate-login": "worktree", "dedicated": "clone"}
KEYS = {"name", "provider", "model", "identity", "repo", "work", "tools", "budget", "pause_in_gaming", "enabled"}
OPTIONAL = {"checks", "prompts"}  # absent means: detect the base commit's checks; no composer chips
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,19})\}")
BUDGET = {"max_turns": (1, 200), "timeout_s": (60, 86400), "daily_turns": (1, 10000)}
DEFAULT_BUDGET = {"max_turns": 30, "timeout_s": 1800, "daily_turns": 200, "daily_usd": 10.0}
MCP_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
MODEL = re.compile(r"[A-Za-z0-9._:\[\]-]{0,64}")
REPO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}")
INSTRUCTIONS_LIMIT = 32 * 1024   # DIGGER.md; Codex receives it as one argument
DEFINITION_LIMIT = 64 * 1024
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|_KEY$|^KEY_|APIKEY", re.I)
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
STARTER = """# {name}

You are the {name} digger for the {repo} repository: one job, done the same way every time.

<!-- Describe the job, its inputs and what "done" looks like. Keep secrets out of
     this file: it is instructions, not a credential store, and not a guardrail. -->

Follow the repository's own AGENTS.md / CLAUDE.md. Use only the tools you are given.
Never merge, deploy or push.
"""


class DiggerError(Exception):
    pass


def sibling(filename):
    """A shared script: beside this file when installed, libexec or dashboard in the repository."""
    here = Path(__file__).resolve().parent
    for candidate in (here / filename, here.parent / "libexec" / filename, here.parent / "dashboard" / filename):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("grave_digger_" + candidate.stem.replace("-", "_"), candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise DiggerError(f"{filename} is not installed beside diggers.py; re-run raise.sh")


table = sibling("providers.py")      # the one provider command table
keeper = sibling("keeper.py")        # the turn runner the Gravekeeper uses
jobs = sibling("agent-jobs.py")      # worktrees, checks, verdicts and the gaming flag


# ------------------------------------------------------------------ schema ----
def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise DiggerError("digger name must match ^[a-z][a-z0-9-]{1,19}$")
    if name in RESERVED:
        raise DiggerError(f"'{name}' is reserved for the Gravekeeper")
    return name


def printable(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()


def validate(value, name=None, instructions=None):
    """Raise DiggerError unless ``value`` is a complete, known-keys-only definition
    whose compiled command passes the provider table's bypass policy."""
    if not isinstance(value, dict):
        raise DiggerError("digger.json must hold one JSON object")
    unknown, missing = set(value) - KEYS - OPTIONAL, KEYS - set(value)
    if unknown:
        raise DiggerError("unknown key(s): " + ", ".join(sorted(unknown)))
    if missing:
        raise DiggerError("missing key(s): " + ", ".join(sorted(missing)))
    check_name(value["name"])
    if name is not None and value["name"] != name:
        raise DiggerError(f"name {value['name']!r} does not match its directory {name!r}")
    if value["provider"] not in table.PROVIDERS:
        raise DiggerError("provider must be one of: " + ", ".join(table.PROVIDERS))
    if not isinstance(value["model"], str) or not MODEL.fullmatch(value["model"]):
        raise DiggerError("model must be empty (CLI default) or a model name")
    if value["identity"] not in IDENTITIES:
        raise DiggerError("identity must be one of: " + ", ".join(IDENTITIES))
    if not isinstance(value["repo"], str) or not REPO.fullmatch(value["repo"]):
        raise DiggerError("repo must name a checkout under GRAVE_ROOT/repos")
    if value["work"] != WORK[value["identity"]]:
        raise DiggerError("a dedicated digger works in its own clone; shared and separate-login diggers in a worktree"
                          f" (identity {value['identity']} needs work={WORK[value['identity']]!r})")
    tools = value["tools"]
    if not isinstance(tools, dict) or set(tools) != {"mcp", "allow", "deny"}:
        raise DiggerError("tools must hold exactly mcp, allow and deny")
    if not isinstance(tools["mcp"], dict) or len(tools["mcp"]) > 8:
        raise DiggerError("tools.mcp must map at most 8 server ids to commands")
    for server_id, server in tools["mcp"].items():
        if not MCP_ID.fullmatch(server_id):
            raise DiggerError(f"MCP server id {server_id!r} must match ^[a-z][a-z0-9-]{{0,31}}$")
        if not isinstance(server, dict) or set(server) != {"command", "args"}:
            raise DiggerError(f"MCP server {server_id} must hold exactly command and args")
        if not printable(server["command"], 200) or not isinstance(server["args"], list) or len(server["args"]) > 32 \
                or not all(printable(arg, 500) for arg in server["args"]):
            raise DiggerError(f"MCP server {server_id}: command and args must be printable text")
    for key in ("allow", "deny"):
        rules = tools[key]
        if not isinstance(rules, list) or len(rules) > 64 or len(set(map(str, rules))) != len(rules):
            raise DiggerError(f"tools.{key} must be a list of at most 64 distinct rules")
        for rule in rules:
            try:
                table.parse_rule(rule)
            except ValueError as error:
                raise DiggerError(f"tools.{key}: {error}") from None
    budget = value["budget"]
    if not isinstance(budget, dict) or set(budget) != set(DEFAULT_BUDGET):
        raise DiggerError("budget must hold exactly " + ", ".join(sorted(DEFAULT_BUDGET)))
    for key, (low, high) in BUDGET.items():
        if type(budget[key]) is not int or not low <= budget[key] <= high:
            raise DiggerError(f"budget.{key} must be an integer from {low} to {high}")
    usd = budget["daily_usd"]
    if type(usd) not in (int, float) or not math.isfinite(usd) or not 0 <= usd <= 10000:
        raise DiggerError("budget.daily_usd must be a number from 0 to 10000")
    for key in ("pause_in_gaming", "enabled"):
        if type(value[key]) is not bool:
            raise DiggerError(f"{key} must be true or false")
    if not jobs.valid_checks(value.get("checks")):
        raise DiggerError(f"checks must be null or at most {jobs.CHECK_LIMIT} printable commands"
                          f" of up to {jobs.CHECK_LENGTH} characters")
    prompts = value.get("prompts", [])
    if not isinstance(prompts, list) or len(prompts) > 8 or not all(
            isinstance(p, dict) and set(p) == {"label", "template"} and printable(p["label"], 40)
            and printable(p["template"], 2000) and not set("{}") & set(PLACEHOLDER.sub("", p["template"]))
            for p in prompts):
        raise DiggerError("prompts must be at most 8 {label, template} chips; a template's {field} names"
                          " are lowercase words")
    argv, _ = compile_command(value, "" if instructions is None else instructions)
    if not table.policy_ok(argv):
        raise DiggerError("compiled command carries a permission-bypass flag; remove it from the definition"
                          " (DIGGER.md and MCP arguments included)")
    return value


def layout(definition, instructions=""):
    """Where one digger's compiled policy, instructions and provider state live."""
    name, identity, provider = definition["name"], definition["identity"], definition["provider"]
    if identity == "dedicated":
        home = HOMES / name
        policy = POLICIES / name
        return {"policy": str(policy), "instructions": str(policy / "DIGGER.md"), "instructions_text": instructions,
                "home": str(home), "path": f"{TOOLS / 'bin'}:{SYSTEM_PATH}",
                "provider_home": str(home / (".codex" if provider == "codex" else ".claude")),
                "work": str(home / "repos" / definition["repo"])}
    base = STORE / name
    return {"policy": str(base / "policy"), "instructions": str(base / "DIGGER.md"), "instructions_text": instructions,
            "provider_home": str(base / provider) if identity == "separate-login" else None, "work": None}


def compile_command(definition, instructions=""):
    try:
        return table.digger_command(definition, "digger", True, layout(definition, instructions))
    except ValueError as error:
        raise DiggerError(str(error)) from None


def generated(definition, instructions):
    """The policy directory's exact contents. A dedicated digger reads its own
    definition from here: it cannot read the owner's config tree."""
    try:
        files = table.digger_files(definition)
    except ValueError as error:
        raise DiggerError(str(error)) from None
    files.pop("digger.rules", None)  # lives in the digger's CODEX_HOME/rules, written separately
    if definition["identity"] == "dedicated":
        files["digger.json"] = json.dumps(definition, indent=2, sort_keys=True) + "\n"
        files["DIGGER.md"] = instructions
    return files


def codex_rules(definition):
    """Refused shell prefixes for a Codex digger with its own CODEX_HOME, else None."""
    if definition["provider"] != "codex" or definition["identity"] == "shared":
        return None
    return table.digger_files(definition)["digger.rules"]


# ------------------------------------------------------------- file store ----
def owner_uid():
    if TEST:
        return os.geteuid()
    path = ROOT / "config/owner"
    if path.is_symlink():
        raise DiggerError("config/owner must not be a symlink")
    try:
        return pwd.getpwnam(path.read_text().strip()).pw_uid
    except (OSError, KeyError):
        raise DiggerError("appliance owner not recorded; re-run raise.sh") from None


def private_dir(path, uid, create=False, mode=0o700):
    if create:
        try:
            os.mkdir(path, mode)
        except FileExistsError:
            pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & (0o777 & ~mode):
        raise DiggerError(f"{path} must be a real directory owned by uid {uid} with mode {mode:o}")


def read_private(path, uid, limit, mode=0o600):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise DiggerError(f"{path} is missing") from None
    except OSError as error:
        raise DiggerError(f"{path}: {error.strerror}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & (0o777 & ~mode) or info.st_size > limit:
            raise DiggerError(f"{path} must be a regular file owned by uid {uid}, mode {mode:o}, at most {limit} bytes")
        data = stream.read(limit + 1)
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise DiggerError(f"{path} must be UTF-8 text") from None


def write_file(path, text, mode=0o600, group=None):
    """Atomic replace; the temporary file is created beside the target, never followed."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        os.fchmod(fd, mode)
        if group is not None and not TEST:
            os.fchown(fd, 0, group)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def names():
    if not STORE.exists():
        return []
    private_dir(STORE, owner_uid())
    return sorted(path.name for path in STORE.iterdir() if not path.name.startswith("."))


def load(name):
    """``(definition, instructions)`` from the owner's store, validated."""
    check_name(name)
    uid = owner_uid()
    private_dir(STORE, uid)
    private_dir(STORE / name, uid)
    text = read_private(STORE / name / "digger.json", uid, DEFINITION_LIMIT)
    try:
        definition = json.loads(text)
    except json.JSONDecodeError as error:
        raise DiggerError(f"digger.json does not parse: {error}") from None
    instructions = read_private(STORE / name / "DIGGER.md", uid, INSTRUCTIONS_LIMIT)
    if "\x00" in instructions:
        raise DiggerError("DIGGER.md must not contain NUL bytes")
    return validate(definition, name, instructions), instructions


def save(definition):
    write_file(STORE / definition["name"] / "digger.json", json.dumps(definition, indent=2, sort_keys=True) + "\n")


def repo_path(name):
    path = ROOT / "repos" / name
    if path.is_symlink() or not (path / ".git").is_dir() or (path / ".git").is_symlink():
        raise DiggerError(f"repo {name}: choose a primary, non-symlink Git checkout under GRAVE_ROOT/repos")
    return path


def git(*args):
    result = subprocess.run(["git", *map(str, args)], capture_output=True, text=True, timeout=600,
                            env={"GIT_SSH_COMMAND": "ssh -o BatchMode=yes", **os.environ,
                                 "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise DiggerError("git " + " ".join(map(str, args[:3])) + ": " + (result.stderr.strip() or "failed"))
    return result.stdout.strip()


def source_origin(repo):
    """The primary checkout's origin, which a dedicated clone must carry."""
    try:
        return git("-C", repo_path(repo), "remote", "get-url", "origin")
    except DiggerError:
        return ""


def scrubbed(extra=None):
    """The caller's environment without credentials, so a status reflects the
    digger's own login rather than an API key in the owner's shell."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.update(extra or {})
    return env


def sync_owner_side(definition, instructions):
    """Compile the policy for an owner-account digger into its private store."""
    uid = owner_uid()
    base = STORE / definition["name"]
    private_dir(base / "policy", uid, create=True)
    files = generated(definition, instructions)
    for stale in set(os.listdir(base / "policy")) - set(files):
        if not stale.startswith("."):
            os.unlink(base / "policy" / stale)
    for name, text in files.items():
        write_file(base / "policy" / name, text)
    for directory in channel(definition):
        private_dir(directory, uid, create=True)
    home = layout(definition)["provider_home"]
    if home:
        # Codex refuses a CODEX_HOME under a temporary directory, so it lives here.
        private_dir(home, uid, create=True)
        rules = codex_rules(definition)
        if rules is not None:
            private_dir(Path(home) / "rules", uid, create=True)
            write_file(Path(home) / "rules" / "digger.rules", rules)


# --------------------------------------------------------------- root side ----
def digger_user(name):
    return "grave-digger-" + name


def account(name, create=False):
    """The dedicated digger's Unix account; drift is reported, never repaired."""
    if TEST:
        return pwd.getpwuid(os.geteuid())
    home = HOMES / name
    try:
        entry = pwd.getpwnam(digger_user(name))
    except KeyError:
        if not create:
            raise DiggerError(f"Unix user {digger_user(name)} is missing; run: grave digger enable {name}") from None
        if os.path.lexists(home):
            raise DiggerError(f"unmanaged digger home already exists: {home}")
        shell = shutil.which("nologin", path="/usr/sbin:/usr/bin:/sbin:/bin") or "/usr/sbin/nologin"
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", str(home),
                        "--shell", shell, "--comment", f"gravedecay digger {name}", digger_user(name)], check=True)
        entry = pwd.getpwnam(digger_user(name))
    if entry.pw_uid == 0 or entry.pw_dir != str(home):
        raise DiggerError(f"unsafe Unix account drift for {digger_user(name)} (uid 0 or HOME is not {home})")
    return entry


def trusted_parent(path, create=False):
    """Every ancestor of a root-managed digger path is owned by root or the
    owner and not writable by anyone else; created root-owned 0755 when asked."""
    owners = {0, owner_uid(), os.geteuid()}
    current = Path("/")
    for part in Path(path).parts[1:]:
        current = current / part
        if create and not os.path.lexists(current):
            os.mkdir(current, 0o755)
        info = os.lstat(current)
        sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners or (info.st_mode & 0o022 and not sticky):
            raise DiggerError(f"unsafe ancestor for digger state: {current}")


def as_digger(name, argv, check=True, capture=False, stdin=None, input=None, extra=None):
    """Run ``argv`` as the dedicated digger with a clean environment and its HOME."""
    entry = account(name)
    home = str(HOMES / name)
    env = {"HOME": home, "PATH": f"{TOOLS / 'bin'}:{SYSTEM_PATH}", "LANG": os.environ.get("LANG", "C.UTF-8"),
           "TERM": os.environ.get("TERM", "dumb"), "USER": entry.pw_name, "LOGNAME": entry.pw_name,
           "DISABLE_AUTOUPDATER": "1", "GRAVE_ROOT": str(ROOT), **({"GRAVE_DIGGER_TEST": "1"} if TEST else {}),
           **(extra or {})}
    if TEST:
        command = argv
        env["PATH"] = env["PATH"] + ":" + os.environ.get("PATH", "")
    else:
        command = ["runuser", "-u", entry.pw_name, "--", "env", "-i", *[f"{k}={v}" for k, v in env.items()], *argv]
    return subprocess.run(command, check=check, capture_output=capture, text=True, stdin=stdin, input=input,
                          env=env if TEST else None, cwd=home)


def helper(name, *args, **options):
    """This script's own unprivileged helpers, run as the digger inside its HOME."""
    return as_digger(name, [sys.executable, "-I", str(Path(__file__).resolve()), "__as-digger", *args], **options)


def prepare_home(name):
    entry = account(name, create=True)
    trusted_parent(HOMES, create=True)
    home = HOMES / name
    try:
        os.mkdir(home, 0o700)
        if not TEST:
            os.chown(home, entry.pw_uid, entry.pw_gid)
    except FileExistsError:
        pass
    info = os.lstat(home)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != entry.pw_uid or info.st_mode & 0o077:
        raise DiggerError(f"unsafe digger home ownership or mode: {home} (expected {entry.pw_name}, 0700)")
    return entry


def write_policy(definition, instructions, entry):
    """Root-owned, digger-readable: the agent cannot rewrite its own policy."""
    trusted_parent(POLICIES, create=True)
    path = POLICIES / definition["name"]
    if not os.path.lexists(path):
        os.mkdir(path, 0o750)
        if not TEST:
            os.chown(path, 0, entry.pw_gid)
    info = os.lstat(path)
    expected_uid = os.geteuid() if TEST else 0
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid or info.st_mode & 0o027:
        raise DiggerError(f"unsafe digger policy directory: {path}")
    files = generated(definition, instructions)
    for stale in set(os.listdir(path)) - set(files):
        if not stale.startswith("."):
            os.unlink(path / stale)
    for filename, text in files.items():
        write_file(path / filename, text, 0o640, group=entry.pw_gid)
    rules = codex_rules(definition)
    if rules is not None:
        helper(definition["name"], "write", ".codex/rules/digger.rules", input=rules)


def tool_source(provider, path, native_only=not TEST):
    """Validate the owner's provider CLI before root copies it for the digger."""
    source = Path(os.path.realpath(path))
    trusted = (0, owner_uid())
    info = os.stat(source)
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in trusted or not info.st_mode & 0o100:
        raise DiggerError(f"{provider}: {source} is not an executable owned by root or the owner")
    with open(source, "rb") as stream:
        if native_only and stream.read(4) != b"\x7fELF":
            raise DiggerError(f"{provider}: {source} is a script, not a native build; install the native {provider}"
                              f" CLI or set DIGGER_{provider.upper()}_BIN in grave.conf to it"
                              " (a script needs its interpreter, which the digger cannot reach)")
    # The standalone Codex release keeps helpers beside bin/: copy that whole install.
    tree = source.parent.parent if (source.parent.name == "bin"
                                    and any((source.parent.parent / mark).exists() for mark in ("codex-resources", "codex-path"))) else None
    for directory, dirs, files in os.walk(tree) if tree else ():
        for entry in [*dirs, *files]:
            entry_info = os.lstat(os.path.join(directory, entry))
            if entry_info.st_uid not in trusted or not (stat.S_ISREG(entry_info.st_mode) or stat.S_ISDIR(entry_info.st_mode)
                                                        or stat.S_ISLNK(entry_info.st_mode)):
                raise DiggerError(f"{provider}: {tree} holds a file root will not copy: {entry}")
    return source, tree, info


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()[:16]


def install_tool(provider, path):
    """A root-owned, world-readable copy of the owner's provider CLI. The owner's
    HOME stays closed to the digger; the copy is shared by every digger."""
    source, tree, info = tool_source(provider, path)
    trusted_parent(TOOLS, create=True)
    os.makedirs(TOOLS / "bin", 0o755, exist_ok=True)
    stamp = {"source": str(source), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    current = TOOLS / f"{provider}.source"
    try:
        if json.loads(current.read_text()) == stamp and os.access(TOOLS / "bin" / provider, os.X_OK):
            return False
    except (OSError, ValueError):
        pass
    version = f"{provider}-{digest(source)}"
    target = TOOLS / version
    if not target.exists():
        partial = Path(tempfile.mkdtemp(dir=TOOLS, prefix=".partial-"))
        try:
            if tree:
                shutil.copytree(tree, partial / "install", symlinks=True)
            else:
                (partial / "install").mkdir()
                shutil.copy2(source, partial / "install" / provider)
            for directory, dirs, files in os.walk(partial):
                for entry in [directory, *(os.path.join(directory, f) for f in files)]:
                    if os.path.islink(entry):
                        continue
                    if not TEST:
                        os.chown(entry, 0, 0)
                    mode = os.lstat(entry).st_mode
                    # 0o755 at most: never a set-id bit on a root-owned copy.
                    os.chmod(entry, (mode | 0o444 | (0o111 if mode & 0o100 or stat.S_ISDIR(mode) else 0)) & 0o755)
            os.chmod(partial, 0o755)
            os.rename(partial, target)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
    binary = (Path(version) / "install" / source.relative_to(tree)) if tree else Path(version) / "install" / provider
    link = TOOLS / "bin" / f".{provider}.new"
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(Path("..") / binary, link)
    os.replace(link, TOOLS / "bin" / provider)
    write_file(current, json.dumps(stamp) + "\n", 0o644)
    for old in TOOLS.glob(f"{provider}-*"):
        if old.name != version:
            shutil.rmtree(old, ignore_errors=True)
    return True


def clone(definition, bundle, branch, origin):
    """The digger's private clone, from a bundle of the owner's committed HEAD."""
    name, repo = definition["name"], definition["repo"]
    uid = owner_uid()
    fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
            raise DiggerError("clone bundle must be a private regular file owned by the appliance owner")
        helper(name, "clone", repo, branch, origin, stdin=stream)


def root_provision(args):
    definition, instructions = load(args.name)
    if definition["identity"] != "dedicated":
        raise DiggerError(f"{args.name} is not a dedicated digger")
    entry = prepare_home(args.name)
    if args.tool:
        install_tool(definition["provider"], args.tool)
    write_policy(definition, instructions, entry)
    prepare_channel(args.name, entry)
    if not clone_present(definition):
        if not args.bundle:
            print("clone-missing")
            sys.exit(3)
        clone(definition, args.bundle, args.branch, args.origin)
    print(json.dumps({"name": args.name, "user": entry.pw_name, "home": str(HOMES / args.name)}))


def clone_present(definition):
    path = HOMES / definition["name"] / "repos" / definition["repo"]
    return helper(definition["name"], "is-dir", str(path.relative_to(HOMES / definition["name"])),
                  check=False, capture=True).returncode == 0


def login_argv(provider, api_key=False, console=False):
    if provider == "codex":
        if console:
            raise DiggerError("--console is a Claude option; Codex takes API billing with --api-key")
        return ["codex", "login", "--with-api-key" if api_key else "--device-auth"]
    if api_key:
        raise DiggerError("Claude takes API billing through its Console login: use --console")
    return ["claude", "auth", "login", "--console" if console else "--claudeai"]


def status_argv(provider):
    return ["codex", "login", "status"] if provider == "codex" else ["claude", "auth", "status"]


def logged_in(provider, result):
    """Read a status probe without ever printing its output: it can carry an account."""
    if result.returncode:
        return False
    if provider == "codex":
        return True
    try:
        return json.loads(result.stdout).get("loggedIn") is True
    except (ValueError, AttributeError):
        return False


def root_login(args):
    definition, _ = load(args.name)
    if definition["identity"] != "dedicated":
        raise DiggerError(f"{args.name} is not a dedicated digger")
    argv = login_argv(definition["provider"], args.api_key, args.console)
    key = sys.stdin.read(8192) if args.api_key else None
    sys.stdout.flush()
    result = as_digger(args.name, argv, check=False, input=key)
    sys.exit(result.returncode)


def root_status(args):
    definition, _ = load(args.name)
    result = as_digger(args.name, status_argv(definition["provider"]), check=False, capture=True)
    print(json.dumps({"loggedIn": logged_in(definition["provider"], result)}))


def root_check(args):
    """Doctor lines for one dedicated digger: identity, HOME, clone, policy, tool, login."""
    definition, instructions = load(args.name)
    name, provider = definition["name"], definition["provider"]
    lines = []
    try:
        entry = account(name)
    except DiggerError as error:
        print(json.dumps([["fail", f"digger {name}: {error}"]]))
        return
    home = HOMES / name
    info = os.lstat(home) if os.path.lexists(home) else None
    if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != entry.pw_uid or info.st_mode & 0o077:
        lines.append(["fail", f"digger {name}: HOME {home} must be a real directory owned by {entry.pw_name}, mode 0700"])
    else:
        lines.append(["ok", f"digger {name}: {entry.pw_name} (uid {entry.pw_uid}) owns a private HOME"])
        work = helper(name, "clone-status", definition["repo"], check=False, capture=True)
        try:
            state = json.loads(work.stdout)
        except ValueError:
            state = {"error": (work.stderr or "clone probe failed").strip()}
        if state.get("error"):
            lines.append(["fail", f"digger {name}: clone {state['error']}"])
        elif state.get("origin", "") != args.origin:
            lines.append(["fail", f"digger {name}: clone origin {state.get('origin') or '(none)'} does not match"
                                  f" the source repo's {args.origin or '(none)'}"])
        else:
            lines.append(["ok", f"digger {name}: private clone of {definition['repo']} with matching origin"])
    try:
        policy = POLICIES / name
        pinfo = os.lstat(policy)
        expected_uid = os.geteuid() if TEST else 0
        drift = [f for f, text in generated(definition, instructions).items()
                 if not (policy / f).is_file() or (policy / f).is_symlink() or (policy / f).read_text() != text
                 or os.lstat(policy / f).st_uid != expected_uid or os.lstat(policy / f).st_mode & 0o037]
        if not stat.S_ISDIR(pinfo.st_mode) or pinfo.st_uid != expected_uid or pinfo.st_mode & 0o027 or drift:
            raise DiggerError(", ".join(drift) or "directory ownership or mode")
        rules = codex_rules(definition)
        if rules is not None and helper(name, "read", ".codex/rules/digger.rules", check=False, capture=True).stdout != rules:
            raise DiggerError(".codex/rules/digger.rules")
        lines.append(["ok", f"digger {name}: root-owned compiled policy matches digger.json"])
    except (OSError, DiggerError) as error:
        lines.append(["fail", f"digger {name}: compiled policy drifted ({error}) — run: grave digger enable {name}"])
    tool = TOOLS / "bin" / provider
    probe = as_digger(name, ["sh", "-c", f'command -v {provider}'], check=False, capture=True)
    if probe.returncode or not probe.stdout.strip():
        lines.append(["fail", f"digger {name}: {provider} is not executable by {entry.pw_name} — run: grave digger enable {name}"])
    else:
        lines.append(["ok", f"digger {name}: {provider} executable by {entry.pw_name}"])
        try:
            stamp = json.loads((TOOLS / f"{provider}.source").read_text())
            current = os.stat(args.tool) if args.tool else None
            if tool.exists() and current and (stamp["size"], stamp["mtime_ns"]) != (current.st_size, current.st_mtime_ns):
                lines.append(["warn", f"digger {name}: {provider} copy is older than the owner's — run: grave digger enable {name}"])
        except (OSError, ValueError, KeyError):
            pass
        state = as_digger(name, status_argv(provider), check=False, capture=True)
        if not logged_in(provider, state):
            lines.append(["warn", f"digger {name}: {provider} not logged in — run: grave digger login {name}"])
        else:
            lines.append(["ok", f"digger {name}: {provider} logged in as {entry.pw_name}"])
    try:
        prepare_channel(name, entry, create=False)
        records = helper(name, "check-records", name, check=False, capture=True)
        lines += json.loads(records.stdout)
    except (DiggerError, ValueError, OSError) as error:
        lines.append(["fail", f"digger {name}: conversation records unreadable ({str(error)[:200]}) — run: grave digger enable {name}"])
    print(json.dumps(lines))


def root_remove(args):
    check_name(args.name)
    if not TEST:
        subprocess.run(["systemctl", "disable", "--now", f"gravedecay-digger@{args.name}.service"],
                       capture_output=True, check=False)
    shutil.rmtree(POLICIES / args.name, ignore_errors=True)
    if args.purge:
        if not TEST:
            try:
                account(args.name)
            except DiggerError:
                pass
            else:
                subprocess.run(["pkill", "-KILL", "-u", digger_user(args.name)], check=False)
                subprocess.run(["userdel", digger_user(args.name)], check=True)
        for path in (HOMES / args.name, INBOXES / args.name, RECORDS / args.name):
            if os.path.lexists(path):
                trusted_parent(path.parent)
                make_writable(path)  # pinned tools are read-only
                shutil.rmtree(path)
    print(json.dumps({"name": args.name, "purged": bool(args.purge)}))


def root_main(argv):
    if not TEST and os.geteuid() != 0:
        raise DiggerError("internal digger helper requires root")
    parser = argparse.ArgumentParser(prog="grave __digger")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("provision"); p.add_argument("name"); p.add_argument("--tool")
    p.add_argument("--bundle"); p.add_argument("--branch", default=""); p.add_argument("--origin", default="")
    p = sub.add_parser("login"); p.add_argument("name")
    p.add_argument("--api-key", action="store_true"); p.add_argument("--console", action="store_true")
    p = sub.add_parser("status"); p.add_argument("name")
    p = sub.add_parser("check"); p.add_argument("name"); p.add_argument("--origin", default=""); p.add_argument("--tool")
    p = sub.add_parser("remove"); p.add_argument("name"); p.add_argument("--purge", action="store_true")
    p = sub.add_parser("unit"); p.add_argument("name"); p.add_argument("action", choices=("start", "stop"))
    p = sub.add_parser("prune"); p.add_argument("name")
    args = parser.parse_args(argv)
    if args.op == "provision" and args.origin and (args.origin.startswith("-") or not printable(args.origin, 500)):
        raise DiggerError("unsafe origin URL")
    if args.op in ("provision",) and args.branch and not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", args.branch):
        raise DiggerError("unsafe branch name")
    {"provision": root_provision, "login": root_login, "status": root_status,
     "check": root_check, "remove": root_remove, "unit": root_unit, "prune": root_prune}[args.op](args)


# ------------------------------------------------------- as-digger helpers ----
def inside_home(relative, create=True):
    """A path under $HOME with no symlinked component; parents created 0700 when asked."""
    home = Path(os.environ["HOME"]).resolve()
    path = home / relative
    if ".." in Path(relative).parts or Path(relative).is_absolute():
        raise DiggerError("path must stay inside the digger HOME")
    current = home
    for part in Path(relative).parts[:-1]:
        current = current / part
        if not os.path.lexists(current):
            if not create:
                return path
            os.mkdir(current, 0o700)
        if os.path.islink(current) or not current.is_dir():
            raise DiggerError(f"{current} must be a real directory")
    if os.path.islink(path):
        raise DiggerError(f"{path} must not be a symlink")
    return path


def as_digger_main(argv):
    """Runs as the dedicated digger (root reached here through runuser)."""
    op, args = argv[0], argv[1:]
    if op == "write":
        write_file(inside_home(args[0]), sys.stdin.read(DEFINITION_LIMIT))
    elif op == "read":
        path = inside_home(args[0], create=False)
        if path.is_file():
            sys.stdout.write(path.read_text())
    elif op == "is-dir":
        path = inside_home(args[0], create=False)
        sys.exit(0 if path.is_dir() and not path.is_symlink() else 1)
    elif op == "clone":
        repo, branch, origin = args
        dest = inside_home(f"repos/{repo}")
        if os.path.lexists(dest):
            raise DiggerError(f"{dest} already exists")
        partial = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{repo}.partial-"))
        bundle = Path(f"{partial}.bundle")
        try:
            with open(bundle, "wb") as out:
                shutil.copyfileobj(sys.stdin.buffer, out, 1 << 20)
            git("init", "-q", partial)
            git("-C", partial, "fetch", "-q", bundle, "HEAD")
            git("-C", partial, "checkout", "-q", "-B", branch or "main", "FETCH_HEAD")
            if origin:
                git("-C", partial, "remote", "add", "origin", origin)
            os.rename(partial, dest)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
            if bundle.exists():
                bundle.unlink()
    elif op == "clone-status":
        dest = inside_home(f"repos/{args[0]}", create=False)
        info = os.lstat(dest) if os.path.lexists(dest) else None
        if info is None or not stat.S_ISDIR(info.st_mode):
            print(json.dumps({"error": f"{dest} is missing or not a real directory"}))
        elif info.st_uid != os.geteuid() or not (dest / ".git").is_dir() or (dest / ".git").is_symlink():
            print(json.dumps({"error": f"{dest} is not a Git checkout owned by the digger"}))
        else:
            result = subprocess.run(["git", "-C", str(dest), "config", "--get", "remote.origin.url"],
                                    capture_output=True, text=True)
            print(json.dumps({"origin": result.stdout.strip()}))
    elif op == "prune":
        print("\n".join(prune_lines(load_policy(args[0])[0])))
    elif op == "check-records":
        print(json.dumps(record_lines(load_policy(args[0])[0])))
    else:
        raise DiggerError("unknown helper: " + op)


# -------------------------------------------------------------- owner side ----
def root(*args, capture=False, input=None, check=True):
    """The dedicated tier's privileged half: in-process under test, else the
    root entry of the grave CLI the owner's scoped sudo grant already covers."""
    if TEST:
        command = [sys.executable, str(Path(__file__).resolve()), "__root", *args]
    else:
        command = ["sudo", "-n", GRAVE, "__digger", *args]
    result = subprocess.run(command, capture_output=capture, text=True, input=input)
    if check and result.returncode not in (0, 3):
        detail = (result.stderr or "").strip().splitlines()
        raise DiggerError((detail[-1] if detail else "root helper failed")
                          + ("" if TEST else " (needs the scoped `sudo -n grave` grant; run from the terminal tile or SSH)"))
    return result


def native(path):
    try:
        with open(path, "rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def owner_tool(provider):
    """The owner's native provider build, looking through launcher scripts:
    DIGGER_<PROVIDER>_BIN in grave.conf, then PATH, then what mise runs."""
    candidates = [os.environ.get(f"DIGGER_{provider.upper()}_BIN", ""), shutil.which(provider) or ""]
    if shutil.which("mise"):
        result = subprocess.run(["mise", "which", provider], capture_output=True, text=True, timeout=30)
        candidates.append(result.stdout.strip() if result.returncode == 0 else "")
    found = [os.path.realpath(path) for path in candidates if path]
    if not found:
        raise DiggerError(f"{provider} is not installed for the appliance owner")
    return next((path for path in found if TEST or native(path)), found[0])


def provision(definition):
    """Create or adopt a dedicated digger's identity, policy, tool copy and clone."""
    name = definition["name"]
    tool = owner_tool(definition["provider"])
    result = root("provision", name, "--tool", tool, capture=True)
    if result.returncode == 3:
        source = repo_path(definition["repo"])
        branch = subprocess.run(["git", "-C", str(source), "symbolic-ref", "--short", "-q", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        bundle = STORE / name / ".clone.bundle"
        try:
            git("-C", source, "bundle", "create", bundle, "HEAD")
            os.chmod(bundle, 0o600)
            result = root("provision", name, "--bundle", str(bundle), "--branch", branch,
                          "--origin", source_origin(definition["repo"]), capture=True)
        finally:
            if bundle.exists():
                bundle.unlink()
    if result.returncode:
        raise DiggerError("provisioning did not finish: " + (result.stdout + result.stderr).strip())


def sync(definition, instructions):
    if definition["identity"] == "dedicated":
        provision(definition)
    else:
        sync_owner_side(definition, instructions)


def cmd_new(args):
    check_name(args.name)
    repo = args.repo
    if not repo:
        here = Path.cwd().resolve()
        if here.parent == (ROOT / "repos").resolve():
            repo = here.name
    if not repo:
        raise DiggerError("--repo is required (or run from a primary checkout under GRAVE_ROOT/repos)")
    repo_path(repo)
    mcp = {}
    for item in args.mcp:
        server_id, _, command = item.partition("=")
        words = shlex.split(command)
        if not words:
            raise DiggerError("--mcp takes ID='command arg …'")
        mcp[server_id] = {"command": words[0], "args": words[1:]}
    allow = args.allow or ([f"mcp__{server_id}__*" for server_id in mcp] if mcp else ["Read", "Grep", "Glob"])
    definition = {"name": args.name, "provider": args.provider, "model": args.model, "identity": args.identity,
                  "repo": repo, "work": WORK.get(args.identity, ""),
                  "tools": {"mcp": mcp, "allow": allow, "deny": args.deny},
                  "budget": dict(DEFAULT_BUDGET, max_turns=args.max_turns, timeout_s=args.timeout),
                  "pause_in_gaming": True, "enabled": True}
    if args.check:
        definition["checks"] = args.check
    instructions = (Path(args.instructions).read_text() if args.instructions
                    else STARTER.format(name=args.name, repo=repo))
    if len(instructions.encode()) > INSTRUCTIONS_LIMIT or "\x00" in instructions:
        raise DiggerError(f"DIGGER.md must be text of at most {INSTRUCTIONS_LIMIT} bytes")
    validate(definition, args.name, instructions)
    uid = owner_uid()
    private_dir(STORE, uid, create=True)
    try:
        os.mkdir(STORE / args.name, 0o700)
    except FileExistsError:
        raise DiggerError(f"digger {args.name} already exists; names are never silently reused") from None
    write_file(STORE / args.name / "DIGGER.md", instructions)
    save(definition)
    try:
        sync(definition, instructions)
        unit(definition, True)
    except (DiggerError, OSError, subprocess.SubprocessError) as error:
        raise DiggerError(f"{error}; the definition is saved — fix the cause, then: grave digger enable {args.name}") from None
    print(json.dumps(show_record(definition)))
    print(f"next: grave digger login {args.name}", file=sys.stderr)


def show_record(definition):
    where = layout(definition)
    record = {"definition": definition, "instructions": where["instructions"], "policy": where["policy"]}
    if definition["identity"] == "dedicated":
        record.update(user=digger_user(definition["name"]), home=where["home"], clone=where["work"])
    elif where["provider_home"]:
        record["provider_home"] = where["provider_home"]
    return record


def cmd_list(_args):
    rows = []
    for name in names():
        try:
            definition, _ = load(name)
            rows.append(f"{name}\t{definition['provider']}\t{definition['identity']}\t{definition['repo']}\t"
                        + ("enabled" if definition["enabled"] else "disabled"))
        except DiggerError as error:
            rows.append(f"{name}\tinvalid\t{error}")
    print("\n".join(rows) if rows else "No diggers. Define one: grave digger new <name> --repo <repo>")


def cmd_show(args):
    definition, _ = load(args.name)
    print(json.dumps(show_record(definition), indent=2))


def status_record(definition):
    name, provider = definition["name"], definition["provider"]
    record = {"name": name, "provider": provider, "identity": definition["identity"], "enabled": definition["enabled"]}
    if definition["identity"] == "dedicated":
        record["user"] = digger_user(name)
        result = root("status", name, capture=True, check=False)
        try:
            record["loggedIn"] = json.loads(result.stdout)["loggedIn"] is True
        except (ValueError, KeyError, TypeError):
            record["loggedIn"] = False
            record["error"] = (result.stderr or "status probe failed").strip().splitlines()[-1:]
        return record
    home = layout(definition)["provider_home"]
    extra = {("CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"): home} if home else {}
    if not shutil.which(provider):
        return dict(record, loggedIn=False, error=f"{provider} is not installed")
    result = subprocess.run(status_argv(provider), capture_output=True, text=True, timeout=30, env=scrubbed(extra))
    return dict(record, loggedIn=logged_in(provider, result))


def cmd_status(args):
    targets = [args.name] if args.name else names()
    for name in targets:
        definition, _ = load(name)
        print(json.dumps(status_record(definition)))


def cmd_login(args):
    definition, instructions = load(args.name)
    provider, identity = definition["provider"], definition["identity"]
    if identity == "shared":
        raise DiggerError(f"{args.name} is a shared digger: it uses the owner's own {provider} login"
                          " (⚙️ settings → log in); there is nothing separate to log in")
    argv = login_argv(provider, args.api_key, args.console)
    key = None
    if args.api_key:
        key = getpass.getpass("API key (not echoed): ") if sys.stdin.isatty() else sys.stdin.read(8192)
        key = key.strip() + "\n"
    print(f"{args.name}: {provider} login as {digger_user(args.name) if identity == 'dedicated' else 'its own config dir'}"
          f" ({identity})", flush=True)
    if identity == "dedicated":
        provision(definition)  # the tool copy and identity must exist before a login
        flags = (["--api-key"] if args.api_key else []) + (["--console"] if args.console else [])
        sys.exit(root("login", args.name, *flags, input=key, check=False).returncode)
    sync_owner_side(definition, instructions)
    home = layout(definition)["provider_home"]
    env = scrubbed({("CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"): home})
    sys.exit(subprocess.run(argv, env=env, input=key, text=True).returncode)


def cmd_enable(args, enabled=True):
    definition, instructions = load(args.name)
    if enabled:
        sync(definition, instructions)
        for stale in channel(definition)[0].glob("*.json"):  # a kill from before must not stop the new loop
            try:
                if json.loads(stale.read_text()).get("op") == "kill":
                    stale.unlink()
            except (OSError, ValueError):
                pass
    definition["enabled"] = enabled
    save(definition)
    unit(definition, enabled)
    print(f"{args.name}: {'enabled (policy recompiled, unit restarted)' if enabled else 'disabled (unit stopped)'}")


def cmd_remove(args):
    check_name(args.name)
    uid = owner_uid()
    private_dir(STORE / args.name, uid)
    try:
        definition, _ = load(args.name)
        dedicated = definition["identity"] == "dedicated"
    except DiggerError:
        dedicated = True  # an unreadable definition may still own an identity; root checks
    root("unit", args.name, "stop", capture=True)
    if dedicated:
        root("remove", args.name, *(["--purge"] if args.purge else []), capture=True)
    if args.purge:
        shutil.rmtree(STORE / args.name)
        print(f"{args.name}: purged (definition and login"
              + (", Unix user, HOME and clone deleted)" if dedicated else " deleted)"))
        return
    # Names are never silently reused: the retained store keeps its login and records.
    retained = STORE / ".removed"
    private_dir(retained, uid, create=True)
    target = retained / f"{args.name}-{time.strftime('%Y%m%dT%H%M%S')}"
    os.rename(STORE / args.name, target)
    print(f"{args.name}: removed; definition and login kept at {target}"
          + ("; Unix user, HOME and clone kept (purge with --purge)" if dedicated else ""))


# ------------------------------------------------------------------ runner ----
DONE = "[[digger:done]]"   # the last line of the agent's final message ends the conversation
POLL = 1.0                 # seconds between inbox scans
QUEUE_LIMIT = 8            # asks waiting in one inbox
REQUEST_LIMIT = 16 * 1024
STATUS_EVERY = 15          # seconds between heartbeats in .status (never *.json: that glob is the records)
LOGIN_EVERY = 600          # seconds between provider login probes
PIN_LIMIT = 32 * 1024 * 1024
DIFF_LIMIT = 256 * 1024
OPS = ("ask", "cancel", "finish", "kill")
INBOXES = HOMES / ".inbox"
RECORDS = HOMES / ".records"
HOST = os.uname().nodename
# Conversation turn state -> the job runner's lifecycle status for valid_result.
LIFECYCLE = {"timed_out": "timed-out", "cancelled": "cancelled", "interrupted": "interrupted"}
PROTOCOL = """

## How this conversation ends

You work in an isolated checkout on branch {branch}. When the job is done, end your
final message with this line on its own:

{done}

The runner then commits what you changed, runs checks you did not choose and reports a
verdict. Without that line the requester can answer you. Never push, merge or deploy.
"""
STOP = threading.Event()
_dashboard = []


def channel(definition):
    """``(inbox, records)``: the owner writes requests, the digger writes records.
    A dedicated digger's pair sits outside both HOMEs, and each side reads the
    other's through a setgid group directory root creates; owner tiers keep both
    private in the store."""
    name = definition["name"]
    if definition["identity"] == "dedicated":
        return INBOXES / name, RECORDS / name
    return STORE / name / "inbox", STORE / name / "conversations"


def work_root(definition):
    """Pinned tools per conversation, and for a clone its checkouts, live under here."""
    return HOMES / definition["name"] if definition["identity"] == "dedicated" else STORE / definition["name"]


def prepare_channel(name, entry, create=True):
    """Root side: the inbox is the owner's with the digger's group, the records the
    digger's with the owner's group; both 2750, so new files inherit the group."""
    owner = pwd.getpwuid(owner_uid())
    for parent in (INBOXES, RECORDS):
        trusted_parent(parent, create=create)
    for path, uid, gid in ((INBOXES / name, owner.pw_uid, entry.pw_gid), (RECORDS / name, entry.pw_uid, owner.pw_gid)):
        if not os.path.lexists(path) and create:
            os.mkdir(path, 0o750)
            if not TEST:
                os.chown(path, uid, gid)
            os.chmod(path, 0o2750)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o7777 != 0o2750 \
                or (not TEST and (info.st_uid, info.st_gid) != (uid, gid)):
            raise DiggerError(f"unsafe digger channel directory: {path} (expected uid {uid}, gid {gid}, mode 2750)")


def new_id():
    return keeper.new_id()


def check_id(value, what="ID"):
    if not isinstance(value, str) or not keeper.ID_PATTERN.fullmatch(value):
        raise DiggerError(f"{what} must be <Unix seconds>-<32 lowercase hex digits>")
    return value


def read_request(path, uid):
    """One request the owner wrote; anything else in the inbox is refused."""
    text = read_private(path, uid, REQUEST_LIMIT, mode=0o640)
    value = json.loads(text)
    if (not isinstance(value, dict) or set(value) != {"id", "op", "conversation", "message", "requested_by", "at"}
            or value["id"] != path.stem or value["op"] not in OPS or type(value["at"]) not in (int, float)
            or not isinstance(value["message"], str) or len(value["message"]) > keeper.MAX_MESSAGE
            or not printable(value["requested_by"], 200)):
        raise DiggerError("malformed request")
    check_id(value["id"]); check_id(value["conversation"])
    return value


def reader(definition):
    """The Keeper runner's record reader over this digger's records, never its recovery."""
    runner = keeper.Runner(channel(definition)[1], HOST)
    runner.mode = 0o640 if definition["identity"] == "dedicated" else 0o600
    return runner


def read_records(definition):
    runner = reader(definition)
    rows = []
    for path in sorted(runner.path.glob("*.json"), reverse=True):
        if path.is_symlink():
            continue  # a dedicated digger writes this directory; the owner never follows its links
        try:
            record = runner._read(path)
        except OSError:
            continue  # doctor names it
        if record and record.get("digger") == definition["name"]:
            rows.append(record)
    return rows


def read_conversation(definition, conversation):
    path = channel(definition)[1] / (check_id(conversation, "conversation ID") + ".json")
    record = None if path.is_symlink() else reader(definition)._read(path)
    return record if record and record.get("digger") == definition["name"] else None


def loop_status(definition):
    try:
        value = json.loads((channel(definition)[1] / ".status").read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def live(status):
    return bool(status) and not status.get("stopped") and time.time() - status.get("heartbeat", 0) < 3 * STATUS_EVERY


def pending_asks(definition, records=None):
    """Asks in the inbox that no record has taken and the loop has not refused."""
    inbox = channel(definition)[0]
    taken = {r["id"] for record in (records if records is not None else read_records(definition))
             for r in record.get("requests", [])}
    refused = (loop_status(definition) or {}).get("rejected", {})
    rows = []
    for path in sorted(inbox.glob("*.json")) if inbox.is_dir() else ():
        if path.stem in taken or path.stem in refused:
            continue
        try:
            request = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(request, dict) and request.get("op") == "ask":
            rows.append(request)
    return rows


def submit(definition, op, conversation, message="", requested_by="owner", request_id=None):
    """Owner side: one request file in the digger's inbox. The same request ID twice
    is one request, so a client that lost the reply retries with its saved ID."""
    inbox = channel(definition)[0]
    request_id = check_id(request_id or new_id(), "request ID")
    check_id(conversation, "conversation ID")
    if op not in OPS:
        raise DiggerError("unknown request: " + str(op))
    if op == "ask" and (not isinstance(message, str) or not message.strip() or len(message) > keeper.MAX_MESSAGE):
        raise DiggerError(f"message must be 1 to {keeper.MAX_MESSAGE} characters")
    if not printable(requested_by, 200):
        raise DiggerError("requested_by must be printable text")
    if definition["identity"] != "dedicated":
        private_dir(inbox, owner_uid(), create=True)
    elif not inbox.is_dir():
        raise DiggerError(f"{inbox} is missing; run: grave digger enable {definition['name']}")
    path = inbox / (request_id + ".json")
    if path.exists():
        return request_id, False
    now = time.time()
    if not now - keeper.KEY_AGE <= int(request_id.split("-")[0]) <= now + 300:
        raise DiggerError("request ID is too old or in the future")
    for old in inbox.glob("*.json"):
        if int(old.name.split("-")[0]) < now - keeper.KEY_AGE:
            old.unlink(missing_ok=True)
    if op == "ask" and len(pending_asks(definition)) >= QUEUE_LIMIT:
        raise DiggerError(f"{definition['name']} already has {QUEUE_LIMIT} requests waiting; try again later")
    request = {"id": request_id, "op": op, "conversation": conversation, "message": message if op == "ask" else "",
               "requested_by": requested_by, "at": now}
    write_file(path, json.dumps(request) + "\n", 0o640 if definition["identity"] == "dedicated" else 0o600)
    return request_id, True


def relative_word(word):
    return bool(word) and not word.startswith(("-", "/", "~")) and ".." not in Path(word).parts


def pin_paths(definition, checkout, base):
    """Repository paths a conversation pins at its base: the directory of every file
    an MCP command or argument names, and the repo-side job knowledge."""
    words = {w for server in definition["tools"]["mcp"].values() for w in [server["command"], *server["args"]]
             if relative_word(w)}
    words.add(f".grave/diggers/{definition['name']}.md")
    listing = git("-C", checkout, "ls-tree", base, "--", *sorted(words)) if words else ""
    paths = set()
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        if meta.split()[1:2] == ["blob"]:
            parent = str(Path(path).parent)
            paths.add(path if parent == "." or path.startswith(".grave/diggers/") else parent)
    return sorted(paths)


def pinned_servers(definition, pin):
    """Every command word that names a pinned file runs from the pin, not the checkout."""
    def word(value):
        return str(Path(pin) / value) if relative_word(value) and (Path(pin) / value).is_file() else value
    return {sid: {"command": word(server["command"]), "args": [word(arg) for arg in server["args"]]}
            for sid, server in definition["tools"]["mcp"].items()}


def compose_instructions(definition, instructions, repo_text, branch):
    tail = PROTOCOL.format(branch=branch, done=DONE)
    head = instructions.rstrip() + "\n"
    if repo_text.strip():
        head += (f"\n## Job knowledge from the repository (.grave/diggers/{definition['name']}.md at the base commit)\n\n"
                 + repo_text.strip() + "\n")
    room = INSTRUCTIONS_LIMIT - len(tail.encode())
    if len(head.encode()) > room:
        head = head.encode()[:room - 64].decode("utf-8", "ignore") + "\n[job knowledge truncated at 32 KiB]\n"
    return head + tail


def tree_digest(path):
    hasher = hashlib.sha256()
    for directory, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = Path(directory, name)
            hasher.update(str(full.relative_to(path)).encode() + b"\0" + full.read_bytes() + b"\0")
    return hasher.hexdigest()


def read_only(path):
    """Every file and directory in the pin has had its write bits removed."""
    return all(not os.lstat(Path(directory, name)).st_mode & 0o222
               for directory, dirs, files in os.walk(path) for name in ["", *dirs, *files])


def make_writable(path):
    for directory, dirs, files in os.walk(path):
        os.chmod(directory, 0o700)
        for name in files:
            os.chmod(Path(directory, name), 0o600)


def dashboard_module():
    """The dashboard's transcript parser, for the result's cost and quota; loaded once."""
    if not _dashboard:
        _dashboard.append(sibling("gravedecay.py"))
    return _dashboard[0]


class DiggerRunner(keeper.Runner):
    """The Keeper's runner bound to one digger: its command, checkout and records."""

    def __init__(self, definition, instructions):
        inbox, records = channel(definition)
        super().__init__(records, HOST, timeout=definition["budget"]["timeout_s"])
        self.definition, self.instructions, self.inbox = definition, instructions, inbox
        self.mode = 0o640 if definition["identity"] == "dedicated" else 0o600
        self.started = time.time()
        self.seen, self.rejected, self.finishing = set(), {}, set()
        self.gaming_cancelled, self.requeue_pending = set(), set()
        self.paused, self.login, self.login_at, self.status_at = False, None, 0, 0

    # ---- checkout ----
    def prepare(self, record):
        """A checkout on ``digger/<name>/<conversation>`` and the pinned tools; returns a note."""
        d, cid = self.definition, record["id"]
        name, branch, note = d["name"], f"digger/{d['name']}/{cid}", ""
        if d["work"] == "worktree":
            session = f"digger-{name}-{cid}"
            directory, _history, lock, base = jobs.worktree({"repo": d["repo"], "name": name, "digger": name}, session, branch)
            os.close(lock)  # the conversation, not a scheduler lock, owns this checkout
            record["session"] = session
        else:
            clone = HOMES / name / "repos" / d["repo"]
            try:
                current = git("-C", clone, "symbolic-ref", "--short", "-q", "HEAD")
            except DiggerError:
                current = ""
            try:
                git("-C", clone, "fetch", "-q", "origin")
                base = git("-C", clone, "rev-parse", "--verify", f"refs/remotes/origin/{current or 'HEAD'}^{{commit}}")
            except DiggerError as error:
                base = git("-C", clone, "rev-parse", "HEAD")
                note = (f"The runner could not fetch origin ({str(error)[:200]}); this conversation starts from the"
                        f" clone's {current or 'HEAD'} at {base[:12]}.")
            parent = HOMES / name / "worktrees"
            parent.mkdir(mode=0o700, exist_ok=True)
            directory = parent / cid
            git("-C", clone, "worktree", "add", "-q", "-b", branch, directory, base)
        record.update(checkout=str(directory), base=base, branch=branch)
        record["pin"], record["pin_digest"] = self.pin(record)
        return note

    def pin(self, record):
        """Tools and job knowledge from the base commit into a read-only directory
        outside the checkout: the agent edits the checkout, never the tool it calls."""
        d, checkout, base = self.definition, Path(record["checkout"]), record["base"]
        (work_root(d) / "pins").mkdir(mode=0o700, exist_ok=True)
        pin = work_root(d) / "pins" / record["id"]
        pin.mkdir(mode=0o700)
        paths = pin_paths(d, checkout, base)
        if paths:
            archive = subprocess.run(["git", "-C", str(checkout), "archive", "--format=tar", base, "--", *paths],
                                     capture_output=True, timeout=120, env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))
            if archive.returncode or len(archive.stdout) > PIN_LIMIT:
                raise DiggerError("could not pin the digger's tools at the base commit (git archive failed or over 32 MiB)")
            with tarfile.open(fileobj=__import__("io").BytesIO(archive.stdout)) as tar:
                for member in tar.getmembers():
                    if not member.isfile() or member.name.startswith("/") or ".." in Path(member.name).parts:
                        continue  # git archive emits files, directories and symlinks; only files are pinned
                    target = pin / member.name
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
                    os.chmod(target, 0o555 if member.mode & 0o100 else 0o444)
        knowledge = pin / f".grave/diggers/{d['name']}.md"
        text = compose_instructions(d, self.instructions, knowledge.read_text(errors="replace") if knowledge.is_file() else "",
                                    record["branch"])
        (pin / ".instructions.md").write_text(text)
        if d["provider"] == "claude":
            (pin / ".mcp.json").write_text(table.digger_files(d, pinned_servers(d, pin))["claude-mcp.json"])
        for directory, dirs, files in os.walk(pin, topdown=False):
            for name in files:
                full = Path(directory, name)
                os.chmod(full, os.lstat(full).st_mode & 0o555)
            os.chmod(directory, 0o555)
        return str(pin), tree_digest(pin)

    # ---- one turn ----
    def launch(self, record):
        d, pin = self.definition, Path(record["pin"])
        text = (pin / ".instructions.md").read_text()
        where = dict(layout(d, text), checkout=record["checkout"], servers=pinned_servers(d, pin),
                     instructions=str(pin / ".instructions.md"), session=record["session_id"])
        if d["provider"] == "claude":
            where["mcp_config"] = str(pin / ".mcp.json")
        argv, extra = table.digger_command(d, "digger", True, where)
        if not table.policy_ok(argv):
            raise DiggerError("compiled command carries a permission-bypass flag")
        record["settling"] = True  # cleared once after_turn has decided whether this turn ends the conversation
        env = dict(keeper.scrub_env(os.environ), **extra, GIT_TERMINAL_PROMPT="0")
        return argv, record["checkout"], env

    def after_turn(self, record):
        cid, done = record["id"], False
        try:
            with self.mutex:
                if cid in self.gaming_cancelled:
                    self.gaming_cancelled.discard(cid)
                    record["message"] = jobs.GAMING_REASON
                    if record["turn"]["id"] not in record["requeues"]:  # once: a requeued turn cancelled again is final
                        record["requeue"] = {"id": new_id(), "message": last_request(record)}
                        record["message"] += "; requeued for after gaming mode"
                        self.requeue_pending.add(cid)
                done = record["state"] == "succeeded" and ends_with_marker(record)
                if done:
                    record["phase"] = "finishing"
                else:
                    record["settling"] = False
                self._save(record)
            if done:
                self.finish(cid, "done marker")
        except Exception as error:  # a packaging problem never wedges the loop
            print(f"{self.definition['name']}: after turn {cid}: {str(error)[:300]}", file=sys.stderr, flush=True)

    # ---- result ----
    def finish(self, cid, why):
        """The job runner's result package for the conversation's checkout."""
        self.finishing.add(cid)
        run = {"status": "succeeded"}
        try:
            with self.mutex:
                record = self._read(self._path(cid))
                record.update(phase="finishing", updated_at=time.time())
                self._save(record)
            try:
                result = self.package(record, run)
            except Exception as error:
                print(f"{self.definition['name']}: result for {cid}: {str(error)[:300]}", file=sys.stderr, flush=True)
                result = None
            if not jobs.valid_result(result, run["status"]) or len(json.dumps(result)) > jobs.RECORD_LIMIT:
                result = jobs.package(jobs.verdict_for(run))
            with self.mutex:
                record = self._read(self._path(cid))
                record.update(phase="finished", settling=False, result=result, result_status=run["status"],
                              finished_by=why, finished_at=time.time(), updated_at=time.time())
                record["budget"]["usd"] = (result.get("cost") or {}).get("usd")
                self._save(record)
        finally:
            self.finishing.discard(cid)
            self.cancelled.discard(cid)

    def package(self, record, run):
        d, checkout, base = self.definition, Path(record["checkout"]), record["base"]
        if git("-C", checkout, "status", "--porcelain"):
            git("-C", checkout, "add", "-A")
            git("-C", checkout, "-c", f"user.name=digger {d['name']}", "-c", f"user.email={digger_user(d['name'])}@{HOST}.invalid",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify",
                "-m", f"digger {d['name']}: {first_request(record)[:72]}")
        changes = jobs.change_summary(checkout, base)
        tests = jobs.test_changes(checkout, base) if changes["commits"] else None
        budget = jobs.TAIL_BUDGET - (len(json.dumps(tests)) if tests else 0)
        checks = []
        if jobs.verdict_for(run, changes) != "no-changes":
            commands, source = d.get("checks"), "owner"
            if commands is None:
                commands, source = jobs.detect_checks(checkout, base), "base:" + base
            log = self.path / (record["id"] + ".log")
            write_file(log, "", self.mode)
            started = time.time()
            def stop():
                return ("cancelled", "stopped by the owner") if record["id"] in self.cancelled or STOP.is_set() else None
            for cmd in commands:
                checks.append(jobs.run_check(cmd, source, checkout, base, log, stop, d["budget"]["timeout_s"], run, started,
                                             budget // len(commands)))
                if run["status"] != "succeeded":
                    break
        cost, quota = self.cost(checkout, record["created_at"])
        self.diff(record)
        return jobs.package(jobs.verdict_for(run, changes, checks), changes, checks, cost, tests, quota)

    def cost(self, checkout, since):
        """Spend and the plan quota from this digger's own transcripts, never the owner's."""
        home = layout(self.definition)["provider_home"]
        dirs = {"claude_dir": home} if self.definition["provider"] == "claude" else {"codex_dir": home}
        try:
            value = dashboard_module().transcript_cost(str(checkout), since, **dirs)
        except Exception as error:
            print("cost unavailable: " + str(error)[:200], file=sys.stderr, flush=True)
            return None, None
        if not value:
            return None, None
        return {key: value[key] for key in ("usd", "estimated", "model")}, value.get("quota")

    def diff(self, record):
        """base..HEAD beside the record, so the owner can read a dedicated digger's work."""
        command = ["git", "-C", record["checkout"], "-c", "core.pager=cat", "diff", "--no-color", "--no-ext-diff",
                   "--no-textconv", record["base"], "HEAD"]
        result = subprocess.run(command, capture_output=True, timeout=60, env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))
        text = result.stdout[:DIFF_LIMIT].decode("utf-8", "replace")
        if len(result.stdout) > DIFF_LIMIT:
            text += "\n[diff truncated at 256 KiB; open the checkout for the rest]\n"
        write_file(self.path / (record["id"] + ".diff"), text or "(no committed changes since the base commit)\n", self.mode)

    # ---- requests ----
    def reject(self, request_id, why):
        self.seen.add(request_id)
        self.rejected[request_id] = str(why)[:300]
        while len(self.rejected) > 32:
            self.rejected.pop(next(iter(self.rejected)))
        self.heartbeat(force=True)

    def busy(self):
        return any(lock.locked() for lock in self.locks.values())

    def running(self):
        return next((cid for cid, _ in self.processes.items()), None)

    def ask(self, request):
        d, cid, rid = self.definition, request["conversation"], request["id"]
        self.seen.add(rid)
        try:
            record = self._read(self._path(cid))
            if record is None:
                super().create(cid, "digger", d["provider"])
                with self.mutex:
                    record = self._read(self._path(cid))
                    record.update(model=d["model"], digger=d["name"], requested_by=request["requested_by"], requests=[],
                                  requeues=[], phase="open", settling=False, checkout=None, base=None, branch=None,
                                  session=None, pin=None, pin_digest=None, result=None, budget={"turns": 0, "usd": None})
                    self._save(record)
                try:
                    note = self.prepare(record)
                except (DiggerError, OSError, ValueError, subprocess.SubprocessError) as error:
                    with self.mutex:
                        record.update(state="failed", phase="finished", result_status="failed", updated_at=time.time(),
                                      message=f"Could not prepare a checkout: {str(error)[:400]}",
                                      result=jobs.package("provider-failed"))
                        self._save(record)
                    self.reject(rid, record["message"])
                    return
                with self.mutex:
                    if note:
                        self._append(record, {"type": "text", "name": "runner", "text": note})
                    self._save(record)
            if record.get("phase") != "open":
                self.reject(rid, "this conversation has finished; start a new one")
                return
            with self.mutex:
                record = self._read(self._path(cid))
                record["requests"].append({"id": rid, "at": request["at"]})
                record["budget"]["turns"] += 1
                self._save(record)
            self.send(cid, rid, request["message"], "digger")
        except keeper.KeeperError as error:
            self.reject(rid, str(error))

    def requeue(self):
        for cid in sorted(self.requeue_pending):
            self.requeue_pending.discard(cid)
            with self.mutex:
                record = self._read(self._path(cid))
                pending = record.pop("requeue", None) if record else None
                if not pending:
                    continue
                record["requeues"].append(pending["id"])
                record["requests"].append({"id": pending["id"], "at": time.time()})
                self._save(record)
            try:
                self.send(cid, pending["id"], pending["message"], "digger")
            except keeper.KeeperError as error:
                print(f"{self.definition['name']}: requeue {cid}: {error}", file=sys.stderr, flush=True)
            return

    def start_finish(self, request):
        cid = request["conversation"]
        self.seen.add(request["id"])
        record = self._read(self._path(cid))
        if not record or record.get("phase") != "open" or not record.get("checkout"):
            self.reject(request["id"], "nothing to finish: no open conversation with a checkout")
            return
        lock = self.locks.setdefault(cid, threading.Lock())
        if not lock.acquire(blocking=False):
            self.seen.discard(request["id"])  # a turn is still running; finish once it ends
            return
        def run():
            try:
                self.finish(cid, "owner")
            finally:
                lock.release()
        threading.Thread(target=run, daemon=True).start()

    def stop_conversation(self, cid):
        if cid in self.finishing:
            self.cancelled.add(cid)  # the check's stop predicate reads this
        else:
            try:
                self.cancel(cid, "digger")
            except keeper.KeeperError:
                pass

    def pauses(self):
        """Gaming mode pauses every digger except a dedicated one told not to."""
        return self.definition["pause_in_gaming"] or self.definition["identity"] != "dedicated"

    def tick(self):
        self.paused = self.pauses() and jobs.gaming()
        current = self.running()
        if self.paused and current and self.processes.get(current) and current not in self.gaming_cancelled:
            self.gaming_cancelled.add(current)
            self.cancel(current, "digger")
        owner = os.lstat(self.inbox).st_uid
        blocked = self.paused or self.busy()
        if not blocked and self.requeue_pending:
            self.requeue()
            blocked = True
        for path in sorted(self.inbox.glob("*.json")):
            if path.stem in self.seen:
                continue
            try:
                request = read_request(path, owner)
            except (OSError, ValueError, DiggerError) as error:
                self.reject(path.stem, error)
                continue
            if request["op"] == "kill":
                self.seen.add(request["id"])
                if request["at"] >= self.started:
                    self.kill()
                    return
            elif request["op"] == "cancel":
                self.seen.add(request["id"])
                self.stop_conversation(request["conversation"])
            elif request["op"] == "finish":
                if not self.busy():
                    self.start_finish(request)
            elif not blocked and not self.busy():
                self.ask(request)
                blocked = True
        self.heartbeat()

    def kill(self):
        STOP.set()
        for cid in list(self.processes) + list(self.finishing):
            self.stop_conversation(cid)

    def probe_login(self):
        d = self.definition
        home = layout(d)["provider_home"] if d["identity"] == "separate-login" else None
        extra = {("CODEX_HOME" if d["provider"] == "codex" else "CLAUDE_CONFIG_DIR"): home} if home else {}
        try:
            result = subprocess.run(status_argv(d["provider"]), capture_output=True, text=True, timeout=30, env=scrubbed(extra))
        except (OSError, subprocess.SubprocessError):
            return False
        return logged_in(d["provider"], result)

    def heartbeat(self, force=False, stopped=False):
        now = time.time()
        if not force and not stopped and now - self.status_at < STATUS_EVERY:
            return
        if now - self.login_at >= LOGIN_EVERY:
            self.login, self.login_at = self.probe_login(), now
        value = {"instance": self.instance, "pid": os.getpid(), "started": self.started, "heartbeat": now,
                 "paused": self.paused, "loggedIn": self.login, "rejected": self.rejected, "stopped": stopped}
        write_file(self.path / ".status", json.dumps(value) + "\n", self.mode)
        self.status_at = now

    def serve(self):
        """Reconcile, then loop until SIGTERM or a kill request."""
        for record in [self._recover(r) for r in read_records(self.definition)]:
            self.seen.update(r["id"] for r in record.get("requests", []))
            if record.get("requeue"):
                self.requeue_pending.add(record["id"])
            if record.get("phase") == "finishing" or record.get("settling"):
                interrupted = record.get("phase") == "finishing"
                record.update(settling=False, updated_at=time.time())
                if interrupted:
                    record.update(phase="finished", result_status="interrupted", result=jobs.package("interrupted"),
                                  finished_by="restart", finished_at=time.time(),
                                  message="The digger stopped while packaging this result; checks did not finish.")
                self._save(record)
        self.heartbeat(force=True)
        while not STOP.is_set():
            try:
                self.tick()
            except (OSError, ValueError, DiggerError, keeper.KeeperError, subprocess.SubprocessError) as error:
                print(f"{self.definition['name']}: {str(error)[:300]}", file=sys.stderr, flush=True)
            STOP.wait(POLL)
        for cid in list(self.processes) + list(self.finishing):
            self.stop_conversation(cid)
        for lock in list(self.locks.values()):
            if lock.acquire(timeout=60):
                lock.release()
        self.heartbeat(stopped=True)


def turn_events(record):
    """The events of the latest turn: everything after the last requester message."""
    events = record["events"]
    starts = [i for i, e in enumerate(events) if e["type"] == "text" and e["name"] == "owner"]
    return events[starts[-1] + 1:] if starts else events


def ends_with_marker(record):
    texts = [e["text"] for e in turn_events(record) if e["type"] == "text" and e["name"] != "runner" and e["text"].strip()]
    return bool(texts) and texts[-1].rstrip().splitlines()[-1].strip() == DONE


def last_request(record):
    return next((e["text"] for e in reversed(record["events"]) if e["type"] == "text" and e["name"] == "owner"), "")


def first_request(record):
    return next((e["text"] for e in record["events"] if e["type"] == "text" and e["name"] == "owner"), "work").splitlines()[0]


def load_policy(name):
    """A dedicated digger reads its definition from the root-owned policy copy."""
    check_name(name)
    uid = os.geteuid() if TEST else 0
    text = read_private(POLICIES / name / "digger.json", uid, DEFINITION_LIMIT, mode=0o640)
    instructions = read_private(POLICIES / name / "DIGGER.md", uid, INSTRUCTIONS_LIMIT, mode=0o640)
    try:
        definition = json.loads(text)
    except json.JSONDecodeError as error:
        raise DiggerError(f"digger.json does not parse: {error}") from None
    return validate(definition, name, instructions), instructions


def cmd_serve(args):
    """The digger unit's loop. A dedicated unit passes --identity dedicated."""
    if args.identity == "dedicated":
        definition, instructions = load_policy(args.name)
        if not TEST and os.geteuid() != pwd.getpwnam(digger_user(args.name)).pw_uid:
            raise DiggerError(f"a dedicated digger serves as {digger_user(args.name)} only")
    else:
        definition, instructions = load(args.name)
    if (definition["identity"] == "dedicated") != (args.identity == "dedicated"):
        raise DiggerError(f"{args.name} is a {definition['identity']} digger; its unit is the other template")
    if not definition["enabled"]:
        raise DiggerError(f"{args.name} is disabled")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    runner = DiggerRunner(definition, instructions)
    if not runner.inbox.is_dir() or not runner.path.is_dir():
        raise DiggerError(f"inbox or records directory missing; run: grave digger enable {args.name}")
    runner.serve()


# ------------------------------------------------------------- prune ----------
def prune_lines(definition):
    """The agent-worktree prune rules for finished conversations: a clean checkout
    whose branch is untouched, or merged and pushed, is removed; history stays."""
    lines, runner = [], reader(definition)
    source = repo_path(definition["repo"]) if definition["work"] == "worktree" \
        else HOMES / definition["name"] / "repos" / definition["repo"]
    for record in read_records(definition):
        cid, checkout = record["id"], record.get("checkout")
        if record.get("phase") != "finished" or record.get("pruned") or not checkout:
            continue
        say = lambda why: lines.append(f"{cid}: {why}")
        if not Path(checkout).is_dir():
            say("checkout already gone"); continue
        if git("-C", checkout, "status", "--porcelain", "--untracked-files=all", "--ignored"):
            say("local files or changes; retained"); continue
        head = git("-C", checkout, "rev-parse", "HEAD")
        if head != record["base"]:
            merged = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", head, "HEAD"]).returncode == 0 \
                or bool(git("-C", source, "branch", "-r", "--contains", head))
            if not merged:
                say("not merged into the source checkout; retained"); continue
            if git("-C", checkout, "rev-list", "HEAD", "--not", "--remotes"):
                say("unpushed commits; retained"); continue
        git("-C", source, "worktree", "remove", checkout)
        if record.get("pin") and Path(record["pin"]).is_dir():
            make_writable(record["pin"])
            shutil.rmtree(record["pin"])
        record["pruned"] = True
        runner._save(record)
        if record.get("session"):
            meta = ROOT / "agents" / record["session"] / "meta.json"
            value = json.loads(meta.read_text())
            jobs.write_json(meta, dict(value, pruned=True))
        say("checkout removed; branch and record retained")
    return lines or ["nothing to prune"]


def cmd_prune(args):
    definition, _ = load(args.name)
    if definition["identity"] == "dedicated":
        result = root("prune", args.name, capture=True)
        print(result.stdout, end="")
    else:
        print("\n".join(prune_lines(definition)))


# --------------------------------------------------------- owner side, CLI ----
def unit_name(definition):
    return f"gravedecay-digger{'' if definition['identity'] == 'dedicated' else '-owner'}@{definition['name']}.service"


def unit(definition, start):
    """Start (restarting a running one, so it reads the new policy) or stop the
    digger's unit, through the root half."""
    root("unit", definition["name"], "start" if start else "stop", capture=True)


def root_unit(args):
    check_name(args.name)
    units = [f"gravedecay-digger@{args.name}.service", f"gravedecay-digger-owner@{args.name}.service"]
    if args.action == "stop":
        if not TEST:
            subprocess.run(["systemctl", "disable", "--now", *units], capture_output=True, check=False)
        return
    definition, _ = load(args.name)
    wanted = unit_name(definition)
    if TEST:
        return
    subprocess.run(["systemctl", "disable", "--now", *[u for u in units if u != wanted]], capture_output=True, check=False)
    subprocess.run(["systemctl", "enable", wanted], capture_output=True, check=True)
    subprocess.run(["systemctl", "restart", wanted], check=True)


def root_prune(args):
    definition, _ = load(args.name)
    sys.stdout.write(helper(args.name, "prune", args.name, capture=True).stdout)


def print_event(event, out=sys.stdout):
    if event["type"] == "text" and event["name"] not in ("owner",):
        print(event["text"], file=out if event["name"] != "runner" else sys.stderr, flush=True)
    elif event["type"] == "tool_call":
        print(f"→ {event['name']} {event['text'][:200]}", file=sys.stderr, flush=True)
    elif event["type"] == "tool_result":
        print(f"← {event['text'].splitlines()[0][:200] if event['text'] else ''}", file=sys.stderr, flush=True)
    elif event["type"] == "error":
        print("error: " + event["text"], file=sys.stderr, flush=True)


def print_result(record):
    result, changes = record["result"], record["result"].get("changes") or {}
    print(f"verdict: {result['verdict']}  ({record.get('finished_by', 'runner')})", file=sys.stderr)
    if changes:
        print(f"branch {record['branch']}: {changes['commits']} commit(s), {changes['files']} file(s),"
              f" +{changes['insertions']} −{changes['deletions']}", file=sys.stderr)
    for check in result["checks"]:
        print(f"check `{check['cmd']}` ({check.get('source', 'owner')}): exit {check['exit_code']}", file=sys.stderr)
    if result.get("cost"):
        print(f"cost ≈ ${result['cost']['usd']:.2f} ({result['cost']['model']})", file=sys.stderr)
    for window in (result.get("quota") or {}).get("windows", []):
        print(f"codex plan quota: {window['used_percent']}% of the {window['window_minutes']}-minute window",
              file=sys.stderr)


def follow(definition, conversation, request_id, until_finished=False, cursor=0):
    """Print a conversation's events after ``cursor`` as they land until the request's
    turn has settled (or, with ``until_finished``, the result is in). Returns the exit status."""
    said = False
    while True:
        record = read_conversation(definition, conversation)
        status = loop_status(definition)
        refused = (status or {}).get("rejected", {}).get(request_id)
        if refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 1
        taken = bool(record) and (until_finished or any(r["id"] == request_id for r in record.get("requests", [])))
        for event in record["events"] if record else ():
            if event["seq"] > cursor:
                cursor = event["seq"]
                print_event(event)
        if taken:
            finished = record.get("phase") == "finished"
            settled = record["state"] != "running" and record.get("phase") != "finishing" and not record.get("settling")
            if settled and (finished or not until_finished and (record["turn"] or {}).get("id") == request_id):
                if finished:
                    print_result(record)
                elif record["state"] == "succeeded":
                    print(f"waiting for you: grave digger resume {definition['name']} {conversation} \"<reply>\""
                          f" (or: grave digger finish {definition['name']} {conversation})", file=sys.stderr)
                if record["state"] not in ("succeeded", "idle"):
                    print(f"{record['state']}: {record['message']}", file=sys.stderr)
                return 0 if record["state"] in ("succeeded", "idle") else 1
        elif not said:
            said = True
            if not live(status):
                print(f"queued, but {definition['name']} is not running; it starts on: grave digger enable"
                      f" {definition['name']}", file=sys.stderr)
                return 3
            print("queued", file=sys.stderr)
        time.sleep(1)


def cmd_ask(args):
    definition, _ = load(args.name)
    if not definition["enabled"]:
        raise DiggerError(f"{args.name} is disabled; run: grave digger enable {args.name}")
    conversation, cursor = getattr(args, "conversation", None) or new_id(), 0
    if getattr(args, "conversation", None):
        record = read_conversation(definition, conversation)
        if record is None:
            raise DiggerError(f"no conversation {conversation} for {args.name}")
        if record.get("phase") != "open":
            raise DiggerError("that conversation has finished; start a new one with: grave digger ask")
        cursor = record["cursor"]
    request_id, _ = submit(definition, "ask", conversation, args.prompt)
    print(f"conversation {conversation} ({definition['provider']}, {definition['identity']})", file=sys.stderr, flush=True)
    sys.exit(follow(definition, conversation, request_id, cursor=cursor))


def cmd_cancel(args):
    definition, _ = load(args.name)
    submit(definition, "cancel", check_id(args.conversation, "conversation ID"))
    print(f"{args.name}: cancel requested for {args.conversation}; the turn stops within a second or two")


def cmd_finish(args):
    definition, _ = load(args.name)
    record = read_conversation(definition, args.conversation)
    if record is None or record.get("phase") != "open":
        raise DiggerError("no open conversation " + args.conversation)
    request_id, _ = submit(definition, "finish", args.conversation)
    sys.exit(follow(definition, args.conversation, request_id, until_finished=True))


def cmd_kill(args):
    """Stop everything now: running turn, queued asks, the unit; the digger is disabled."""
    definition, _ = load(args.name)
    request_id, _ = submit(definition, "kill", new_id())
    definition["enabled"] = False
    save(definition)
    for _ in range(10):
        if not live(loop_status(definition)):
            break
        time.sleep(.5)
    try:
        unit(definition, False)
    finally:
        (channel(definition)[0] / (request_id + ".json")).unlink(missing_ok=True)
    print(f"{args.name}: killed and disabled; queued requests were dropped. Records and checkouts are kept"
          f" (grave digger enable {args.name} brings it back)")


# --------------------------------------------------------- owner side, API ----
def summary(definition):
    """One row for the dashboard's Diggers section; reads files only."""
    records, status = read_records(definition), loop_status(definition)
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    finished = [r for r in records if r.get("phase") == "finished" and r.get("result")]
    return {"name": definition["name"], "provider": definition["provider"], "model": definition["model"],
            "identity": definition["identity"], "repo": definition["repo"], "enabled": definition["enabled"],
            "live": live(status), "paused": bool((status or {}).get("paused")), "loggedIn": (status or {}).get("loggedIn"),
            "queue": len(pending_asks(definition, records)),
            "running": next((r["id"] for r in records if r["state"] == "running"), None),
            "today": {"turns": sum(1 for r in records for q in r.get("requests", []) if q["at"] >= today),
                      "usd": round(sum((r["result"].get("cost") or {}).get("usd") or 0 for r in finished
                                       if r.get("finished_at", 0) >= today), 4)},
            "last_verdict": max(finished, key=lambda r: r.get("finished_at", 0))["result"]["verdict"] if finished else None,
            "prompts": definition.get("prompts", [])}


def conversation_rows(definition):
    return [{"id": r["id"], "state": r["state"], "phase": r.get("phase"), "summary": r["summary"],
             "requested_by": r.get("requested_by"), "created_at": r["created_at"], "updated_at": r["updated_at"],
             "verdict": (r.get("result") or {}).get("verdict")} for r in read_records(definition)]


def conversation_view(definition, conversation, after=0):
    """A record's public form after a cursor; a conversation still in the inbox reads as queued."""
    record = read_conversation(definition, conversation)
    waiting = [q for q in pending_asks(definition) if q["conversation"] == conversation]
    if record is None:
        if not waiting:
            refused = refusals(definition, conversation)
            return {"id": conversation, "digger": definition["name"], "state": "failed", "phase": "finished",
                    "events": [], "queued": 0, "turn": None, "message": refused[-1]["reason"], "result": None,
                    "refused": refused} if refused else None
        return {"id": conversation, "digger": definition["name"], "state": "queued", "phase": "open", "events": [],
                "queued": len(waiting), "turn": None, "message": "", "result": None, "live": live(loop_status(definition)),
                "refused": []}
    view = keeper.Runner._public(record, after)
    view["queued"] = len(waiting)
    view["refused"] = refusals(definition, conversation)
    return view


def refusals(definition, conversation):
    """The loop's reasons for refusing requests about this conversation (a finish of
    a conversation that never got a checkout, an expired ID), newest last."""
    refused, rows = (loop_status(definition) or {}).get("rejected", {}), []
    for request_id, reason in refused.items():
        try:
            request = json.loads((channel(definition)[0] / (request_id + ".json")).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(request, dict) and request.get("conversation") == conversation:
            rows.append({"request": request_id, "op": request.get("op"), "reason": reason})
    return rows


def diff_text(definition, conversation):
    path = channel(definition)[1] / (check_id(conversation, "conversation ID") + ".diff")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DiggerError("no diff")
        return stream.read(DIFF_LIMIT + 4096).decode("utf-8", "replace")


def notify_finished(seen):
    """Owner side, from the job scheduler's loop: one agent-done page per finished
    conversation, for every tier (a dedicated digger cannot read the channels)."""
    for name in names():
        try:
            definition, _ = load(name)
            records_dir = channel(definition)[1]
            mtime = records_dir.stat().st_mtime_ns
        except (DiggerError, OSError):
            continue
        if seen.get(name) == mtime:
            continue
        seen[name] = mtime
        ledger_path = STORE / name / ".notified.json"
        try:
            ledger, first = json.loads(read_private(ledger_path, owner_uid(), 65536)), False
        except (DiggerError, ValueError):
            ledger, first = [], True  # the first sweep records what is already finished without paging
        fresh = [r for r in read_records(definition) if r.get("phase") == "finished" and r["id"] not in ledger]
        if not fresh:
            if first:
                write_file(ledger_path, "[]\n")
            continue
        ledger = (ledger + [r["id"] for r in fresh])[-256:]
        write_file(ledger_path, json.dumps(ledger) + "\n")
        for record in [] if first else fresh:
            verdict = record["result"]["verdict"]
            body = (record["summary"] or first_request(record))[:200] + " — open Diggers on the Work tab to review."
            try:
                subprocess.run([GRAVE, "notify", "--event", "agent-done", "--link",
                                f"/grave/?tab=work&digger={name}&conversation={record['id']}",
                                f"Digger {name}: {verdict}", body], timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass  # the record stays; a page is best effort


# ------------------------------------------------------------------ doctor ----
def owner_home_closed():
    home = Path(pwd.getpwuid(owner_uid()).pw_dir)
    return not os.stat(home).st_mode & 0o007


def unit_lines(definition):
    """An enabled digger's unit is enabled and running, a disabled one's is not, and
    neither waits on T3 (a digger answers with T3 stopped, like scheduled jobs)."""
    if TEST:
        return []
    name, wanted = definition["name"], unit_name(definition)
    systemctl = lambda *a: subprocess.run(["systemctl", *a], capture_output=True, text=True, timeout=30)
    other = [u for u in (f"gravedecay-digger@{name}.service", f"gravedecay-digger-owner@{name}.service") if u != wanted]
    if any(systemctl("is-active", "--quiet", u).returncode == 0 for u in other):
        return [["fail", f"digger {name}: the {definition['identity']} digger runs from the wrong unit template"
                         f" — run: grave digger enable {name}"]]
    enabled = systemctl("is-enabled", "--quiet", wanted).returncode == 0
    active = systemctl("is-active", "--quiet", wanted).returncode == 0
    if not definition["enabled"]:
        return [["fail", f"digger {name}: disabled but {wanted} is running — run: grave digger disable {name}"]] if active \
            else [["ok", f"digger {name}: disabled; its unit is stopped"]]
    if not (enabled and active):
        return [["fail", f"digger {name}: {wanted} is not enabled and running — run: grave digger enable {name}"]]
    shown = systemctl("show", wanted, "-p", "After", "-p", "Requires", "-p", "Wants", "-p", "BindsTo",
                      "-p", "Requisite", "-p", "PartOf").stdout
    if "t3code.service" in shown:
        return [["fail", f"digger {name}: {wanted} orders after or depends on t3code.service; re-run raise.sh"]]
    return [["ok", f"digger {name}: {wanted} enabled and running, independent of T3"]]


def record_lines(definition):
    """Every conversation record parses; an unfinished one belongs to the live loop;
    a finished one carries a valid result whose checks came from the owner or the
    base; a retained checkout is a worktree of the recorded repo or clone; its
    pinned tools are read-only and unchanged since the base was pinned."""
    name, problems = definition["name"], []
    runner, status = reader(definition), loop_status(definition)
    source = repo_path(definition["repo"]) if definition["work"] == "worktree" \
        else HOMES / name / "repos" / definition["repo"]
    paths = sorted(runner.path.glob("*.json")) if runner.path.is_dir() else []
    for path in paths:
        try:
            record = runner._read(path)
            cid = record["id"]
            if record.get("digger") != name:
                raise DiggerError("names another digger")
            if record["state"] == "running" or record.get("phase") == "finishing" or record.get("settling"):
                if not (live(status) and status.get("instance") == record["instance"]):
                    raise DiggerError(f"unfinished, but no live {unit_name(definition)} owns it — run: grave digger enable {name}")
            if record.get("phase") == "finished":
                result = record.get("result")
                if not jobs.valid_result(result, record.get("result_status", "succeeded")) \
                        or len(json.dumps(result)) > jobs.RECORD_LIMIT:
                    raise DiggerError("finished without a valid result and verdict")
                for check in result["checks"]:
                    if check.get("source", "owner") not in ("owner", "base:" + str(record.get("base"))):
                        raise DiggerError("ran a check from " + check["source"] + ", not the owner or its base commit")
            checkout = record.get("checkout")
            if checkout and not record.get("pruned") and Path(checkout).exists():
                expected = (ROOT / "worktrees" / definition["repo"] / str(record.get("session"))) \
                    if definition["work"] == "worktree" else HOMES / name / "worktrees" / cid
                if Path(checkout) != expected or Path(checkout).is_symlink() or not Path(git(
                        "-C", checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")).samefile(source / ".git"):
                    raise DiggerError(f"checkout {checkout} is not a worktree of {source}")
                pin = record.get("pin")
                if not pin or Path(pin) != work_root(definition) / "pins" / cid or not Path(pin).is_dir() \
                        or not read_only(pin) or tree_digest(pin) != record.get("pin_digest"):
                    raise DiggerError("pinned tools changed or became writable since the base was pinned")
        except (OSError, DiggerError, KeyError, TypeError) as error:
            problems.append(f"{path.stem}: {str(error)[:200]}")
    if problems:
        return [["fail", f"digger {name}: conversation {problem}"] for problem in problems[:5]]
    return [["ok", f"digger {name}: {len(paths)} conversation record(s) valid; checkouts and pinned tools intact"]]


def check_lines():
    """One ``[level, message]`` per finding; levels ok, warn, fail."""
    lines = []
    if not STORE.exists():
        return [["ok", "no diggers defined"]]
    try:
        all_names = names()
    except DiggerError as error:
        return [["fail", f"digger store: {error}"]]
    dedicated = False
    for name in all_names:
        try:
            definition, instructions = load(name)
            repo_path(definition["repo"])
        except DiggerError as error:
            lines.append(["fail", f"digger {name}: {error}"])
            continue
        provider, identity = definition["provider"], definition["identity"]
        argv, _ = compile_command(definition, instructions)
        if not table.policy_ok(argv):
            lines.append(["fail", f"digger {name}: compiled command carries a bypass flag"])
            continue
        lines.append(["ok", f"digger {name}: {provider}/{identity} definition valid, compiled argv refuses bypass flags"])
        if identity == "dedicated":
            dedicated = True
            try:
                tool = ["--tool", owner_tool(provider)]
            except DiggerError:
                tool = []
            result = root("check", name, "--origin", source_origin(definition["repo"]), *tool, capture=True, check=False)
            try:
                lines += json.loads(result.stdout)
            except ValueError:
                detail = (result.stderr or "").strip().splitlines()
                lines.append(["fail", f"digger {name}: identity probe failed: {detail[-1] if detail else 'no output'}"])
            lines += unit_lines(definition)
            continue
        base = STORE / name
        try:
            uid = owner_uid()
            files = generated(definition, instructions)
            drift = [f for f, text in files.items() if read_private(base / "policy" / f, uid, DEFINITION_LIMIT) != text]
            home = layout(definition)["provider_home"]
            if home:
                private_dir(home, uid)
                rules = codex_rules(definition)
                if rules is not None and read_private(Path(home) / "rules/digger.rules", uid, DEFINITION_LIMIT) != rules:
                    drift.append("rules/digger.rules")
            if drift:
                raise DiggerError(", ".join(drift))
        except DiggerError as error:
            lines.append(["fail", f"digger {name}: compiled policy drifted ({error}) — run: grave digger enable {name}"])
            continue
        if not shutil.which(provider):
            lines.append(["fail", f"digger {name}: {provider} is not installed"])
            continue
        record = status_record(definition)
        if record["loggedIn"]:
            lines.append(["ok", f"digger {name}: {provider} logged in" + (" (own config dir)" if home else " (owner login)")])
        elif identity == "shared":
            lines.append(["warn", f"digger {name}: {provider} not logged in — log in as the appliance owner"])
        else:
            lines.append(["warn", f"digger {name}: {provider} not logged in — run: grave digger login {name}"])
        lines += unit_lines(definition) + record_lines(definition)
    if dedicated:
        secrets = ROOT / "config/secrets"
        if secrets.exists() and os.lstat(secrets).st_mode & 0o077:
            lines.append(["fail", "config/secrets must be 0700: digger identities must never read the owner's secrets"])
        if owner_home_closed():
            lines.append(["ok", "owner HOME closed to digger identities"])
        else:
            lines.append(["fail", "owner HOME is reachable by other users (chmod 700 ~): digger identities must never"
                                  " reach the owner's ~/.config/gh or provider logins"])
    return lines


def cmd_check(_args):
    try:
        lines = check_lines()
    except (DiggerError, OSError, subprocess.SubprocessError) as error:
        lines = [["fail", f"digger probe: {error}"]]
    for level, message in lines:
        print(f"{level}\t{message}")


# -------------------------------------------------------------------- main ----
USAGE = ("usage: grave digger new <name> --repo <repo> [--provider codex|claude] [--identity shared|separate-login|dedicated]"
         " [--check CMD] | list | show <name> | login <name> [--api-key|--console] | status [<name>] | enable <name>"
         " | disable <name> | ask <name> \"<request>\" | resume <name> <conversation> \"<reply>\""
         " | cancel <name> <conversation> | finish <name> <conversation> | kill <name> | prune <name>"
         " | remove <name> [--purge]")


def main(argv):
    if argv[:1] == ["__root"]:
        return root_main(argv[1:])
    if argv[:1] == ["__as-digger"]:
        return as_digger_main(argv[1:])
    parser = argparse.ArgumentParser(prog="grave digger", usage=USAGE)
    sub = parser.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("new")
    p.add_argument("name"); p.add_argument("--repo"); p.add_argument("--provider", choices=table.PROVIDERS, default="codex")
    p.add_argument("--identity", choices=IDENTITIES, default="dedicated"); p.add_argument("--model", default="")
    p.add_argument("--allow", action="append", default=[], metavar="RULE")
    p.add_argument("--deny", action="append", default=[], metavar="RULE")
    p.add_argument("--mcp", action="append", default=[], metavar="ID='COMMAND ARGS'")
    p.add_argument("--instructions", metavar="FILE")
    p.add_argument("--max-turns", type=int, default=DEFAULT_BUDGET["max_turns"])
    p.add_argument("--timeout", type=int, default=DEFAULT_BUDGET["timeout_s"])
    p.add_argument("--check", action="append", default=[], metavar="CMD",
                   help="verification command run in the checkout when a conversation finishes; repeatable."
                        " Without it the base commit's package.json test script, pytest or make test.")
    sub.add_parser("list")
    for verb in ("show", "enable", "disable", "kill", "prune"):
        sub.add_parser(verb).add_argument("name")
    p = sub.add_parser("serve"); p.add_argument("name"); p.add_argument("--identity", choices=("dedicated",))
    p = sub.add_parser("ask"); p.add_argument("name"); p.add_argument("prompt")
    p = sub.add_parser("resume"); p.add_argument("name"); p.add_argument("conversation"); p.add_argument("prompt")
    for verb in ("cancel", "finish"):
        p = sub.add_parser(verb); p.add_argument("name"); p.add_argument("conversation")
    p = sub.add_parser("status"); p.add_argument("name", nargs="?")
    p = sub.add_parser("login"); p.add_argument("name")
    p.add_argument("--api-key", action="store_true", help="Codex: read an API key (not echoed) instead of a device code")
    p.add_argument("--console", action="store_true", help="Claude: Anthropic Console (API billing) instead of a subscription")
    p = sub.add_parser("remove"); p.add_argument("name"); p.add_argument("--purge", action="store_true")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    {"new": cmd_new, "list": cmd_list, "show": cmd_show, "status": cmd_status, "login": cmd_login,
     "enable": cmd_enable, "disable": lambda a: cmd_enable(a, False), "remove": cmd_remove,
     "check": cmd_check, "serve": cmd_serve, "ask": cmd_ask, "resume": cmd_ask, "cancel": cmd_cancel,
     "finish": cmd_finish, "kill": cmd_kill, "prune": cmd_prune}[args.verb](args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except DiggerError as error:
        sys.exit(f"grave digger: {error}")
    except subprocess.CalledProcessError as error:
        sys.exit(f"grave digger: {Path(str(error.cmd[0])).name} failed (exit {error.returncode})")
