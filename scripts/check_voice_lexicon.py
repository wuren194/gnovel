#!/usr/bin/env python3
"""Mechanical slice of the voice-signature check (G-VOICE-SIGNATURE §2.4).

OOC is a semantic judgement and stays with the Editor.  Two parts of it are not:

1. Per-character banned lexicon.  StoryDesign characters carry
   ``voiceProfile.bannedPhrasesForRole``; a dialogue line attributed to that
   character containing one of those phrases is a mechanical fact, not a taste
   call.
2. Globally banned AI interjections (該死/可恶/天哪 …), listed in
   shared/registries/text-patterns.json under ``voice.globalBannedInterjections``.

Speaker attribution uses a limited grammar, not literary understanding. Explicit
speech tags support a blocking private-lexicon finding; action or turn-taking
context only supports an advisory. Names mentioned in dialogue are never speaker
evidence. Unknown attribution remains visible in coverage for the Editor.

Usage:
    python3 scripts/check_voice_lexicon.py <prose.md> --design <story-design.json> --json
    python3 scripts/check_voice_lexicon.py <prose.md> --json      # global rules only
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

try:
    from scripts import state_cache
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
except ImportError:  # 以 `python3 scripts/xxx.py` 直接运行时
    import state_cache
    from core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser


ROOT = Path(__file__).resolve().parents[1]
TEXT_PATTERNS_PATH = ROOT / "shared" / "registries" / "text-patterns.json"

#: Quoted dialogue in the shapes this corpus actually uses.
QUOTE_RE = re.compile(r"[「“\"]([^」”\"]+)[」”\"]")
#: Existing bound on narrative context; context never crosses a dialogue turn.
ATTRIBUTION_LOOKBACK = 3


class Finding(NamedTuple):
    line: int
    rule_id: str
    severity: str
    character_id: str | None
    speaker_confidence: str
    phrase: str
    quote: str
    speaker_certain: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "ruleId": self.rule_id,
            "severity": self.severity,
            "characterId": self.character_id,
            "speakerConfidence": self.speaker_confidence,
            "speakerCertain": self.speaker_certain,
            "phrase": self.phrase,
            "quote": self.quote,
        }


class VoiceLexiconError(RuntimeError):
    """Input could not be read or is structurally unusable."""


def _voice_settings() -> dict[str, Any]:
    try:
        from scripts.core.data_provider import registry
    except ImportError:
        from core.data_provider import registry
    try:
        data = registry('text-patterns', TEXT_PATTERNS_PATH)
    except (OSError, ValueError) as exc:
        raise VoiceLexiconError(f"无法加载 {TEXT_PATTERNS_PATH}: {exc}") from exc
    voice = data.get("voice") if isinstance(data, dict) else None
    if not isinstance(voice, dict):
        raise VoiceLexiconError("text-patterns 缺少 voice 对象")
    return voice


def load_global_banned() -> list[str]:
    return list(_voice_settings().get("globalBannedInterjections", []))


def load_characters(design_path: Path | None) -> list[dict[str, Any]]:
    """Extract (characterId, name, bannedPhrases) triples from a StoryDesign."""
    if design_path is None:
        return []
    try:
        design = json.loads(design_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoiceLexiconError(f"无法读取 StoryDesign {design_path}: {exc}") from exc
    characters = design.get("characters")
    if not isinstance(characters, list):
        raise VoiceLexiconError("StoryDesign 缺少 characters 数组")
    return _characters_from_design(characters)


def _characters_from_design(characters: list) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in characters:
        if not isinstance(item, dict):
            continue
        profile = item.get("voiceProfile") or {}
        banned = profile.get("bannedPhrasesForRole") or []
        result.append({
            "characterId": item.get("characterId", ""),
            "name": str(item.get("name", "")).strip(),
            "bannedPhrases": [str(phrase) for phrase in banned if str(phrase).strip()],
            "lexiconSource": item.get('lexiconSource', 'structure.voiceProfile'),
        })
    return result


def characters_for_project(root: Path) -> list[dict]:
    try:
        from scripts.core.structure_store import load_structure
    except ImportError:
        from core.structure_store import load_structure
    structure = load_structure(root)
    characters = _characters_from_design(state_cache.build_design(root, structure=structure)['characters'])
    return characters


class Attribution(NamedTuple):
    line: int
    quote: str
    character_id: str | None
    confidence: str

    @property
    def speaker_certain(self) -> bool:
        return self.character_id is not None and self.confidence in {"line", "speech-tag"}

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "quote": self.quote, "characterId": self.character_id,
                "confidence": self.confidence, "speakerCertain": self.speaker_certain}


class AttributionGrammar:
    """Small positive grammar: unsupported narration never becomes a speech tag."""

    def __init__(self, characters: list[dict[str, Any]]) -> None:
        settings = _voice_settings().get("attribution")
        if not isinstance(settings, dict):
            raise VoiceLexiconError("text-patterns.voice 缺少 attribution 语法")
        tokens = {}
        for key in ("speechVerbs", "speechModifiers", "speakerPrefixes", "listenerPrefixes", "actionPrefixes"):
            values = settings.get(key)
            if (not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)):
                raise VoiceLexiconError(f"voice.attribution.{key} 必须是非空词表")
            tokens[key] = "(?:" + "|".join(re.escape(word) for word in sorted(values, key=len, reverse=True)) + ")"
        self.named = [item for item in characters if item["name"]]
        names = "(?:" + "|".join(re.escape(item["name"]) for item in
                                  sorted(self.named, key=lambda row: len(row["name"]), reverse=True)) + ")"
        if not self.named:
            names = r"(?!)"
        subject = rf"{tokens['speakerPrefixes']}?(?P<speaker>{names})"
        modifiers = tokens['speechModifiers'] + "*"
        listener = tokens['listenerPrefixes']
        self.speech = re.compile(rf"{subject}{modifiers}(?:{listener}{names}{modifiers})?"
                                 rf"{tokens['speechVerbs']}(?:{listener}?{names})?")
        self.action = re.compile(rf"{subject}{tokens['actionPrefixes']}")

    def names_in(self, text: str) -> set[str]:
        return {item["characterId"] for item in self.named if item["name"] in text}

    def _id(self, name: str) -> str | None:
        ids = {item["characterId"] for item in self.named if item["name"] == name}
        return next(iter(ids)) if len(ids) == 1 else None

    def speech_id(self, clause: str) -> str | None:
        match = self.speech.fullmatch(clause)
        return self._id(match['speaker']) if match else None

    def action_id(self, clause: str) -> str | None:
        match = self.action.match(clause)
        # A second named participant makes action-only context ambiguous.
        return self._id(match['speaker']) if match and len(self.names_in(clause)) == 1 else None


def _clause(fragment: str, *, before: bool) -> str:
    """Use only the clause adjoining this quote, not names elsewhere on its line."""
    clauses = re.split(r"[。.!！？?；;，,]", fragment.strip().strip("：:。.!！？?；;，, "))
    return (clauses[-1] if before else clauses[0]).strip().strip("：:")


def _quote_neighbors(line: str, quotes: list[re.Match], index: int) -> tuple[str, str]:
    quote = quotes[index]
    before = line[quotes[index - 1].end() if index else 0:quote.start()]
    after = line[quote.end():quotes[index + 1].start() if index + 1 < len(quotes) else len(line)]
    # In “甲。”乙说：“乙。”, the intervening tag belongs to the next quote.
    if (index + 1 < len(quotes) and after.rstrip().endswith((":", "："))
            and not re.search(r"[。.!！？?；;]", after)):
        after = ""
    # Conversely, “甲。”甲说。“乙。” has a postfix tag for the previous quote.
    if index and before.rstrip().endswith(tuple("。.!！？?；;")):
        before = ""
    return before, after


def _local_speaker(before: str, after: str, grammar: AttributionGrammar) -> tuple[str | None, str]:
    clauses = [_clause(before, before=True), _clause(after, before=False)]
    explicit = {cid for clause in clauses if (cid := grammar.speech_id(clause)) is not None}
    if explicit:
        return (next(iter(explicit)), "line") if len(explicit) == 1 else (None, "ambiguous")
    tentative = {cid for clause in clauses if (cid := grammar.action_id(clause)) is not None}
    if tentative:
        return (next(iter(tentative)), "line-action") if len(tentative) == 1 else (None, "ambiguous")
    names = grammar.names_in(" ".join(clauses))
    return None, "ambiguous" if len(names) > 1 else "unattributed"


def _previous_speaker(history: list[str], grammar: AttributionGrammar) -> tuple[str | None, str]:
    for offset, earlier in enumerate(reversed(history[-ATTRIBUTION_LOOKBACK:]), start=1):
        if not earlier.strip():
            continue
        clause = _clause(earlier, before=True)
        explicit = grammar.speech_id(clause)
        if explicit is not None and earlier.rstrip().endswith((":", "：")):
            return explicit, "speech-tag"
        tentative = grammar.action_id(clause)
        if tentative is not None:
            return tentative, f"lookback-{offset}"
        # Never search through unrelated narration or a name used as an object.
        return None, "ambiguous" if len(grammar.names_in(clause)) > 1 else "unattributed"
    return None, "unattributed"


def _has_other_narration(fragment: str, grammar: AttributionGrammar) -> bool:
    text = fragment.strip().strip("：:。.!！？?；;，, ")
    return bool(text) and grammar.speech_id(text) is None


def attribute_dialogue(text: str, characters: list[dict[str, Any]]) -> list[Attribution]:
    """One attribution stream shared by findings and coverage; no quoted-name lookback."""
    grammar = AttributionGrammar(characters)
    attributions: list[Attribution] = []
    history: list[str] = []
    speakers: set[str] = set()
    last_speaker = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("#", "```", "|", ">")):
            history.clear()
            speakers.clear()
            last_speaker = None
            continue
        quotes = list(QUOTE_RE.finditer(line))
        if not quotes:
            history.append(stripped)
            history = history[-ATTRIBUTION_LOOKBACK:]
            if stripped:
                # Turn-taking is valid only for uninterrupted established turns.
                speakers.clear()
                last_speaker = None
            continue
        for index, quote in enumerate(quotes):
            before, after = _quote_neighbors(line, quotes, index)
            if _has_other_narration(before, grammar):
                speakers.clear()
                last_speaker = None
            cid, confidence = _local_speaker(before, after, grammar)
            if cid is None and confidence != "ambiguous" and not (before.strip() or after.strip()):
                cid, confidence = _previous_speaker(history, grammar)
                if cid is None and not any(history) and len(speakers) == 2 and last_speaker in speakers:
                    cid, confidence = next(iter(speakers - {last_speaker})), "turn-taking"
            if cid is None and confidence == "unattributed" and grammar.names_in(quote.group(1)):
                confidence = "mention-only"
            row = Attribution(number, quote.group(1), cid, confidence)
            attributions.append(row)
            history.clear()
            if row.speaker_certain:
                speakers.add(cid)
            elif confidence != "turn-taking":
                speakers.clear()
            last_speaker = cid
            if _has_other_narration(after, grammar):
                speakers.clear()
                last_speaker = None
    return attributions


def _findings(attributions: list[Attribution], characters: list[dict[str, Any]], global_banned: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    by_id = {item["characterId"]: item for item in characters}
    for row in attributions:
        for phrase in global_banned:
            if phrase in row.quote:
                findings.append(Finding(row.line, "VOICE-GLOBAL-INTERJECTION", "error", row.character_id,
                                        row.confidence, phrase, row.quote, row.speaker_certain))
        profile = by_id.get(row.character_id)
        if profile:
            for phrase in profile["bannedPhrases"]:
                if phrase in row.quote:
                    findings.append(Finding(row.line, "OOC_VOICE_DRIFT", "error" if row.speaker_certain else "warning",
                                            row.character_id, row.confidence, phrase, row.quote, row.speaker_certain))
    return sorted(findings, key=lambda item: (item.line, item.rule_id, item.phrase))


def scan(text: str, characters: list[dict[str, Any]], global_banned: list[str]) -> list[Finding]:
    return _findings(attribute_dialogue(text, characters), characters, global_banned)


def build_report(text: str, characters: list[dict[str, Any]], global_banned: list[str]) -> dict[str, Any]:
    attributions = attribute_dialogue(text, characters)
    findings = _findings(attributions, characters, global_banned)
    confident = [item for item in findings if item.speaker_certain]
    return {
        "schemaVersion": 1,
        "tool": "voice-lexicon-check",
        "limitations": [
            "lexical-only",
            "no-subtext-proof",
            "no-syntax-proof",
            "speaker-attribution-is-heuristic",
        ],
        "metrics": {
            "findingCount": len(findings),
            "confidentFindingCount": len(confident),
            "charactersWithBannedLexicon": sum(1 for item in characters if item["bannedPhrases"]),
        },
        "recommendation": ("surgical_edit" if any(item.severity == "error" for item in findings)
                           else "review_advisory" if findings else "pass"),
        "findings": [item.as_dict() for item in findings],
        "coverage": {'characters': [{'characterId': row['characterId'], 'name': row['name'],
                        'bannedPhraseCount': len(row['bannedPhrases']), 'source': row.get('lexiconSource', 'provided')}
                       for row in characters], 'globalBannedPhraseCount': len(global_banned),
                     'dialogueAttributions': [row.as_dict() for row in attributions],
                     'unattributedCount': sum(row.character_id is None for row in attributions)},
    }


def main() -> int:
    parser = FailClosedArgumentParser(description="Voice-signature lexical check (mechanical slice of OOC review)")
    parser.add_argument("file_path", type=Path, help="Prose markdown or text file")
    parser.add_argument("--design", type=Path, help="StoryDesign JSON providing voiceProfile.bannedPhrasesForRole")
    parser.add_argument("--project-root", type=Path, help="项目根：从当前 structure.json 的人物源记录读取声纹")
    parser.add_argument("--json", action="store_true", help="Output the stable JSON report")
    parser.add_argument("--output", type=Path, help="Write the report to a file")
    args = parser.parse_args()
    try:
        text = args.file_path.read_text(encoding="utf-8")
        if args.project_root:
            characters = characters_for_project(args.project_root)
        else:
            characters = load_characters(args.design)
        global_banned = load_global_banned()
        report = build_report(text, characters, global_banned)
    except (OSError, VoiceLexiconError, state_cache.StateCacheError) as exc:
        print(f"无法执行声纹词面检查: {exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    if args.json or not args.output:
        print(serialized)
    return exit_code_for_recommendation(report["recommendation"])


if __name__ == "__main__":
    raise SystemExit(main())
