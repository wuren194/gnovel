"""Artifact template generation and schema declaration drift checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from copy import deepcopy

from .artifacts import ARTIFACT_SCHEMA_VERSION, envelope
from .jsonio import read_json
from .project import TOOLBOX_ROOT
from .review_artifacts import (AUXILIARY_GATES, STAGE_B_SLOTS, STAGE_A_DECISIONS,
                              VERDICTS, REPAIR_SCOPES, validate_review_artifact)

ARTIFACT_TYPES = (
    "stage_a_review", "candidate_prose", "diagnostics", "stage_b_review",
    "title_candidates", "repair_plan", "state_candidate",
)
SCHEMA_PATH = TOOLBOX_ROOT / "shared" / "schemas" / "artifacts.json"
EXAMPLES_DIR = TOOLBOX_ROOT / "shared" / "examples" / "artifacts"
ZERO_SHA = "0" * 64


class ArtifactTemplateError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _payload(artifact_type: str) -> dict[str, Any]:
    slots = {name: {"conclusion": "待填写"} for name in (*STAGE_B_SLOTS, *AUXILIARY_GATES)}
    from .behavior_review import blank_coverage
    return {
        "stage_a_review": {"decision": "pending", "behaviorCoverage": blank_coverage(), "activeSeats": [1, 2, 4, 5, 6, 7, 10], "sourcePlanHash": ZERO_SHA, "consensus": [], "disagreements": [], "openItems": []},
        "candidate_prose": {"text": "待填写正文"},
        "diagnostics": {"passed": False, "draftSha256": ZERO_SHA, "inputHash": ZERO_SHA},
        "stage_b_review": {"verdict": "INCOMPLETE", "behaviorCoverage": blank_coverage(), "factAssessment": {"status": "unassessed"}, "slots": slots, "findings": [], "finalProseSha256": ZERO_SHA},
        "title_candidates": {"candidates": [{"candidateId": "title-1", "title": "待填写标题"}]},
        "repair_plan": {"repairScope": "local", "findings": []},
        "state_candidate": {"baseStateSha256": ZERO_SHA, "designSha256": ZERO_SHA, "sourcePlanHash": ZERO_SHA, "finalProseSha256": ZERO_SHA, "changes": []},
    }[artifact_type]


def artifact_guide(artifact_type: str) -> dict:
    """Instructions are schema-derived metadata, never conclusions or approval."""
    schema = read_json(SCHEMA_PATH)['$defs'][artifact_type]
    guide = {'enums': {key: value['enum'] for key, value in schema.get('properties', {}).items() if 'enum' in value}}
    if artifact_type == 'stage_b_review':
        guide['cognitionReviews'] = ('cognitionReviews只定位已登记姓名/别名在受支持引语中的使用，不是越界裁决或全章认知覆盖。'
            '逐项核实characterId、information、reason和sources；supported需说明该角色为何在此时已接触来源。'
            'current_prose引用本章使用点之前的原文；prior_prose给前章chapterId和quote，工具绑定已接纳正文；'
            'source_fragment给本章输入中的belief/in_world_text片段ID和quote，须同角色且当前有效。'
            'not_applicable须说明误匹配、非角色信息使用等具体理由。needs_material/repair_required关联既有findingId，不能通过；'
            '未检测到姓名不表示没有秘密、机制、内心或行动中的认知问题，这些仍由cognition席审查并可显式追加semantic条目。'
            '引用位置和先后只提供机械证据，来源是否支持判断由模型负责。不得自动补写获知情节。')
        guide['factAssessment'] = 'B 用 continuity 席盘点本章持续事实/变更：unassessed、insufficient_material、no_change 或 changes。需回流项放 findings，保留 findingId 和 factChange（pending、reason、当前 evidence，及 requestId 或精确 targetIds）。正文修复不等于事实登记完成。topic新事实使用 source.chapter-facts-preview 的 findingId 关联；typed修订走源事务后登记精确目标的正文证据。'
        guide['completion'] = ('POLISH_READY：每席填写实际结论，并提供 evidence 精确引文数组；'
                               '不适用时改填 notApplicableReason。引文只核验材料对应，不证明结论正确。')
        guide['evidenceExample'] = {'text': '从当前候选正文逐字引用；重复文字须另给 start/end 字符偏移'}
        guide['writingNotes'] = ('当前候选有 writingNotes 时，writingDecisions 逐个填写 id、decision、reason；'
                                 'POLISH_READY 要求全部 accept。包装器绑定整份候选和所读诊断；作者批准仍单独执行。')
        guide['seatStructure'] = {'primarySeats': list(STAGE_B_SLOTS), 'auxiliaryGates': list(AUXILIARY_GATES),
                                  'explanation': '十个主席位 + 认知和组织过满两个独立闸门，共12个结论槽位。'}
    if artifact_type == 'state_candidate':
        guide['changes'] = {'value': [], 'instruction': '只绑定当前状态；新增和修订事实经源事务及正文证据登记。'}
        guide['proposedFacts'] = '不接受独立事实副本；通过 source.change-preview 提案，批准后用 evidence 登记语义结论和正文引用。'
    if artifact_type == 'candidate_prose':
        guide['notes'] = '直接传 text 和 writingNotes 对象；也可指定工作台 input 与 notes 文件。deviations 是待裁决材料，新增事实走源事务。'
        guide['selection'] = '有 active 选择时先取 workflow.candidate-handoff，再传原 selectionRef 与实际 selectionReview；工具生成身份绑定，不按同文猜关联。'
    if artifact_type == 'stage_a_review':
        guide['modeLevelReason'] = '铺垫/剧情模式配高档上限时，说明实际预期强度与适用范围；结构化推演须填完整定位、篇幅和放慢余量。'
        guide['seatReviews'] = {'1': {'conclusion': '填写该席实际判断', 'evidence': [{'text': '逐字引用当前推演'}]}}
        guide['seatReviewSource'] = '对全部 activeSeats 填写；也可从推演里逐行“身份N（名称）：结论”提取已有判断，不补缺席结论。'
    if artifact_type in {'stage_a_review', 'stage_b_review'}:
        from .artifact_bindings import BINDABLE_FIELDS
        from .behavior_review import coverage_schema
        guide['fieldSchemas'] = {
            'behaviorCoverage': coverage_schema(),
            **{field: schema['properties'][field]
               for field in ('behaviorReviews', 'relationshipCausalityReviews')},
        }
        guide['toolOwnedFields'] = sorted(BINDABLE_FIELDS.get(artifact_type, ()))
        guide['candidateInputs'] = '候选批次的 candidateRecipe 从 Planner 准备冻结并继承；不是可填写的审查结论或批准。'
        guide['formAction'] = {'operation': 'workflow.review-form', 'arguments': {'artifactType': artifact_type},
                               'requiredInputs': ['chapterId']}
        guide['authorship'] = '表单负责当前绑定、实际席位和空槽；模型填写真实判断、引文与裁决。空白或形状正确不代表审查通过。'
        guide['behaviorCoverage'] = '每章独立判断 relationship/living 的适用状态。空数组不表示不适用；覆盖理由和引文复用 A 席4/触发后席9，B 的 logic/relationship/continuity。B 须检查正文新增安排，用 origin=prose 记录，处置须区分 within_plan/local_repair/replan_required/needs_material。语义标准见 G-CAUSAL。'
        guide['relationshipCausalityReviews'] = (
            '章节合同声明 relationshipCausality 时，逐个 id 填 conclusion 和 evidence；'
            '阶段 A 引用当前章计划文本，或用 evidence.declarationField 引用同一项的结构化说明字段；'
            '阶段 B 只能引用当前候选正文。核对实际能力、两种选择代价、'
            '有效范围及依据继续/替换/解除；不得从外观推定能力。机器只校验完成和引文，不判语义成立。')
    return guide


def artifact_template(artifact_type: str, *, project_id: str | None = None, chapter_id: str | None = None) -> dict[str, Any]:
    if artifact_type not in ARTIFACT_TYPES:
        raise ArtifactTemplateError("ARTIFACT_TYPE_UNKNOWN", f"unsupported artifact type: {artifact_type}", {"supported": list(ARTIFACT_TYPES)})
    return envelope(artifact_type, {**_payload(artifact_type), '_guide': artifact_guide(artifact_type)},
                    project_id=project_id, chapter_id=chapter_id)


def validate_artifact_schema_declarations() -> dict[str, Any]:
    """Check declaration coverage without adding a jsonschema runtime dependency."""
    schema = read_json(SCHEMA_PATH)
    declared = set(schema.get("properties", {}).get("artifactType", {}).get("enum", []))
    defs = set(schema.get("$defs", {}))
    expected = set(ARTIFACT_TYPES)
    problems = []
    if schema.get("properties", {}).get("schemaVersion", {}).get("const") != ARTIFACT_SCHEMA_VERSION:
        problems.append("envelope schemaVersion differs from runtime")
    if declared != expected:
        problems.append(f"artifactType enum mismatch: expected={sorted(expected)} actual={sorted(declared)}")
    if defs != expected:
        problems.append(f"$defs mismatch: expected={sorted(expected)} actual={sorted(defs)}")
    for kind, field, values in [('stage_a_review', 'decision', STAGE_A_DECISIONS),
                                ('stage_b_review', 'verdict', VERDICTS),
                                ('stage_b_review', 'repairScope', REPAIR_SCOPES),
                                ('repair_plan', 'repairScope', REPAIR_SCOPES)]:
        if set(schema['$defs'][kind]['properties'][field]['enum']) != values:
            problems.append(f'{kind}.{field} enum differs from runtime')
    from .relationship_causality_review import REVIEW_FIELD, REVIEW_FIELDS, DECLARATION_QUOTE_FIELDS
    for kind in ('stage_a_review', 'stage_b_review'):
        row = schema['$defs'][kind]['properties'].get(REVIEW_FIELD, {}).get('items', {})
        if set(row.get('required', [])) != REVIEW_FIELDS or set(row.get('properties', {})) != REVIEW_FIELDS:
            problems.append(f'{kind}.{REVIEW_FIELD} fields differ from runtime')
        quote_properties = row.get('properties', {}).get('evidence', {}).get('items', {}).get('properties', {})
        fields = set(quote_properties.get('declarationField', {}).get('enum', []))
        if fields != (DECLARATION_QUOTE_FIELDS if kind == 'stage_a_review' else set()):
            problems.append(f'{kind}.{REVIEW_FIELD} quotation fields differ from runtime')
    from .behavior_review import DOMAINS, COVERAGE_STATUSES, DISPOSITIONS, coverage_schema
    from .fact_inventory import ASSESSMENTS
    for kind in ('stage_a_review', 'stage_b_review'):
        properties = schema['$defs'][kind]['properties']
        coverage = properties.get('behaviorCoverage', {})
        if {key: coverage.get(key) for key in coverage_schema()} != coverage_schema():
            problems.append(f'{kind}.behaviorCoverage input shape differs from runtime')
        for domain in DOMAINS:
            if set(properties.get('behaviorCoverage', {}).get('properties', {}).get(domain, {}).get('enum', [])) != COVERAGE_STATUSES:
                problems.append(f'{kind}.behaviorCoverage differs from runtime')
        if set(properties.get('behaviorReviews', {}).get('items', {}).get('properties', {}).get('disposition', {}).get('enum', [])) != DISPOSITIONS:
            problems.append(f'{kind}.behaviorReviews differs from runtime')
    if set(schema['$defs']['stage_b_review']['properties'].get('factAssessment', {}).get('properties', {}).get('status', {}).get('enum', [])) != ASSESSMENTS:
        problems.append('factAssessment differs from runtime')
    from .cognition_review import DISPOSITIONS, ROW_FIELDS
    from .current_candidate import selection_review_schema
    if schema['$defs']['candidate_prose']['properties'].get('selectionReview') != selection_review_schema():
        problems.append('candidate_prose.selectionReview input shape differs from runtime')
    cognition = schema['$defs']['stage_b_review']['properties'].get('cognitionReviews', {}).get('items', {}).get('properties', {})
    if set(cognition) != ROW_FIELDS or set(cognition.get('disposition', {}).get('enum', [])) != DISPOSITIONS:
        problems.append('cognitionReviews differs from runtime')
    for artifact_type in ARTIFACT_TYPES:
        template = artifact_template(artifact_type, project_id="PROJECT", chapter_id="CHAPTER")
        try:
            validate_review_artifact(template)
        except ValueError as exc:
            problems.append(f"template invalid for {artifact_type}: {exc}")
        example_path = EXAMPLES_DIR / f"{artifact_type}.json"
        if not example_path.is_file():
            problems.append(f"example missing for {artifact_type}")
        else:
            try:
                example = read_json(example_path)
                validate_review_artifact(example)
                if example.get("artifactType") != artifact_type:
                    problems.append(f"example type mismatch for {artifact_type}")
            except (OSError, ValueError) as exc:
                problems.append(f"example invalid for {artifact_type}: {exc}")
    return {"ok": not problems, "schemaPath": str(SCHEMA_PATH.relative_to(TOOLBOX_ROOT)), "artifactTypes": list(ARTIFACT_TYPES), "problems": problems}


def review_skeleton_payload(root: Path, *, chapterId: str, artifactType: str,
                            repairRef: str | None = None) -> tuple[dict, dict]:
    """Build the shared blank form and its actual seat plan without saving it."""
    from .artifact_bindings import BINDABLE_FIELDS
    from .review_plan import resolve_effective_review_plan
    from .structure_store import StructureError, load_structure
    if artifactType not in {'stage_a_review', 'stage_b_review'}:
        raise StructureError('ARTIFACT_TYPE_UNSUPPORTED', 'review skeleton supports stage A or B')
    structure = load_structure(root)
    chapter = next((r for r in structure['chapters'] if r['chapterId'] == chapterId), None)
    if chapter is None or chapter.get('status') == 'archived':
        raise StructureError('CHAPTER_NOT_FOUND', 'review requires an active chapter')
    plan = resolve_effective_review_plan(
        'govel-planner' if artifactType == 'stage_a_review' else 'govel-editor',
        'chapter' if artifactType == 'stage_a_review' else 'stage_b', chapter=chapter)
    if artifactType == 'stage_a_review':
        from .review_plan import chapter_stage_a_plan
        plan = chapter_stage_a_plan(structure, chapterId)
    payload = _payload(artifactType)
    for field in BINDABLE_FIELDS.get(artifactType, []):
        payload.pop(field, None)
    if artifactType == 'stage_a_review':
        payload['activeSeats'] = plan['activeSeats']
        payload['seatReviews'] = {str(seat): {'conclusion': '', 'evidence': []} for seat in payload['activeSeats']}
    else:
        payload['slots'] = {name: {'conclusion': '', 'evidence': []} for name in (*STAGE_B_SLOTS, *AUXILIARY_GATES)}
    if repairRef is not None:
        from .prepare_dependencies import validate_dependencies
        from .repair_review import review_input
        from .transport_cache import get
        saved = get(root, repairRef, kind='repair')
        if artifactType != 'stage_b_review' or saved['chapterId'] != chapterId:
            raise StructureError('REPAIR_REVIEW_SCOPE_INVALID', 'repair skeleton must select this chapter stage B')
        validate_dependencies(root, saved['dependencies'], code='REPAIR_REVIEW_STALE')
        payload = review_input(repairRef)
    from .relationship_causality import blank_reviews
    rows = blank_reviews(structure, chapterId)
    if rows:
        payload['relationshipCausalityReviews'] = rows
    from .behavior_review import blank_coverage, blank_reviews as living_reviews
    payload['behaviorCoverage'] = blank_coverage()
    payload['behaviorReviews'] = living_reviews(structure, chapterId)
    if artifactType == 'stage_b_review':
        payload['factAssessment'] = {'status': 'unassessed'}
        from .finalization import _load_workflow
        from .fact_inventory import retained_findings
        from .repair_history import read_versions
        workflow = _load_workflow(root)
        retained, unavailable = retained_findings(root, structure, workflow, chapterId)
        if unavailable:
            raise StructureError('FACT_HISTORY_UNAVAILABLE', 'restore historical B reports before replacing them', {'unavailableReports': unavailable})
        payload['findings'] = [row for row in retained.values() if row['factChange']['status'] == 'pending']
        versions, _ = read_versions(root, structure, workflow, chapterId, 'stage_b_review')
        previous = {}
        for version in versions:
            previous.update({r['id']: r for r in version['report']['payload'].get('behaviorReviews', [])})
        rows = {r['id']: r for r in payload['behaviorReviews']}
        rows.update({identity: row for identity, row in previous.items() if row['disposition'] != 'within_plan' and identity not in rows})
        payload['behaviorReviews'] = list(rows.values())
        from .cognition_review import scan_candidates, retained_findings as cognition_findings
        from .mechanical_evidence import _registered
        candidate = _registered(root, structure, workflow, chapterId, 'candidate_prose')
        if candidate is not None:
            from .repair_history import stage_b_obligations
            obligations = stage_b_obligations(root, structure, workflow, chapterId, candidate)
            if obligations['unavailableReports']:
                raise StructureError('REPAIR_HISTORY_UNAVAILABLE', 'restore historical B reports before claiming completion',
                                     {'unavailableReports': obligations['unavailableReports']})
            existing_ids = {row.get('findingId') for row in payload['findings']}
            for identity, obligation in obligations['pending'].items():
                if repairRef is None and identity not in existing_ids:
                    payload['findings'].append({**deepcopy(obligation['finding']), 'findingId': identity})
            cognition = scan_candidates(structure, chapterId, candidate['payload']['text'])
            if cognition:
                payload['cognitionReviews'] = cognition
        existing = {row.get('findingId') for row in payload['findings']}
        # Blocking retained cognition is already owned by the frozen repair
        # rows. assemble_review carries it forward unless explicitly resolved;
        # duplicating it here makes a successful resolution impossible to submit.
        repair_owned = set(obligations['blockingIssueIds']) if candidate is not None and repairRef is not None else set()
        payload['findings'].extend(row for row in cognition_findings(root, structure, workflow, chapterId)
                                   if row['findingId'] not in existing and row['findingId'] not in repair_owned)
    return payload, plan


def write_review_skeleton(root: Path, *, chapterId: str, artifactType: str, output: str,
                          repairRef: str | None = None) -> dict:
    """Write an unregistered payload with real seat names, never conclusions."""
    from .locks import project_lock_reentrant
    from .structure_store import StructureError
    from .write_sets import apply_write_set, inventory, json_bytes, safe_project_path, guard_observation
    root = root.resolve()
    with project_lock_reentrant(root):
        state, _ = guard_observation(root / '.govel/structure.json')
        payload, _ = review_skeleton_payload(root, chapterId=chapterId, artifactType=artifactType, repairRef=repairRef)
        path = safe_project_path(root, output)
        relative = str(path.relative_to(root))
        if not relative.startswith('工作台/') or path.exists():
            raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'skeleton needs a new file under 工作台')
        payload['_guide'] = artifact_guide(artifactType)
        outputs = {relative: json_bytes(payload)}
        expected = inventory(root, outputs)
        if any(row['before']['exists'] for row in expected):
            raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'skeleton output appeared during preparation')
        apply_write_set(root, outputs, expected=expected,
                        guards=[{'path': '.govel/structure.json', **state}], operation='review_skeleton')
        return {'ok': True, 'code': 'REVIEW_SKELETON_WRITTEN', 'path': relative,
                'artifactType': artifactType, 'registered': False, 'approved': False}
