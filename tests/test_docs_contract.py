import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MULTIUSER = (ROOT / "docs/MULTIUSER.md").read_text()
README = (ROOT / "README.md").read_text()
CONTRIBUTING = (ROOT / "CONTRIBUTING.md").read_text()
GRAVE = (ROOT / "bin/grave").read_text()
WORKSPACES = (ROOT / "bin/grave-workspaces").read_text()
SCHEDULES = (ROOT / "docs/SCHEDULES.md").read_text()


def fenced_commands(text, prefix):
    """Lines inside ``` blocks that start with `prefix`, continuation lines joined."""
    commands, block, pending = [], False, ""
    for line in text.splitlines():
        if line.startswith("```"):
            block = not block
            continue
        if not block:
            continue
        line = line.strip()
        if pending:
            pending += " " + line
        elif line.startswith(prefix):
            pending = line
        else:
            continue
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        commands.append(pending.split("#")[0].strip()); pending = ""
    return commands


class DocsContractTests(unittest.TestCase):
    def test_multiuser_ports_example_matches_the_validator(self):
        # Regression #63: the example record omitted "dash", but grave-workspaces
        # validate() rejects any ports set != PORT_BASE keys, so the documented
        # shape produced a registry that dies on every users/gateway op.
        base = re.search(r"PORT_BASE\s*=\s*\{([^}]*)\}", WORKSPACES).group(1)
        keys = set(re.findall(r'"(\w+)":', base))
        self.assertEqual(keys, {"t3", "term", "dash"})
        example = re.search(r'"ports":\s*(\{[^}]*\})', MULTIUSER).group(1)
        self.assertEqual(set(json.loads(example)), keys)

    def test_audit_contract_does_not_promise_an_unimplemented_field(self):
        # Regression #63: the doc claimed a "request correlation ID" that neither
        # audit() writer records.
        self.assertNotIn("correlation", MULTIUSER.lower())

    def test_cli_help_lists_implemented_verbs(self):
        # Regression #63: help omitted `users status` and `restore … workspaces`,
        # both implemented and referenced by the docs.
        self.assertIn("grave users [status|", GRAVE)
        self.assertIn("repo <name> | workspaces]", GRAVE)


class ReadmeContractTests(unittest.TestCase):
    """Every documented command is one the CLI dispatches, the help lists and a
    test or doctor exercises; the README makes no claim the runner cannot keep."""

    DISPATCH = set(re.findall(r"^  ([a-z-]+)\)", GRAVE.split("# ------------------------------------------------------------------ main ----")[1], re.M))
    HELP = GRAVE.split("cmd_help() {")[1].split("EOF")[1]

    def test_every_grave_command_in_the_readme_exists_and_is_tested(self):
        commands = [c for c in fenced_commands(README, "grave ") if not c.startswith("grave <")]
        self.assertGreaterEqual(len(commands), 12)
        tests = "\n".join(p.read_text() for p in (ROOT / "tests").glob("test_*.py"))
        for command in commands:
            verb = command.split()[1]
            self.assertIn(verb, self.DISPATCH, command)
            self.assertIn("grave " + verb, self.HELP, command)
            self.assertRegex(tests, r"\b" + re.escape(verb) + r"\b", f"no test mentions `grave {verb}`")

    def test_first_run_command_matches_the_runner(self):
        usage = re.search(r"grave agents run <name> --prompt-file.*", GRAVE).group(0)
        runs = [c for c in fenced_commands(README, "grave agents run") if "resurrect" in c]
        self.assertTrue(runs)
        for command in runs:
            for flag in re.findall(r"--[a-z-]+", re.sub(r"'[^']*'", "", command)):
                self.assertIn(flag, usage, command)
        prompt = (ROOT / "prompts/resurrect.txt").read_text()
        for phrase in ("what builds and what fails", "dev setup", "bounded repair", "Node first, then Python",
                       "Never merge, deploy, push"):
            self.assertIn(phrase, prompt)

    def test_raise_commands_match_the_scripts(self):
        self.assertIn("./raise.sh --profile generic", README)
        for profile in ("generic", "aws", "t2-macbook", "steam-machine"):
            self.assertTrue((ROOT / "profiles" / f"{profile}.sh").exists(), profile)
            self.assertIn(f"`{profile}`", README)

    def test_readme_claims_only_what_the_runner_keeps(self):
        for claim in ("cannot influence", "agent-proof", "tamper-proof", "proves agent work"):
            self.assertNotIn(claim, README.lower())
        self.assertIn("a command the agent did not choose", README)
        self.assertIn("root-equivalent", README)
        self.assertNotIn("!", README.replace("<img alt=", "").replace("![", ""))

    def test_docs_index_links_every_doc(self):
        for doc in sorted((ROOT / "docs").glob("*.md")):
            self.assertIn(f"](docs/{doc.name})", README, doc.name)
        for extra in ("AGENTS.md", "CONTRIBUTING.md", "profiles/README.md", "prompts/resurrect.txt"):
            self.assertIn(f"]({extra})", README, extra)
        self.assertIn("[REQUIRED-CHECKS.md](REQUIRED-CHECKS.md)", SCHEDULES)

    def test_contributing_runs_what_ci_runs(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        browser = (ROOT / ".github/workflows/browser.yml").read_text()
        self.assertIn("python -m unittest discover -s tests", ci)
        self.assertIn("python -m unittest discover -s tests", CONTRIBUTING)
        self.assertIn("GRAVEDECAY_ALLOWED_USERS=browser@example.test", browser)
        self.assertIn("GRAVEDECAY_ALLOWED_USERS=browser@example.test", CONTRIBUTING)
        for needle in ("--init", "python:3.13", "jq tmux procps iproute2", "GRAVEDECAY_PLATFORM=linux",
                       "tests/browser/forward.py", "npm ci", "npx playwright test"):
            self.assertIn(needle, CONTRIBUTING, needle)
        self.assertTrue((ROOT / "tests/browser/forward.py").exists())
        for template in ("bug.md", "recipe.md"):
            self.assertIn("name:", (ROOT / ".github/ISSUE_TEMPLATE" / template).read_text())


if __name__ == "__main__":
    unittest.main()
