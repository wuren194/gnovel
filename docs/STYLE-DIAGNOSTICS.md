# 核对风格候选

风格扫描返回可定位的候选，由 Editor 阅读上下文后决定保留或修改。它不判断作者是否使用 AI，也不替代阶段 B、作者批准或正文归属证明。

在工具箱目录运行 `python3 scripts/ai_cliche_linter.py <正文文件路径> --json` 可单独检查词面；`project diagnostics rules` 提供当前模式的实际规则卡。项目内的候选仍沿既有诊断、审阅和提交工具处理，不能用单脚本通过替代完整流程。

## 读懂结果

| 字段 | 含义 |
| --- | --- |
| `ruleId`、`pattern` | 稳定规则身份与展示名称 |
| `line`、`column` | 原始输入中的行列，从 1 开始；列按 Unicode 字符计数 |
| `start`、`end` | 原始输入的 Python 字符偏移，从 0 开始，结束不包含在内；不是 UTF-8 字节或 UTF-16 单元 |
| `matched`、`context` | 原始命中文字与所在行；跨行命中完整保存在 `matched` |
| `review` | `semantic` 需要语义判断；`mechanical` 按已声明的形式约束处理 |
| `severity`、`gate` | 提示级别与有效机械阻断标记；语义候选不产生机械否决 |
| `rewriteHint` | 需要核对的用途和修改方向，不是自动改写指令 |
| `occurrences`、`density` | 聚集提示的各次命中位置、汉字分母、每千汉字密度与阈值 |

公开风格规则默认 `advisory / semantic / gate=false`。有候选时返回 `review_advisory`，CLI 退出码为 1，聚合机械门仍可通过。无候选返回 `pass`。明确配置的机械形式约束或张力上限违规仍可要求修复；工程失败继续按既有退出码处理。调用方应读取结构结果，不能把任何非零退出码都解释成定稿被拒。

Editor 认定候选确实损害当前文本后，按阶段 B 记录引文、理由和修复范围。过去已经成立且仍有效的阻断不会因这次规则调整而被自动关闭。

## 范围和密度

规则真源是 `shared/registries/text-patterns.json`。`ai.regex` 保存句式，`ai.triple` 保存并列模式，`ai.clusters` 按成员规则合并局部提示；`diagnosticPolicy.sentenceRhythm` 与 `diagnosticPolicy.degeneration` 保存节奏和复读阈值。实际数值由规则卡查询，本文不复制一套阈值。

`scope=narrative` 排除成对引语，包含跨行引语；`scope=all` 检查含对白的正文。两者都排除标题、围栏代码、已闭合 frontmatter 和 HTML 注释。注释内的文字不会被执行为“豁免下一段”的指令。未闭合引号不自动隐藏余下正文。

单规则可用 `perParagraphLimit`、`minMatches` 和 `minPerKHan` 控制提示。章尾规则用 `endingWindowHan` 限定输入末尾的叙述范围；扫描局部片段时，这只是该片段的结尾，不能据此确认整章结构。

库存表达聚集先收集成员的原始命中，再按局部汉字窗口检查次数与密度，避免每段的首个豁免把跨段聚集漏掉。对白和元数据不计入叙述分母，重叠成员只计一次。聚集提示已经携带成员定位时，不再重复输出这些单项提示。单次换气、普通目光、独立比喻或时间词不因此被判错。

短句连续复读只提示语义核对；长句逐字复读继续按既有形式条件报告。复读同时检查行内句子和相邻行。明确的生成拒绝语、流程说明、占位符和截断保留检查；普通第一人称的“不能”“无法”和道歉不等于助手发言。

S-10q 保留词面启发式数值，默认 `info`。感官词数量、心理词或句式计数不能证明文学质量，不能为提高分数强加动作、删去心理或削弱因果。

## 本地覆盖与验证

Provider 的 `text-patterns` 是完整覆盖表。更新引擎时，覆盖表也须包含当前规则身份、数值字段及其摘要；不会静默用公共规则替换用户的题材内容。风格规则的 `review` 默认是 `semantic`；需要硬性形式约束时，必须明确声明 `mechanical`、阻断严重度和 `gate=true`。仍须有作品层面的适用依据，不能据此声称已完成文学校准。

公开回归使用原创中性短例，运行：

```sh
python3 -m unittest tests.test_ai_style_rules tests.test_degeneration_rules tests.test_checker_evaluation
python3 scripts/local_ci.py
```

`evals/checker-eval.json` 标注的是指定规则应否提供形式证据，不是“好文章／坏文章”或“人类／AI”标签。修改标注后须重新审阅并生成相应基线，不能与不同样本摘要的旧结果直接比较。私人文稿和题材包的回归放在仓库外。

报告继续标注 `UNCALIBRATED`。合成回归的零误报只覆盖这些控制样例；扩大规则、增加提示数或通过测试，都不证明在真实小说上的精确率、召回率或文学效果。
