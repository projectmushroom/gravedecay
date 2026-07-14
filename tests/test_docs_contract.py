import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MULTIUSER = (ROOT / "docs/MULTIUSER.md").read_text()
GRAVE = (ROOT / "bin/grave").read_text()
WORKSPACES = (ROOT / "bin/grave-workspaces").read_text()


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


if __name__ == "__main__":
    unittest.main()
