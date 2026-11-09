"""Compile effective multi-identity review seat plans without copying semantics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import file_sha256, sha256_json
from .project import TOOLBOX_ROOT

ALL_SEATS = tuple(range(1, 11))
CORE_CHAPTER_SEATS = (1, 2, 4, 5, 6, 7, 10)
TITLE_SEATS = (1, 2, 3, 6, 10)
DECISION_TYPES = ("design", "arc", "chapter", "title", "stage_b")
SOURCE_CONTRACT = "shared/contracts/G-MULTI-IDENTITY-REVIEW.md"


def chapter_stage_a_plan(structure: dict, chapter_id: str) -> dict:
    chapter = next(row for row in structure['chapters'] if row['chapterId'] == chapter_id)
    plan = resolve_effective_review_plan('gnovel-planner', 'chapter', chapter=chapter)
    from .behavior_review import blank_reviews
    if blank_reviews(structure, chapter_id) and 9 not in plan['activeSeats']:
        plan['activeSeats'] = sorted([*plan['activeSeats'], 9])
        plan['inactiveSeats'].remove(9)
        plan['inactiveReasons'].pop('9', None)
        plan['reviewPlanHash'] = sha256_json({k: v for k, v in plan.items() if k != 'reviewPlanHash'})
    return plan


def stage_a_completion_issues(structure: dict, chapter_id: str, payload: dict) -> list[dict]:
    """Independent seat errors share the enforcing gate; invalid parents stop descent."""
    if payload.get('decision') not in {'adopt', 'modify'}:
        return []
    from .review_artifacts import require_completed_text
    from .evidence_quotes import quote_span
    required = chapter_stage_a_plan(structure, chapter_id)['activeSeats']
    seats = payload.get('activeSeats')
    if (not isinstance(seats, list) or any(type(seat) is not int or seat not in ALL_SEATS for seat in seats)
            or len(set(seats)) != len(seats)):
        return [{'code': 'REVIEW_SEATS_INVALID', 'message': 'activeSeats must contain distinct valid seat numbers',
                 'field': 'activeSeats', 'notChecked': ['seatReviews']}]
    issues = []
    missing = sorted(set(required) - set(seats))
    if missing:
        issues.append({'code': 'REVIEW_SEATS_MISSING', 'message': 'provide all currently required review seats',
                       'missing': missing, 'field': 'activeSeats'})
    reviews = payload.get('seatReviews')
    if not isinstance(reviews, dict) or set(reviews) != {str(seat) for seat in seats}:
        issues.append({'code': 'STAGE_A_SEATS_REQUIRED',
            'message': 'every active seat requires its actual conclusion and plan quotations',
            'field': 'seatReviews', 'notChecked': ['seatReviews.*.conclusion', 'seatReviews.*.evidence']})
        return issues
    fragments = sorted((row for row in structure['fragments'] if row['ownerId'] == chapter_id
                        and row['contentRole'] == 'plan' and row.get('status') != 'archived'),
                       key=lambda row: (row['order'], row['fragmentId']))
    text = '\n\n'.join(row['text'] for row in fragments)
    for seat, row in reviews.items():
        if not isinstance(row, dict):
            issues.append({'code': 'STAGE_A_SEATS_INVALID', 'message': 'seat review must be an object',
                           'seat': seat, 'field': f'seatReviews.{seat}', 'notChecked': ['conclusion', 'evidence']})
            continue
        try:
            require_completed_text(row.get('conclusion'), f'seatReviews.{seat}.conclusion')
        except ValueError as exc:
            issues.append({'code': exc.code, 'message': str(exc), 'seat': seat, 'field': f'seatReviews.{seat}.conclusion'})
        quotes = row.get('evidence')
        if not isinstance(quotes, list) or not quotes:
            issues.append({'code': 'STAGE_A_SEAT_EVIDENCE_REQUIRED', 'message': 'each seat requires current plan quotations',
                           'seat': seat, 'field': f'seatReviews.{seat}.evidence'})
            continue
        for index, quote in enumerate(quotes):
            try:
                quote_span(text, quote)
            except ValueError as exc:
                issues.append({'code': exc.code, 'message': str(exc), **exc.details,
                    'artifactType': 'stage_a_review', 'seat': seat,
                    'field': f'seatReviews.{seat}.evidence[{index}]', 'quoteIndex': index,
                    'readAction': {'operation': 'source.chapter-plan', 'arguments': {'chapterId': chapter_id, 'view': 'detail'}}})
    return issues


def require_stage_a_completion(structure: dict, chapter_id: str, payload: dict) -> None:
    """Completion credit is identical for packaged and complete-envelope inputs."""
    from .structure_store import StructureError
    issues = stage_a_completion_issues(structure, chapter_id, payload)
    if issues:
        first = issues[0]
        raise StructureError(first['code'], first['message'],
            {**{k: v for k, v in first.items() if k not in {'code', 'message'}}, 'issues': issues})


class ReviewPlanError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def resolve_effective_review_plan(
    station: str,
    decision_type: str,
    *,
    chapter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return active seat indexes and contract evidence only.

    Seat duties, veto order, and output wording remain solely in the source contract.
    This object is an auditable load index, not another review rule document.
    """
    if decision_type not in DECISION_TYPES:
        raise ReviewPlanError("REVIEW_DECISION_TYPE_UNKNOWN", f"unsupported decision type: {decision_type}", {"supported": list(DECISION_TYPES)})
    chapter = chapter or {}
    reasons: dict[str, str] = {}
    auxiliary: list[str] = []
    if decision_type in {"design", "arc"}:
        active = list(ALL_SEATS)
        mode = "full"
    elif decision_type == "title":
        active = list(TITLE_SEATS)
        mode = "title-5"
        for seat in set(ALL_SEATS) - set(active):
            reasons[str(seat)] = "标题决策无该席位的结构判断对象"
    elif decision_type == "stage_b":
        active = list(ALL_SEATS)
        auxiliary = ["cognition", "organization"]
        mode = "stage-b-full"
    else:
        heavy = any(chapter.get(key) is True for key in ("heavyPlot", "volumeEnd", "arcEnd"))
        ordinary = chapter.get("reviewClass") == "ordinary" or all(
            chapter.get(key) is False for key in ("heavyPlot", "volumeEnd", "arcEnd")
        )
        if heavy or chapter.get("reviewClass") == "heavy" or not ordinary:
            active, mode = list(ALL_SEATS), "full"
        else:
            active, mode = list(CORE_CHAPTER_SEATS), "chapter-core-7"
            optional = {
                3: bool(chapter.get("newPlay")),
                8: bool(chapter.get("hookRisk") or chapter.get("volumeDebtRisk")),
                9: bool(chapter.get("newLocation") or chapter.get("newProp") or chapter.get("newSetting")),
            }
            for seat, enabled in optional.items():
                if enabled:
                    active.append(seat)
                else:
                    reasons[str(seat)] = {3: "本章无新增玩法", 8: "本章钩子与卷债务无新增风险", 9: "本章无新增地点、道具或设定"}[seat]
            active.sort()
    contract = TOOLBOX_ROOT / SOURCE_CONTRACT
    result = {
        "schemaVersion": 1,
        "station": station,
        "decisionType": decision_type,
        "chapterReviewClass": chapter.get("reviewClass", "unspecified"),
        "mode": mode,
        "activeSeats": active,
        "inactiveSeats": [seat for seat in ALL_SEATS if seat not in active],
        "inactiveReasons": reasons,
        "auxiliaryGates": auxiliary,
        "sourceContract": {"path": SOURCE_CONTRACT, "sha256": file_sha256(contract)},
        "semanticRulesEmbedded": False,
    }
    result["reviewPlanHash"] = sha256_json(result)
    return result
