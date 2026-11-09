#!/usr/bin/env python3
"""Aggregate script for gnovel prose diagnostics.

Runs 4 separate diagnostic scripts and merges their JSON reports:
1. ai_cliche_linter.py
2. validate-text.py
3. check_voice_lexicon.py
4. check_degeneration.py  (NEW v13: model degeneration detection)

可选 --line-details：额外运行 debug_validation.py 并把行号级违规明细
并入 lineDetails 字段，一条命令拿全部门级定位（取代两次调用）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from scripts import state_cache
    from scripts.core.artifacts import file_sha256, sha256_json
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
    from scripts.core.reports import best_recommendation, gate_passed, severity_score
    from scripts.core.data_provider import binding as provider_binding, pinned_environment
except ImportError:  # 以 `python3 scripts/xxx.py` 直接运行时
    import state_cache
    from core.artifacts import file_sha256, sha256_json
    from core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
    from core.reports import best_recommendation, gate_passed, severity_score
    from core.data_provider import binding as provider_binding, pinned_environment


def run_script(cmd: list[str], *, environment=None) -> dict:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=environment)
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"error": "Invalid JSON from script stdout", "stderr": result.stderr}
            data["__returncode"] = result.returncode
            return data
        else:
            return {"error": "Empty output", "stderr": result.stderr}
    except Exception as e:
        return {"error": str(e)}


def main():
    parser = FailClosedArgumentParser(description="Aggregate gnovel prose diagnostics")
    parser.add_argument("draft_file", type=Path, help="Draft file to check")
    parser.add_argument("--intensity-level", type=int, required=True, help="Dirty level 1-10")
    parser.add_argument("--mode", type=str, required=True, choices=["plot", "buildUp", "mixed", "peakScene"], help="Scene mode (与 ai_cliche_linter/validate-text 同一词表)")
    parser.add_argument("--design", type=Path, help="Path to story_design.json")
    parser.add_argument("--project-root", type=Path, help="项目根：从当前 structure.json 读取人物声纹并重建派生缓存")
    parser.add_argument(
        "--strict-floor",
        action="store_true",
        help="把张力等级地板（S-06/S-09b）作为阻断门；默认只报告建议，避免按词数重写整章",
    )
    parser.add_argument(
        "--scene-paragraphs",
        default="",
        metavar="RANGES",
        help="mixed 模式显式关键场景段，按1-based非空段号，如3-8,12；透传给 validate-text.py（仅 --mode mixed 有效）",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    parser.add_argument(
        "--line-details",
        action="store_true",
        help="额外运行 debug_validation 并入行号级明细（lineDetails 字段）",
    )
    parser.add_argument("--toolbox-root", type=Path, help="Root directory of the toolbox")

    args = parser.parse_args()
    provider = provider_binding()
    child_environment = pinned_environment(provider)

    toolbox_root = args.toolbox_root
    if not toolbox_root:
        toolbox_root = Path(__file__).resolve().parent.parent

    scripts_dir = toolbox_root / "scripts"

    cliche_script = scripts_dir / "ai_cliche_linter.py"
    validate_script = scripts_dir / "validate-text.py"
    voice_script = scripts_dir / "check_voice_lexicon.py"
    degen_script = scripts_dir / "check_degeneration.py"

    scripts_exist = True
    for s in [cliche_script, validate_script, voice_script, degen_script]:
        if not s.exists():
            print(f"Error: Script not found: {s}", file=sys.stderr)
            scripts_exist = False

    if not scripts_exist:
        sys.exit(EXIT_FAIL_CLOSED)

    # 1. ai_cliche_linter.py
    cliche_cmd = [
        sys.executable, str(cliche_script),
        str(args.draft_file),
        "--intensity-level", str(args.intensity_level),
        "--mode", args.mode,
        "--json"
    ]
    cliche_res = run_script(cliche_cmd, environment=child_environment)

    # 2. validate-text.py
    validate_cmd = [
        sys.executable, str(validate_script),
        "--mode", args.mode,
        "--intensity-level", str(args.intensity_level),
    ]
    if not args.strict_floor:
        validate_cmd += ["--no-strict-l5", "--no-dialogue-floor"]
    else:
        validate_cmd += ['--strict-floor']
    # mixed 模式：显式指定关键场景段，避免 validate-text 退回 whole-text 兼容语义
    if args.scene_paragraphs:
        validate_cmd += ["--scene-paragraphs", args.scene_paragraphs]
    validate_cmd += ["--json"]
    # validate-text.py reads from stdin for the draft file if not provided as positional?
    # Wait, validate-text.py positional arg or stdin. Let's pass it as positional.
    validate_cmd.insert(2, str(args.draft_file))
    validate_res = run_script(validate_cmd, environment=child_environment)

    # 3. check_voice_lexicon.py
    voice_res = None
    design_arg = args.design
    if design_arg is None and args.project_root:
        try:
            design_arg = state_cache.design_path(args.project_root)
        except state_cache.StateCacheError as exc:
            print(f"Error resolving design cache: {exc}", file=sys.stderr)
            sys.exit(EXIT_FAIL_CLOSED)
    if design_arg:
        voice_cmd = [
            sys.executable, str(voice_script),
            str(args.draft_file),
            "--design", str(design_arg),
            "--json"
        ]
        if args.project_root:
            voice_cmd += ['--project-root', str(args.project_root)]
        voice_res = run_script(voice_cmd, environment=child_environment)

    # 4. check_degeneration.py (NEW v13)
    degen_cmd = [
        sys.executable, str(degen_script),
        str(args.draft_file),
        "--json"
    ]
    degen_res = run_script(degen_cmd, environment=child_environment)

    # 可选：行号级违规明细（debug_validation 复用 validate-text 同一判定逻辑）
    line_details = None
    if args.line_details:
        debug_script = scripts_dir / "debug_validation.py"
        if not debug_script.exists():
            print(f"Error: Script not found: {debug_script}", file=sys.stderr)
            sys.exit(EXIT_FAIL_CLOSED)
        debug_cmd = [
            sys.executable, str(debug_script),
            str(args.draft_file),
            "--mode", args.mode,
            "--intensity-level", str(args.intensity_level),
            "--json",
        ]
        if args.strict_floor:
            debug_cmd.append('--strict-floor')
        if args.scene_paragraphs:
            debug_cmd += ['--scene-paragraphs', args.scene_paragraphs]
        line_details = run_script(debug_cmd, environment=child_environment)
        if 'error' not in line_details and line_details.get('__returncode') not in (0, 1, 2):
            line_details['error'] = 'unexpected exit code from debug_validation.py'
        if "error" in line_details:
            print(f"Error running debug_validation.py: {line_details['error']}", file=sys.stderr)
            if "stderr" in line_details:
                print(line_details["stderr"], file=sys.stderr)
            sys.exit(EXIT_FAIL_CLOSED)

    # If any script failed to run or parse, or exited with an unexpected code,
    # fail closed rather than silently accepting partial/empty results.
    for res, name in [
        (cliche_res, "ai_cliche_linter.py"),
        (validate_res, "validate-text.py"),
        (voice_res, "check_voice_lexicon.py"),
        (degen_res, "check_degeneration.py"),
    ]:
        if res is None:
            continue
        if "error" in res:
            print(f"Error running {name}: {res['error']}", file=sys.stderr)
            if "stderr" in res:
                print(res["stderr"], file=sys.stderr)
            sys.exit(EXIT_FAIL_CLOSED)
        rc = res.get("__returncode", 0)
        # 诊断脚本的正常退出码：0=pass, 1/2=发现需处理（JSON 仍有效）。
        # 任何其他码（含崩溃/异常中断/网关错误）一律 fail-closed。
        if rc not in (0, 1, 2):
            print(
                f"Error running {name}: unexpected exit code {rc}（脚本非正常完成，结果不可信）",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAIL_CLOSED)

    aggregated = {
        "schemaVersion": 1,
        "providerBinding": provider,
        "draftSha256": file_sha256(args.draft_file),
        "inputHash": sha256_json({
            "draftSha256": file_sha256(args.draft_file),
            "mode": args.mode,
            "intensityLevel": args.intensity_level,
            "sceneParagraphs": args.scene_paragraphs,
            "strictFloor": args.strict_floor,
        }),
        "summary": {
            "totalFindings": 0,
            "perScript": {
                "ai_cliche_linter": 0,
                "validate_text": 0,
                "check_voice_lexicon": 0,
                "check_degeneration": 0,
            },
            "perSeverity": {
                "blocking": 0,
                "error": 0,
                "warning": 0,
                "advisory": 0,
                "info": 0,
            }
        },
        "aggregatedRecommendation": "pass",
        "findings": [],
        "scriptResults": {
            "ai_cliche_linter": cliche_res,
            "validate_text": validate_res,
            "check_voice_lexicon": voice_res,
            "check_degeneration": degen_res,
        },
    }
    if line_details is not None:
        aggregated["lineDetails"] = line_details

    findings = []
    recs = ["pass"]

    # Process ai_cliche_linter
    if cliche_res:
        recs.append(cliche_res.get("recommendation", "pass"))
        ceiling = cliche_res.get('intensityLevelCheck') or {}
        if ceiling.get('status') == 'INCONSISTENT':
            findings.append({'source': 'ai_cliche_linter', 'severity': 'error', 'ruleId': 'INTENSITY-CEILING',
                             'message': ceiling['message'], 'evidence': ceiling['violations'], 'raw': ceiling})
            aggregated['summary']['perScript']['ai_cliche_linter'] += 1
        if ceiling.get('contextReview'):
            findings.append({'source': 'ai_cliche_linter', 'severity': 'advisory', 'ruleId': 'INTENSITY-CONTEXT',
                'message': '日常或医疗语境歧义词，须语义复核，未判定越界', 'evidence': ceiling['contextReview'], 'raw': ceiling})
            aggregated['summary']['perScript']['ai_cliche_linter'] += 1
        for f in cliche_res.get("findings", []):
            sev = f.get("severity", "warning")
            if sev == "block":
                sev = "error"
            findings.append({
                "source": "ai_cliche_linter",
                "severity": sev,
                "ruleId": f.get("ruleId"),
                "message": f"[{f.get('pattern')}] {f.get('matched')} in '{f.get('context')}'",
                "line": f.get("line"),
                "raw": f
            })
            aggregated["summary"]["perScript"]["ai_cliche_linter"] += 1

    # Process validate-text
    if validate_res:
        all_pass = validate_res.get("all_pass", True)
        if not all_pass:
            recs.append("surgical_edit")

        for c in validate_res.get("checks", []):
            if c.get("pass") and c.get("level") != "info":
                continue

            sev = "error"
            if c.get("level") == "info":
                sev = "info"
            elif c.get("level") == "warning":
                sev = "warning"

            msg = c.get("name", "")
            if c.get("detail"):
                msg += f": {c.get('detail')}"
            if c.get("fix_hint"):
                msg += f" (Fix: {c.get('fix_hint')})"

            findings.append({
                "source": "validate_text",
                "severity": sev,
                "ruleId": c.get("rule_id", c.get("name")),
                "message": msg,
                "line": c.get("line"),
                "evidence": c.get("evidence", ""),
                "raw": c
            })
            aggregated["summary"]["perScript"]["validate_text"] += 1

    # Process check_voice_lexicon
    if voice_res is None:
        # 未提供 --design：显式标记跳过，不得静默缺省
        aggregated["scriptResults"]["check_voice_lexicon"] = {
            "status": "skipped",
            "reason": "--design 未提供，角色禁词检查未运行",
        }
        findings.append({
            "source": "check_voice_lexicon",
            "severity": "info",
            "ruleId": "SKIPPED",
            "message": "check_voice_lexicon 未运行：--design 未提供（角色禁词与全局 AI 感叹词未检查）",
            "line": None,
            "evidence": "",
            "raw": {"status": "skipped"},
        })
    else:
        recs.append(voice_res.get("recommendation", "pass"))
        for f in voice_res.get("findings", []):
            sev = f.get("severity", "error")
            if f.get("ruleId") == "VOICE-GLOBAL-INTERJECTION":
                message = f"引语命中全局禁词「{f.get('phrase')}」；该词面规则不依赖说话人归属。"
            elif f.get("speakerCertain") is True:
                message = f"明确发言标记指向角色 {f.get('characterId')}，引语命中其专属禁词「{f.get('phrase')}」。"
            else:
                message = (f"引语命中角色 {f.get('characterId')} 的专属禁词「{f.get('phrase')}」，"
                           "但说话人仅为推测，归属待复核。")
            findings.append({
                "source": "check_voice_lexicon",
                "severity": sev,
                "ruleId": f.get("ruleId"),
                "message": message,
                "line": f.get("line"),
                "evidence": f.get("quote", ""),
                "raw": f
            })
            aggregated["summary"]["perScript"]["check_voice_lexicon"] += 1

    # Process check_degeneration
    if degen_res:
        # 直接采用子检查器的 recommendation：评分表已含全部取值，无需隐式翻译
        # （此前的 rewrite_with_generator → rewrite_required 翻译会丢一层严重度）。
        recs.append(degen_res.get("recommendation", "pass"))

        for b in degen_res.get("blocking", []):
            findings.append({
                "source": "check_degeneration",
                "severity": "blocking",
                "ruleId": b.get("pattern"),
                "message": f"[模型退化] {b.get('pattern')}: {b.get('matched')}",
                "line": b.get("line"),
                "raw": b
            })
            aggregated["summary"]["perScript"]["check_degeneration"] += 1

        for a in degen_res.get("advisory", []):
            findings.append({
                "source": "check_degeneration",
                "severity": "advisory",
                "ruleId": a.get("pattern"),
                "message": f"[模型退化 advisory] {a.get('pattern')}: {a.get('matched')}",
                "line": a.get("line"),
                "raw": a
            })
            aggregated["summary"]["perScript"]["check_degeneration"] += 1
    # Sort findings by severity
    findings.sort(key=lambda x: severity_score(x["severity"]), reverse=True)
    aggregated["findings"] = findings
    aggregated["summary"]["totalFindings"] = len(findings)

    for f in findings:
        sev = f["severity"]
        if sev in aggregated["summary"]["perSeverity"]:
            aggregated["summary"]["perSeverity"][sev] += 1

    # Determine aggregated recommendation（子检查器与聚合器共用 reports 的一张评分表）
    best_rec = best_recommendation(recs)
    aggregated["aggregatedRecommendation"] = best_rec
    # 工件协议使用布尔闸门，避免每个消费者各自解释 recommendation 枚举。
    # draftSha256 + passed 共同组成最终化的机械门绑定证据。review_advisory 是非阻断信号，
    # surfaced 进推荐但不拦门（此前它不在评分表里得 0 分，会被 pass 压掉、从推荐和门禁里一起消失）。
    aggregated["passed"] = gate_passed(best_rec)
    from core.prose_calibration import calibrate
    aggregated["calibration"] = calibrate(Path(args.draft_file).read_bytes().decode("utf-8"))

    pinned_environment(provider)  # Refuse changed inputs before publishing any result.
    if args.json:
        print(json.dumps(aggregated, ensure_ascii=False, indent=2))
    else:
        # Human-readable table
        print("=" * 60)
        print("AGGREGATED DIAGNOSTICS REPORT")
        print("=" * 60)
        print(f"Recommendation: {aggregated['aggregatedRecommendation'].upper()}")
        print(f"Total Findings: {aggregated['summary']['totalFindings']}")
        print(f"By Severity: Error({aggregated['summary']['perSeverity']['error']}) Warning({aggregated['summary']['perSeverity']['warning']}) Info({aggregated['summary']['perSeverity']['info']})")
        print("-" * 60)

        for f in findings:
            line_info = f"Line {f['line']}: " if f['line'] is not None else ""
            print(f"[{f['severity'].upper()}] {f['source']} - {f.get('ruleId', 'N/A')}")
            print(f"  {line_info}{f['message']}")
            print()

        if line_details:
            print("=" * 60)
            print("LINE-LEVEL DETAILS (debug_validation)")
            print("=" * 60)
            blocks = line_details.get("blocks", {})
            for rule, items in blocks.items():
                if not items:
                    continue
                print(f"[{rule}] {len(items)} 处")
                for it in items:
                    ln = it.get("line", "")
                    loc = f"L{ln}" if ln else "    "
                    text = str(it.get("text", "")).strip()
                    extra = it.get("fix") or it.get("detail") or ""
                    print(f"  {loc}  {text}" + (f"  → {extra}" if extra else ""))
                print()

    # Exit code per shared contract (RUNTIME-CONFIG): pass→0, advisory/surgical→1, rewrite→2.
    # Decoupled from the finalization gate (`passed`): advisory is a reported finding (exit 1)
    # yet does not block finalization (passed stays True).
    sys.exit(exit_code_for_recommendation(best_rec))

if __name__ == "__main__":
    main()
