"""The Swift client's golden responses must conform to the shared OpenAPI."""
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


class SwiftManagementContract(unittest.TestCase):
    def test_swift_fixtures_match_openapi(self):
        fixtures = json.loads((ROOT / 'clients/apple/GravedecayKit/Tests/GravedecayKitTests/Fixtures/management.json').read_text())
        document = json.loads((ROOT / 'docs/openapi.json').read_text())
        for name, value in fixtures.items():
            schema = {'capabilities': 'Capabilities', 'operation': 'Operation'}.get(name, name.title() + 'Resource')
            with self.subTest(resource=name):
                Draft202012Validator({'$ref': '#/components/schemas/' + schema,
                                      'components': document['components']},
                                     format_checker=FormatChecker()).validate(value)
