# 续写上下文的边界、预算与证据

## 唯一装配入口

工位上下文只有一个正式装配入口：`scripts/core/context_builder.py` 的 `build_station_context()`（别名 `build_context()`；`build_continuation_context()` 保留为兼容名）。`scripts/continuation_context.py` 只是 re-export 与 CLI 兼容 facade，不再包含任何独立装配逻辑。

装配管线是确定性的：

```
project truth + source plan + POV/认识层过滤 + author decisions
+ knowledge retrieval + voice profile + recent manuscript context
        ↓ 唯一 Context Builder
station context → dependency manifest + 确定性 contextHash + health/invalidation 证据
```

`task.prepare`、`source context-health`、`task context`（`context.read`）都只**消费**这个 Builder。它们不各自路由知识、不各自判断声纹或决定层，因此相同输入必然得到相同的 `contextHash`、`knowledgePack` 与 `dependencyManifest`。`scripts.continuation_context` 仍是向后兼容的导入路径，但它导出的是同一实现对象。

工位覆盖策略集中在 `context_builder.STATION_CONTEXT`：`planner`/`writer`/`editor`（与 `query`）取完整工位包；`repair`/`titler`/`continuity`/`composer` 取 `none`，它们的写作契约来自 `promptPlan.onDemandReferences`，与叙事上下文是两条通道，互不冒充。未知工位默认不拿上下文。

## 两个不同的入口

- `novelctl project prepare --task <planner|writer|editor|repair|titler|continuity>`：**任务准备清单**。只读汇总本次任务的必读材料（含卡片路径与行范围）、上游工件证据、授权与连续性门、缺项和唯一下一步，并把就绪拆成 `structureHealthy` / `settingsReady` / `contextReady` / `authorizationReady` / `scopeReady` / `taskReady` 六项分别判断。它不发布缓存、不改项目文件。机器就绪不等于语义正确：`settingsReady` 只证明卡片结构与稳定 ID 一致，原文路径也不代表该文件已通过 POV 检验。
- 当前格式项目的工位上下文另有 `source context-health` 检查与任务准备报告中的 `context.health`：报告已构建输入的过期摘要、预算遗漏、重复和冲突，不能证明未登记散文语义完全一致。显式提供章节目标时，知识检索只发生在 Builder 内一次；`prepare.knowledgeRoute` 与 `prepare.knowledgePack` 是那次结果的投影，给出来源和排序理由；它不读取未装载的参考原文，也不把参考全文注入记忆包。
- `source context-preheat --after-chapter-id CH_ID` 只读下一登记章的计划摘要、弧线和登记引用。当前章未稳定时结果仅是预热元数据；稳定且已有非空正文后，以首次结果的 `inputHash` 作为 `--expected-input-hash` 重查，只有 `reusableMetadata=true` 才能复用元数据。目标章计划输入改变会使旧结果过期。此入口不读正文、不生成下一章剧情、不缓存完整 Context Pack；Planner 仍在稳定后调用当前 Context Builder 和 Health 检查。
  - `readList` 是**读取参考**：给出该读哪些文件、在哪几行、为什么。参考不强制注入，脚本不替你读原文，也不把卡片正文抬成字段值塞进报告。
  - `context` 报出上下文状态：`contextHash`、`budget`、`layers`、`message` 与 `povScoped`。**`povScoped=false` 表示没有做 POV 过滤**（未传 `--pov-character`）——同一对话里连续跑多工位时，这会把其他角色的知识与上一轮规划层的全知内容带进正文；有视角的任务必须传视角。
  - Planner、Writer、Editor 的准备入口分别使用本工位的上下文预算和规则动作；Editor 不复用 Planner 的准备上下文。最近章节路径排除归档旧稿，避免其被误当作当前续写输入。
  - `chapter` 投影以 当前 `placement` 为真源，`volumeId` 只作为派生的 `derivedVolumeId` 出现，不再看起来像当前章节真源字段。
  - `scopeReady` 反映本章已登记范围和准备材料。缺人物或关联时，先通过源查询定位稳定 ID，再以受审源事务登记计划的实际依赖／引用；不从推演 Markdown 反向投影实体关系。
  - 字段级查询是作者的工具，不是注入通道：只读 `novelctl project settings show [selector] --kind --field [--strict]`，见 [源字段读取](SINGLE-SOURCE.md#读取模式)。
- `novelctl project context --view station`：**续写记忆包**。正文与设定之外的记忆层（角色残余、伏笔窗口、知识差、决策、最近章路径）按预算装载，见下文。

三处长度不要混为一谈：装载计划长度（`prompt-plan` 的 `characterCounts`）描述本次要注入的契约片段；契约正文长度由片段自身决定；记忆预算（`continuation.memoryBudgets`）只约束记忆包与 station 响应。

续写上下文仍从现有 schema-5 StoryState / StoryDesign 投影读取；标记为 Context read 的入口以只读投影装配，不发布派生缓存（见下文「Context read 入口一律只读」）。不把 Markdown 解析导入机器状态，不改结构真源或创作内容，也不建立另一套写作流程。入口是 `scripts.core.context_builder.build_station_context`。

## 认识层（epistemic layers）

Builder 输出显式的 `layers` 数组，按固定顺序给出每个角色的 `epistemicRole`、`authoritative`、`contentInjected` 与条目。各层不互相冒充：

| role | epistemicRole | 权威性 |
|---|---|---|
| `CANON FACTS` | `canon_fact` | 权威：现行正式世界事实（实体、属性、值、事实片段、已登记 Canon 关系） |
| `CANON SEMANTIC CONTRACT` | `canon_semantic_contract` | 权威：`fieldDefinitions`（FieldDefinition）——规定 Attribute/Value 如何被解释的语义契约；**不是世界事实本身**，也不注入内容 |
| `CURRENT BELIEFS` | `in_world_belief` | 非权威：某视角相信，不等于 Canon |
| `IN-WORLD TEXT` | `in_world_text` | 非权威：信、报纸、文书等世界内文本 |
| `APPROVED PLAN` | `authorial_plan` | 权威规划：计划片段、已批准候选、已批准结构 |
| `UNAPPROVED CANDIDATES` | `unapproved_candidate` | 非权威：draft/withdrawn 等未批准候选 |
| `AUTHOR DECISIONS` | `authorial_planning_decision` | 权威规划：active、适用、未 stale 的作者决定 |
| `REFERENCE KNOWLEDGE` | `reference_knowledge` | 非权威：外部写作参考，即使写得很确定也不是 Canon |
| `WORK VOICE PROFILE` | `approved_plan` | 权威规划：作品级叙述风格 |

`CANON SEMANTIC CONTRACT` 与 `CANON FACTS` 是两个认识层：字段定义的改动会改变属性的**解释方式**，但字段本身不是「世界里为真」的命题，所以它既不落入 `canon_fact`，也不许进 `APPROVED PLAN` 或 `REFERENCE KNOWLEDGE`。它的写入口另有 P4 门：被现有 Attribute 引用的 FieldDefinition 走 `FIELD_DEFINITION_MIGRATION_REQUIRED`，不因换了认识层而放宽。

`memoryPack` 的 `working`/`semantic`/`episodic` 是**记忆层级**（预算维度），与 epistemic role 是**两个正交维度**：同一条记录可以是 `memory tier = semantic` 且 `epistemic role = CANON FACTS`。Builder 不会把 memoryPack 改名成 layers。

源派生的层只列**紧凑的 ID+hash+role 出处**，正文仍走 POV 限定查询（`query_source` / `query_source_chapter_plan`）；层不注入散文。未归入任何角色的记录显式列在 `APPROVED PLAN.unclassifiedRecordIds`，不静默丢弃。工位默认只注入 `UNAPPROVED CANDIDATES` 给 Planner，其他工位标 `withheldForStation`。

`WORK VOICE PROFILE` 是作品级叙述风格（`approved_plan`），与某个角色的对白声纹（实体上的 `voiceProfile`）是两个概念，不得互相当作真源。

## 机器证明与模型语义的传输边界

`scripts/core/context_transport.py` 从已通过 Health 与内容 hash 核对的 **最终 station 包**派生 `modelContext`，不重新读项目、不重新路由知识、不重新选择 POV，也不改变 Builder 的取舍。机器接口的 `layers`、`dependencyManifest`、`memoryPack`、预算和 `contextHash` 全部保留。模型语义不再重复打印这些证明。

模型读取任务、POV、去重后的完整记忆 value、作者决定、作品声纹、明确标为 `REFERENCE KNOWLEDGE` 的参考 excerpt，以及源内容／计划／规则和最近正文的读取入口。记忆层间相同 field+value 只出现一次，完整 value 的未知字段不删。源 Canon、belief、世界内文本与已批准计划仍由原有 POV 限定查询定位，再以 detail 分页阅读；**没有新注入源正文，也没有把路径当成已经读过全文**。Editor 当前稿件仍在准备清单的已登记工件读取项中。

`MEM-*` / `DEC-*` / `KN-*` / `PROSE-*` / `READ-*` 是同一上下文内按固定顺序生成的 handle。机器旁路保留 handle → 原位置、身份、hash 的对应关系；去重记忆的一条 handle 可以对应多个原位置，但只对应一个 field+value 身份。旁路不再复制记忆 value／参考 excerpt／决定 statement／声纹正文，而保留它们的 hash 与位置。遇到问题可通过同一份 station 快照取回原值。未知顶层扩展暂时进入 `unclassifiedContext`，不得为减少长度删除。

- MCP `read_continuation_context` 与 `prepare_task` 默认及 `view=model` 的 `content` 是同一模型结果的精简文字呈现（无减量时为 JSON），`structuredContent` 是其完整结构表示。程序读取后者；宿主决定哪些内容进入模型输入。Context 证明通过 `_meta.contextProofRef` 引用客户端旁路快照，不再拼进模型输入；无项目根的内部调用才内联 `_meta.contextProof`。显式 `view=station` 返回完整机器对象，用于代码／operator／debug。详见[模型交付](MODEL-OUTPUT.md)。
- CLI 保留原默认值，供已有调用方继续消费。工位薄包装改用 `project prepare --view model`；`project task context --view model` 和 `project context --view model` 也返回模型表示。旧 station/full 接口保持可用。
- 准备报告中的 `modelContext` 与 `modelProof` 是同一次 Builder 结果的投影，知识只检索一次。模型准备表示不再次输出 knowledgePack／authorDecisions；readList 若与最近正文路径重合，改为引用其 contextHandle。契约读取仍由 PromptPlan 提供：仅去掉读取范围的 hash，保留路径与行范围、审查计划及工具动作；没有改变工位工作流。
- 模型响应（含 JSON 封装）仍受已有 station 字符预算限制，超限阻塞；不新增注册表数值、不截断事实、不增加第二套 relevance engine。原机器包的预算与驱逐次序不变。

`contextHash` 定义**不变**：hash 最终机器对象（排除自身）；模型表示及 `_meta` 是 hash 后的投影，不回写入机器对象。JSON indent／序列化字段顺序本来就不影响 `sha256_json`，但旧对象内的预算计数、view、CLI 传输字段仍参与旧 hash，未借本轮改变兼容定义。Health 的 expectedHash 仍须与**重新装配的当前**快照核对；展示函数只证明传入快照的自洽，不能替代活性检查，也不能替代写入事务的 CAS。

字段消费者与模型交付边界由 [上下文传输测试](../tests/test_context_transport.py) 和 [语义表示测试](../tests/test_context_semantic_view.py) 验证；当前执行结果以实际门禁报告为准。

## 两种机器返回视图

- `view="full"` 保留原有扁平审计字段。整个对象**不受长度预算约束**，不能直接当工位输入；其中的 `memoryPack` 有独立预算。指定 POV 时，审计字段也遵循相同的信息边界。
- `view="station"` 是完整有界审计 JSON，包括记忆、认识层、路由元数据、最近正文路径、计数、就绪状态和 hash。工位模型应读取上述 model 表示。不附载原始实体集、全局摘要或重复的伏笔描述。`hookDebt` 在该视图只含 `overdueCount` / `staleCount`；具体必须处理的伏笔保留在记忆层。非规范伏笔状态以 `foreshadowingWarningCount` 计数。

调用方必须同时检查异常和 `readiness.ready`，只有无异常且为 true 时才能继续。独立脚本和 `novelctl project context` 对 not-ready 返回非零退出码；其他包装入口也必须传递这个门禁。CLI 的 station 响应通过 `bound_station_context()` 将 `ok/code` 等传输字段纳入预算并重算 hash，不在打包后悄悄超额。

```bash
python3 scripts/novelctl.py project context --project-root /path/to/书 --chapter-id <chapterId> --task-type writer --pov-character <characterId> --view station
```

## 真正的字符预算

唯一数值真源是 [workflow 注册表](../shared/registries/workflow.json) 的 `continuation.memoryBudgets`。`memoryBudgetUnit` 必须为 `serializedCharacters`；`scripts.core.project.continuation_policy()` 校验全部任务名、正整数预算和单位，拒绝空表、遗漏、未知任务配置、布尔值及非法阈值。不再保留脚本内的条数预算或层配比回退值。

预算对应 `len(serialize_context(payload))`，即 `json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)` 的 Unicode 字符数，包含 JSON 键、转义、标点、缩进、来源和计数。不是 token 数，也不是 UTF-8 字节数。打印 JSON 后终端附加的换行不属于 JSON payload。调用方若改变序列化格式，必须重新计量，不能继续引用该计数。

- `memoryPack.budget.usedCharacters` 对应独立序列化整个记忆包。
- `station.contextBudget.usedCharacters` 对应独立序列化整个 station 返回值，包括嵌套缩进、预算自身与 `contextHash`。
- `budget.total` 的单位已由条目变为字符；旧的固定 `working/semantic/episodic` 配额被取消。三个数组及公共 API 参数仍保留。
- 先装载全部必需记录，再按 working、semantic、episodic 顺序尝试可选记录。层内按稳定 ID 与记录内容确定性排序。超大可选记录整条略过，不裁断字符串，不阻止后续较小记录入选。
- station 封装若仍超限，先移除可选知识（`REFERENCE KNOWLEDGE` 让位于稿件真相），再按相反优先级移除可选记忆，最后从最旧的最近章节开始移除路径记录。知识被驱逐时 `dependencyManifest.knowledge` 与认识层同步只保留实际装载项，`knowledgePack.omittedBudget` 计数。必需记录和目标路由元数据不移除。无法容纳时抛出 `ContextBuilderError`，错误码为 `CONTEXT_BUDGET_IMPOSSIBLE`，不返回一个貌似完整的缩水输入。

## 依赖清单与失效

`dependencyManifest` 是 Context 契约本身的一部分：`sourceHash`、`sourcePlanHash`、`chapterId`、`task`、`povCharacter`、`chapterGoalHash`、最近章节正文 hash、摘要 hash、已装载决定的 `decisionId` + 内容 `sha256`、知识资产 ID+hash、`voiceProfileHash`。

**`sourceHash` 是整份 source snapshot 的身份（full-source snapshot identity），当前采用保守失效（conservative invalidation）**：源数据改动会改变 `sourceHash`，从而改变 `contextHash` 并让旧上下文被 health 判为 stale，**包括与本章无关、且没有进入这份上下文的改动**。它按解析后的源数据计算，纯 JSON 排版变化不改变该 hash；prepare/read batch 另有文件依赖绑定。Context 尚不保证无关 source change 不失效。

上下文快照过期不等于全书审查结果清空。`source_impact` 按已登记的章节依赖、计划输入与语义审查输入判断失效，未变的审查及批准仍保留；隐式、未登记依赖不在这个保证内。

因此读这些字段的粒度不要混为一谈：

- `sourceHash` = 整份快照身份，用于**保守失效**（宁可重建，不漏重建）。
- `sourcePlanHash` 与认识层里的逐记录 `contentHash` = **局部 provenance**，用来说明本章计划输入闭包本身有没有变，可用于判断「计划输入未变」这一件事。
- 二者同时存在时，`sourcePlanHash` 未变而 `sourceHash` 已变 = 变的内容不在本章计划闭包内；此时上下文仍是过期的，应重新构建，而不是依据局部 hash 未变就复用旧上下文。

`sourceContext.coverage` 显式声明这一语义：`fullSourceSnapshot` 与 `conservativeInvalidation` 为真。已登记依赖本身也仍是粗粒度到「计划输入闭包」，`source_impact` 不声称覆盖未登记或隐式使用（见 [SINGLE-SOURCE.md](SINGLE-SOURCE.md) 的登记依赖）。

`contextHash = sha256_json(context)`（排除 `contextHash` 自身）。同输入同 hash；下列任何变化都会改变 hash 或让旧快照被 health 判 stale：Canon 事实、Canon 语义契约（字段定义）、Source Plan、适用的 active 决定、知识资产内容、Voice Profile、POV、`chapterGoal`、最近稳定章正文、摘要内容，以及**任何一处 source 快照改动**（保守失效，见上）。

Preheat 与 Builder 有正式契约：Builder 为目标章写入 `sourceContext.preheatInputFingerprint`，等于「紧邻前驱章之后的 preheat 输入指纹」。Planner 可在前章稳定后 preheat，执行任务时用同一函数重新校验依赖，然后构建**全新**上下文——preheat 从不缓存未来上下文。

一次 Builder 调用共用一个 `ReviewInputs`、源索引和目标章节计划；规则检查、源摘要与预热指纹复用该次计算，避免重复构建依赖图。复用范围只在当前调用内，不跨源版本缓存健康状态，不减少原有失效检查。

## Author Decision 的适用性

一条决定进入工位要同时满足三个条件：`active`、`applicable`、`non-stale`。

适用性判断基于**当前章实际的 Context / Source Plan 相关性闭包**，而不是只认章 ID 和挂载的弧线：`scopeIds` 可以指向任何 SourceIndex record 或 Planning Candidate（项目、卷、单元、弧线、章、实体、关系、属性、值、片段、字段定义、规划候选……见 `author_decisions._scoped_records`）。Builder 取的适用集合是：

```
plan.recipe.scopeIds            （项目 / 卷 / 单元 / 弧线 / 章及其祖先弧线）
+ plan.records 中的记录 ID       （本章计划输入闭包：实体、属性、值、字段定义、片段、关系等）
+ 本章相关的 Planning Candidate ID（与 UNAPPROVED/APPROVED 候选层同一份判定）
```

无 `scopeIds` 的全局决定始终适用。落在闭包之外的决定**不注入**——例如只约束另一卷人物的决定，不会因为它是 active 就被塞给 Writer。当前章仍适用但 stale（含经 `dependsOn` 传递）、失效或 unproven 的决定保留在 `unavailableDecisions`，展示原陈述、范围、原因和受审刷新入口；它们不作为有效指导注入，也不能过滤后仍报告就绪。无关章不会因此阻断。

内容证明先于有效决定进入计划计算，避免 scopeSnapshot 依赖含自身的 sourcePlanHash。新的证明带 `scopeSnapshotBasis=author-content`，管理状态、正文 locator 与归属代次不在内容投影中；实际登记依赖变化使旧证明失效，内容回退不自动恢复批准。缺标记的历史证据只读且 unproven，沿 create 的 supersedes 与明确 approve 刷新，不在原记录上重新封签。当前项目级作品文风按实际全项目消费者参与 plan 和影响计算。

`scopeInvalidatedBy` 是随原源事务提交的单调失效证明，指向实际 OP、revision 与相关 scope/dependency；原批准与 scopeSnapshot 保留，内容回退和 undo 不删除该证明。通用保存拒绝编辑 active 决定或绕过必要失效。撤销链仅在核实原 after 镜像及已提交失效证据后承认管理差异，缺证据仍拒绝，不改旧 journal。

`dependencyManifest.decisions` 记录 `{decisionId, sha256}`：`sha256` 是该决定当前内容的指纹，因此清单能证明装进来的是**哪一个版本**，不只是一个 ID。`context_health` 会核对注入的决定与清单是否一致（`CONTEXT_DECISION_PROVENANCE_INVALID`）。

## Context read 入口一律只读

标记为 Context read 的用户入口都以 `read_only=True` 装配：`context.read`（MCP）、`novelctl project task context`、`novelctl project context`、`novelctl project source context-health`、`novelctl project prepare`，以及兼容 facade `python3 scripts/continuation_context.py`。它们不发布派生缓存、不改项目文件。缓存重建是显式的维护操作，仍走 `novelctl project state rebuild`。

## 必需记忆

以下记录不受关键词筛选或预算淘汰：

- 调用者提供的完整 `chapter_goal`；
- 当前章相关角色的完整当前状态，包括身体细节、位置和库存；指定 POV 时至少包括该角色自己的状态；
- 相关角色的知识状态，包括 unknown 等否定知识边界；
- due / overdue 的活跃伏笔及其完整截止、埋设和推进证据；
- 被投影记录显式标记 `mandatory: true` 或 `hardConstraint: true` 的约束。

记忆行仍有 `layer/subject/field/value/status/source_chapter/evidence`，新增 `mandatory/relevance`。**`value` 现在是完整结构化源记录**，而非拼接出的短字符串；knowledge 的 `subject` 使用 knowledgeId，避免多个知识项共享角色 ID 而丢失来源身份。消费代码应读取结构化字段，不对 value 假定字符串类型。此处的标记只针对结构化投影已有记录，不声称覆盖另读的源计划、契约和参考材料。

## POV 信息边界

这是记录级证据过滤，不是对自由文本的语义保密检测。当前机器结构没有完整的事实可见性图，因此采用保守规则：

1. 无 POV 时提供全局规划/审计上下文。
2. 有 POV 时，角色状态与知识状态只允许该角色自己的记录，其他角色即使声明 public 也不放行。该角色自身实体默认可用。
3. 其他叙事记录只有声明 `visibleTo: [角色稳定ID, ...]`、`visibility: "public"` 或 `povCharacter: 角色稳定ID` 才能注入。`visibleTo` 一旦存在就优先，空、非法或不包含当前角色的列表均拒绝，包括该角色自己的记录。
4. characterId、地点关联、章节引用、人物名字命中只证明**相关**，不证明**可知**。不能靠这些关联绕过边界。没有范围证据的关系、决定、伏笔、实体、卷弧说明和章节标题都不注入。
5. 全局故事摘要默认不进入 POV 上下文；只有摘要自身具备上述范围声明才可进入 full 视图，其 characterSummaries 仍只保留当前角色。显式公开/限定的自由文本是否真的符合声明由源数据维护者负责，脚本不会语义改写。
6. 章节 ID、显示序号、文件路径等路由信息可以保留。原文路径仅是按需读取的定位信息，不代表该文件全文已通过 POV 检查。
7. 不可见记录如果承载 due / overdue 伏笔、显式硬约束或被拒绝的自身状态/知识，返回 `readiness.ready=false`、`POV_HARD_CONSTRAINTS_UNAVAILABLE` 和数量。不会泄露隐藏约束正文或 ID，也不会把“因过滤没看到”当作“没有约束”。

当前源的认识层通过 contentRole 与 perspectiveId 明确表达；源查询的 perspective 模式只交付该视角的 belief／in_world_text，不把 Canon 和作者计划当作人物已知。材料装配仍按当前任务的 POV 过滤规则核对所选记录。录入与修改走对应源提案，不使用旧状态表可见性写入口，也不为解除阻塞批量声明 public。

源 perspective 的查询、搜索和阅读导出同时约束元数据：只呈现实际入选 belief／in_world_text 的所属对象，未明确可见的作者侧名称和标题以稳定 ID 表示。不能通过 overview、ownerLabel、书名或时间标签绕过原文的视角选择；author 读取仍保留原名称。

## 当前章相关性

相关性优先使用目标章稳定 ID、目标记录中的实体 ID、双向 chapter reference、当前 POV、章纲出现的实体稳定 ID / 名称 / 别名。相关角色再带出其地点和库存；带同地点、角色、来源章等 ID 的记录因此可以入选，不要求其描述重复章纲词语。中文与字母数字关键词匹配只作补充。没有目标实体范围且没有章纲时，可选记忆保持无范围回退；硬约束始终不因相关性丢失。

## 来源计数与可复现性

- `filterProvenance` 按源集合记录 input、retained、omittedPov，并对隐藏章节标题和存在的摘要记录计数；隐藏记录仅计数，不列出其内容和 ID。
- `memoryPack.provenance` 按层记录 candidates、eligible、omittedRelevance、retained、retainedMandatory、omittedBudget。每层满足 `candidates = retained + omittedRelevance + omittedBudget`。这些候选计数从已通过 POV 的记录开始。
- `recentChapterProvenance` 记录 station 最近章节路径被预算省略的数量；归档章在路径候选生成前排除。`committedChapters` 只计当前状态为 `stable` 且正文路径确实存在的章，不把预分配或归档 ID 当作已提交正文。
- 保留行带稳定 ID、完整原始结构和 evidence，未载入记录不伪造摘要。`configSource` 指向注册表字段；`contextHash` 包含最终视图、预算和计数，排除自身。相同输入和注册表产生相同结果。

### 大型项目的来源分页

station 包超限时，Builder 将已分类、未注入正文的源 ID/hash 列表转换为 `sourceRecordManifest`（完整列表摘要、条数、源快照及计划 hash、原章计划查询）。`itemCount` 仍是该层总数，`inlineItemCount` 是包内条数；两者差额由 manifest 计数解释。`view=full` 保留完整列表，原有带 POV 的内容查询继续负责正文读取。未批准候选、未分类记录、未知字段、实际记忆和作者决定不作此压缩。来源分页先于可选知识驱逐，且不提高预算、不删事实、不改变计划 hash 与保守失效规则。
