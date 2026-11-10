# 精简模型交付

检查与章节收尾默认先交付结论、当前问题和下一步；明确通过的机械明细可计数省略，同次完整结果按引用读取。所有检查、工件绑定和批准条件保持原语义，未知与未检查不算通过。

MCP 的任务准备、续写上下文、进度、命令帮助和下表入口默认尝试用短中文呈现：先说明调用结果、就绪状态、缺项和下一步，再列必要材料与精确参数。表达由程序确定性生成，不调用模型总结。均匀列表共用一行列名，每行保留相应的 JSON 值；原文、引用、未知字段、空列表和 null 均保留，不把未知当作通过。

只有文字在字符数、UTF-8 字节数及 MCP 字符串转义后都确实更短时才采用；小结果和没有收益的结果继续用紧凑 JSON。`structuredContent` 保留同一精简结果的完整结构，程序消费者应读取它，不再假定 `content[0].text` 一定是 JSON。CLI 继续输出 JSON。显式 full／station／audit 及未支持文字呈现的操作保持 JSON；呈现失败也回退 JSON，不把已经完成的写入报告成失败。

本页说明模型收到什么，以及如何取回详情。真实输入、领域结果与最终响应由默认开启的[本地日志](OPERATIONS.md#本地调用诊断)分别保存；日志不是作品真源或作者批准。

`task.prepare` 的 model 视图将 `readingTools` 表示为 operation ID 到 tool 名的映射，保留全部可用操作，省去每行重复的字段名。参数定义按 `readingToolsGuide` 查询；原准备快照仍保存完整 schema，材料、席位、未知项与授权说明保持可读。

`get_tool_help`／`capabilities.help` 默认 `view=model`：保留完整参数 schema、调用入口、效果与授权说明；重复的 CLI 表示和通用指引按需展开。沿 `fullHelpAction` 或传 `view=full` 读取同一筛选、分页的完整帮助和 CLI 示例。显式提供 `operation + arguments` 时，两种视图都返回准确的 CLI 命令；帮助始终不执行操作。

## 查看问题与完整证据

以下入口使用确定性模型视图，不调用模型生成摘要；其他查询、正文和未知字段不作通用删减。

| 入口 | 默认交付 | 完整视图 |
| --- | --- | --- |
| workflow.chapter-receipt | 当前阻断、归属与授权状态；已验证工件计数 | view=full |
| workflow.chapter-status | 章节状态、未解决问题与下一步 | responseView=full |
| workflow.closeout-prepare / closeout | 当前缺项、执行前提、准备或恢复引用 | responseView=full |
| diagnostics.chapter-check | 机械问题与仍需语义判断的合同项 | responseView=full |
| diagnostics.aggregate | 原检查完成后呈现当前问题与必要覆盖说明 | view=full |
| workflow.diagnostics-diff | 新增、消失、改变及仍未解决的问题位置 | view=full |

CLI 对应参数是 `--view` 或 `--response-view`，以目录中的实际 schema 为准。列表中的 workflow.chapter-receipt、chapter-status、diagnostics-diff 对应 operation ID 带 `project.` 前缀。

精简时返回 fullDetails 动作，读取的是同一次业务调用的完整快照，不重新执行原操作。已有领域报告也可能返回 reportRef、detailsOperation 或 readAction，按实际动作读取；大结果分页必须沿 nextCursor 续读。缓存丢失会明确报不可用，不能把空结果当作通过，更不能只为查看明细重放写操作。

机械工件的 registered、fresh、envelopeValid 全部明确成立，批准条件也已满足，且没有待处理 finding 或未知扩展字段时，才可省略逐件成功明细。动作中的 project/chapter、bindingGeneration、prepareRef、requestId 与实际参数保留。零机械问题不代表 POV、因果、人物行为已经审查，也不代表作者已批准。

## 只提交审校的实际填写

先调用 workflow.review-form。默认返回 formRef 与 fields，后者保留实际席位和本章必须处置的条目，但不要求模型回抄 hash 或重建信封。工位填写 fields 后，把它作为 edits 提交给 workflow.submit。

审校条目使用以下两种形式之一：

| 已持有的材料 | 提交字段 |
| --- | --- |
| 当前冻结表单与实际填写 | artifactType、formRef、edits |
| 已有完整审校 payload | artifactType、payload |

两种形式不能混合在同一条目。edits 只修改允许的语义字段：结论、原文证据、适用性、处置和真实发现。工具恢复原绑定，核验项目、章节、材料、席位与规则仍属于同一份准备；不把旧结论自动挂到新计划上。未填的槽位仍为空，不默认通过或“不适用”。阶段 B 的逐席审查和事实盘点继续执行。

需要完整绑定时沿 fullAction 读取原表。大表单的 readAction 则分页读取 model 表，仍返回可填写的 fields，不要求模型从完整 payload 中剥离机械字段。同一引用的 model/full 分页游标不能混用。首次提交完成输入冻结后，原 requestId 恢复使用持久化输入；首次尚未提交而表单引用已丢失时，重新准备和复核。这里的缓存独立性仅指 formRef：repair 子工件尚未登记时仍需原 repair 缓存，缺失会保持未完成，不能给旧请求换入新材料；已有子工件回执按原恢复规则续办。

## 一次看清收尾缺项

workflow.closeout-prepare 在可独立核对的范围内汇总缺项，区分：工件不存在、文件或绑定失效、当前批准不匹配、标题未决定、诊断过期、正文归属冲突。每项保留具体类型与位置，恢复动作写明执行前提。

- 缺状态工件：返回带 requestId 的 state-prepare 动作，不要求猜参数。
- 新诊断尚未批准：定位当前诊断工件；依既有授权核对新证据，不重批已失效的旧材料。
- 正文目标已存在而归属未知：保留目标路径与冲突，不提供自动删除、覆盖或接管动作。首次定稿本身负责创建正式正文及归属。
- workflow.closeout 尚缺摘要处置或连续性授权时返回 missingInputs，由工位提供真实 summary／summaryUpdate。准备阶段的 runAction 只给引用与请求 ID，不补齐语义输入或授予批准。

有阻断时不发可执行的 runAction。材料、批准和标题决定就绪后，重新准备得到 prepareRef 和可用 requestId，再调用 workflow.closeout。中断沿原 requestId 接续已冻结输入；要补充不同内容或授权，重新准备并用新的请求。已完成阶段不重做，旧回执不覆盖。

## 减少重复读取与调用

现有机制可直接复用：task.read-batch 一次读取选定材料，priorReceipt 只省略仍明确持有且未变的内容；task.prepare 的 heldPrepareRef 只交付相对持有基线的变化；source 的 resolutionRef 和候选的 anchorRef 让局部修改只提交补丁。使用条件与例子见[日常操作](OPERATIONS.md)。

找工具先按当前任务或精确 operation 查询，已有定义不反复展开 audit 目录。语义正文、引文与责任判断按任务读取，不能因摘要简短就跳过必要原文。换窗、引用丢失或不能确认仍持有时恢复完整读取。

## 验证范围

表单回归覆盖实际 A/B 提交、跨项目与过期引用、禁止改机械字段、缺席位拒绝、分页视图、原请求恢复和缓存丢失，见 [test_review_form_edits.py](../tests/test_review_form_edits.py)。收尾回归覆盖独立问题并存、定向批准提示、未知目标保护与请求续办，见 [test_closeout_guidance.py](../tests/test_closeout_guidance.py)。返回投影与同次完整快照由 [test_model_response.py](../tests/test_model_response.py) 核对，公开 CLI/MCP 流程见 [test_review_guidance_replay.py](../tests/test_review_guidance_replay.py)。

短文本的原文、状态、精确动作与回退由 [test_model_text.py](../tests/test_model_text.py) 验证；帮助两种视图、schema、分页和公开入口由 [test_command_help.py](../tests/test_command_help.py) 验证。

效率记录应统计完成同一任务的输入与输出总量，包含必要的详情读取、失败和重试。传输比较同时统计文本与包含 `content`、`structuredContent` 的整包大小，不能假定宿主只把其中一份送给模型。本地 tokenizer 数值只说明相同样本的编码长度，不是客户端账单或文学效果；把问题放在第一行本身不会减少全文 token。
