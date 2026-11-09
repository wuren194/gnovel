"""Deterministic, bounded retrieval of curated writing knowledge.

Round 2 (Post-R1 Audit, Knowledge Retrieval P1): applicability/exclusions take
part in filtering, weak body-only evidence no longer fills result slots, and
excerpts return the matched Markdown section instead of the head of the file.
"""
from __future__ import annotations

import re

from .artifacts import sha256_json
from .knowledge_index import asset_token_sets, index_state
from .knowledge_store import list_assets, read_asset
from .project import knowledge_policy, resolve_knowledge_mode, on_demand_asset_paths
from .structure_store import StructureError

HAN = re.compile(r'[\u4e00-\u9fff]')
HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
SENTENCE_ENDINGS = '。！？；!?;…\n'


def _query_tokens(query: str) -> list[str]:
    """Query vocabulary: ASCII words and whole CJK runs plus their bigrams."""
    lower = query.casefold()
    words = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', lower)
    tokens = set(words)
    for word in words:
        if HAN.search(word):
            tokens.update(word[index:index + 2] for index in range(len(word) - 1))
    return sorted(tokens)


def _matched_intents(query: str, aliases: dict[str, list[str]]) -> set[str]:
    """Match a canonical intent key or one of its aliases as a contiguous phrase.
    Reordered phrasings (推进关系) are carried as explicit aliases, never as a
    character-set overlap: a set-subsumption test fires on unrelated queries whose
    characters merely happen to be scattered across the text (推进系统关系图)."""
    lower = query.casefold()
    matched = set()
    for key, stored in aliases.items():
        if key.casefold() in lower or any(alias.casefold() in lower for alias in stored):
            matched.add(key)
    return matched


def _hit(token: str, text: str) -> bool:
    """ASCII tokens match whole words; CJK tokens match as substrings."""
    if token.isascii():
        return re.search(rf'(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])', text) is not None
    return token in text


def _token_frequencies(tokens: list[str], token_sets: dict[str, set[str]]) -> dict[str, int]:
    """Document frequency of each query token over active asset bodies."""
    return {token: sum(token in body for body in token_sets.values()) for token in tokens}


def _evidence_units(text: str, tokens: list[str]) -> list[str]:
    """Maximal covered spans: merge overlapping/touching CJK matches, dedupe
    repeated phrases. ASCII whole words remain separate. Gaps and punctuation
    split units. No segmentation model or index changes are involved."""
    spans = []
    for token in tokens:
        pattern = (rf'(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])'
                   if token.isascii() else re.escape(token))
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, text))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sorted({text[start:end] for start, end in merged})


def _exclusion_units(query: str, exclusions: str, unclaimed: list[str], prefixes: list[str]) -> list[str]:
    units = _evidence_units(exclusions, unclaimed)
    # A matching adoption prefix may itself be covered by a boundary bigram.
    # Separate it from the object before deduplication, longest prefix first.
    for index, unit in enumerate(units):
        prefix = next((p for p in sorted(prefixes, key=lambda p: (-len(p), p))
                       if unit.startswith(p) and len(unit) > len(p)), None)
        if prefix:
            units[index] = unit[len(prefix):]
    units = sorted(set(units))
    # Explicit adoption is independent of merely mentioning the excluded phrase.
    # It must immediately precede that exact unclaimed unit (e.g. 按照阶段模板).
    adoption = ['use:' + unit for unit in units
                if any(prefix + unit in query for prefix in prefixes)]
    return units + adoption


def route_knowledge(*, planningIntent: str, limit: int | None = None,
                    station: str | None = None, kind: str | None = None,
                    mode: str | None = None) -> dict:
    policy = knowledge_policy()
    mode = resolve_knowledge_mode(mode)
    max_results = policy['strategy']['broadMaxResults'] if mode == 'broad' else policy['maxResults']
    if not isinstance(planningIntent, str) or not planningIntent.strip():
        raise StructureError('KNOWLEDGE_INTENT_INVALID', 'nonempty planningIntent required')
    if len(planningIntent) > policy['maxIntentChars']:
        raise StructureError('KNOWLEDGE_INTENT_TOO_LARGE', 'planningIntent exceeds the configured budget')
    limit = max_results if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= max_results:
        raise StructureError('KNOWLEDGE_LIMIT_INVALID', 'invalid knowledge result limit')
    if mode == 'off':
        return {'ok': True, 'code': 'KNOWLEDGE_ROUTE', 'planningIntent': planningIntent,
                'station': station, 'knowledgeMode': mode, 'mode': 'off',
                'normalizedIntents': [], 'items': [], 'matchedCount': 0, 'hasMore': False,
                'coverage': {'source': 'disabled', 'rawCorpusLoaded': False, 'sectionTextInjected': False}}
    from .knowledge_store import STATIONS, KINDS
    if (station is not None and station not in STATIONS) or (kind is not None and kind not in KINDS):
        raise StructureError('KNOWLEDGE_FILTER_INVALID', 'invalid knowledge filter')
    all_rows = list_assets()['items']
    direct_references = on_demand_asset_paths()
    catalog = {'items': [row for row in all_rows if row['status'] == 'active'
                        and (station is None or station in row['stations'])
                        and (kind is None or row['kind'] == kind) and row['path'] not in direct_references]}
    intents = _matched_intents(planningIntent, policy['intents'])
    tokens = [token for token in _query_tokens(planningIntent) if len(token) >= 2]
    token_sets = asset_token_sets(all_rows)
    if token_sets is not None:
        eligible = {row['assetId'] for row in all_rows if row['path'] not in direct_references}
        token_sets = {key: value for key, value in token_sets.items() if key in eligible}
    # Generic tokens (present in more than `genericDfMax` active bodies) and the
    # curated weak-token list (genre words like 设计/规范 whose document frequency
    # looks topical but whose semantics do not discriminate) carry no signal: they
    # cannot make a candidate, add score, or prevent a veto.
    frequencies = _token_frequencies(tokens, token_sets) if token_sets is not None else {}
    weak = set(policy['weakTokens'])
    evidence = [token for token in tokens
                if token not in weak and frequencies.get(token, 0) <= policy['genericDfMax']]

    positive_evidence = (evidence if intents else
                         [token for token in evidence if token not in policy['lexicalWeakTokens']])
    weights = policy['rankingWeights']
    scored = []
    for row in catalog['items']:
        shared = sorted(intents & set(row['intents']))
        if mode == 'curated' and intents and not shared:
            continue
        title = row['title'].casefold()
        tags = ' '.join(row['tags']).casefold()
        applicability = row['applicability'].strip().casefold()
        exclusions = row['exclusions'].strip().casefold()
        title_hits = [token for token in positive_evidence if _hit(token, title)]
        tag_hits = [token for token in positive_evidence if not _hit(token, title) and _hit(token, tags)]
        body_hits = []
        if token_sets is not None:
            body = token_sets.get(row['assetId'], set())
            body_hits = [token for token in positive_evidence if token in body]
        applicability_hits = [token for token in positive_evidence if applicability and _hit(token, applicability)]
        if (not shared and not title_hits and not tag_hits and not applicability_hits
                and len(body_hits) < policy['bodyOnlyMinTokens']):
            continue
        if exclusions:
            # Tokens already promised by the asset's own title/tags are its declared
            # topic, not evidence that the query wants the excluded thing. Only the
            # remaining (unclaimed) overlap can veto a candidate.
            unclaimed = [token for token in evidence
                         if _hit(token, exclusions)
                         and not _hit(token, title) and not _hit(token, tags)]
            if len(_exclusion_units(planningIntent.casefold(), exclusions, unclaimed,
                                    policy['exclusionUsePrefixes'])) >= policy['exclusionsMinUnclaimedOverlap']:
                continue
        title_units = _evidence_units(title, title_hits)
        tag_units = _evidence_units(tags, tag_hits)
        if mode == 'broad' and intents and not shared:
            # Cross-intent expansion requires a complete, discriminating phrase;
            # an incidental bigram (推进/阶段) cannot bypass intent filtering.
            strong = [unit for unit in title_units + tag_units
                      if unit not in policy['lexicalWeakTokens']
                      and (unit.isascii() or len(unit) > 2)
                      and _hit(unit, planningIntent.casefold())]
            if not strong:
                continue
        if not shared:
            scattered = [unit for unit in title_units + tag_units
                         if not _hit(unit, planningIntent.casefold())]
            # A scattered title concept needs a genuinely different span in the
            # applicability/body channel, not a second copy of its fragments.
            corroboration = (_evidence_units(applicability, applicability_hits)
                             + _evidence_units(planningIntent.casefold(), body_hits))
            if scattered:
                independent = [other for other in corroboration
                               if not any(_hit(token, other) and _hit(token, unit)
                                          for token in evidence for unit in scattered)
                               and not any(_hit(token, other) and _hit(token, exclusions)
                                           for token in evidence)]
                if not independent:
                    continue
        def phrase_score(units, weight):
            # A verbatim multi-bigram phrase is one strong evidence unit. Reuse
            # the existing strong-evidence weight; no new numeric threshold.
            return sum(weights['intent'] if len(unit) > 2 and not unit.isascii()
                       and _hit(unit, planningIntent.casefold()) else weight for unit in units)
        score = (len(shared) * weights['intent']
                 + (weights['tag'] if row['intents'] and row['intents'][0] in shared else 0)
                 + phrase_score(title_units, weights['title'])
                 + phrase_score(tag_units, weights['tag'])
                 + len(body_hits) * weights['body'] + len(applicability_hits) * weights['applicability'])
        if score < policy['minScore']:
            continue
        if not (row['applicability'].strip() and row['exclusions'].strip()):
            # Legacy assets (metadata predates the applicability/exclusions review)
            # stay retrievable on the same evidence bar, but rank below fully
            # reviewed assets of equal relevance.
            score -= policy['legacyMetadataPenalty']
        scored.append({**row, 'score': score, 'matchedIntents': shared,
                       'matchedTerms': sorted(title_hits + tag_hits)})
    scored.sort(key=lambda row: (-row['score'], row['assetId']))
    items, seen = [], set()
    for row in scored:
        if row['assetId'] in seen:
            continue
        seen.add(row['assetId'])
        items.append(row)
        if len(items) == limit:
            break
    return {'ok': True, 'code': 'KNOWLEDGE_ROUTE', 'planningIntent': planningIntent,
            'station': station, 'knowledgeMode': mode, 'normalizedIntents': sorted(intents),
            'mode': 'intent' if intents else 'lexical_fallback',
            'items': items, 'matchedCount': len(scored), 'hasMore': len(scored) > len(items),
            'coverage': {'source': 'knowledge/assets active only', 'rawCorpusLoaded': False,
                         'sectionTextInjected': False, 'index': index_state(all_rows)}}


def _section_blocks(lines: list[str]) -> list[dict]:
    """Markdown sections: heading line plus body until the next heading of the same
    or a higher level (subsections stay inside their parent section)."""
    headings = []
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    if not headings:
        return [{'start': 0, 'level': 0, 'heading': '', 'ownEnd': len(lines), 'end': len(lines)}]
    starts = [index for index, _, _ in headings]
    blocks = []
    for position, (index, level, heading) in enumerate(headings):
        own_end = starts[position + 1] if position + 1 < len(headings) else len(lines)
        end = len(lines)
        for later, later_level, _ in headings[position + 1:]:
            if later_level <= level:
                end = later
                break
        blocks.append({'start': index, 'level': level, 'heading': heading,
                       'ownEnd': own_end, 'end': end})
    return blocks


def _select_block(blocks: list[dict], tokens: list[str], lines: list[str]) -> dict:
    """Highest-relevance section by token hits in its own text (heading hits count
    twice). Ties prefer the shallower section, which then contains its subsections.
    With no hit at all the first heading keeps the document order stable."""
    best, best_key = None, None
    for block in blocks:
        own = ''.join(lines[block['start']:block['ownEnd']]).casefold()
        heading = block['heading'].casefold()
        own_hits = {token for token in tokens if token in own}
        heading_hits = {token for token in tokens if token in heading}
        score = len(own_hits) + len(heading_hits)
        if score == 0:
            continue
        key = (-score, block['level'], block['start'])
        if best_key is None or key < best_key:
            best, best_key = block, key
    return blocks[0] if best is None else best


def _sentence_prefix(text: str, room: int) -> str:
    best = ''
    for index, char in enumerate(text[:room]):
        if char in SENTENCE_ENDINGS:
            best = text[:index + 1]
    return best


def _truncate(text: str, budget: int) -> str:
    """Truncate on paragraph boundaries, then sentence boundaries, then hard."""
    if len(text) <= budget:
        return text
    paragraphs = re.split(r'\n\s*\n', text)
    kept, used = [], 0
    for paragraph in paragraphs:
        separator = 2 if kept else 0
        if used + separator + len(paragraph) <= budget:
            kept.append(paragraph)
            used += separator + len(paragraph)
            continue
        piece = _sentence_prefix(paragraph, budget - used - separator)
        if piece:
            kept.append(piece)
        break
    if not kept:
        return _sentence_prefix(text, budget) or text[:budget]
    return '\n\n'.join(kept)


def build_knowledge_pack(*, planningIntent: str, station: str, limit: int | None = None,
                         mode: str | None = None) -> dict:
    mode = resolve_knowledge_mode(mode)
    route = route_knowledge(planningIntent=planningIntent, station=station, limit=limit, mode=mode)
    budget = knowledge_policy()['maxExcerptChars']
    tokens = [token for token in _query_tokens(planningIntent) if len(token) >= 2]
    items = []
    omitted = 0
    remaining = knowledge_policy()['strategy']['broadMaxPackChars'] if mode == 'broad' else None
    if station != 'router':
        for selected in route['items']:
            document = read_asset(selected['assetId'], expected_hash=selected['sha256'])
            body = document['body'].strip()
            lines = body.splitlines(keepends=True)
            block = _select_block(_section_blocks(lines), tokens, lines)
            section = ''.join(lines[block['start']:block['end']]).strip()
            if remaining is not None and remaining <= 0:
                omitted += 1
                continue
            excerpt = _truncate(section, budget if remaining is None else min(budget, remaining))
            if remaining is not None:
                remaining -= len(excerpt)
            heading = (('#' * block['level']) + ' ' + block['heading']).strip() if block['heading'] else ''
            items.append({'assetId': selected['assetId'], 'title': selected['title'],
                          'path': selected['path'], 'sha256': selected['sha256'],
                          'source': selected['source'], 'role': 'REFERENCE KNOWLEDGE',
                          'excerpt': excerpt, 'sectionHeading': heading, 'bodyLineStart': block['start'] + 1,
                          'truncated': len(excerpt) < len(section),
                          'matchedIntents': selected['matchedIntents']})
    return {'ok': True, 'code': 'KNOWLEDGE_PACK', 'role': 'REFERENCE KNOWLEDGE',
            'mode': mode, 'policyVersion': knowledge_policy()['strategy']['version'],
            'policyHash': sha256_json(knowledge_policy()),
            'omittedBudget': omitted, 'items': items, 'route': route}
