"""Verified report delivery and work notes, without converting history into current review.

失效报告保留原建议与笔记，但不对当前正文产生定位或注释许可。正文、计划和实际工件
字节分别核验；工作笔记始终只是作者工作状态，不等于审校通过或批准。
"""

from __future__ import annotations

from pathlib import Path
import json
import re
from typing import Any

from .clock import utc_now
from .locks import project_lock
from .paths import safe_project_path
from .review_report_sources import ReviewSources, exception_reason, reason
from .structure_store import StructureError
from .write_sets import apply_write_set, guard_observation, inventory, json_bytes

NOTES_SCHEMA_VERSION = 1
NOTES_DIR = ".govel/workbench/review-notes"
NOTE_STATES = {"processed", "pending_review"}
_UNSET_BODY = object()


def localize_findings(findings: list[dict[str, Any]], body: str) -> list[dict[str, Any]]:
    """Anchor every finding by exact quote match; never invent a location."""
    rows = []
    for index, finding in enumerate(findings):
        anchor_text = str(finding.get("location", "") or "")
        anchor: dict[str, Any]
        if not anchor_text.strip():
            anchor = {"text": "", "state": "missing", "occurrences": 0, "offsets": []}
        else:
            offsets: list[list[int]] = []
            start = 0
            while True:
                hit = body.find(anchor_text, start)
                if hit < 0:
                    break
                offsets.append([hit, hit + len(anchor_text)])
                start = hit + 1
                if len(offsets) >= 50:  # 防病态重复引文拖垮渲染；超过即 ambiguous
                    break
            if not offsets:
                anchor = {"text": anchor_text, "state": "missing", "occurrences": 0, "offsets": []}
            elif len(offsets) == 1:
                anchor = {"text": anchor_text, "state": "unique", "occurrences": 1, "offsets": offsets}
            else:
                anchor = {"text": anchor_text, "state": "ambiguous", "occurrences": len(offsets), "offsets": offsets[:8]}
        rows.append({
            "findingIndex": index,
            "severity": finding.get("severity", ""),
            "ruleId": finding.get("ruleId", ""),
            "summary": finding.get("summary", ""),
            "repairSuggestion": finding.get("repairSuggestion"),
            "slot": finding.get("slot"),
            "anchor": anchor,
        })
    return rows


def _notes_path(root: Path, chapter_id: str) -> Path:
    return safe_project_path(root, f"{NOTES_DIR}/{chapter_id}.json")


def load_issue_notes(root: Path, chapter_id: str) -> dict[str, Any]:
    path = _notes_path(root, chapter_id)
    observed, raw = guard_observation(path)
    if raw is None and observed['exists']:
        raise StructureError('REVIEW_NOTES_INVALID', 'review work notes are not a readable regular file')
    return _validated_notes(raw, chapter_id)


def _validated_notes(raw: bytes | None, chapter_id: str) -> dict:
    if raw is None:
        return {'schemaVersion': NOTES_SCHEMA_VERSION, 'chapterId': chapter_id, 'notes': {}}
    try:
        notes = json.loads(raw)
        if (not isinstance(notes, dict) or set(notes) != {'schemaVersion', 'chapterId', 'notes'}
                or type(notes['schemaVersion']) is not int or notes['schemaVersion'] != NOTES_SCHEMA_VERSION
                or notes['chapterId'] != chapter_id or not isinstance(notes['notes'], dict)):
            raise ValueError('notes must retain their chapter identity and schema')
        for key, row in notes['notes'].items():
            if (not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}:(0|[1-9][0-9]*)', key) is None
                    or not isinstance(row, dict) or set(row) != {'state', 'note', 'at'}
                    or row['state'] not in NOTE_STATES or not isinstance(row['note'], str)
                    or not isinstance(row['at'], str) or not row['at'].strip()):
                raise ValueError('notes must identify a report finding and a valid working state')
    except (ValueError, TypeError) as exc:
        raise StructureError('REVIEW_NOTES_INVALID', 'review work notes are invalid',
                             {'chapterId': chapter_id, 'reason': str(exc)}) from exc
    return notes


def _notes(sources: ReviewSources) -> tuple[dict, dict]:
    raw, observation = sources.observe(_notes_path(sources.root, sources.chapter_id))
    if raw is None and observation['exists']:
        raise StructureError('REVIEW_NOTES_INVALID', 'review work notes are not a readable regular file')
    return _validated_notes(raw, sources.chapter_id), observation


def note_issue(
    root: Path,
    chapter_id: str,
    *,
    report_sha256: str,
    finding_index: int,
    state: str,
    note: str = "",
) -> dict[str, Any]:
    """Annotate one verified current finding; guard inputs through the commit marker."""
    root = root.expanduser().resolve()
    if not isinstance(state, str) or state not in NOTE_STATES:
        raise StructureError("REVIEW_NOTE_STATE_INVALID", "unsupported note state", {"supported": sorted(NOTE_STATES)})
    if type(finding_index) is not int or finding_index < 0:
        raise StructureError("REVIEW_NOTE_INVALID", "findingIndex must be a non-negative integer")
    if not isinstance(note, str):
        raise StructureError('REVIEW_NOTE_INVALID', 'note must be text')
    with project_lock(root):
        sources = ReviewSources(root, chapter_id, guard_formal_locators=True)
        view = _report_view(sources)
        report = view['report']
        if (report is None or report['artifactSha256'] != report_sha256 or not view['canAnnotate']):
            raise StructureError('REVIEW_REPORT_STALE', 'work notes require this exact currently applicable report',
                                 {'chapterId': chapter_id, 'versionBinding': view['versionBinding']})
        if finding_index >= len(report['findings']):
            raise StructureError('REVIEW_NOTE_INVALID', 'findingIndex does not identify a finding in this report')
        notes, observed = _notes(sources)
        key = f'{report_sha256}:{finding_index}'
        notes['notes'][key] = {'state': state, 'note': note, 'at': utc_now()}
        outputs = {observed['path']: json_bytes(notes)}
        expected = inventory(root, outputs)
        if any(expected[0]['before'][field] != observed[field] for field in ('exists', 'sha256')):
            raise StructureError('REVIEW_INPUTS_CHANGED', 'work notes changed after being read')
        sources.validate(stage='review-note-inputs')
        guards = [row for row in sources.observations.values() if row['path'] != observed['path']]
        apply_write_set(root, outputs, expected=expected, guards=guards, operation='review_note')
        return {'ok': True, 'code': 'REVIEW_NOTED', 'chapterId': chapter_id, 'key': key, 'state': state}


def resolve_localization_body(root: Path, chapter_id: str) -> tuple[str | None, str]:
    """Existing body-selection helper; report delivery performs its own single read."""
    root = root.expanduser().resolve()
    with project_lock(root):
        sources = ReviewSources(root, chapter_id)
        selected = sources.body()
        sources.validate()
        return selected['text'], selected['kind']


def _report_view(sources: ReviewSources, *, body=_UNSET_BODY, body_kind=None) -> dict:
    chapter_id = sources.chapter_id
    try:
        report = sources.artifact('stage_b_review')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise StructureError('REVIEW_REPORT_UNREADABLE', 'registered stage B report is unavailable or invalid',
                             {'chapterId': chapter_id, 'cause': exception_reason(exc, 'REVIEW_ARTIFACT_INVALID')}) from exc
    if report is None:
        return {
            'ok': True, 'code': 'REVIEW_REPORT_ABSENT', 'chapterId': chapter_id,
            'report': None, 'localization': [], 'notes': {}, 'canAnnotate': False,
            'reviewedBodySha256': None, 'localizedAgainst': {'kind': 'none', 'bodySha256': None, 'verified': False},
            'versionBinding': {'known': False, 'matchesBody': None, 'current': False, 'state': 'absent',
                               'reason': 'no registered stage B report', 'reasons': []},
        }
    from .artifacts import sha256_text
    payload, report_sha = report['payload'], report['artifactSha256']
    selected = sources.body()
    selected_body, selected_kind = selected['text'], selected['kind']
    reasons = [*selected['reasons'], *sources.applicability(report, selected)]
    if body is _UNSET_BODY:
        body, body_kind = selected_body, selected_kind
    else:
        if body is not None and not isinstance(body, str):
            raise StructureError('REVIEW_BODY_INVALID', 'comparison body must be text or null')
        body_kind = selected_kind if body_kind is None else body_kind
    trusted = selected_body is not None and body == selected_body and body_kind == selected_kind
    if not trusted and body is not None:
        reasons.append(reason('REVIEW_BODY_COMPARISON_MISMATCH', 'supplied comparison is not the verified selected source'))
    reviewed_hash = payload.get('finalProseSha256')
    known = isinstance(reviewed_hash, str) and re.fullmatch(r'[0-9a-f]{64}', reviewed_hash) is not None
    body_hash = sha256_text(body) if body is not None else None
    matches = reviewed_hash == body_hash if known and body_hash is not None else None
    if not known:
        reasons.append(reason('REVIEW_BODY_BINDING_INVALID', 'report has no valid reviewed body digest'))
    if matches is False:
        reasons.append(reason('REVIEW_BODY_MISMATCH', 'the report and selected body are different versions'))
    current = trusted and matches is True and not reasons
    invalidated = bool(sources.entry('stage_b_review').get('invalidatedBy'))
    state = ('current' if current else
             'unavailable' if body is None or not known or selected['reasons']
             or any(row.get('unavailable') for row in reasons) else
             'historical' if invalidated else
             'mismatch' if matches is False or not trusted else 'historical')
    findings = payload.get('findings', [])
    if current:
        localization = localize_findings(findings, body)
    else:
        localization = [{**row, 'anchor': {**row['anchor'], 'state': 'historical', 'offsets': [], 'occurrences': 0}}
                        for row in localize_findings(findings, '')]
    notes, _ = _notes(sources)
    return {
        'ok': True, 'code': 'REVIEW_REPORT', 'chapterId': chapter_id,
        'report': {
            'artifactSha256': report_sha, 'verdict': payload.get('verdict'),
            'repairScope': payload.get('repairScope'), 'slots': payload.get('slots', {}), 'findings': findings,
        },
        'localization': localization,
        'notes': {key: row for key, row in notes['notes'].items() if key.startswith(report_sha + ':')},
        'reviewedBodySha256': reviewed_hash if known else None,
        'localizedAgainst': {'kind': body_kind, 'bodySha256': body_hash, 'verified': trusted},
        'versionBinding': {'known': known, 'matchesBody': matches, 'current': current, 'state': state,
                           'reason': '; '.join(row['message'] for row in reasons) or None, 'reasons': reasons},
        'canAnnotate': current,
    }


def review_report_view(root: Path, chapter_id: str, *, body=_UNSET_BODY, body_kind=None) -> dict[str, Any]:
    """One locked, version-checked snapshot for every report transport.

    An explicitly supplied comparison can only reduce applicability; it cannot
    claim a current source or grant annotation permission on its own.
    """
    root = root.expanduser().resolve()
    with project_lock(root):
        sources = ReviewSources(root, chapter_id)
        report = _report_view(sources, body=body, body_kind=body_kind)
        sources.validate()
        return report
