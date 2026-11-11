"""Project marker and workflow policy loading."""

from __future__ import annotations

import math
from pathlib import Path

from .jsonio import read_json
from .static_cache import cached_derived, cached_json

TOOLBOX_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = TOOLBOX_ROOT / "toolbox.manifest.json"


def toolbox_manifest() -> dict:
    data = cached_json(MANIFEST_PATH)
    if not isinstance(data, dict):
        raise ValueError("toolbox.manifest.json 顶层必须是对象")
    return data


def world_render_policy() -> dict:
    """Browser pacing settings; generated JS is bound to this registry by a test."""
    relative = toolbox_manifest().get("sharedProtocols", {}).get("worldRenderPolicy")
    if not relative:
        raise ValueError("manifest 缺少 sharedProtocols.worldRenderPolicy")
    policy = cached_json(TOOLBOX_ROOT / relative)
    fields = {"activeFps", "idleFps", "interactionTailMs", "frameToleranceMs",
              "maxDeltaSeconds", "walkSettleEpsilon", "exposure"}
    if (not isinstance(policy, dict) or type(policy.get("schemaVersion")) is not int
            or policy["schemaVersion"] != 1 or set(policy) != fields | {"schemaVersion"}):
        raise ValueError("world render policy 结构或版本无效")
    if any(type(policy[key]) not in (int, float) or not math.isfinite(policy[key]) or policy[key] <= 0 for key in fields):
        raise ValueError("world render policy 参数必须为有限正数")
    if policy["idleFps"] > policy["activeFps"] or policy["frameToleranceMs"] >= 1000 / policy["activeFps"]:
        raise ValueError("world render policy 帧率或时间容差无效")
    return {key: value for key, value in policy.items() if key != "schemaVersion"}


def garden_atmosphere_policy() -> dict:
    """Validate the garden art-direction registry before exporting it to the browser."""
    relative = toolbox_manifest().get("sharedProtocols", {}).get("gardenAtmospherePolicy")
    if not relative:
        raise ValueError("manifest 缺少 sharedProtocols.gardenAtmospherePolicy")
    policy = cached_json(TOOLBOX_ROOT / relative)

    def number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def positive(value):
        return number(value) and value > 0

    def count(value):
        return type(value) is int and value > 0

    def ratio(value):
        return number(value) and 0 <= value <= 1

    def color(value):
        return (isinstance(value, str) and len(value) == 7 and value[0] == "#"
                and all(char in "0123456789abcdefABCDEF" for char in value[1:]))

    def interval(value):
        return isinstance(value, list) and len(value) == 2 and all(positive(v) for v in value) and value[0] <= value[1]

    def vector(value):
        return isinstance(value, list) and len(value) == 3 and all(number(v) for v in value) and any(value)

    def columns(value):
        return (isinstance(value, list) and bool(value) and all(type(v) is int and v >= 0 for v in value)
                and len(set(value)) == len(value))

    schema = {
        "lighting": {**dict.fromkeys(("sunColor", "skyColor", "groundColor", "fogColor"), color),
                     **dict.fromkeys(("sunIntensity", "ambientIntensity", "environmentIntensity", "fogDensity", "shadowExtent"), positive),
                     "sunPosition": vector, "environmentSize": count, "shadowSize": count},
        "sky": dict.fromkeys(("zenith", "horizon", "sunset", "ground"), color),
        "palette": dict.fromkeys(("stone", "leaf", "youngLeaf", "deepLeaf", "stem", "flower", "flowerCenter"), color),
        "pavilion": {"columnCount": count, "firstColumnAngle": number,
                     **dict.fromkeys(("columnRadius", "columnBaseRadius", "shaftRadius", "shaftBase", "entablatureTop", "corniceRadius", "railHeight"), positive)},
        "planting": {**dict.fromkeys(("treeCount", "innerShrubCount", "outerShrubCount", "stemsPerColumn", "drapeCount"), count),
                     **dict.fromkeys(("treeRadius", "treeHeight", "treeWidth", "innerShrubRadius", "outerShrubRadius", "leafSize", "flowerScale"), interval),
                     **dict.fromkeys(("leavesPerMeter", "windAmplitude", "windSpeed"), positive),
                     "columnIndices": columns, "flowerChance": ratio, "balancedRatio": ratio, "cypressRatio": ratio},
        "effects": {**dict.fromkeys(("stoneEmission", "vegetationEmissionGain", "leafBounce", "foliageSaturation", "sigilGain", "mistOpacity", "moteGain", "pendantTrailGain"), ratio),
                    "mistColor": color},
    }
    if (not isinstance(policy, dict) or type(policy.get("schemaVersion")) is not int or policy["schemaVersion"] != 1
            or set(policy) != {"schemaVersion", "seed", *schema} or not count(policy.get("seed"))):
        raise ValueError("garden atmosphere policy 结构或版本无效")
    for section, fields in schema.items():
        values = policy[section]
        if not isinstance(values, dict) or set(values) != set(fields):
            raise ValueError(f"garden atmosphere policy {section} 字段无效")
        for key, validate in fields.items():
            if not validate(values[key]):
                raise ValueError(f"garden atmosphere policy {section}.{key} 参数无效")
    if max(policy["planting"]["columnIndices"]) >= policy["pavilion"]["columnCount"]:
        raise ValueError("garden atmosphere policy 柱号超出模型范围")
    return {key: value for key, value in policy.items() if key != "schemaVersion"}


def workflow_policy() -> dict:
    manifest = toolbox_manifest()
    relative = manifest.get("sharedProtocols", {}).get("workflowPolicy")
    if not relative:
        raise ValueError("manifest 缺少 sharedProtocols.workflowPolicy")
    data = cached_json(TOOLBOX_ROOT / relative)
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise ValueError("workflow policy 不存在或版本不受支持")
    repair = data.get("repair")
    if not isinstance(repair, dict) or not isinstance(repair.get("maxAutomaticRounds"), int) or repair["maxAutomaticRounds"] < 0:
        raise ValueError("workflow policy 缺少有效 repair.maxAutomaticRounds")
    approval = data.get("approval")
    if not isinstance(approval, dict) or not isinstance(approval.get("extremeLevelAlwaysManual"), bool):
        raise ValueError("workflow policy 缺少 approval.extremeLevelAlwaysManual")
    return data


def local_ci_policy() -> dict:
    """Local test resource limits; separate from novel workflow policy."""
    policy = cached_json(TOOLBOX_ROOT / 'shared/registries/local-ci.json')
    if (not isinstance(policy, dict) or set(policy) != {'schemaVersion', 'defaultMaxWorkers'}
            or type(policy.get('schemaVersion')) is not int or policy['schemaVersion'] != 1
            or type(policy.get('defaultMaxWorkers')) is not int or policy['defaultMaxWorkers'] < 1):
        raise ValueError('local CI policy 需要有效版本和正整数 defaultMaxWorkers')
    return {'defaultMaxWorkers': policy['defaultMaxWorkers']}


def user_workflow_policy() -> dict:
    """Presentation aliases only; never project lifecycle state."""
    policy = workflow_policy().get("userWorkflow")
    if not isinstance(policy, dict) or not isinstance(policy.get("stages"), dict):
        raise ValueError("workflow policy 缺少 userWorkflow.stages")
    from .task_prepare import TASK_STATIONS
    from .workflow import ACTIONS
    tasks = set()
    for alias, row in policy["stages"].items():
        if not isinstance(alias, str) or not alias or alias in TASK_STATIONS or alias == "continue" or not isinstance(row, dict):
            raise ValueError("userWorkflow stage alias 无效")
        if not isinstance(row.get("label"), str) or not row["label"].strip() or not isinstance(row.get("subtasks"), list):
            raise ValueError("userWorkflow stage label/subtasks 无效")
        members = [row.get("task"), *row["subtasks"]]
        if not all(isinstance(task, str) and task in TASK_STATIONS for task in members):
            raise ValueError("userWorkflow stage task/label 无效")
        if tasks.intersection(members) or len(set(members)) != len(members):
            raise ValueError("userWorkflow task 必须只有一个展示阶段")
        tasks.update(members)
    actions = policy.get("actionTasks")
    if tasks != set(TASK_STATIONS) or not isinstance(actions, dict) or set(actions) != ACTIONS:
        raise ValueError("userWorkflow 必须覆盖所有 task/action")
    if any(task is not None and (not isinstance(task, str) or task not in TASK_STATIONS) for task in actions.values()):
        raise ValueError("userWorkflow action task 无效")
    return policy


def repair_policy() -> dict:
    repair = workflow_policy().get("repair")
    if not isinstance(repair, dict) or not isinstance(repair.get("maxAutomaticRounds"), int) or repair["maxAutomaticRounds"] < 0:
        raise ValueError("workflow policy 缺少有效 repair.maxAutomaticRounds")
    return {"maxAutomaticRounds": repair["maxAutomaticRounds"]}


def review_policy() -> dict:
    policy = workflow_policy().get("review")
    values = policy.get("unfinishedConclusions") if isinstance(policy, dict) else None
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(set(values)) != len(values)):
        raise ValueError("review.unfinishedConclusions 必须是非空、去重的占位词表")
    return {"unfinishedConclusions": list(values)}


def continuation_policy() -> dict:
    policy = workflow_policy().get("continuation")
    if not isinstance(policy, dict):
        raise ValueError("workflow policy 缺少 continuation")
    threshold = policy.get("staleSummaryThreshold")
    budgets = policy.get("memoryBudgets")
    if type(threshold) is not int or threshold < 1:
        raise ValueError("continuation policy 字段无效")
    tasks = {"planner", "writer", "editor", "query"}
    if not isinstance(budgets, dict) or set(budgets) != tasks:
        raise ValueError("continuation.memoryBudgets 必须包含 planner/writer/editor/query")
    if not all(type(value) is int and value > 0 for value in budgets.values()):
        raise ValueError("continuation.memoryBudgets 必须是正整数（不接受布尔值）")
    if policy.get("memoryBudgetUnit") != "serializedCharacters":
        raise ValueError("continuation.memoryBudgetUnit 必须是 serializedCharacters")
    return {"staleSummaryThreshold": threshold,
            "memoryBudgets": dict(budgets), "memoryBudgetUnit": policy["memoryBudgetUnit"]}


def chapter_length_bounds() -> tuple[int, int]:
    """章节字数区间的唯一真源，避免脚本各自硬编码同一阈值。"""
    length = workflow_policy().get("chapterLength")
    if not isinstance(length, dict):
        raise ValueError("workflow policy 缺少 chapterLength")
    low, high = length.get("minHanCharacters"), length.get("maxHanCharacters")
    if not isinstance(low, int) or not isinstance(high, int) or low <= 0 or high < low:
        raise ValueError("chapterLength 必须是正整数区间且 max >= min")
    return low, high


def diagnostic_policy() -> dict:
    """Numeric prose diagnostics policy shared by checks and their rule card."""
    import re
    from .data_provider import registry
    policy = registry('text-patterns', TOOLBOX_ROOT / 'shared/registries/text-patterns.json')['diagnosticPolicy']
    minimum = policy.get('proseQuality', {}).get('minScore')
    if type(minimum) is not int or not 1 <= minimum <= 100:
        raise ValueError('diagnosticPolicy.proseQuality.minScore must be an integer from 1 to 100')
    if policy['proseQuality'].get('severity') not in {'info', 'fail'}:
        raise ValueError('diagnosticPolicy.proseQuality.severity must be info or fail')
    lengths, sensory, talk = (policy[k] for k in ('sentenceLength', 'sensoryDensity', 'dialogueDynamics'))
    numbers = [*lengths['byMode'].values(), lengths['stickyCap'], lengths['unpunctuatedCap'],
               sensory['minPerKHan'], sensory['minChannels'], sensory['minTextLength'],
               talk['windowSize'], talk['minLevel'], *talk['perLevel'].values(), talk['defaultPerWindow']]
    if (any(type(n) not in (int, float) or n <= 0 for n in numbers)
            or set(lengths['byMode']) != {'plot', 'buildup', 'mixed', 'peakscene'}
            or lengths['lengthSeverity'] not in {'info', 'fail'}
            or sensory['severity'] not in {'info', 'fail'}
            or not 0 < policy['paragraphRatio']['max'] < 1):
        raise ValueError('diagnosticPolicy contains invalid thresholds or severities')
    for pattern in sensory['channels'].values():
        re.compile(pattern)
    re.compile(policy['factSignals'])
    for key in ('coreActionPattern', 'stickyPattern', 'sensoryStayPattern'):
        re.compile(policy['sceneDynamics'][key])
    if any(type(policy['sceneDynamics'][key]) is not int or policy['sceneDynamics'][key] <= 0
           for key in ('minIntensityLevel', 'minNarrativeLength', 'stickyMinCommas', 'sensoryStayMinLength', 'stickyMinLength')):
        raise ValueError('diagnosticPolicy.sceneDynamics contains invalid thresholds')
    if (not isinstance(talk['shortTiers'], list) or not talk['shortTiers']
            or any(type(row.get(key)) is not int or row[key] <= 0 for row in talk['shortTiers'] for key in ('belowTextSize', 'required'))):
        raise ValueError('diagnosticPolicy.dialogueDynamics.shortTiers is invalid')
    return policy


def call_logging_policy() -> dict:
    """User-local call diagnostics; independent of prose and project readiness."""
    return cached_derived(TOOLBOX_ROOT / "shared/registries/call-logging.json",
                          _validated_call_logging_policy)


def _validated_call_logging_policy(policy: object) -> dict:
    import re
    from .call_log_redaction import credential_exception_key
    counts = {"inlinePayloadBytes", "maxPayloadBytes", "maxMetadataBytes", "maxStoreBytes", "retentionDays",
              "maxCalls", "defaultQueryLimit", "maxQueryLimit", "maxDepth"}
    times = {"lockTimeoutSeconds", "lockRetrySeconds"}
    fields = counts | times | {"schemaVersion", "defaultMode", "credentialFields", "stringCredentialFields",
                              "credentialValueExceptions", "credentialTextPatterns"}
    if (not isinstance(policy, dict) or set(policy) != fields
            or type(policy.get("schemaVersion")) is not int or policy["schemaVersion"] != 1
            or policy.get("defaultMode") not in {"off", "summary", "full"}):
        raise ValueError("invalid call logging policy structure")
    if any(type(policy[key]) is not int or policy[key] < 1 for key in counts):
        raise ValueError("call logging limits must be positive integers")
    if any(type(policy[key]) not in (int, float) or not math.isfinite(policy[key]) or policy[key] <= 0
           for key in times):
        raise ValueError("call logging lock times must be finite positive numbers")
    if (policy["inlinePayloadBytes"] > policy["maxPayloadBytes"]
            or policy["maxMetadataBytes"] > policy["maxPayloadBytes"]
            or policy["maxPayloadBytes"] > policy["maxStoreBytes"]
            or policy["defaultQueryLimit"] > policy["maxQueryLimit"]
            or policy["lockRetrySeconds"] > policy["lockTimeoutSeconds"]):
        raise ValueError("call logging defaults exceed their limits")
    if not isinstance(policy["credentialFields"], list) or not isinstance(policy["stringCredentialFields"], list):
        raise ValueError("invalid call logging credential fields")
    credentials = [*policy["credentialFields"], *policy["stringCredentialFields"]]
    if (not policy["credentialFields"]
            or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key)
                   for key in credentials)):
        raise ValueError("invalid call logging credential fields")
    normalized = [re.sub(r"[^a-z0-9]", "", key.casefold()) for key in credentials]
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate call logging credential fields")
    exceptions = policy["credentialValueExceptions"]
    if not isinstance(exceptions, dict):
        raise ValueError("call logging credential value exceptions must be an object")
    exception_fields = set()
    for field, values in exceptions.items():
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", field):
            raise ValueError("invalid call logging credential exception field")
        field = re.sub(r"[^a-z0-9]", "", field.casefold())
        if field not in normalized or field in exception_fields:
            raise ValueError("unknown or duplicate call logging credential exception field")
        exception_fields.add(field)
        if not isinstance(values, list) or not values:
            raise ValueError("call logging credential exceptions must list exact JSON values")
        seen = set()
        for value in values:
            try:
                encoded = credential_exception_key(value)
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise ValueError("invalid call logging credential exception JSON") from exc
            if encoded in seen:
                raise ValueError("duplicate call logging credential exception value")
            seen.add(encoded)
    patterns = policy["credentialTextPatterns"]
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("call logging credential patterns are required")
    for row in patterns:
        if (not isinstance(row, dict) or set(row) != {"pattern", "replacement"}
                or not isinstance(row["pattern"], str) or not row["pattern"]
                or not isinstance(row["replacement"], str)):
            raise ValueError("invalid call logging credential pattern")
        try:
            re.compile(row["pattern"]).sub(row["replacement"], "")
        except re.error as exc:
            raise ValueError("invalid call logging credential expression") from exc
    return policy


def project_template_path(key: str) -> Path:
    """按 manifest 声明解析项目模板路径；模板缺失时 fail closed。"""
    templates = toolbox_manifest().get("sharedProtocols", {}).get("projectTemplates", {})
    relative = templates.get(key)
    if not relative:
        raise ValueError(f"manifest 缺少 sharedProtocols.projectTemplates.{key}")
    return TOOLBOX_ROOT / relative


def default_approval_mode() -> str:
    """新项目的默认审批模式，真源是注册表而非 argparse 默认值。"""
    approval = workflow_policy().get("approval")
    if not isinstance(approval, dict):
        raise ValueError("workflow policy 缺少 approval")
    mode = approval.get("defaultMode")
    if mode not in {"manual", "auto"}:
        raise ValueError("approval.defaultMode 必须是 manual 或 auto")
    return mode


def query_policy() -> dict:
    policy = workflow_policy().get("query")
    if not isinstance(policy, dict):
        raise ValueError("workflow query policy is required")
    for key in ("defaultLimit", "maxLimit", "defaultDepth", "maxDepth", "searchContextCharacters"):
        if type(policy.get(key)) is not int or policy[key] < 1:
            raise ValueError(f"invalid query.{key}")
    if policy["defaultLimit"] > policy["maxLimit"] or policy["defaultDepth"] > policy["maxDepth"]:
        raise ValueError("query defaults exceed maximums")
    if set(policy.get("maxBytes", {})) != {"overview", "schema", "list", "detail", "traverse"} or any(
            type(v) is not int or v < 1 for v in policy["maxBytes"].values()):
        raise ValueError("invalid query.maxBytes")
    return policy


def tool_discovery_policy() -> dict:
    relative = toolbox_manifest()['sharedProtocols']['toolRegistry']
    policy = cached_json(TOOLBOX_ROOT / relative).get('discovery')
    if not isinstance(policy, dict) or set(policy) != {'maxResults', 'maxQueryCharacters', 'intents'}:
        raise ValueError('tool discovery policy is invalid')
    if any(type(policy[key]) is not int or policy[key] < 1 for key in ('maxResults', 'maxQueryCharacters')):
        raise ValueError('tool discovery limits must be positive integers')
    intents = policy['intents']
    if not isinstance(intents, dict) or not intents:
        raise ValueError('tool discovery intents are required')
    for operation, groups in intents.items():
        if (not isinstance(operation, str) or not operation.strip() or not isinstance(groups, list) or not groups
                or any(not isinstance(group, list) or not group
                       or any(not isinstance(term, str) or not term.strip() for term in group)
                       for group in groups)):
            raise ValueError('tool discovery terms must name an operation and nonempty term groups')
    return policy


def knowledge_modes() -> list[str]:
    """Public protocol choices remain discoverable before opening user data."""
    modes = cached_json(TOOLBOX_ROOT / 'shared/registries/knowledge.json')['strategy']['modes']
    if (not isinstance(modes, list) or not modes
            or any(not isinstance(mode, str) or not mode for mode in modes)
            or len(set(modes)) != len(modes)):
        raise ValueError('invalid public knowledge mode declaration')
    return modes


def knowledge_policy() -> dict:
    """Validated policy for curated writing knowledge; values have runtime consumers."""
    from .data_provider import registry
    policy = registry('knowledge', TOOLBOX_ROOT / 'shared/registries/knowledge.json')
    positives = ('maxResults', 'maxExcerptChars', 'maxIntentChars', 'minScore',
                 'bodyOnlyMinTokens', 'genericDfMax', 'exclusionsMinUnclaimedOverlap')
    if (not isinstance(policy, dict)
            or set(policy) != {*positives, 'legacyMetadataPenalty', 'weakTokens', 'lexicalWeakTokens', 'exclusionUsePrefixes', 'rankingWeights', 'intents', 'strategy'}
            or any(type(policy[key]) is not int or policy[key] < 1 for key in positives)
            or type(policy['legacyMetadataPenalty']) is not int or policy['legacyMetadataPenalty'] < 0
            or not isinstance(policy['lexicalWeakTokens'], list)
            or any(not isinstance(token, str) or not token.strip()
                   for token in policy['lexicalWeakTokens'])
            or not isinstance(policy['exclusionUsePrefixes'], list) or not policy['exclusionUsePrefixes']
            or any(not isinstance(prefix, str) or not prefix.strip()
                   for prefix in policy['exclusionUsePrefixes'])
            or not isinstance(policy['weakTokens'], list)
            or any(not isinstance(token, str) or not token for token in policy['weakTokens'])
            or not isinstance(policy['rankingWeights'], dict)
            or set(policy['rankingWeights']) != {'intent', 'title', 'tag', 'body', 'applicability'}
            or any(type(value) is not int or value < 1 for value in policy['rankingWeights'].values())
            or not isinstance(policy['intents'], dict) or not policy['intents']
            or any(not isinstance(key, str) or not key or not isinstance(words, list) or not words
                   or any(not isinstance(word, str) or not word for word in words)
                   for key, words in policy['intents'].items())):
        raise ValueError('invalid knowledge policy')
    strategy = policy['strategy']
    if (not isinstance(strategy, dict)
            or set(strategy) != {'defaultMode', 'modes', 'version', 'broadMaxResults', 'broadMaxPackChars'}
            or strategy['modes'] != ['off', 'broad', 'curated']
            or strategy['defaultMode'] not in strategy['modes']
            or not isinstance(strategy['version'], str) or not strategy['version'].strip()
            or any(type(strategy[k]) is not int or strategy[k] < 1
                   for k in ('broadMaxResults', 'broadMaxPackChars'))):
        raise ValueError('invalid knowledge strategy policy')
    return policy


def resolve_knowledge_mode(override: str | None = None, *, project_root: Path | None = None) -> str:
    """Task override > project marker knowledgeMode > registry default; no environment fallback."""
    from .structure_store import StructureError
    strategy = knowledge_policy()['strategy']
    setting = None
    if project_root is not None:
        try:
            marker = project_marker(Path(project_root))
        except FileNotFoundError:
            # Knowledge also has toolbox-only entry points with no project marker.
            # A broken marker symlink is corrupt configuration, not an absent setting.
            if project_marker_path(Path(project_root)).is_symlink():
                raise
            marker = {}
        if 'knowledgeMode' in marker:
            setting = marker['knowledgeMode']
            if not isinstance(setting, str) or setting not in strategy['modes']:
                raise StructureError('KNOWLEDGE_MODE_INVALID', 'invalid project knowledgeMode')
    mode = override if override is not None else setting if setting is not None else strategy['defaultMode']
    if not isinstance(mode, str) or mode not in strategy['modes']:
        raise StructureError('KNOWLEDGE_MODE_INVALID', 'knowledgeMode must be off, broad or curated')
    return mode


def source_model_policy() -> dict:
    """Single-source storage versions, identities and supported value semantics.

    Per-record validation reads this hundreds of times per structure, so the
    registry is parsed and validated once per file change, not once per call.
    """
    return cached_derived(TOOLBOX_ROOT / cached_derived(MANIFEST_PATH, _source_model_path),
                          _validated_source_model)


def pair_policy() -> dict:
    policy = source_model_policy()
    return {**policy['pairClassification'], 'idSuffixPattern': policy['idSuffixPattern'],
            'recordStatuses': policy['recordStatuses']}


def _source_model_path(manifest: object) -> str:
    if not isinstance(manifest, dict):
        raise ValueError("toolbox.manifest.json 顶层必须是对象")
    return manifest["sharedProtocols"]["sourceModel"]


def _validated_source_model(policy: object) -> dict:
    if not isinstance(policy, dict):
        raise ValueError("source model policy must be an object")
    binding = policy.get("proseBinding", {})
    if (not isinstance(binding, dict) or binding.get("format") != "PROSE_BINDING_V1"
            or binding.get("states") != ["unbound", "managed", "unresolved"]):
        raise ValueError("invalid prose binding registry (format/states are frozen by contract)")
    import re
    pattern = policy.get('idSuffixPattern')
    if not isinstance(pattern, str) or not pattern:
        raise ValueError('source model needs an ID suffix pattern')
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError('invalid source ID suffix pattern') from exc
    import uuid
    suffix_length = policy.get('generatedSuffixLength')
    if (type(suffix_length) is not int or not 0 < suffix_length <= len(uuid.UUID(int=0).hex)
            or not re.fullmatch(pattern, '0' * suffix_length)):
        raise ValueError('invalid generated source ID suffix length')
    collections = policy.get('collections')
    if not isinstance(collections, dict) or not collections:
        raise ValueError('source model needs collection specifications')
    ids, prefixes = set(), set()
    for name in ('entityPrefixes', 'objectPrefixes', 'fragmentPrefixes', 'receiptPrefixes'):
        mapping = policy.get(name)
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError('source model needs typed prefixes: ' + name)
        for kind, prefix in mapping.items():
            if (not isinstance(kind, str) or not isinstance(prefix, str)
                    or not re.fullmatch('[a-z]+', prefix) or prefix in prefixes):
                raise ValueError('invalid or duplicate source prefix: ' + name)
            prefixes.add(prefix)
    if set(policy['fragmentPrefixes']) != set(policy['contentRoles']):
        raise ValueError('fragment prefixes must cover every content role')
    for name, spec in collections.items():
        expected = {'idField'} if name == 'fragments' else {'idField', 'prefix'}
        if not isinstance(name, str) or not isinstance(spec, dict) or set(spec) != expected:
            raise ValueError('invalid source collection specification')
        key = spec['idField']
        if not isinstance(key, str) or not key.endswith('Id') or key in ids:
            raise ValueError('invalid or duplicate source collection identity')
        ids.add(key)
        if name != 'fragments':
            prefix = spec['prefix']
            if not isinstance(prefix, str) or not re.fullmatch('[a-z]+', prefix) or prefix in prefixes:
                raise ValueError('invalid or duplicate source collection prefix')
            prefixes.add(prefix)
    prefix = policy.get('valueIdPrefix')
    if not isinstance(prefix, str) or not prefix.isascii() or not prefix.isalnum() or prefix in prefixes:
        raise ValueError('invalid source value identity prefix')
    prefixes.add(prefix)
    for name in ('planningCandidates', 'authorDecisions'):
        spec = policy.get(name)
        expected = {'idPrefix', 'statuses', 'maxSplitParts', 'maxMergeSources'} if name == 'planningCandidates' else {'idPrefix', 'statuses'}
        if (not isinstance(spec, dict) or set(spec) != expected
                or not isinstance(spec['idPrefix'], str) or not spec['idPrefix'].isascii()
                or not spec['idPrefix'].isalnum() or spec['idPrefix'] in prefixes
                or not isinstance(spec['statuses'], list) or not spec['statuses']
                or any(not isinstance(status, str) or not status for status in spec['statuses'])
                or len(set(spec['statuses'])) != len(spec['statuses'])):
            raise ValueError('invalid source model planning identity: ' + name)
        if name == 'planningCandidates' and any(type(spec[key]) is not int or spec[key] < 2
                                                for key in ('maxSplitParts', 'maxMergeSources')):
            raise ValueError('invalid planning candidate transformation limits')
        prefixes.add(spec['idPrefix'])
    for name in ('valueTypes', 'valueStates', 'contentRoles', 'recordStatuses', 'presentationKinds',
                 'ruleMethods', 'ruleEnforcements', 'ruleActions', 'ruleIssueStatuses',
                 'timeSpecKinds', 'knowledgeModes', 'placementScopes', 'unitMemberTypes',
                 'profileTopics', 'recallTopics', 'cognitionAttitudes', 'checkStatuses'):
        values = policy.get(name)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values) or len(values) != len(set(values)):
            raise ValueError('invalid source model enumeration: ' + name)
    # Placement shapes and unit member handling are hard-coded per scope/type, so the
    # registry truth must match what the code knows how to validate (fail closed on drift).
    if set(policy['placementScopes']) != {'volume', 'project'}:
        raise ValueError('placementScopes must be exactly volume and project')
    if set(policy['unitMemberTypes']) != {'chapter', 'planning_candidate'}:
        raise ValueError('unitMemberTypes must be exactly chapter and planning_candidate')
    pairs = policy.get('pairClassification')
    if (not isinstance(pairs, dict) or set(pairs) != {'idPrefix', 'assessmentStates', 'classificationStates'}
            or not isinstance(pairs.get('idPrefix'), str) or not pairs['idPrefix'].isascii()
            or not pairs['idPrefix'].isalnum() or pairs['idPrefix'] in prefixes
            or pairs.get('assessmentStates') != ['unassessed', 'assessed', 'not_applicable']
            or pairs.get('classificationStates') != ['unassessed', 'unknown', 'known', 'not_applicable']):
        raise ValueError('invalid pair classification policy; states are bound to applicability semantics')
    return policy


def protected_paths() -> list[str]:
    """受保护路径的唯一真源。manifest 侧的同名清单必须与之一致，由测试锁定。"""
    paths = workflow_policy().get("protectedPaths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        raise ValueError("protectedPaths 必须是非空字符串列表")
    return list(paths)


def serial_planning_policy() -> dict:
    """串行规划开关。字段缺失即 fail closed，不静默按宽松值放行。"""
    policy = workflow_policy().get("serialPlanning")
    if not isinstance(policy, dict):
        raise ValueError("workflow policy 缺少 serialPlanning")
    for key in ("chapterRangeRequired", "prewarmNextChapter", "currentVolumeOnly"):
        if not isinstance(policy.get(key), bool):
            raise ValueError(f"serialPlanning.{key} 必须是布尔值")
    return dict(policy)


def project_marker_path(root: Path) -> Path:
    return root.expanduser().resolve() / ".govel" / "project.json"


def project_marker(root: Path) -> dict:
    data = read_json(project_marker_path(root))
    if not isinstance(data, dict):
        raise ValueError("project marker 顶层必须是对象")
    return data


def supported_schema_versions() -> list[int]:
    """项目 schema 版本的唯一真源：manifest 的 projectVersionPolicy.supportedSchemaVersions。"""
    versions = toolbox_manifest().get("projectVersionPolicy", {}).get("supportedSchemaVersions")
    if not isinstance(versions, list) or not versions or not all(type(v) is int and v > 0 for v in versions):
        raise ValueError("manifest projectVersionPolicy.supportedSchemaVersions 必须是非空正整数列表")
    return list(versions)


def current_schema_version() -> int:
    """当前（唯一受支持写入的）schema 版本。多版本支持未实现，列表必须恰含一个元素。"""
    versions = supported_schema_versions()
    if len(versions) != 1:
        raise ValueError("仅支持单一 schema 版本；supportedSchemaVersions 必须恰含一个元素")
    return versions[0]


def unsupported_error_code() -> str:
    """旧版本项目报错码的唯一真源：manifest 的 projectVersionPolicy.unsupportedErrorCode。"""
    code = toolbox_manifest().get("projectVersionPolicy", {}).get("unsupportedErrorCode")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("manifest projectVersionPolicy.unsupportedErrorCode 必须是非空字符串")
    return code


def gc_keep_backups_default() -> int:
    """`project structure gc --keep-backups` 默认值的唯一真源（workflow.json maintenance 节）。"""
    value = workflow_policy().get("maintenance", {}).get("structureGcKeepBackups")
    if type(value) is not int or value < 0:
        raise ValueError("workflow policy 缺少有效 maintenance.structureGcKeepBackups（非负整数）")
    return value


def structure_batch_max_steps() -> int:
    """`structure batch-preview` 单个计划步数上限的唯一真源（workflow.json maintenance 节）。"""
    value = workflow_policy().get("maintenance", {}).get("structureBatchMaxSteps")
    if type(value) is not int or value < 1:
        raise ValueError("workflow policy 缺少有效 maintenance.structureBatchMaxSteps（正整数）")
    return value


def canon_proposal_batch_max_items() -> int:
    """Read the reviewed Canon batch bound from the maintenance registry."""
    value = workflow_policy().get("maintenance", {}).get("canonProposalBatchMaxItems")
    if type(value) is not int or value < 1:
        raise ValueError("maintenance.canonProposalBatchMaxItems must be a positive integer")
    return value


def state_compaction_policy() -> dict:
    """state_compactor 两个阈值的唯一真源（workflow.json maintenance.stateCompaction）。"""
    section = workflow_policy().get("maintenance", {}).get("stateCompaction")
    if not isinstance(section, dict):
        raise ValueError("workflow policy 缺少 maintenance.stateCompaction")
    threshold = section.get("threshold")
    old_chapter = section.get("oldChapterThreshold")
    if type(threshold) is not int or threshold < 1 or type(old_chapter) is not int or old_chapter < 1:
        raise ValueError("maintenance.stateCompaction 的 threshold/oldChapterThreshold 必须是正整数")
    return {"threshold": threshold, "oldChapterThreshold": old_chapter}


def check_layout_invariants() -> None:
    """校验机制不变量与 manifest 文档常量一致；一侧改动另一侧未跟上时 fail closed。

    `.govel`/`工作台`/`正文` 等是布局不变量而非可调配置——manifest 字段是文档
    常量，本函数是真源纪律第 4 条要求的一致性绑定，由 tests 调用。
    """
    from .importer import REQUIRED_PROJECT_DIRS  # 延迟导入避免环（importer → project）

    manifest = toolbox_manifest()
    storage = manifest.get("storage", {})
    for key in ("projectMarker", "cache", "aux"):
        value = storage.get(key)
        if not isinstance(value, str) or not value.startswith(".govel"):
            raise ValueError(f"manifest storage.{key} 必须是 .govel 下的路径（布局不变量）")
    workbench = manifest.get("agileWorkbench", {}).get("preferredPath")
    if workbench != "工作台":
        raise ValueError("manifest agileWorkbench.preferredPath 必须等于机制常量 工作台")
    if "工作台" not in REQUIRED_PROJECT_DIRS or "正文" not in REQUIRED_PROJECT_DIRS:
        raise ValueError("REQUIRED_PROJECT_DIRS 缺少机制常量目录（工作台/正文）")


def on_demand_asset_paths() -> set[str]:
    """Manifest-declared direct references are explicitly read, not auto-retrieved."""
    paths = set()
    for station, references in toolbox_manifest().get('onDemandReferences', {}).items():
        if station.startswith('_'):
            continue
        for reference in references:
            path = (TOOLBOX_ROOT / reference).resolve()
            if path.is_file() and path.is_relative_to(TOOLBOX_ROOT / 'knowledge/assets'):
                paths.add(path.relative_to(TOOLBOX_ROOT).as_posix())
    return paths
