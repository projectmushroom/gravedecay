#!/usr/bin/env python3
"""Diggers: focused agents bound to one job, one repository and the channels you bind them to.

This milestone defines them: a validated definition, an identity tier, the
digger's own provider login, a deny-by-default policy compiled through the one
provider table, and the doctor probe. Nothing runs a turn yet (docs/DIGGERS.md).

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
import stat
import subprocess
import sys
import tempfile
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


def providers():
    """The shared provider command table: beside this file when installed, libexec in the repository."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "providers.py", here.parent / "libexec" / "providers.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("grave_providers", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise DiggerError("providers.py is not installed beside diggers.py; re-run raise.sh")


table = providers()


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
    unknown, missing = set(value) - KEYS, KEYS - set(value)
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
                            env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))
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
           "DISABLE_AUTOUPDATER": "1", **(extra or {})}
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
        if os.path.lexists(HOMES / args.name):
            trusted_parent(HOMES)
            shutil.rmtree(HOMES / args.name)
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
    args = parser.parse_args(argv)
    if args.op == "provision" and args.origin and (args.origin.startswith("-") or not printable(args.origin, 500)):
        raise DiggerError("unsafe origin URL")
    if args.op in ("provision",) and args.branch and not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", args.branch):
        raise DiggerError("unsafe branch name")
    {"provision": root_provision, "login": root_login, "status": root_status,
     "check": root_check, "remove": root_remove}[args.op](args)


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
    definition["enabled"] = enabled
    save(definition)
    print(f"{args.name}: {'enabled (policy recompiled)' if enabled else 'disabled'}")


def cmd_remove(args):
    check_name(args.name)
    uid = owner_uid()
    private_dir(STORE / args.name, uid)
    try:
        definition, _ = load(args.name)
        dedicated = definition["identity"] == "dedicated"
    except DiggerError:
        dedicated = True  # an unreadable definition may still own an identity; root checks
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


# ------------------------------------------------------------------ doctor ----
def owner_home_closed():
    home = Path(pwd.getpwuid(owner_uid()).pw_dir)
    return not os.stat(home).st_mode & 0o007


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


def cmd_run(args):
    check_name(args.name)
    sys.exit(f"{args.name}: the digger runner arrives with M2 (#246); nothing runs in this milestone")


# -------------------------------------------------------------------- main ----
USAGE = ("usage: grave digger new <name> --repo <repo> [--provider codex|claude] [--identity shared|separate-login|dedicated]"
         " | list | show <name> | login <name> [--api-key|--console] | status [<name>] | enable <name> | disable <name>"
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
    sub.add_parser("list")
    for verb in ("show", "enable", "disable", "run"):
        sub.add_parser(verb).add_argument("name")
    p = sub.add_parser("status"); p.add_argument("name", nargs="?")
    p = sub.add_parser("login"); p.add_argument("name")
    p.add_argument("--api-key", action="store_true", help="Codex: read an API key (not echoed) instead of a device code")
    p.add_argument("--console", action="store_true", help="Claude: Anthropic Console (API billing) instead of a subscription")
    p = sub.add_parser("remove"); p.add_argument("name"); p.add_argument("--purge", action="store_true")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    {"new": cmd_new, "list": cmd_list, "show": cmd_show, "status": cmd_status, "login": cmd_login,
     "enable": cmd_enable, "disable": lambda a: cmd_enable(a, False), "remove": cmd_remove,
     "check": cmd_check, "run": cmd_run}[args.verb](args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except DiggerError as error:
        sys.exit(f"grave digger: {error}")
    except subprocess.CalledProcessError as error:
        sys.exit(f"grave digger: {Path(str(error.cmd[0])).name} failed (exit {error.returncode})")
