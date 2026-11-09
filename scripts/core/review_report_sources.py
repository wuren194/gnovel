"""Observed review inputs; reading a stale artifact never grants it current credit."""
from __future__ import annotations

import json
from pathlib import Path

from .paths import stored_project_path
from .artifacts import sha256_text
from .paths import safe_project_path
from .review_artifacts import validate_artifact_binding
from .structure_store import StructureError, validate_structure
from .workflow import normalize_workflow
from .write_sets import guard_observation, validate_guards


def reason(code: str, message: str) -> dict:
    return {'code': code, 'message': message}


def exception_reason(exc: Exception, fallback: str) -> dict:
    return reason(getattr(exc, 'code', fallback), str(exc))


class ReviewSources:
    """Parse the same bytes kept as read-only guards, scoped to this chapter."""

    def __init__(self, root: Path, chapter_id: str, *, guard_formal_locators: bool = False):
        self.root = root
        self.chapter_id = chapter_id
        self.guard_formal_locators = guard_formal_locators
        self.observations: dict[str, dict] = {}
        self._artifacts: dict[str, dict | None] = {}
        self.structure = self.read_json('.govel/structure.json')
        validate_structure(self.structure)
        self.chapter = next((row for row in self.structure['chapters'] if row['chapterId'] == chapter_id), None)
        if self.chapter is None:
            raise StructureError('CHAPTER_NOT_FOUND', 'unknown review chapter', {'chapterId': chapter_id})
        self.workflow = normalize_workflow(self.read_json('.govel/workflow.json'))
        self.entries = self.workflow['artifacts'].get(chapter_id, {})
        if not isinstance(self.entries, dict):
            raise StructureError('WORKFLOW_STATE_INVALID', 'chapter artifacts must be an object')

    def observe(self, value: str | Path) -> tuple[bytes | None, dict]:
        path = safe_project_path(self.root, value, allow_absolute=True)
        relative = path.relative_to(self.root).as_posix()
        state, raw = guard_observation(path)
        observation = {'path': relative, **state}
        previous = self.observations.get(relative)
        if previous is not None and previous != observation:
            raise StructureError('REVIEW_INPUTS_CHANGED', 'review input changed while being read', {'path': relative})
        self.observations[relative] = observation
        return raw, observation

    def read_json(self, value: str | Path, *, optional: bool = False):
        raw, observation = self.observe(value)
        if raw is None:
            if optional and not observation['exists']:
                return None
            raise StructureError('REVIEW_INPUT_UNREADABLE', 'review input is not a readable regular file',
                                 {'path': observation['path']})
        return json.loads(raw)

    def validate(self, *, stage: str = 'review-read') -> None:
        validate_guards(self.root, list(self.observations.values()), stage=stage)

    def entry(self, kind: str) -> dict | None:
        if kind not in self.entries:
            return None
        entry = self.entries[kind]
        if not isinstance(entry, dict):
            raise StructureError('REVIEW_ARTIFACT_INVALID', 'registered artifact reference must be an object',
                                 {'artifactType': kind})
        return entry

    def artifact(self, kind: str) -> dict | None:
        if kind in self._artifacts:
            return self._artifacts[kind]
        entry = self.entry(kind)
        if entry is None:
            self._artifacts[kind] = None
            return None
        path = entry.get('artifactPath')
        if not isinstance(path, str) or not path:
            raise StructureError('REVIEW_ARTIFACT_INVALID', 'registered artifact has no path', {'artifactType': kind})
        raw, observation = self.observe(stored_project_path(self.root, path))
        if raw is None:
            raise StructureError('REVIEW_ARTIFACT_INVALID', 'registered artifact file is unavailable',
                                 {'artifactType': kind, 'path': observation['path']})
        artifact = json.loads(raw)
        validate_artifact_binding(artifact, project_id=self.structure['projectId'], chapter_id=self.chapter_id)
        if (artifact['artifactType'] != kind or artifact['artifactSha256'] != entry.get('artifactSha256')
                or observation['sha256'] != entry.get('artifactFileSha256')):
            raise StructureError('REVIEW_ARTIFACT_INVALID', 'registered artifact differs from its observed file',
                                 {'artifactType': kind, 'path': observation['path']})
        self._artifacts[kind] = artifact
        return artifact

    def body(self) -> dict:
        """The chosen source cannot silently fall back when it is damaged."""
        kind = 'none'
        try:
            from .current_candidate import selected_candidate
            selected = selected_candidate(self.root, self.structure, self.chapter_id, observe=self.observe)
            if selected['selection'] is not None:
                kind = 'active_candidate'
                return {'text': selected['text'], 'kind': kind, 'reasons': [], 'selection': selected['selection']}
            if self.entry('candidate_prose') is not None:
                kind = 'candidate_prose'
                candidate = self.artifact('candidate_prose')
                return {'text': candidate['payload']['text'], 'kind': kind, 'reasons': []}
            kind = 'formal_prose'
            from .prose_binding import OWNED_CLEAN, require_observation
            # Formal ownership checks physical aliasing against every reserved
            # locator. These are short-lived read guards, not chapter inputs or
            # recovery outputs; a concurrent topology change must veto the note.
            if self.guard_formal_locators:
                for chapter in self.structure['chapters']:
                    self.observe(chapter['filePath'])
            raw, observation = self.observe(self.chapter['filePath'])
            ownership = require_observation(self.root, self.structure, self.chapter, (OWNED_CLEAN,),
                                            action='read review localization prose')
            if raw is None or ownership['observedContentDigest'] != 'sha256:' + str(observation['sha256']):
                raise StructureError('REVIEW_INPUTS_CHANGED', 'formal prose changed during ownership validation')
            return {'text': raw.decode('utf-8'), 'kind': kind, 'reasons': []}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return {'text': None, 'kind': kind, 'reasons': [exception_reason(exc, 'REVIEW_BODY_UNAVAILABLE')]}

    def applicability(self, report: dict, selected: dict) -> list[dict]:
        """Verify retained explicit bindings; this is not finalization preflight."""
        reasons = []
        entry = self.entry('stage_b_review')
        if entry.get('invalidatedBy'):
            reasons.append(reason('REVIEW_INVALIDATED', 'the registered report was invalidated by changed inputs'))
        if self.chapter.get('status') == 'archived':
            reasons.append(reason('CHAPTER_ARCHIVED', 'the chapter is archived'))
        payload = report['payload']
        try:
            from .source_plan import chapter_plan
            plan_hash = chapter_plan(self.structure, self.chapter_id)['sourcePlanHash']
            if payload.get('sourcePlanHash') != plan_hash:
                reasons.append(reason('REVIEW_PLAN_STALE', 'the review belongs to a different source plan'))
            if selected['kind'] == 'formal_prose' and selected['text'] is not None:
                from .continuity import finalization_evidence
                evidence = finalization_evidence(self.root, self.structure, self.workflow, self.chapter)
                if evidence['factReview']['artifactSha256'] != report['artifactSha256']:
                    reasons.append(reason('REVIEW_FINALIZATION_MISMATCH', 'this report is not the review of the current formal prose'))
            elif selected['text'] is not None:
                candidate = self.artifact('candidate_prose')
                if candidate is None:
                    reasons.append({**reason('REVIEW_CANDIDATE_UNAVAILABLE', 'no registered candidate proves the review input'),
                                    'unavailable': True})
                else:
                    candidate_entry = self.entry('candidate_prose')
                    if candidate_entry.get('invalidatedBy'):
                        reasons.append(reason('CANDIDATE_INVALIDATED', 'the registered candidate was invalidated'))
                    from .writing_notes import validate_candidate_binding
                    # The existing validator may read A; capture that exact file
                    # too so a later note cannot outlive a changed input artifact.
                    if candidate['payload'].get('stageAArtifactSha256'):
                        if self.artifact('stage_a_review') is None:
                            raise StructureError('REVIEW_INPUT_UNAVAILABLE', 'the candidate stage A artifact is not registered')
                    validate_candidate_binding(self.root, self.structure, self.workflow,
                                               self.chapter_id, candidate['payload'])
                    if payload.get('finalProseSha256') != sha256_text(candidate['payload']['text']):
                        reasons.append(reason('REVIEW_CANDIDATE_MISMATCH', 'the report reviewed a different registered candidate body'))
                    expected = payload.get('candidateArtifactSha256')
                    if expected is not None and expected != candidate['artifactSha256']:
                        reasons.append(reason('REVIEW_CANDIDATE_MISMATCH', 'the complete candidate artifact differs from the reviewed one'))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            stale = getattr(exc, 'code', '') in {'CANDIDATE_PLAN_STALE', 'CANDIDATE_STAGE_A_STALE', 'CURRENT_CANDIDATE_UNALIGNED', 'CANDIDATE_SELECTION_REVIEW_REQUIRED'}
            reasons.append({**exception_reason(exc, 'REVIEW_INPUT_UNAVAILABLE'), 'unavailable': not stale})
        # Diagnostics integrity is an independent input. A stale candidate plan
        # must not short-circuit it and hide an unavailable proof behind history.
        try:
            if payload.get('diagnosticsArtifactSha256') and selected['kind'] != 'formal_prose':
                diagnostics = self.artifact('diagnostics')
                if diagnostics is None:
                    reasons.append({**reason('REVIEW_DIAGNOSTICS_UNAVAILABLE', 'the reviewed diagnostics artifact is not registered'),
                                    'unavailable': True})
                elif (self.entry('diagnostics').get('invalidatedBy')
                      or diagnostics['artifactSha256'] != payload['diagnosticsArtifactSha256']):
                    reasons.append(reason('REVIEW_DIAGNOSTICS_STALE', 'the review refers to different or invalidated diagnostics'))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            reasons.append({**exception_reason(exc, 'REVIEW_INPUT_UNAVAILABLE'), 'unavailable': True})
        return reasons
