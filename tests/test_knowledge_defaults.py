"""Public narrative vocabulary retrieves only explicitly supplied reference data."""
import json
import unittest

from scripts.core.knowledge_router import build_knowledge_pack
from tests.fixtures_knowledge import staged_reference


class KnowledgeDefaultsTests(unittest.TestCase):
    def test_resource_and_responsibility_query_uses_external_reference(self):
        with staged_reference() as assets:
            path = assets / 'KN-RELATIONSHIP-TRANSITION.md'
            front, body = path.read_text()[4:].split('\n---\n', 1)
            metadata = json.loads(front)
            metadata.update(title='共同经营旧书店', intents=['power_dynamics'], tags=['合作'],
                            applicability='两位店主商量经营分工及对外承诺。')
            path.write_text('---\n' + json.dumps(metadata, ensure_ascii=False) + '\n---\n' + body)
            report = build_knowledge_pack(planningIntent='两位店主重新商量决策权与责任分配。', station='planner')
            self.assertEqual([item['assetId'] for item in report['items']], ['KN-RELATIONSHIP-TRANSITION'])
            self.assertEqual(report['items'][0]['role'], 'REFERENCE KNOWLEDGE')
            unrelated = build_knowledge_pack(planningIntent='描写城郊的冬季气候。', station='planner')
            self.assertEqual(unrelated['items'], [])


if __name__ == '__main__':
    unittest.main()
