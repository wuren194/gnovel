"""Fixed synthetic current-format contexts; never uses a real novel."""
from pathlib import Path
from copy import deepcopy
import hashlib
import json

from tests.test_context_builder import context_source, save_context_source


def fixed_transport_sample(task: str) -> dict:
    """Hand-authored neutral input; independent of the production Builder.

    Repeated memory locations, a reference, an author decision and a voice
    profile exercise semantic/proof separation without a captured private pack.
    """
    memory = {'subject': 'character-READER', 'field': 'entity', 'source_chapter': 'chapter-PREVIOUS',
              'value': {'entityId': 'character-READER', 'name': '林禾', 'summary': '在书店整理借阅记录。'}}
    reference = {'assetId': 'KN-SAMPLE', 'title': '物件的两次出现',
                 'excerpt': '先让借书条参与日常整理，再让来客辨认字迹。',
                 'role': 'REFERENCE KNOWLEDGE', 'sha256': 'a' * 64,
                 'path': 'knowledge/assets/sample.md', 'source': {'path': 'authored', 'sha256': ''}}
    decision = {'decisionId': 'AD-SAMPLE', 'statement': '本章在书店打烊时结束。',
                'rationale': '保留当晚场景的完整收束。', 'scopeIds': ['chapter-CURRENT']}
    plan_hash = 'b' * 64
    value = {
        'view': 'station', 'task': task, 'targetChapter': 'chapter-CURRENT', 'povCharacter': 'character-READER',
        'memoryPack': {'working': [memory], 'semantic': [deepcopy(memory)], 'episodic': []},
        'sourceContext': {
            'planReady': True, 'sourcePlanHash': plan_hash, 'ruleSummary': {'ready': True},
            'contentQuery': {'tool': 'query_source', 'selection': {
                'knowledgeMode': 'perspective', 'perspectiveIds': ['character-READER']}},
            'planQuery': {'tool': 'query_source_chapter_plan', 'chapterId': 'chapter-CURRENT'},
            'rulesQuery': {'tool': 'query_source_rules', 'action': 'write'},
        },
        'readiness': {'ready': True, 'reasons': []},
        'filterProvenance': {'perspectiveId': 'character-READER', 'excludedIds': []},
        'knowledgePack': {'role': 'REFERENCE KNOWLEDGE', 'items': [reference], 'omittedBudget': 0},
        'authorDecisions': [decision],
        'workVoiceProfile': {'role': 'WORK VOICE PROFILE', 'examples': ['用日常口语呈现人物。']},
        'recentChapters': [{'chapterId': 'chapter-PREVIOUS', 'relativePath': '正文/昨日的借书条.md',
                            'exists': True}],
        'dependencyManifest': {
            'sourceHash': 'c' * 64, 'sourcePlanHash': plan_hash,
            'knowledge': [{'assetId': 'KN-SAMPLE', 'sha256': 'a' * 64}],
            'decisions': [{'decisionId': 'AD-SAMPLE', 'sha256': 'd' * 64}],
            'voiceProfileHash': 'e' * 64,
            'recentChapters': [{'chapterId': 'chapter-PREVIOUS', 'sha256': 'f' * 64}],
        },
        'contextBudget': {'total': 50000, 'usedCharacters': 3000},
        'summaryFreshness': {'stale': False},
    }
    value['contextHash'] = hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value


def fixture(root: Path, size: str) -> tuple[str, str]:
    (root / '正文').mkdir(exist_ok=True)
    source = context_source(root, chapters=1 if size == 'small' else 2)
    # empty_structure allocates a volume ID. Freeze the IDs captured in the
    # pre-change measurement so repeated runs use exactly the same truth.
    volume = {'small': 'volume-B26FDCF3B0D8', 'medium': 'volume-A04BB21CACB3', 'edge': 'volume-68FCC9493D1B'}[size]
    arc = {'small': 'arc-B05F717FEDEB', 'medium': 'arc-3EE9692AF142', 'edge': 'arc-BBF24D058FEC'}[size]
    source = json.loads(json.dumps(source, ensure_ascii=False).replace(source['volumes'][0]['volumeId'], volume)
                        .replace(source['arcs'][0]['arcId'], arc))
    source['createdAt'] = source['updatedAt'] = '2026-10-01T00:00:00+00:00'
    source['entities'][0]['summary'] = '甲当前目标是守住城门并寻找钥匙。'
    source['entities'][0]['visibility'] = 'public'
    source['entities'][0]['mandatory'] = True
    source['chapters'][0]['visibility'] = 'public'
    source['chapters'][0]['filePath'] = '正文/甲—城门🌙.md'
    (root / source['chapters'][0]['filePath']).write_text('甲昨日在城门捡到旧钥匙。', encoding='utf-8')
    if size != 'small':
        source['entities'][1]['visibility'] = 'public'
        source['entities'][1]['summary'] = '乙在码头等待甲。'
        source['arcs'][0]['visibility'] = 'public'
        source['arcs'][0]['summary'] = '规则：城门日落后不得开启。'
        source['arcs'][0]['mandatory'] = True
    if size == 'edge':
        source['entities'][0]['notes'] = '旧门上的刻痕仍然清晰。' * 60
    from scripts.core.prose_binding import seal_binding
    from scripts.core.artifacts import sha256_text
    chapter = source['chapters'][0]
    body = (root/chapter['filePath']).read_text(encoding='utf-8')
    chapter['proseBinding'] = seal_binding(source['projectId'], chapter['chapterId'], chapter['filePath'],
        state='managed', generation=source['revision'], expected_content_digest='sha256:'+sha256_text(body))
    save_context_source(root, source)
    goal = '甲守住城门，关系推进' if size == 'small' else '甲守住城门，关系推进，埋伏笔，叙述视角'
    if size == 'edge':
        goal = '甲守住城门 completely-unrelated-query'
    return 'chapter-AAA', goal
