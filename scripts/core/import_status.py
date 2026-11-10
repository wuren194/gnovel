"""One live follow-up checklist; absent data never claims a semantic omission."""
from pathlib import Path
import json

from .import_commit import RECEIPT
from .structure_store import load_structure
from .write_sets import safe_project_path


def status_from_source(root, source, workflow):
    from .import_verification import import_receipt
    from .import_history import history_status, history_entry
    from .summary_status import summary_freshness
    receipt = import_receipt(root, source)
    origin = {row['chapterId'] for row in receipt['chapters']}
    chapters = [row for row in source['chapters'] if row['chapterId'] in origin and row.get('status') != 'archived']
    accepted = sum(bool(history_entry(workflow, c['chapterId']) and history_status(root, source, workflow, c)['complete']) for c in chapters)
    def item(key, status, message, op, **args):
        from .command_help import cli_action
        return {'id': key, 'status': status, 'message': message,
                'action': cli_action(op, {'projectRoot': str(root), **args})}
    reviewed = bool(chapters) and accepted == len(chapters)
    style = source.get('workVoiceProfile') or {}
    summary = summary_freshness(root, source)
    tasks = [
        item('manuscript_review', 'accepted' if reviewed else 'pending', f'有效回溯审阅 {accepted}/{len(chapters)} 章；未决事实由 AI 核对后再提请采用。', 'import.organization-prepare'),
        item('relationship_assessment', 'assessed' if reviewed else 'pending', f'已登记关系 {len(source["relations"])} 条；零条不证明遗漏，也不证明已核对。', 'import.organization-prepare'),
        item('story_structure', 'retrospective_recorded' if reviewed else 'pending', '卷弧可沿结构工具整理；回溯梗概存真源，不要求生成规划 Markdown，也不冒充未来计划。', 'source.query', view='overview'),
        item('voice_profile', 'active' if style.get('status') == 'active' else 'pending', 'AI 依据原文整理风格候选，经明确采用后激活。', 'source.voice-profile'),
        item('story_summary', 'fresh' if not summary['stale'] else 'pending', '原稿历史采用后，由 AI 生成并复核摘要；摘要不代替事实真源。', 'summary.prepare'),
        item('continuation_authorization', 'independent', '原稿采用不扩大续写范围；新章按正常计划与作者授权推进。', 'task.prepare', task='continue', view='model'),
    ]
    return {'ok': True, 'code': 'IMPORT_FOLLOWUP', 'acceptedChapters': accepted, 'importedChapters': len(chapters),
            'tasks': tasks, 'verifyAction': {'operation': 'import.verify', 'arguments': {'projectRoot': str(root)}},
            'planningMarkdownRequired': False, 'dataCountsProveSemanticCompleteness': False}


def organization_status(root: Path):
    source = load_structure(root)
    path = safe_project_path(root, '.govel/workflow.json')
    workflow = json.loads(path.read_bytes()) if path.is_file() else {}
    return status_from_source(root, source, workflow)
