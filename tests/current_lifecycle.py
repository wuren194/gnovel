"""Current-format lifecycle setup using the production materialization boundary."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_commands import preview_insert_chapter, commit_plan
from tests.fixtures_current import initialize_project


class CurrentLifecycleFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()

    def materialize(self, root, *, title='白塔'):
        source = read_json(root/'.govel/structure.json')
        preview = preview_insert_chapter(root, title=title, volume_id=source['volumes'][0]['volumeId'],
                                         primary_arc_id=source['arcs'][0]['arcId'])
        return commit_plan(root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                           approved=True, actor='test')['chapter']

    def read_ready_current(self, name='current'):
        root = self.base/name
        initialize_project(root)
        chapter = self.materialize(root)
        source = read_json(root/'.govel/structure.json')
        source['fieldDefinitions'].append({'fieldId': 'field-TEST-PLAN', 'key': 'plot.plan', 'label': '章节计划',
                                          'valueType': 'topic', 'appliesTo': ['chapter', 'project', 'volume']})
        source['attributes'].append({'attributeId': 'attribute-TEST-PLAN', 'ownerId': chapter['chapterId'],
                                     'fieldId': 'field-TEST-PLAN', 'values': []})
        source['fragments'].append({'fragmentId': 'plan-TEST-PLAN', 'ownerId': chapter['chapterId'], 'order': 1,
                                    'contentRole': 'plan', 'contentBindings': ['attribute-TEST-PLAN'], 'text': '甲守城门。'})
        write_json(root/'.govel/structure.json', source)
        return root, chapter['chapterId']
