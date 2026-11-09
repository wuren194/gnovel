---
id: CHAPTER-PIPELINE
version: 2.4.0
compatibilityVersion: 5
description: 章节与约定候选批次的执行、返修、版本交接、范围授权和正式入库规则。
---

# 章节流水线

> 当前项目使用 `G-SINGLE-SOURCE.md`：设定与规划统一保存在结构真源，阶段 A 与发布绑定 `sourcePlanHash`。阅读导出不能替代计划或批准。

所有章节，包括试写、续写、扩写和候选稿，都走同一条流水线：

```text
Planner 推演（阶段 A）
→ Writer 一次整章
→ aggregate_diagnostics 机械门
→ 叙事、表达与必要修订核对
→ Editor 阶段 B
→ 定位返修与完整回检
→ Titler 定名（章节标题）
→ 正式入库（finalize；同一事务登记待确认的连续性批次）
→ 连续性收尾确认（作者确认后登记完成证据）
→ 下一章
```

## 进入方式

四类请求从流水线的不同步骤进入，都不新建草稿通道，也不让只读请求启动写作流程：

| 请求 | 起点 | 失效与重做范围 |
|---|---|---|
| 新章 | 前置条件 → 阶段 A | 无既有证据 |
| 结构重规划（改事件、场景、关系阶段、结局方向） | 「停止与重规划」 | 结构侧 preview → commit；该章推演、阶段 A 与全部下游工件失效重做 |
| 局部修订 / 扩写（不动已批准节点） | 返修闭环的 `local` / `expansion` | 候选正文与机械报告失效；已绑定正文 hash 的 `stage_b_review`、`state_candidate` 一并失效，须重新产出 |
| 纯查询（读状态、读正文、问进度） | 只读入口 | 不改变项目状态，不触发流水线 |

改正文必然使绑定其 hash 的证据失效：`diagnostics.draftSha256`、`stage_b_review.finalProseSha256`、`state_candidate.finalProseSha256` 必须与最终候选一致，改后重跑机械门并重新登记批准。改推演则使 `stage_a_review.sourcePlanHash` 与 `state_candidate.sourcePlanHash` 失效。

候选包装同时保存当前推演与阶段 A 绑定；原绑定漂移时不得把旧稿当成已经按新推演写作。有意降低表现强度或调整呈现方式可在候选的 `writingNotes` 登记，保留原推演；这只是提案，阶段 B 须逐项裁决并绑定整份候选，作者批准前不算接受。改变事件、关系阶段或结局方向仍走重规划。新增事实另走受审源提案与正文引证，不因候选登记自动落入设定。

[M] 工作台 active 选择、已登记 candidate 工件与正式正文各有身份。激活后须用冻结转交引用明确关联版本、选择代次、正文、源计划与阶段 A；工位实际判断计划适用及旧 writingNotes 的保留、修订或撤回。只存未激活版本不改变当前选择；同字节另一身份或 A→B→A 不能借旧关联。未对齐或优先来源损坏时，准备、检查、表单、状态和定稿一致阻断，不回退旧稿。

机械报告检查可计算的篇幅、词面与绑定；必须落地项、限制视角和事实新旧仍由 Editor 判断。阶段 B 绑定其核对过的诊断工件后，作者批准阶段 B 可覆盖该份诊断；工具不得伪造独立批准记录。任一绑定变化须重新诊断或审校。

## 前置条件

- 章节必须属于当前卷、已有弧线和作者批准范围；CH001 不豁免弧线前置。
- 当前卷由按阅读顺序第一个未稳定、未归档章节的 placement 确定；卷的 planned/stable 是规划状态，不另造 active 状态。无卷章节按其实际 placement 执行。候选批次不取消卷边界；禁止预热时，未来的规划条目不能冒充已完成前置，已审候选须通过原批次证据核验。
- 若注册表显式允许预热，只允许当前未完成章的紧邻下一章进入推演／阶段 A；更早前置仍核验，Writer、机械门和审校不借此越过正式或候选交接。
- 推演必须包含本章定位、节奏段、篇幅合同、伏笔动作、签名瞬间和阶段 A 裁决。
- 阶段 A 输出必须先展示并获得作者同意；`stage_a_review` 绑定当前 sourcePlanHash，登记并批准后通过 `workflow advance --to planned` 推进章节。明确的批量授权只覆盖指定范围。
- 状态推进复用 chapter_state 门：正文登记后 planned → draft，机械报告登记后 draft → review。`workflow submit` 或 artifact-pack 的 advanceLifecycle 可同时完成这两步；review → stable 只走正式 finalize。
- 触及注册表 `extremeLevel` 的内容始终需要人工确认，自动批准模式不豁免。

## 章节标题

章节标题有两个时点，不得合并：

1. **建条目时的工作标题**：`workflow chapter-preview` 或 `structure insert-preview` 的 title，此时正文还不存在，只写一个可识别的工作标题（可以是本章定位一句话）。工作标题不经 Titler，也不作为定稿。
2. **定稿标题**：Editor 放行后、入库前由 Titler 产出。此时正文、签名瞬间与章节离场状态都已确定，标题才有素材可依。Titler 按 `G-MULTI-IDENTITY-REVIEW.md` 阶段 A 开标题固定 5 席（1/2/3/6/10），候选与圆桌结论一起展示，**由作者选定**，Router 再写入 structure 的 `title` 字段。

章节 `filePath` 在建章时分配，之后只经正式路径事务修改。默认改标题不改文件、不动正文。作者跳过定名时 Router 显式使用 `--skip-title`，保留工作标题并在入库报告标注未定稿；不得把跳过与直接传任意标题混为一条通路。

## 机械门

主流水线只调用：

```text
python3 <toolbox_root>/scripts/aggregate_diagnostics.py <draft> --mode <mode> --intensity-level <level> --project-root <project> --json
```

`mixed` 模式必须显式提供 `--scene-paragraphs`。单检查器只用于开发调试，不构成独立流水线步骤。阈值来自注册表和 `workflowPolicy`，执笔说明见 `gnovel-router/references/mechanical-baseline.md`。

## 叙事与表达核对

在机械门通过后，Editor 依据已批准计划、文风和当前正文核对叙事形成、语篇流动与表达。先明确问题、定位和证据，再按实际缺陷修订；没有违反约定的可选建议随稿呈现。

心理表达、因果、完整结局和句段节奏服从已批准文风及 `G-OVER-ORGANIZATION.md` §4。不得把某种表达方式、悬空结尾或固定修改比例设为通用要求。此步骤由现有 Editor 职责承担，不依赖额外资料包。

作者显式接入的第三方方法或题材材料，经当前 Provider 声明与任务读取范围装载。材料只增加相应范围的参考或规则，不替代当前事实、独立结论位与作者批准。

## 返修闭环

1. 机械问题按报告中的 `source`、`evidence` 和 `recommendation` 定位处理。
2. 返修分三类，按 `repairScope` 声明：
   - **外科修复**（`local`）：局部词面或标点问题，用行号证据定点改。
   - **扩写**（`expansion`）：字数不足下限，或某段写淡了要写厚。按 `G-EXPANSION.md` 放慢已批准节奏段内部的镜头速度——**不新增事件、不改章末钩子、不新增场景、不改关系阶段**。放慢到头仍不够字数时报 `EXPANSION_BUDGET_MISMATCH` 退回 Planner，不在正文阶段加戏。
   - **结构返修**（`structural`）：OOC、因果、物理、主线和全章空转。
3. 每轮返修后重新运行完整 aggregator，不手写或改变检查器顺序。
4. 自动修复显式声明 automaticRepair 并绑定 requestId，轮次由工具与候选登记同事务写入 `.gnovel/workflow.json.repairRounds`；同请求重试不重复计数，候选提交前失败不计轮，提交后的诊断失败保留已记轮次，不另行补记。达到 `workflowPolicy.repair.maxAutomaticRounds` 后停止；向作者展示剩余 findings，并提供带债入库、升级重规划或继续修三种明确决策。

扩写不得升级为结构返修：字数不足本身不是结构问题。反过来，结构问题也不得用扩写掩盖——在有 OOC 或因果硬伤的正文上加字数只是把问题埋深。

返修问题的稳定身份与报告／正文版本定位分开保存。旧报告失效后，其问题和补丁前后文本仍可作为待复核材料；未处置项不得因换报告或省略字段而消失，无法可靠定位的标为待定位。工具不以补丁成功、检查命中消失或身份匹配判断语义已修复。

工位可提交本轮逐项处置、理由和当前原文证据，由工具装配未处置问题；历史结论不是当前通过或批准。正文或输入变化后重新判断，阶段 B 的独立结论位仍按 `G-MULTI-IDENTITY-REVIEW.md` 完整提供。保留的报告与回执只供复核，不参与正文所有权裁定。

[M] fresh B 与 repair 表单、登记、批准和最终门共用现有报告历史。旧 S1/S2/blocking 必须有绑定当前候选身份与计划的显式处置；省略、改级为 S3 或重登较早 resolved 信封不能抹掉较新问题。有效处置不因无关报告换 ID 重开，输入变后待复核；S3/info 建议随稿可见且不自动升阻断。

作者侧报告读取须核验登记的工件、文件字节及项目／章节身份，并区分送审版本已知、正文匹配和当前适用。失效但完整的报告保留原建议与笔记，不对当前稿定位；报告损坏不能回退另一份历史冒充当前报告。问题笔记只绑定真实、当前适用的报告条目，写入前后复核相关输入；“已处理”不产生审校通过或作者批准。

## 串行与稳定事实源

- 当前章只有在 Editor 放行、正文入库和设定回灌完成后，才成为稳定事实源。
- 入库把章节推进到 stable，并登记绑定最终正文和结构的待确认连续性批次。连续性确认是独立批准动作；工具不能代填“无需变化”或“已经刷新”的语义结论。源事实、复核、正文引证与摘要分别通过各自领域入口完成；空批次不证明无事实变化。
- 收尾编排只消费已检查的准备快照、工位提供的摘要处置与已有明确授权；定稿、摘要和连续性各步沿原领域事务原子保存回执，故障从原请求续办，不重复已成功步骤，不代批审校工件。已覆盖本章及该步骤的明确授权直接沿用，不因分步调用重复询问作者。
- 新事实快捷入口只在 stable 且连续性 complete 后装配明确的 topic 事实；源事务批准后才能登记正文引证。工位仍负责事实新旧、归属与证据判断，未列 factRequestIds 只表示没有核对这些事实请求；不能把空状态或缺省输入当作“没有新事实”。源变化后的摘要由工位复核更新，再完成交接。
- 摘要新鲜度是交接建议：缺失或过期时 `stale=true`、`refreshRecommended=true`；当前策略不据此单独阻断续写，`refreshRequired=false`。收尾填写 `not_required` 必须说明本批次不刷新的理由，完成回执保留原新鲜度；填写 `refreshed` 必须验证当前摘要新鲜且 hash 相符，不得把声明当成刷新。
- 普通正式续写以稳定前章、连续性和事实交接完成为前提。仅委托整批候选时，采用下述冻结候选链，不提前定稿解锁后章。
- 到达批准范围末尾后进入等待授权，不自动扩大范围或跨卷。
- 恢复时读取 `.gnovel/workflow.json`，再运行 `project workflow reconcile`，据真实输入及已登记候选链校验唯一 `nextAction`；无法证明兼容的多个中间态返回 `WORKFLOW_AMBIGUOUS`，不得凭会话记忆选择。

### 约定范围的候选批次

- [H/M] 冻结批次请求仅引用既有章节授权内的明确章节集合，不产生另一份可独立扩大的批准范围。阶段 A 仍消费已明确覆盖本章常规推演的宿主授权；新创作取舍交作者决定。候选委托不等于正式 B／正文批准。
- [M] Planner 准备与 A 表单起冻结实际上游候选、源计划、A/B 和有来源的交接输入；Writer 与 candidate 继承同一 recipe。前章须完成真实机械检查与 B 义务，`POLISH_READY` 只证明可供候选续写的完成状态，不授正式批准。
- [S/M] 候选事实只作该链的有来源条件材料，不进入 Canon、正式摘要或正文引证。与已确认事实冲突或需要新能力、责任、关系方向时，保留冲突并交作者取舍。
- [M] 上游变化使实际相关链待复核，保留正文、报告与续办位置；无关章、未激活另存不失效。候选阅读快照明确每章身份、建议、未完成部分和原请求，不改变正式合订输入。
- [M] 正式采用仍逐章走原 closeout，实际采用时准备当前 state/CAS 并满足原工件批准。finalize 只在自身写集封存候选到正式代次的对应；后续 continuity 和事实各自提交后引用原对应，完整资格由已提交回执派生。不预言后续完成、不重写旧 journal、不把下游依赖换成最新正式 hash。
- [M] 机械检查保留原完整运行输入和 inputHash；当前性只可按已核实的实际消费者输入复用。语义依赖证明不代替提交时原期望版本、物理 guards 或 CAS。

## 正式入库

- 候选和报告由内置 `workflow submit`／artifact-pack 保存、绑定与登记；机械证据只能由真实运行器产生。工件只能登记到 structure 中已存在且未归档的章节，禁止为尚未建条目的章节预存工件。
- `manual` 模式必须展示正文摘要、机械门结果、阶段 B 结论和状态变更，再用 `workflow artifact-approve` 按原始 hash 批准；批准按工件类型增量合并，后续批准不得抹掉阶段 A 的批准证据。源正文或报告变化后旧批准失效。批准只确认工件，不写受保护路径。
- 阶段 B 声明 `POLISH_READY` 时，全部席位必须填写实际结论，并以 `evidence` 引用当前候选正文；不适用项须填写 `notApplicableReason`。占位词由 `workflowPolicy.review.unfinishedConclusions` 定义。登记与入库均核验正文 hash 和引文，阻塞席位或阻塞 finding 尚在时不得放行；引文匹配不证明结论正确或材料已完整阅读。旧报告可供历史查询，缺少完成证据时须重新审校后登记，不自动补结论、引文或批准。
- `auto` 模式只在已授权范围内自动批准已登记工件；不得越过卷、范围或 `intensity.extremeLevel` 人工门。
- 正式事实提交只使用 `workflow finalize-chapter`：章节必须存在、处于 `review`、属于批准范围，且 `stage_a_review`、`candidate_prose`、`diagnostics.passed=true`、`stage_b_review=POLISH_READY`、`state_candidate` 全部登记并按原 hash 批准；不跳过标题时另要求已批准 `title_candidates`。提交原子更新正文、结构状态、当前归属 binding、workflow 与待确认连续性记录。普通异常全部恢复；进程中断后 `workflow recover` 检测或回滚持久化事务。
- `diagnostics.draftSha256`、`state_candidate.finalProseSha256` 与候选正文必须一致；`stage_a_review.sourcePlanHash`、`state_candidate.sourcePlanHash` 与当前推演必须一致。
- **正文目标归属**：chapterId 是稳定身份，filePath 是可变定位符。当前 proseBinding 与盘上 observation 决定归属；首次定稿要求空的 unbound 槽，再定稿要求 managed 且内容匹配。unresolved、外部修改、代次冲突或损坏均拒绝。历史回执不能作为当前所有权回退证明，模型不能伪造所有权 capability。细则见 `docs/R1-CURRENT-OWNERSHIP-BINDING.md`。
- 审校空表由工具排列实际席位并绑定当前材料，结论与引文由工位填写；状态工件可由工具生成并登记，state_candidate 绑定当前真源投影、已批准计划与最终正文，changes 保持空数组。事实变化走受审源事务；空 changes／proposedFacts 和状态工件登记不能代替事实盘点、回流或正文证据。
- 同一提交 requestId 固定内容和执行选项，只可接续未完成阶段。输入漂移或工件被替换时重新核对；工具不把旧结论绑定到新版内容。


## 停止与重规划

仅在结构性 `stop_for_replan`、缺失不可推断硬约束、正文生成失败且无可读内容、受保护资产冲突、状态完整性冲突或用户暂停时中断。

重规划分两侧，两侧都不存在一键命令：

- **结构侧**（章节在卷/弧线中的位置、编号、归档）走 preview → commit 事务：

  ```text
  python3 <toolbox>/scripts/novelctl.py project structure archive-preview --project-root <project> --chapter-id <id> --reason <why>
  python3 <toolbox>/scripts/novelctl.py project structure commit --project-root <project> --operation-id <id> --plan-hash <hash> --approve
  ```

  预览返回 `operationId` 与 `planHash`，commit 必须原样回传两者并显式 `--approve`；误提交用 `structure undo --operation-id <id>` 回退，历史见 `structure operations`。

- **规划侧**：用源记录解析与受审预览修改实际 plan 片段；当前 sourcePlanHash 变化后，受影响的阶段 A 与下游证据重新审查。废弃计划显式归档，不保留两份活动权威。

重规划完成后运行 `project doctor` 确认结构与正文无漂移，再继续流水线。

<!-- prompt:fact-handoff -->
## B 事实盘点与收尾交接

B 的 `factAssessment` 明确区分未评估、缺材料、已核对无变化与存在需回流变化；结论与引用复用
continuity 席。存在需回流项时，沿用 `findings.findingId` 保存 `factChange`，它只是持久化义务，
不是新的设定真源。正文缺陷已修不等于有效新事实已登记，POLISH_READY 也不能清除待办。
后续报告必须保留未处置项；不再需要登记时，显式给 `not_required`、当前引文与理由，保留历史。

正式交接顺序为：已批准 B 盘点 → 正式定稿 → 连续性/摘要确认 → 受审源预览与作者批准 → 正文引证 → 下一章。约定候选批次的条件交接遵循前述候选链边界，不提前写入正式事实。
事实待办不阻止正式定稿和连续性确认，避免 chapter-facts 的前置条件形成循环；它阻止最终交接。
新 topic 事实的 chapter-facts 输入带对应 findingId；规范值/既有事实修订走原源事务，再以精确 fact
片段或规范值属性登记引证，人物/字段父 ID 不能冒充具体事实。writingNotes.newFacts 不提供回流通道。

定稿时在现有 continuity 批次封存 B 工件 hash、正文 hash 与 bindingGeneration，证明这份正式正文
当时完成过事实盘点。该历史证明不批准新源、不恢复已失效的 A/B 批准。回流后仍核对真实源请求、
具体事实记录与当前引证；正文或代次改变、报告被替换、事实再次改变使相关证明失效。
空 factRequestIds、空 changes、空 continuity items 均不能代表已确认无变化。

<!-- /prompt:fact-handoff -->
