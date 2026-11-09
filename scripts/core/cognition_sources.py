"""Verify cognition citations against existing prose bindings and source inputs."""
from __future__ import annotations

from .artifacts import sha256_text
from .evidence_quotes import quote_span
from .review_artifacts import ReviewArtifactError
from .structure_store import ordered_chapters


def _error(code, message, **details):
    raise ReviewArtifactError(code, message, details)


def prior_chapter(source, chapter_id, cited_id):
    chapters = ordered_chapters(source)
    positions = {row['chapterId']: index for index, row in enumerate(chapters)}
    if cited_id not in positions or chapter_id not in positions or positions[cited_id] >= positions[chapter_id]:
        _error('COGNITION_PRIOR_CHAPTER_INVALID', 'select an earlier active chapter', chapterId=cited_id)
    chapter = chapters[positions[cited_id]]
    if chapter.get('status') != 'stable':
        _error('COGNITION_PRIOR_CHAPTER_UNSTABLE', 'prior prose must have stable accepted ownership', chapterId=cited_id)
    # Reader order is not story time. Refuse only a fully established reverse
    # ordering; missing endpoints remain the reviewer's explicit semantic task.
    current = chapters[positions[chapter_id]]
    if chapter.get('storyTimeStartId') and current.get('storyTimeEndId'):
        from .source_time import time_point_order
        relation = time_point_order(source, chapter['storyTimeStartId'], current['storyTimeEndId'])
        if relation['relation'] == 'after':
            _error('COGNITION_SOURCE_ORDER_INVALID', 'the cited earlier chapter occurs after this chapter in story time', chapterId=cited_id)
    return chapter


def _prior_body(root, source, chapter_id, citation, *, observe=None):
    from .prose_binding import OWNED_CLEAN, require_observation
    from .write_sets import capture_guards, safe_project_path, validate_guards
    chapter = prior_chapter(source, chapter_id, citation.get('chapterId'))
    guards = capture_guards(root, [chapter['filePath']])
    require_observation(root, source, chapter, (OWNED_CLEAN,), action='cognition evidence')
    raw = observe(chapter['filePath']) if observe else safe_project_path(root, chapter['filePath']).read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeError as exc:
        raise ReviewArtifactError('COGNITION_SOURCE_ENCODING', 'prior prose must be UTF-8') from exc
    binding = {'proseDigest': 'sha256:' + sha256_text(text),
               'bindingGeneration': chapter['proseBinding']['generation']}
    if chapter['proseBinding']['expectedContentDigest'] != binding['proseDigest']:
        _error('COGNITION_SOURCE_STALE', 'prior prose changed while reading its accepted bytes', chapterId=chapter['chapterId'])
    validate_guards(root, guards, stage='cognition-evidence')
    return text, binding, guards


def bind_prior_sources(root, source, chapter_id, payload, observe):
    """Fill mechanical citations, refusing an existing binding to another version."""
    from .cognition_review import validate_shape
    validate_shape(payload)
    for row in payload.get('cognitionReviews', []):
        if not isinstance(row, dict):
            continue
        for citation in row.get('sources', []) if isinstance(row.get('sources', []), list) else []:
            if not isinstance(citation, dict) or citation.get('kind') != 'prior_prose':
                continue
            text, binding, _ = _prior_body(root, source, chapter_id, citation, observe=observe)
            for key, value in binding.items():
                if key in citation and citation[key] != value:
                    _error('COGNITION_SOURCE_STALE', 'prior prose citation belongs to another accepted version',
                           field=key, chapterId=citation['chapterId'])
                citation[key] = value
            quote_span(text, citation.get('quote'))


def validate_sources(root, source, chapter_id, row, text, use):
    """Return file guards; exact text and ordering never establish semantic support."""
    from .source_model import SourceIndex
    from .source_plan import chapter_plan
    from .source_selection import time_match
    index = SourceIndex.build(source)
    chapter = index.objects[chapter_id]
    bound_ids = {entry['id'] for entry in chapter_plan(source, chapter_id)['records']}
    guards = []
    for citation in row['sources']:
        kind = citation['kind']
        if kind == 'current_prose':
            span = quote_span(text, citation['quote'])
            if span['end'] > use['start']:
                _error('COGNITION_SOURCE_ORDER_INVALID', 'same-chapter source must precede this information use; later narration needs a separately established basis', reviewId=row['id'])
        elif kind == 'prior_prose':
            prior_text, binding, observed = _prior_body(root, source, chapter_id, citation)
            if any(citation.get(key) != value for key, value in binding.items()):
                _error('COGNITION_SOURCE_STALE', 'prior prose citation needs its current accepted digest and generation',
                       reviewId=row['id'], chapterId=citation['chapterId'])
            quote_span(prior_text, citation['quote'])
            guards.extend(observed)
        else:
            rid = citation['fragmentId']
            fragment = index.fragments.get(rid)
            if (rid not in bound_ids or not fragment or fragment.get('status') == 'archived'):
                _error('COGNITION_SOURCE_UNBOUND', 'source evidence requires an active fragment in the chapter input closure', fragmentId=rid)
            if (fragment.get('contentRole') not in {'belief', 'in_world_text'}
                    or fragment.get('perspectiveId') != row['characterId']):
                _error('COGNITION_SOURCE_PERSPECTIVE', 'author facts and another character\'s beliefs do not establish this character\'s knowledge', fragmentId=rid)
            as_of = chapter.get('storyTimeStartId')
            period = fragment.get('validDuring') or {}
            if (period.get('fromId') or period.get('toId')) and (not as_of or time_match(index, fragment, as_of) != 'inside'):
                _error('COGNITION_SOURCE_TIME_UNKNOWN', 'source cognition must be established at this chapter\'s start, not future or unresolved time', fragmentId=rid, asOf=as_of)
            quote_span(fragment.get('text', ''), citation['quote'])
    return guards
