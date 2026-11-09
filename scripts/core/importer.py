"""Deterministic source inventory for AI-assisted novel imports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path

from .clock import utc_now
from .jsonio import write_json
from .structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, StructureError, empty_structure, validate_structure

REQUIRED_PROJECT_DIRS = (
    ".govel", ".govel/cache", ".govel/aux", ".govel/plans",
    ".govel/operations", ".govel/backups", "正文", "规划", "设定", "工作台",
)


CHAPTER_HINT = re.compile(r"(?:CH\s*0*(\d+)|第\s*([零〇一二三四五六七八九十百\d]+)\s*章)", re.IGNORECASE)
CHAPTER_BOUNDARY = re.compile(r"(?m)^(?:#{1,6}\s*)?(?P<title>(?:CH\s*0*\d+|第\s*[零〇一二三四五六七八九十百\d]+\s*章)[^\r\n]*)$")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_import_source(source: Path) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    files = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".txt"})
    inventory = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        heading = next((line.lstrip("# ").strip() for line in text.splitlines() if line.strip()), path.stem)
        hint = CHAPTER_HINT.search(path.name) or CHAPTER_HINT.search(heading)
        ordinal = None
        if hint:
            raw_ordinal = hint.group(1) or hint.group(2)
            if raw_ordinal and raw_ordinal.isdigit():
                ordinal = int(raw_ordinal)
        inventory.append({
            "sourcePath": str(path),
            "relativePath": path.name if source.is_file() else str(path.relative_to(source)),
            "sha256": _sha(path),
            "bytes": path.stat().st_size,
            "titleHint": heading[:160],
            "chapterOrdinalHint": ordinal,
        })
    return {
        "schemaVersion": 1,
        "sourceRoot": str(source),
        "createdAt": utc_now(),
        "files": inventory,
        "instructions": {
            "requiredOutput": ["bookName", "volumes", "arcs", "chapters", "chapterArcLinks", "entities", "relations", "references"],
            "rules": [
                "为每个对象生成符合新格式的稳定 ID",
                "每章恰好一条 primary 弧线，可有多条 secondary/intersection 弧线",
                "人物、物件、地点、事件、伏笔、规则和时间锚全部注册为实体",
                "无法判断的字段标记 unresolved，不得猜测",
                "输出只是候选，必须经用户校对后导入",
            ],
        },
    }


def write_import_inventory(source: Path, output: Path) -> dict:
    payload = scan_import_source(source)
    write_json(output, payload)
    return payload


def create_external_manuscript_review(source: Path, *, targetRoot: str) -> dict:
    """Create an evidence-only review copy. It contains no enabled story truth."""
    requested = source.expanduser()
    # resolve() follows the final link, so the symlink guard has to run before it.
    if requested.is_symlink():
        raise StructureError('EXTERNAL_MANUSCRIPT_SOURCE_INVALID', 'source must not be a symlink')
    source = requested.resolve()
    target = Path(targetRoot).expanduser().resolve()
    if not source.exists():
        raise StructureError('EXTERNAL_MANUSCRIPT_SOURCE_INVALID', 'source must be a readable .txt/.md file or directory')
    if target == source or source in target.parents:
        raise StructureError('EXTERNAL_MANUSCRIPT_TARGET_INVALID', 'review target must be separate and empty')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise StructureError('EXTERNAL_MANUSCRIPT_TARGET_INVALID', 'review target must be separate and empty')
    files = [source] if source.is_file() else sorted(
        path for path in source.rglob('*') if path.is_file() and path.suffix.lower() in {'.md', '.txt'})
    if not files or any(path.is_symlink() or not path.resolve().is_relative_to(source if source.is_dir() else source.parent)
                        for path in files):
        raise StructureError('EXTERNAL_MANUSCRIPT_SOURCE_INVALID', 'source contains no safe .txt/.md manuscript files')
    inventory, chapter_hints = [], []
    for path in files:
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8')
        except UnicodeError as exc:
            raise StructureError('EXTERNAL_MANUSCRIPT_ENCODING', 'external manuscript must be UTF-8') from exc
        relative = path.name if source.is_file() else str(path.relative_to(source))
        file_hash = hashlib.sha256(raw).hexdigest()
        inventory.append({'sourcePath': str(path), 'relativePath': relative,
                          'sha256': file_hash, 'bytes': len(raw)})
        matches = list(CHAPTER_BOUNDARY.finditer(text))
        for ordinal, match in enumerate(matches, 1):
            start = len(text[:match.start()].encode('utf-8'))
            end_char = matches[ordinal].start() if ordinal < len(matches) else len(text)
            end = len(text[:end_char].encode('utf-8'))
            chapter_hints.append({
                'candidateId': 'EMC-' + hashlib.sha256(
                    f'{relative}|{start}|{end}|{file_hash}'.encode()).hexdigest()[:20].upper(),
                'sourcePath': relative, 'sourceFileHash': file_hash,
                'sourceLocator': {'byteStart': start, 'byteEnd': end},
                'titleHint': match.group('title').strip(), 'ordinalHint': ordinal,
                'contentHash': hashlib.sha256(raw[start:end]).hexdigest(),
                'status': 'unapproved', 'formalSource': False,
            })
    basis = {'sourceRoot': str(source), 'files': inventory, 'chapterBoundaryCandidates': chapter_hints}
    payload = {
        'schemaVersion': 1, 'kind': 'external_manuscript_review',
        'createdAt': utc_now(), **basis,
        'inventoryHash': hashlib.sha256(json.dumps(
            basis, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'evidenceRoot': 'evidence',
        'extractedCandidates': [],
        'candidateContract': {
            'defaultStatus': 'unapproved', 'formalSource': False,
            'requiredEvidence': ['sourcePath', 'sourceFileHash', 'sourceLocator', 'evidenceHash'],
            'conflictsAllowed': True, 'authorReviewRequired': True,
        },
        'writableStoryTruthEnabled': False,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.external-manuscript-review-', dir=target.parent))
    try:
        for row, path in zip(inventory, files):
            destination = temporary / 'evidence' / row['relativePath']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != row['sha256']:
                raise StructureError('EXTERNAL_MANUSCRIPT_COPY_MISMATCH', 'evidence copy hash mismatch')
        write_json(temporary / 'external-manuscript-inventory.json', payload)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {'ok': True, 'code': 'EXTERNAL_MANUSCRIPT_REVIEW_CREATED',
            'targetRoot': str(target), 'inventoryHash': payload['inventoryHash'],
            'fileCount': len(inventory), 'chapterHintCount': len(chapter_hints),
            'writableStoryTruthEnabled': False, 'sourceFilesModified': False}


def validate_import_candidate(candidate: dict) -> dict:
    """Dry-run validation for an AI-assembled candidate; never touches disk."""
    from .source_model import content_defaults
    allowed = {'bookName', 'sourceRoot', 'volumes', 'arcs', 'chapters', 'chapterArcLinks',
               'arcPhases', 'entities', 'pairs', 'pairAssessment', 'relations', 'references', 'aliases', *content_defaults()}
    if not isinstance(candidate, dict) or set(candidate) - allowed:
        return {'valid': False, 'problems': [{'code': 'IMPORT_CANDIDATE_FIELDS_INVALID',
            'unknown': sorted(set(candidate) - allowed) if isinstance(candidate, dict) else [],
            'message': 'assemble current source records; project snapshots, approval and format metadata are not import candidates'}]}
    structure = {
        "schemaVersion": PROJECT_SCHEMA_VERSION,
        "projectId": str(uuid.uuid4()),
        "bookName": candidate.get("bookName", ""),
        "revision": 0,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "volumes": candidate.get("volumes", []),
        "arcs": candidate.get("arcs", []),
        "chapters": candidate.get("chapters", []),
        "chapterArcLinks": candidate.get("chapterArcLinks", []),
        "arcPhases": candidate.get("arcPhases", []),
        "entities": candidate.get("entities", []),
        "pairs": candidate.get("pairs", []),
        "pairAssessment": candidate.get("pairAssessment", {"status": "unassessed"}),
        "relations": candidate.get("relations", []),
        "references": candidate.get("references", []),
        "lastOperation": None,
    }
    from .source_model import content_defaults
    structure.update({key: candidate.get(key, []) for key in content_defaults()})
    problems: list[dict] = []
    if not str(candidate.get("bookName", "")).strip():
        problems.append({"code": "MISSING_BOOK_NAME", "message": "candidate.bookName is required"})
    for chapter in candidate.get('chapters', []) if isinstance(candidate.get('chapters', []), list) else []:
        if isinstance(chapter, dict) and 'proseBinding' in chapter:
            problems.append({'code': 'OWNERSHIP_UNAUTHORIZED_MUTATION', 'message': 'import candidates cannot declare prose ownership'})
    try:
        validate_structure(structure)
    except StructureError as exc:
        problems.extend(exc.details.get("errors", [{"code": exc.code, "message": str(exc)}]))
    return {
        "valid": not problems,
        "problems": problems,
        "counts": {
            "volumes": len(structure["volumes"]),
            "arcs": len(structure["arcs"]),
            "chapters": len(structure["chapters"]),
            "entities": len(structure["entities"]),
            "relations": len(structure["relations"]),
            "references": len(structure["references"]),
        },
    }


def create_project_from_candidate(root: Path, candidate: dict, *, approval_mode: str = "manual") -> dict:
    """Create a current project from a manually assembled external manuscript candidate."""
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("target project directory must be empty")
    validation = validate_import_candidate(candidate)
    if not validation['valid']:
        problem = validation['problems'][0]
        raise StructureError(problem['code'], problem.get('message', 'current import candidate failed validation'), validation)
    if approval_mode not in {'manual', 'auto'}:
        raise StructureError('INVALID_REQUEST', 'approval mode must be manual or auto')
    candidate = deepcopy(candidate)
    project_id = str(uuid.uuid4())
    chapters = candidate.get("chapters", [])
    from .refactor_paths import is_canonical
    from .write_sets import safe_project_path
    for chapter in chapters:
        if isinstance(chapter, dict) and "proseBinding" in chapter:
            # AI proposal != commit authority：候选自带的 binding 是未经授权的归属
            # 声明，绝不进入真源（实施轮 §42 的 concrete bypass 之一）。
            raise StructureError("OWNERSHIP_UNAUTHORIZED_MUTATION",
                                 "import candidates must not carry proseBinding; the importer seals bindings itself",
                                 {"chapterId": chapter.get("chapterId")})
        relative = chapter.get("filePath") if isinstance(chapter, dict) else None
        if not isinstance(relative, str) or not relative.strip():
            raise StructureError("PROSE_LOCATOR_UNSAFE",
                                 "import candidate chapters require a project-relative filePath locator",
                                 {"chapterId": chapter.get("chapterId") if isinstance(chapter, dict) else None})
        # 路径安全必须先于任何文件系统探测（实施轮 §26–§30）：is_canonical 先做纯词法
        # 形态检查（绝对路径、`..`、空段），再委托共享 safe_project_path 做根内解析与
        # symlink 守卫。越界候选因此连一次 exists 都不会发生，绝不先探测再拒绝。
        if not is_canonical(root, relative):
            raise StructureError("PROSE_LOCATOR_UNSAFE",
                                 "import candidate filePath is not a safe canonical project-relative locator",
                                 {"chapterId": chapter.get("chapterId") if isinstance(chapter, dict) else None,
                                  "filePath": relative})
        if safe_project_path(root, relative).exists():
            # 已有正文的导入绝不自动 managed；Phase 1 没有 explicit adopt，fail closed。
            raise StructureError("PROSE_TARGET_NOT_OWNED",
                                 "import target already holds a prose file; explicit adopt semantics land in a later phase",
                                 {"chapterId": chapter.get("chapterId") if isinstance(chapter, dict) else None,
                                  "filePath": relative})
    structure = {
        "schemaVersion": PROJECT_SCHEMA_VERSION,
        "projectId": project_id,
        "bookName": candidate["bookName"],
        "revision": 1,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "volumes": candidate.get("volumes", []),
        "arcs": candidate.get("arcs", []),
        "chapters": candidate.get("chapters", []),
        "chapterArcLinks": candidate.get("chapterArcLinks", []),
        "arcPhases": candidate.get("arcPhases", []),
        "entities": candidate.get("entities", []),
        "pairs": candidate.get("pairs", []),
        "pairAssessment": candidate.get("pairAssessment", {"status": "unassessed"}),
        "relations": candidate.get("relations", []),
        "references": candidate.get("references", []),
        "aliases": candidate.get("aliases", []),
        "migrations": [{"kind": "ai_import", "source": candidate.get("sourceRoot"), "createdAt": utc_now()}],
        "lastOperation": None,
    }
    # Chapter Materialization（契约 §4 行 1）：导入本身是一次正式提交（revision 1），
    # 全部 greenfield 空章密封为 unbound；候选不得自带 binding（上面已拦），
    # 已有正文已在上方 fail closed，绝不 auto-adopt。
    from .prose_binding import materialization_binding, require_all_binding_health
    for chapter in structure["chapters"]:
        chapter["proseBinding"] = materialization_binding(structure, chapter,
                                                          generation=structure["revision"])
    from .source_model import content_defaults
    structure.update({key: candidate.get(key, []) for key in content_defaults()})
    validate_structure(structure)
    # B20：导入产出的 ownership-aware 结构不得绕过 binding 校验。导入是 sanctioned
    # initializer（实施轮 §42），它一次性封全部 binding，所以这里是唯一需要
    # 「全量健康」而非「目标健康」的调用方。
    require_all_binding_health(structure)
    for rel in REQUIRED_PROJECT_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    from .data_provider import binding as provider_binding
    selected_provider = provider_binding()
    marker = {
        "schemaVersion": PROJECT_SCHEMA_VERSION,
        "workflowVersion": WORKFLOW_VERSION,
        "projectId": project_id,
        "bookName": candidate["bookName"],
        "projectRevision": 0,
        "createdAt": utc_now(),
        "approvalMode": approval_mode,
    }
    if selected_provider['providerId'] is not None:
        marker['dataProvider'] = {'providerId': selected_provider['providerId']}
    write_json(root / ".govel" / "project.json", marker)
    # sanctioned initial write（实施轮 §42）：ownership-aware 项目的第一次落地只走这里的
    # 明确初始化通道——候选自带 binding 已拒、已有正文已拒、全部 binding 由本函数统一密封
    # 并整体验证。generic save_structure 有意不承担这个角色（§22–§25 的 Option A）。
    write_json(root / ".govel" / "structure.json", structure)
    write_json(root / ".govel" / "workflow.json", {"schemaVersion": 2, "approvedChapterIds": [], "chapterStates": {}, "artifacts": {}, "approvals": {}, "repairRounds": {}, "nextAction": None, "updatedAt": utc_now()})
    templates = []
    try:
        from scripts import state_cache
    except ImportError:
        import state_cache
    state_cache.rebuild(root)
    return {"project": marker, "structure": structure, "templates": templates}
