"""Actual semantic inputs of the diagnostic runner, separate from write CAS.

The lexical scripts read the candidate and toolbox policies. The voice checker
consumes the same character projection below; chapter audit consumes the scoped
source plan and AGENTS constraints. Full run snapshots remain in the artifact.
"""
from .artifacts import sha256_json
from .source_plan import chapter_plan
from .write_sets import file_state, safe_project_path


def semantic_inputs(root, structure, chapter_id, prose, full_inputs):
    from .writing_checks import text_module
    state_cache = text_module('state_cache')
    voice = text_module('check_voice_lexicon')
    characters = voice._characters_from_design(state_cache._build_design(structure)['characters'])
    return {'purpose': 'diagnostic-consumer-inputs-v1', 'projectId': structure['projectId'],
            'chapterId': chapter_id, 'sourcePlanHash': chapter_plan(structure, chapter_id)['sourcePlanHash'],
            'candidateArtifactSha256': prose['artifactSha256'],
            'candidateSelection': prose.get('candidateSelection'),
            'draftSha256': full_inputs['draftSha256'], 'proseSource': prose['source'],
            'voiceCharacters': characters, 'agents': file_state(safe_project_path(root, 'AGENTS.md')),
            'implementation': full_inputs['implementation'],
            'settingsSchemaSha256': full_inputs['settingsSchemaSha256']}


def stored_input_proof(payload):
    original, semantic = payload.get('runInputs'), payload.get('semanticInputs')
    if not isinstance(original, dict) or not isinstance(semantic, dict):
        return False
    return (semantic.get('purpose') == 'diagnostic-consumer-inputs-v1'
            and payload.get('inputHash') == sha256_json({**original, **payload['run']})
            and payload.get('semanticInputHash') == sha256_json({**semantic, **payload['run']}))
