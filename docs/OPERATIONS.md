# 日常结构维护

按已确认方案批量修订设定，先查询当前源记录与操作定义，选择对应受审入口并明确影响范围。源字段及写入边界见 [单一真源约定](SINGLE-SOURCE.md)。

实体、卷、弧线和章节归属都有结构事务入口，不需要手改 `.gnovel/structure.json`。新入口的 CLI 参数、MCP schema 与 HTTP 路由共用 `scripts/core/catalog_api.py`，业务位于 `catalog_commands.py`；已有结构操作仍由 `structure_commands.py` 实现。

工具提供可计算的证据，模型判断语义，作者作出创作选择与批准。责任边界见 [产品意图](PRODUCT-INTENT.md)。

## 不写临时脚本的章节交接

以下入口只接受 manifest 声明的当前项目格式。工具承担确定性搬运：分配 ID、保存输入、生成工件信封、绑定 hash、运行机械诊断、计算引文偏移和接续中断。工位提交正文、计划、审校结论、事实与摘要；执行批准动作时消费已经明确覆盖本章及该步骤的作者授权，不因工具分步而重复询问。

客户端已连接MCP时优先使用目录给出的结构化调用，CLI是同一操作的备用入口；两者都沿下表的高层流程。开始写审校内容前先取review-form，已有完整内容才交submit或artifact-pack；静态artifact-template和低层artifact-register不用于拼当前绑定或猜实际席位。

| 已有内容 | 首选操作 | 工具完成的工作 |
|---|---|---|
| 本章工作标题与完整推演 | workflow.chapter-preview | 建章与计划进入一个 preview，自动分配 ID |
| 单份正文或报告 | workflow.artifact-pack | text/payload 保存、绑定和登记 |
| 一章的成组工件 | workflow.submit | 按依赖排序、逐阶段事务、冻结 requestId 与恢复 |
| 待填写的阶段 A/B 审校 | workflow.review-form | 当前材料绑定、实际席位与空白输入对象 |
| 当前状态工件 | workflow.state-prepare | 生成空变化载荷、绑定并登记；不判断新事实 |
| 已读材料上的明确结论与引文 | evidence.pack → evidence.submit | 精确偏移、hash、逐批登记与恢复 |
| 已复核的摘要变化 | summary.commit-delta | 当前输入绑定、保留未改条目 |
| 已授权的章节收尾 | workflow.closeout-prepare → workflow.closeout | 定稿、摘要、连续性逐步执行并原子记录回执 |
| 明确列出的本章新事实 | source.chapter-facts-preview → source.commit → source.chapter-facts-complete | 装配 topic 事实及正文引证，按源事务批准后登记 |

新章预览接受 `outlineText`、`planField`（已有 chapter topic 字段 ID 或 key）、明确的 `dependsOn` 及可选 `chapterContract`；volumeId 与 placement 二选一。错误中的 `availablePlanFields` 给出本项目可选字段实例，`source.schema` 解释字段形状；空新书按 `createFieldAction` 填真实 key/label/reason 和新 requestId，经原 source.change-preview→commit 注册字段后，用原建章 requestId 重试，不会重复建章或烧掉失败请求。工具不默认替作者创建固定字段。返回 `identities.chapterId` 和冻结 `nextAction`；预览本身不批准。

`approve-chapters` 的语义是替换全部范围。回执 `scopeChange` 列出 before、after、added、removed；缩小范围不会保留被移除章。新工件不能继承旧 hash 的批准，普通批准继续由宿主转达已经明确的作者授权，不宣称独立验证真人。

工作台已激活稿先取 `workflow.candidate-handoff` 的冻结 selectionRef，再显式说明计划适用与 writingNotes 处置交给 artifact-pack/submit。保存、激活和转交各有身份；当前选择未对齐时，不能重审旧登记稿来定稿。fresh B 会带上历史未决问题，S1/S2 必须显式处置，S3 建议随稿保留。

正文提交的 MCP 参数示例（替换 projectRoot、章节与全文）：

```json
{
  "projectRoot": "/path/to/书",
  "requestId": "ch001-draft-1",
  "chapterId": "CH001",
  "artifacts": [{"artifactType": "candidate_prose", "text": "工位完成的整章正文。", "writingNotes": {}}],
  "advanceLifecycle": true,
  "diagnostics": {}
}
```

工具名是 `submit_chapter_artifacts`。空 diagnostics 从本章计划读取模式与强度；无法解析时返回缺项。诊断失败仍保留已登记候选。恢复只传同一 requestId，不重发正文，也不换选项：

```bash
python3 scripts/novelctl.py project workflow submit --project-root /path/to/书 --request-id ch001-draft-1
```

审校完成后可一次提交 stage_b_review 和 title_candidates。审校用 formRef＋edits，已有完整内容可用 payload；已有 state_candidate 载荷也可同批提交，通常用下述 state-prepare 生成。每种类型最多一份；阶段 A 必须单独提交，批准后才推进 planned。工具不补席位结论、不伪造诊断、不自动选标题。正文引用与候选版本不符、输入变更、工件被替换或旧请求换载荷均拒绝续办。

批次 `submissionComplete` 只表示指定登记阶段完成。查看 results 中的诊断 passed、未解决 findings 与 chapter receipt；approvalAction 需要现有明确作者授权，调用不会因批次成功自动发生。诊断、语义审校、批准、定稿与连续性是分别可核查的结果。

### 约定整批候选

先用 `workflow.candidate-batch` 的 requestId 与明确 chapterIds 冻结既有授权中的本次委托。Planner 使用 `workflow.candidate-recipe`（或 prepare 的 batchRef）取得真实上游身份与条件交接；A review-form 传同 batchRef，完整 A 包装可传返回的 recipeRef。原阶段 A 获已有授权后，candidate 继承其 recipe，不在写完后补选前章。

前章有当前真实机械报告、完整 B 与已处置义务后，可以供下一章候选使用；这一步不要求把候选偷偷正式定稿。候选中的事实以有来源的条件材料显示，不能由普通 Canon 查询或正式引证认领。新取舍仍交作者。

`workflow.candidate-progress` 推导每章当前进度，`workflow.candidate-reading` 冻结原文与建议，`workflow.candidate-export` 将明确身份的阅读快照导出到工作台目录。第二章未完成时仍交付第一章有效候选、第二章现稿与问题、第三章未开始以及原续办。正式接纳用原逐章 closeout；实际接纳时准备 state/CAS 并取得确切工件批准，不预批未来工件。纯身份转换须有原 finalize 与实际 continuity 提交证据，真实计划、事实或标题变化仍核验受影响链。

### 外部改稿检查材料

accept-external 预览返回原 requestId、冻结 H1/H2、对象观测与差异。找不到准确 H1 时明确材料不足；不会用摘要还原稿件。原请求状态与仅到期续期已接通，输入变为 H3、对象更换或附件变化都拒绝续用。当前独立用户在场批准组件尚未提供，`execution.executable=false`；预览成功不是采用批准，普通 commit 仍拒绝。重新取最新 hash 不能修复这个能力缺口。

### 直接填写审校表单与生成状态工件

```bash
python3 scripts/novelctl.py project workflow review-form --project-root /path/to/书 --chapter-id CH001 --artifact-type stage_b_review
python3 scripts/novelctl.py project workflow state-prepare --project-root /path/to/书 --chapter-id CH001 --request-id ch001-state-1
```

review-form 默认返回 formRef、fields、guide 和 submitAction。工具在临时快照冻结当前材料与实际席位；fields 只交付需填写的语义部分。阶段 B 保留全部结论槽位，已有写作说明时附待裁决项。把填写后的 fields 放入 edits，连同 formRef 交给 workflow.submit；不重发哈希、席位列表或整份信封。返修可附 repairRef。输入变化后读取变化、重新取表并实际复核受影响结论。

每个审校条目的结构为 `{"artifactType":"stage_b_review","formRef":"取自表单返回","edits":{}}`，其中 edits 必须填入真实判断，空对象不能完成审查。payload 与 formRef／edits 二选一。语义增量不能改绑定或删除既有审查义务；未填写保持缺项。首次提交后，同一 requestId 的恢复使用已持久化输入，不依赖临时 formRef 仍存在；未完成 repair 子工件仍需原 repair 缓存，边界见[精简模型交付](MODEL-OUTPUT.md)。

`source.schema.chapterContract.inputSchema` 给出模式等基础字段形状；默认 guide 提供语义说明与枚举，完整嵌套覆盖与条目定义沿 fullAction 的 guide.fieldSchemas 查看。格式拒绝看具体field／allowed与details.recovery；不能用更换hash或改成“不适用”消除真实审校义务。计划未完成适用性评估时先核对并经受审源事务处理，重复取空表不能补齐计划。当前源本身损坏时，普通源预览同样阻断；回执只给可读schema并标明需受控维护恢复有效基线，不虚构能直接修复坏源的普通写命令。

默认语义表单较大时会返回 responseCompacted=true 和 readAction；沿该动作分页读完。核对完整绑定时用 fullAction 读取同一冻结表单，或在首次取表时指定 `--view full`。不要为拿到绑定详情反复取表，再把新表引用套在旧结论上。

state-prepare 自动生成并登记当前状态绑定，省去手填空 changes；重试只传原 requestId。返回的 approvalAction 仍按已有作者授权执行。该工件不盘点新事实，空 changes、空 proposedFacts 或 `factsAssessment=not_performed` 均不能解释为本章没有事实变化。

## 按意图找工具与导出

不知道 operation 名时先描述意图；不要把默认 MCP 列表当作全部库存，也不必为一次查询展开 audit。

```bash
python3 scripts/novelctl.py project tools list --query '把全部设定导出成一个 Markdown'
python3 scripts/novelctl.py project tools list --query '查看人物资料'
python3 scripts/novelctl.py project tools list --operation source.export
python3 scripts/novelctl.py project source export --help
```

`query` 只按 `shared/registries/tools.json.discovery` 中的显式词组匹配，不读取项目、不执行操作、不猜项目模式或作者批准。返回少量完整定义及匹配词；`hasMore=true` 时缩小意图或按 ID 精确查询，不截断 schema 后冒充完整参数。数量、长度来自该注册表，响应预算复用 query 注册表的实际 UTF-8 字节上限。未知意图返回空候选，多个候选由调用者按用途选择；`query` 不与 `operation` 或 audit 同用。

| 任务 | 入口 |
| --- | --- |
| 查看人物、地点等当前源字段 | `project settings show` |
| 分页筛选已登记设定与规划（single-source） | `project source query` |
| 预览设定与规划的 Markdown 文本 | `project source render` |
| 保存完整设定与规划阅读快照 | `project source export` |
| 按章节顺序拼接已入库正文 | `project export` |
| 按字面词找设定里的不一致候选（命中不等于冲突） | `project source search` |
| 时间锚点与章节跨度的顺序问题 | `project structure timeline` |
| 按显示编号查一章的稳定 ID、标题与进度 | `project workflow chapter-status` |
| 多章重排、归档、登记实体合成一次提交 | `project structure batch-preview` |

独立查询和导出不需要章节 prepare。`source render/export` 的选择、预设与覆盖边界见 [长段与阅读快照](SINGLE-SOURCE.md#长段与阅读快照)。未指定 selection 或 presetId 时取全部已登记设定与规划，默认不含归档，不包含正文或项目备份；`requireReviewed` 默认不代替语义审查，有预设时沿用预设要求。`project export` 默认只取 stable 章节，并核验当前正文归属及实际字节；所选缺章在回执和文件中标记部分交付。目标已有不同内容时默认拒绝，明确覆盖须同时传 `--overwrite` 和 `--expected-file-hash`；成功替换保留旧输出副本。正文归属与输出保护见 [当前正文归属](R1-CURRENT-OWNERSHIP-BINDING.md)。

```bash
python3 scripts/novelctl.py project source export --project-root /path/to/书 --output 工作台/设定与规划.md
python3 scripts/novelctl.py project source export --project-root /path/to/书 --output 工作台/含归档阅读.md --selection '{"includeArchived":true}'
python3 scripts/novelctl.py project export --project-root /path/to/书 --format md --output 工作台/成品正文.md
```

MCP 先调用默认可见的 `list_capabilities`，参数如 `{"query":"导出全部设定"}`。结果 `invocation.tool` 指出可调用入口：`read_discovered_operation` 只接受已注册且允许 MCP 的 read 操作，`export_discovered_operation` 只接受 export-write 操作。外层必须显式给 `projectRoot`，内层 `arguments` 按目标的 `inputSchema` 填写，不能再放 projectRoot。

```json
{
  "name": "export_discovered_operation",
  "arguments": {
    "projectRoot": "/path/to/书",
    "operation": "source.export",
    "arguments": {"output": "工作台/设定与规划.md"}
  }
}
```

两个入口不接受任意 shell、内部维护或审批操作，不能递归调用；原领域校验、POV 选择、锁和路径保护全部照常执行。导出仍是写操作，必须由调用者显式请求；只读调用保持原模型视图与私有证明引用。候选写入等不属于这两个入口的操作继续通过其原 CLI／MCP 入口执行，不会因目录命中而获得授权。CLI 也可使用 `project tools read/export`，但已知目标时直接调用原命令更简洁。参数帮助与示例来自同一 OperationSpec，部分命令由 CLI parser 反射，不在 Skill 中复制全量参数表。

## 当前源字段筛选与图导出

四维 query（logic／timeline／attributes／nodes）的 kind 接受当前源 owner 类型，包括 project、volume、arc、unit、chapter 和实体类型；完整枚举以运行时 schema 为准。field、hasField 与 fieldEq 的键必须解析为已登记 FieldDefinition 的稳定 ID、key 或唯一 label；未知字段或歧义标签拒绝，不猜同义字段。fieldEq 的值是显示投影中的精确字符串，不表示语义相等。

```bash
python3 scripts/novelctl.py project query attributes --project-root /path/to/书 --kind character --view list --field-eq '{"appearance.skin":"白皙"}'
python3 scripts/novelctl.py project query nodes --project-root /path/to/书 --kind chapter --view list
```

第一个例子假设 appearance.skin 已登记；实际先读 source schema／字段定义。四维 query 不再接受旧 scope 参数；source scan-report 的 scope=source|prose 是扫描范围选择，仍是不同的明确参数。

图与时间线可按需导出 Mermaid 阅读快照：

```bash
python3 scripts/novelctl.py project structure graph --project-root /path/to/书 --format mermaid --output 工作台/关系图.md
python3 scripts/novelctl.py project structure timeline --project-root /path/to/书 --format mermaid --output 工作台/时间线.md
```

项目内输出只允许工作台，项目外需要显式绝对路径。快照不带 GENERATED 托管标记、不维护索引或反向更新真源；已有不同内容返回 EXPORT_EXISTS 并保留原件，同内容可以复用。图导出是作者侧阅读资料，不能当作经过人物视角过滤的上下文。

## 单操作参数与摘要格式

需要查一个操作时，不展开完整 audit 库存；精确选择返回同一 OperationSpec 的用途、效果类别、必填项、嵌套参数和可用示例。发现写入口不构成执行或作者批准授权。

```bash
python3 scripts/novelctl.py project tools list --operation workflow.patch-candidate
python3 scripts/novelctl.py project task summary-prepare --project-root /path/to/书
python3 scripts/novelctl.py project task summary-commit --project-root /path/to/书 --input 工作台/摘要.json
```

摘要准备返回 schemaVersion 与 inputSkeleton。填写语义字段后提交，机器版本不猜测；完整输入快照和 inputSnapshotRef 均可用。提交和读取使用同一格式校验，不支持的版本在保存前拒绝。差异引用只绑定材料，不证明已理解正文或已经批准。

大批输入可在正式预览前汇总参数错误，不必逐次提交后只修一个字段。`arguments` 文件保存目标操作的参数对象，工具只按同一 OperationSpec 校验，不读取其中的业务文件路径、不解析章节身份，也不调用目标处理器：

```bash
python3 scripts/novelctl.py project tools validate --project-root /path/to/书 --operation source.fact-patch-preview --arguments @工作台/批量修订参数.json
```

`ok=true` 表示预检完成；是否通过结构检查看 `schemaValid`。`errorCount` 是本次发现的错误总数，`errors` 按原 JSON 文本分页，包含精确字段路径；同一 id 的 start/end 连续拼接后得到完整错误对象。续页可用 `--validation-ref` 回传原 validationRef，省去重复发送整批参数；operation、limit 与 maxBytes 不变，仅增加 `--cursor`。引用只检查首次冻结的参数，不表示后来编辑的参数文件仍相同；改完后用新的 arguments 重新预检。缓存丢失或校验契约变化时旧引用拒绝使用，长字段名或联合分支证据不截断。

若各分支共享必填常量字段，且调用者给出的 kind／op 等值能唯一确定分支，预检直接列齐该分支的独立字段错误，并标记 schemaBranch；这是按显式参数确定 JSON 形状，不是语义猜测。父类型错误或联合形状未定时，descendantChecks 说明未完成的下层检查，alternatives 保留分支拒绝依据。预检通过不表示目标存在、业务约束满足、权限有效或作者已批准；正式调用仍运行原验证器与领域门禁。

已有摘要只改部分条目时，用 prepare 返回的 deltaSkeleton。`prepareRef` 同时绑定准备输入和旧摘要版本；`storySummary` 仍须由工位完整重述因果，不能只交新增段落。`plotLine` 以 chapterId、`characterSummaries` 以 characterId 指定 `op=add/replace/delete`，新增和替换必须给 summary，删除必须显式给出；未提交的条目原样保留。工具填充项目、格式、覆盖范围、来源和输入快照，不搬运这些机器字段，也不猜语义。

```bash
python3 scripts/novelctl.py project task summary-delta --project-root /path/to/书 --prepare-ref <准备引用> --story-summary '经正文复核后的完整因果重述' --plot-line @工作台/章节摘要差量.json
python3 scripts/novelctl.py project task summary-read --project-root /path/to/书 --chapter-id CH001
python3 scripts/novelctl.py project task summary-read --project-root /path/to/书 --view history
python3 scripts/novelctl.py project task summary-read --project-root /path/to/书 --version <历史版本>
```

差量项示例：`[{"op":"replace","chapterId":"CH001","summary":"本章经复核的关键推进"}]`。输入变化或另一会话已经提交摘要时拒绝旧 prepareRef，必须重新准备；完整 inputSkeleton 带 prepareRef 时执行相同并发校验。完整摘要输入也可显式提交。每次替换前保留旧摘要精确字节，历史版本按摘要 hash 查询，不进入当前 Canon，也不获得审校或批准信用。摘要读取按字符分页并受实际 UTF-8 字节预算限制，续页固定筛选条件和版本。

## 增量读取与候选装配

`task read-batch` 的 credential 与 sessionReadReceipt 是项目绑定短引用，旧压缩回执仍可输入。跨工位或下一轮读取时以 priorReceipt 回传；仍须先取得该轮有效 credential/readIds。同一分页序列固定 credential、readIds、预算、priorReceipt，仅增加 cursor；末页回执累计本序列已交付字符范围。新批次按同文件版本的绝对字符覆盖做并集和差集，包含和交叠范围只交付尚未持有的部分；源查询还绑定其内容表示。全部覆盖的材料汇总到 unchangedSummary，全部未变时一页返回空 items；部分材料保留原 id、fileStart、start/end，多段空洞可分别分页。文件改动则重新交付。忘记回执或需要重读时不传 priorReceipt，不影响权限与语义审查义务。

引用缓存保存在用户私有临时目录，项目文件保持只读。准备快照按当前授权、实际内容、查询成员集合和工具箱依赖校验，续页不重新执行完整准备。未登记且未消费的工作台笔记不构成依赖；已登记工件、约束、真源和查询成员变化仍失效，错误的 changedItems 指出变更项。缓存缺失或依赖变化必须重新准备，旧引用不会换绑最新版。短引用不证明模型仍记得文本，也不获得审校或批准信用。mappingsRef、reportRef、diff.reference 与 MCP 的 contextProofRef 可通过下列入口分页读取历史审计快照：

```bash
python3 scripts/novelctl.py project task snapshot --project-root /path/to/书 --reference <原引用>
```

准备报告 reading.sourceReads 为单一真源查询提供可选 readId，与文件 readId 使用同一批读入口。查询选择器固定为本次准备值；指定 POV 时只交付相应认知内容，计划读取仅提供 plan 片段，Canon 和其他登记输入继续走 POV 内容查询。未知影响与缺失设定不因为交付完成而被判为一致。

返修可把完整机械检查与返修首屏一起交给工具。显式请求保留既有候选／诊断写集及批准边界；出错时返回已完成阶段，相同请求重试复用原候选：

```bash
python3 scripts/novelctl.py project workflow patch-candidate --project-root /path/to/书 --credential <准备凭据> --patches @/path/to/书/工作台/补丁.json --request-id repair-001 --automatic-repair --advance-lifecycle --diagnostics '{"mode":"plot","intensityLevel":1}'
```

自动返修显式传 `automaticRepair=true`（CLI 为 `--automatic-repair`），并提供 requestId；patch-candidate、artifact-pack 和 submit 的整章重写共用同一计数路径，默认 false。上限在候选提交前检查，计数与候选及请求回执同一事务提交；提交前失败不计轮，候选已登记后的诊断失败保留该轮。原请求重试不重复计数，不再另调 repair-round。repairAccounting 返回实际轮次，计数不表示修复已通过。

repair-context 按共享字符窗口与独立问题分页，text 是该材料的原始文本切片；同一个 id 的 start/end 连续拼接后得到完整材料。finding 的完整字段与定位保存在其 JSON 文本材料中；外层 finding、location 是紧凑定位，截断显式标记。paragraphCalibration 只附在相关窗口，仍不影响 passed。diagnostics 默认模型返回通过报告引用读取完整证据；需要完整审计接口时在 diagnostics run 指定 `--view full`。

```bash
python3 scripts/novelctl.py project task outline-check --project-root /path/to/书 --chapter-id CH001
python3 scripts/novelctl.py project workflow review-skeleton --project-root /path/to/书 --chapter-id CH001 --artifact-type stage_b_review --output 工作台/CH001-review-input.json
python3 scripts/novelctl.py project workflow artifact-pack --project-root /path/to/书 --chapter-id CH001 --artifact-type candidate_prose --input 工作台/CH001-候选.md --advance-lifecycle --diagnostics '{"mode":"plot","intensityLevel":1}'
```

推演检查是只读算术与解析。定位表字数用 `下限-上限`，节奏段字数列用整数；张力等级曲线和非唯一 POV 不擅自缩成单值。`outlineFacts` 还列出无法解析的实体、缺失引用边和原有结构投影预览入口。诊断参数缺省时从可解析定位字段补齐，mixed 的 sceneParagraphs 仍须显式给出。结构化单源计划不从导出的 Markdown 反推。

建章与计划可用 `workflow chapter-preview` 一次预览；计划正文与合同由工位提供。`artifact-template` 同时接受 `--type` 和 `--artifact-type`。工件模板与审校骨架的 `_guide` 列出 schema 中的枚举；`artifact-pack` 丢弃这份说明，不把它登记为结论。

`advanceLifecycle` 只适用于候选正文打包或 `register=true` 的诊断；最高停在 review。候选登记与 draft 同写集，诊断登记与 review 同写集；第二阶段故障会返回 `ARTIFACT_PACK_DIAGNOSTICS_INCOMPLETE`，保留已完成的候选登记，可直接重试诊断。机械失败报告也可进入 review，语义结论与作者批准继续独立执行。未给该选项时保持原有独立状态迁移方式。

Sepia 量测在聚合报告 `calibration` 中，精确位置和段落统计在 repair-context 分页中。它们不进入机械 findings 或 passed 的计算。连续性仅登记本章明确确认，源事实通过受审源提案处理；证据输入变化后重新读取。

局部源记录修订：先 resolve 取得 `sessionRef`、`recordHash`，再向对应 proposal-preview 传 `expectedRecordHash` 和 patch，省略 expectedOldRecord/newRecord。每项形如 `{"op":"set","path":["values",0,"value","number"],"value":185}`、`{"op":"remove","path":["summary"]}` 或 `{"op":"text","path":["text"],"original":"原文","replacement":"新文"}`。原文必须唯一命中，重叠路径拒绝，数组元素不能 remove（避免索引移动）；完整旧、新记录输入也受同一校验。`kind=planning` 用于既有规划片段，仍须预览后经结构提交批准。Canon 提案只接受一个原有可写字段；来源证据、作者声明和禁止变更身份的校验继续生效。

### 批量解析与规划补丁交接

已选定多个同类记录时，用 `source content-resolve-batch` 在同一快照上解析，支持 attribute、fact、planning 三种 kind。它返回紧凑绑定和每条记录的 `recordRef`／`readAction`，不重复返回完整长记录；未找到或不在章节范围内的目标标为 `not_found`，不给写入绑定。数量与分页沿用 query 策略，不提高预算。

```bash
python3 scripts/novelctl.py project source content-resolve-batch --project-root /path/to/书 --kind planning --record-ids SF-FIRST --record-ids SF-SECOND
```

后续页保持 kind、recordIds 顺序、chapterId 和预算，仅增加返回的 cursor。续页复用已解析快照，源、授权或工具规则变化则拒绝；原文仍须经 readAction 阅读。

将返回的 `resolutionRef` 保存为 `RESOLUTION_REF`。规划补丁文件只需给出原文与替换，不必逐项转写 hash：

```json
[{"action":"patch","collection":"fragments","id":"SF-FIRST","patch":[{"op":"text","path":["text"],"original":"已读取的唯一原文","replacement":"已确定的新文"}]}]
```

```bash
python3 scripts/novelctl.py project source change-preview --project-root /path/to/书 --resolution-ref "$RESOLUTION_REF" --changes @/path/to/书/工作台/规划补丁.json --request-id 本轮规划 --reason 已确定的规划修订
```

引用只补齐 baseRevision 和 expectedRecordHash，只接受其中已解析的规划片段补丁。显式值与引用冲突、重复目标、身份／归属变动或规划升格仍拒绝；预览后仍经原 `project structure commit` 批准提交。无引用时必须提供原版本和逐项 hash，完整接口不变。完全相同的请求重试沿 requestId 复用已有计划／回执，不因缓存丢失重做；新请求不能借旧引用自动换绑。

多类型的既有设定更新可用 `project source proposal-batch-preview`，按同一 sourceHash 自动装配逐项旧值绑定，不能借批量扩大原单项权限；准备、提交与恢复须使用同一源版本和请求身份，字段说明见 [单一真源约定](SINGLE-SOURCE.md)。大字段回执可用 `project source change-report --view text` 完整分页，不需绕过工具直接读事务文件。

已用字段的语义键、类型和适用对象可用同一批量入口的 `field_migration` 项修订，全部属性和已知值须明确列出，属性证据沿原规则核验。字段及所有声明属性共同占用批次预算，未改属性也不能与另一项重复。名称／说明仍可单用 `field_definition`。范围限制见 [读取与修订字段](SINGLE-SOURCE.md#读取模式)，实际参数以当前操作定义为准。

审校报告使用 `project review report-view`，核验原登记与实际稿件后返回版本适用性。`known` 只表示送审正文摘要可识别，`matchesBody` 表示正文一致，`current` 还要求相关计划与绑定成立；完整但失效的报告保留建议，损坏报告返回错误。`project review note-issue` 只注释当前适用报告内的真实问题，工作笔记不构成通过或批准。报告的读取与定位见 [模型交付](MODEL-OUTPUT.md#查看问题与完整证据)。

## 章节工件的登记与批准

工位已有正文或语义结论时，优先直接传 `text` 或 `payload` 给 `artifact-pack`，或以 `workflow submit` 提交可恢复批次；工具负责保存到 `工作台/`。`input` 是相对项目根的路径；正文读取 UTF-8 文本并保留换行，显式 beat 注释转为段落范围元数据，其余类型读取 payload JSON 对象，不需要手写 envelope、项目/章节身份或绑定 hash。规则卡与检查范围通过 `project diagnostics rules` 查询；正文偏离与新事实交接按 [章节契约](../shared/contracts/CHAPTER-PIPELINE.md) 执行。

```bash
python3 scripts/novelctl.py project workflow artifact-pack --project-root /path/to/书 --chapter-id CH001 --artifact-type candidate_prose --input 工作台/CH001-候选.md
python3 scripts/novelctl.py project workflow artifact-pack --project-root /path/to/书 --chapter-id CH001 --artifact-type stage_b_review --input 工作台/CH001-审校内容.json
```

回执返回新工件 `artifactPath`，随后按该文件批准。支持 stage_a_review、candidate_prose、stage_b_review、state_candidate、title_candidates、repair_plan；diagnostics 使用已有机械运行器。阶段 A 的 activeSeats、阶段 B 的全部席位结论、verdict 和 findings 由工位提供，不自动补通过；缺少 title candidateId 时按本批顺序生成。状态候选复用入库的结构校验，非空设定变化走 source 事务。省略的绑定按当前真源计算，显式填写的绑定必须相符；先登记正文再包装依赖正文的报告，正文变更后重新审查。工具只能验证绑定与结构，不能证明语义审查发生过。

从 12.81 起，新登记或再次入库的 `POLISH_READY` 报告必须逐席提供当前正文引文，或说明该席为何不适用。旧报告仍能读取；缺少这些材料会收到 `REVIEW_EVIDENCE_REQUIRED`，需重新核对后登记。席位输入例如：

```json
{"conclusion":"钥匙交接在当前段落中有记录", "evidence":[{"text":"他把钥匙放回桌上，合上了登记本。"}]}
```

引文必须与候选正文逐字一致，重复出现时提供 `start`、`end`（Unicode 字符偏移，左闭右开），也可使用原证据页的 `page` 绑定。不适用时省略 evidence 并填写 `notApplicableReason`；不能同时声称有证据与不适用。`finalProseSha256` 由包装器补齐。占位结论、错误引用和未解决的阻塞项会拒绝完成登记，核验通过仍不构成作者批准或语义正确证明。

入库预检使用现有回执，CLI 与 MCP 默认 `model` 视图：

```bash
python3 scripts/novelctl.py project workflow chapter-receipt --project-root /path/to/书 --chapter-id CH001 --skip-title
python3 scripts/novelctl.py project workflow chapter-receipt --project-root /path/to/书 --chapter-id CH001 --skip-title --view full
```

model 保留阻塞、所有权、`bindingGeneration`、工件摘要和读取完整回执的参数，不回显正文；full 保留完整 payload。正式定稿失败时 details.blockers 列出本次预检收集的阻塞，首个错误码与实际失败一致。能力目录中“创建章节”“新建章节”指向结构预览，“定稿预检”指向回执，“写下一章”指向 `task.prepare` 的 `task=continue` 示例，由当前状态选择任务。

定稿和连续性回执将摘要过期标为 `refreshRecommended`，当前策略下 `refreshRequired=false`。`not_required` 只表示作者确认本批次不刷新，并保留过期警告；`refreshed` 仍要求当前摘要新鲜及 hash 一致。连续性完成只证明该批次已处置，不代表摘要已刷新。

工件写入按信封 hash 命名的新路径，与 workflow 登记同时事务提交；输入和绑定来源变化会否决本次提交。原文件不被包装结果覆盖，已有批准不会自动授予新工件。

流水线工件的 hash 由工具计算，AI 不手抄十六进制串。登记时用 `--bind` 注入绑定字段；批准时用 `--artifact` 按文件提交。绑定语义与 finalization 的核对一一对应，只是把入库时才会爆的绑定漂移提前到注册时失败。

```bash
# 绑定注入：FIELD 按当前真源取值；FIELD=PATH 额外核对文件内容与真源一致
python3 scripts/novelctl.py project workflow artifact-register --project-root /path/to/书 --chapter-id CH001 --artifact /path/to/书/工作台/CH001-diagnostics.json --bind draftSha256
# 一次注入多个字段
python3 scripts/novelctl.py project workflow artifact-register --project-root /path/to/书 --chapter-id CH001 --artifact /path/to/书/工作台/CH001-state_candidate.json --bind finalProseSha256 --bind baseStateSha256 --bind designSha256 --bind sourcePlanHash

# 按文件批准：读信封自带类型与 hash，仍走注册条目核对与文件新鲜度检查
python3 scripts/novelctl.py project workflow artifact-approve --project-root /path/to/书 --chapter-id CH001 --artifact @/path/to/书/工作台/CH001-diagnostics.json
# 混用与按 hash 的入口均可
python3 scripts/novelctl.py project workflow artifact-approve --project-root /path/to/书 --chapter-id CH001 --artifact @工作台/CH001-stage_b_review.json --artifact-hash title_candidates=<sha256>
```

可绑定字段按工件类型白名单校验：diagnostics 的 `draftSha256`；stage_a_review 的 outline 绑定；stage_b_review 的 `finalProseSha256`；state_candidate 的全部绑定字段。正文绑定取已注册 candidate_prose 的 `payload.text`（与 finalization 同一基准），未注册 candidate_prose 时先注册正文再绑定其他工件。计划绑定字段使用 `sourcePlanHash`。状态绑定直接计算当前单一真源投影；缓存不是状态权威。入库仍检查状态候选与当前投影一致。

批准文件必须与注册条目同一内容：信封 hash 不匹配、文件被篡改、信封 `chapterId` 与目标章不同、同类型给出两个不同 hash，都拒绝。登记回执在工件条目上记录 `bindings`（来源路径或 `current_truth`、内容 hash、注入前旧值），审计不再依赖会话记忆。`--artifact-hash type=sha256` 文件入口保留，行为不变。

## 返修增量与章节看板

返修每轮重跑完整 aggregator 后，用增量对比代替重读全部 findings；看板一次呈现章节状态，替代多次查询拼装。两者都只读、实时重算，不消费缓存的就绪结论。

```bash
# 返修增量：against 缺省取该章已注册的 diagnostics 工件；也可显式指定任意上一轮报告
python3 scripts/novelctl.py project workflow diagnostics-diff --project-root /path/to/书 --chapter-id CH001 --current /path/to/书/工作台/CH001-diagnostics-v2.json --view model
# 章节看板：阶段、工件登记/批准/新鲜、阻塞原因、由实际状态推导的下一步
python3 scripts/novelctl.py project workflow chapter-status --project-root /path/to/书 --chapter-id CH001
```

增量对比按检查器锚点字段归组，行号与周边文本不进身份——同一问题行号漂移报 persisting 而不是一修一增。同锚点多处时按组报告 fixed/persisting 数量（`mixed`），工具无法知道旧报告哪一行对应新报告哪一行，不假装逐处配对。锚点字段缺失或改写导致整句 evidence 变化的条目显式标 `unmatched` 两侧呈现；validate_text 的整句改写会呈现为一对 fixed+new，coverage 里写明这一边界。组状态 `fixed`／`persisting`／`new`／`mixed`／`severity_changed` 只是呈现，不证明「问题真的修好了」。

`--view model` 将数量、严重度未变的组移入按来源归组的 `persisting`，逐项保留 identity、当前 lines、count、severities；这些仍是未解决问题。`groups` 保留变化项，unmatched 两侧完整保留。省略 view 或使用 `--view full` 保持原审计输出。prepare 的 model 视图同样只移除工具计划的重复描述与执行记账字段，工位必读材料、阻塞和授权信息不省略。

看板的 `nextActions` 由实际证据推导（注册了什么、批准没有、闸门缺什么），同一阶段存在多个合法分支时全部列出并附依据字段；`needsApproval: true` 的动作等待作者，不是可自动执行。`blockers` 只在 review/stale 阶段展示入库预检结论——其他阶段跑预检必然报「还没到入库」，那不是本章待办。命令是入口提示，不构成授权。

## 聚合查询与派生历史索引

章节待处理事项、角色复核负担、已提交设定变更影响和作者决定关联计划，可通过同一个只读接口分页查询。默认 `view=summary` 返回分组计数，`view=items` 返回带稳定 ID 和原领域详情入口的条目；不是另建一套通过判定。章节汇总的规则、流程阻塞、报告问题可能描述同一义务，因此计数不是互不重复的语义错误数。

```bash
python3 scripts/novelctl.py project query aggregate --project-root /path/to/书 --kind chapter-issues --chapter-id CH001
python3 scripts/novelctl.py project query aggregate --project-root /path/to/书 --kind character-reviews --character-id C001 --view items
python3 scripts/novelctl.py project query aggregate --project-root /path/to/书 --kind setting-impact --operation-id OP-EXAMPLE
python3 scripts/novelctl.py project query aggregate --project-root /path/to/书 --kind decision-plans --decision-id AD-EXAMPLE
```

`character-reviews` 按片段归属统计，不推断角色在正文中的隐式参与；`chapter-issues` 的源复核范围来自已登记章计划。两类源内容汇总的 `selection` 沿用原内容选择器；`knowledgeMode=perspective` 仅允许相应认识身份的片段，章节汇总省略作者侧流程、诊断和规则内容。设定影响与作者决定属于作者侧资料，不能混用 POV selection。设定影响显示原操作冻结的历史范围和撤销标记，不冒充当前批准；决定关联只跟踪显式范围及依赖决定，不推断隐式因果。

历史查询索引现有操作日志、候选版本、保留的候选／诊断工件、workflow 当前及已有备份快照、语义复核／正文引证／规则回执。只返回历史元数据，不返回候选正文、引文和人物认识内容，不继承审校信用。`historicalSnapshot` 不代表当前有效；需要最新状态时回到原领域详情入口。未被原系统保留的历史不能由索引恢复，同一工件的登记、请求回执和保留文件也可能各有一条记录。

```bash
python3 scripts/novelctl.py project history query --project-root /path/to/书 --kind candidate --chapter-id CH001
python3 scripts/novelctl.py project history query --project-root /path/to/书 --kind evidence --request-id review-001
python3 scripts/novelctl.py project history index-state --project-root /path/to/书
python3 scripts/novelctl.py project history index-rebuild --project-root /path/to/书
python3 scripts/novelctl.py project knowledge index-rebuild
```

历史索引是 `.gnovel/cache/history.sqlite3` 中可删除、可重建的投影，不是新任务账本或事件日志。读操作不会偷偷重建；索引缺失、损坏、版本不兼容或输入变化时回退现有文件并标明原因，损坏真源则拒绝。显式重建复用未变化文件的元数据，删除来源后相应记录也消失。查询仅允许固定类型、chapterId、recordId、requestId 筛选，不开放 SQL；预算来自原查询策略，游标绑定筛选和输入版本。

知识资产以仓库外数据根中的 `knowledge/assets/` Markdown 为真源；配置与目录范围见 [本地资料规范](../knowledge/README.md)。目录元数据按文件身份与策略复用，并可从已校验 SQLite 恢复；重建仅对改变的启用资产重新分词，随后原子替换派生数据库。SQLite 的落盘仍是完整投影替换，不是逐行更新；真正返回知识正文时仍核验实时字节。新增、删除、移动、状态变化、同 mtime 改写和策略变化均会重新校验。原始参考小说与归档原文不进入这两个运行时检索池。

## 成组执行任务工具

需要准备材料并读取当前任务的工具动作结果时，可一次调用：

```bash
python3 scripts/novelctl.py project tools execute --project-root /path/to/书 --task editor --chapter-id CH001 --authorization read --pov-character C001 --chapter-goal 关系推进
python3 scripts/novelctl.py project tools execute --project-root /path/to/书 --task editor --chapter-id CH001 --authorization candidate --diagnostics @/path/to/书/工作台/检查参数.json
```

检查参数 JSON 使用 `mode`、`intensityLevel`，可附 `sceneParagraphs`、`strictFloor`、`register`。数值来自已批准的本章声明；mixed 的区间必须显式提供。`register: true` 只登记机械证据，批准仍是独立操作。缺参数返回 TOOL_ACTION_INPUT_REQUIRED 及字段名，不猜模式或等级。

`executed[].result` 是真实返回内容；异常或下游 `ok: false` 会使整体返回 TOOL_ACTIONS_INCOMPLETE。`taskReady` 来自本次准备结果，登记证据后重算；执行完成不代表语义审校完成。准备结果已有上下文时不再重复调用 context.read。指定 POV 时跳过没有 POV 过滤的通用 query／summary 输入，按 prepare 中的 scoped sourceReads 读取。只读授权不运行 candidate-write；其他写入和批准仍用原独立命令。

仍持有先前准备报告时，成组执行可直接使用差量交付：

```bash
python3 scripts/novelctl.py project tools execute --project-root /path/to/书 --task editor --chapter-id CH001 --authorization read --held-prepare-ref "$HELD_PREPARE_REF"
```

`HELD_PREPARE_REF` 取已交付的 task.prepare 结果（包括 executed 中的准备结果）。差量位于同一 executed 条目，合并规则与独立 prepare 一致；登记后刷新始终与调用者传入的基线比较，不使用未交付的中间准备。当前阻塞与授权仍重算，准备已覆盖的上下文不重复获取。未明确持有、基线丢失或作用域变化时按原完整路径交付；该选项不自动继承读取回执或扩大执行授权。

## 读取返修证据与装配提交参数

机械运行器将 `fresh`（输入未变化）、`passed`（检查通过）分开表达。完整失败报告交给工位修稿，不对同样输入重复检查；每次改稿后仍运行完整检查。缺少完整指纹的报告不能作为当前自动复用证据；当前批准仍须绑定实际登记版本。

```bash
python3 scripts/novelctl.py project workflow repair-context --project-root /path/to/书 --chapter-id CH001
python3 scripts/novelctl.py project review report-view --project-root /path/to/书 --chapter-id CH001
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind review --record-ids SF-HEIGHT
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind prose --chapter-id CH001
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind continuity --chapter-id CH001 --paths .gnovel/aux/story-summary.json
```

`repair-context` 返回当前正文 hash、每项问题 ID、原报告字段、定位状态、原文与相邻行；机械报告保存 lineDetails 和前轮差异。没有精确锚点的全章规则保留 missing，不伪造行号。过期报告列入 staleReports，全文语义审查仍由工位完成。新机械报告按 hash 保存，前轮报告不覆盖。

### 接续返修问题

问题的 `issueId` 与版本定位 `evidenceId` 分开。已有 `findingId` 沿用；旧格式按章节、来源、finding 内容与重复项序号派生身份，席位说明变化不改问题 ID。改写或拆合问题时须显式保留／处置原身份，不按相同规则名强行配对。

登记条目的 `repairHistory` 只引用既有工件，不另建待办账本。失效报告中的问题继续交付为 `needs_recheck`；补丁回执用于对照原文和新文，不证明修复。无法重定位的历史项为 `needs_location`，不签发猜测锚点；历史文件丢失或固定路径已被覆盖时，返回 `unavailableReport`，不能凭缺项宣称全部完成。仅追踪已保留的登记版本，不扫描任意未登记工作台文件。

`REPAIR_REF` 取本轮 `repair-context.repairRef`。优先直接取得本轮待填 payload，不写文件或登记报告：

```bash
python3 scripts/novelctl.py project workflow review-form --project-root /path/to/书 --chapter-id CH001 --artifact-type stage_b_review --repair-ref "$REPAIR_REF"
```

输入保留 `repairRef`，由工位填写全部 slots、verdict、必要的 repairScope；`repairDecisions` 逐项填写 `issueId`、`decision`（resolved / open / needs_information）、`reason`、`quotes`。quotes 沿既有引文机制使用 text 及可选定位，resolved/open 要有当前原文证据。新增问题放 findings；不必复制旧 finding、hash、偏移和补丁关联。再用原 `workflow artifact-pack` 包装为 stage_b_review，工具保留未处置项，并把逐项处置写进报告的 `repairReview`。未处置或历史缺失时拒绝 POLISH_READY；旧批准不继承，完整审校仍按契约执行。

`reviewInputRef` 可用 `task snapshot` 分页查看同一输入骨架。候选补丁返回 `completedStages` 和带实参的 `resumeAction`；对本版本生成的冻结请求，可以只传原 requestId 恢复，不再传整批替换文本。缓存丢失不重做已登记候选；旧请求缺恢复参数时须提供原调用。

```bash
python3 scripts/novelctl.py project workflow patch-candidate --project-root /path/to/书 --request-id repair-001
```

### 装配所选复核材料

先从 `query aggregate` 选择 source-review 行，保存返回的 `selectionRef` 为 `AGGREGATE_REF`，再把选定 recordIds 一次交给原证据入口：

```bash
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind review --aggregate-ref "$AGGREGATE_REF" --record-ids SF-HEIGHT --record-ids SF-LIFE
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind review --record-ids SF-HEIGHT --include-context
```

材料包含当前原文、共享依赖、待填 decisionInput、历史结论及其失效原因、登记来源变更和相关规则。`packAction` 已填 snapshotRef，逐项语义决策仍使用原 evidence pack/submit。长材料继续分页；历史材料与当前原文分开标记，来源变更的 current 是当前值，不冒充操作当时的 after-image。缺失旧备份明确显示不可用。

聚合引用只允许选其中真实的 source-review 待办；历史 setting-impact、decision-plans 行不能直接当作复核义务。直接调用也可传 selection。依赖可跨所选任务的 owner，但保留内容排除、时间与 POV 条件；POV 不交付作者侧历史、规则结论和隐藏依赖，只报告省略数量。选择或输入变化后重新准备，不自动换绑旧引用。

`evidence prepare` 默认返回阅读文本 materials 与短 snapshotRef；需要完整快照时显式 `--view full`。`evidence pack` 回传 snapshotRef 和逐项语义决策，工具保存完整参数并返回 submissionRef；snapshot 对象与 `--view full` 完整结果也可显式使用。复核 decisions 逐项给出 recordId、decision、reason、quotes（每条含 text）；正文引证 decisions 给出 quote、targetIds、kind、reason；连续性 decisions 使用既有 confirmation 对象，仅把 evidence 中的 sha256 交由工具补齐。重句需给 start/end 消歧；偏移按原始字符串字符计算，保留 CRLF。

```bash
python3 scripts/novelctl.py project evidence pack --project-root /path/to/书 --snapshot-ref <原引用> --decisions @/path/to/书/工作台/结论.json --request-id review-001
python3 scripts/novelctl.py project evidence submit --project-root /path/to/书 --submission-ref <装配引用>
python3 scripts/novelctl.py project evidence submit --project-root /path/to/书 --request-id review-001
python3 scripts/novelctl.py project evidence status --project-root /path/to/书 --request-id review-001
```

装配只读；submit 才登记 review 或 prose。工具按实际参数字节预算冻结批次，每批沿原领域接口原子提交，已完成批次在后续失败时保留。重试从既有回执计算未完成项，缓存消失可按 requestId 恢复；同 ID 改内容拒绝。响应预算不合法或无法容纳进度时在写入前拒绝；后续失败信息过长时返回紧凑进度与 errorRef，完整错误通过 task snapshot 读取。超长单项通过引用交接，不裁剪理由或引文。未选择项仍在进度中标 not_selected，不自动算作复核完成。连续性使用独立 `evidence confirm-continuity --submission-ref <装配引用> --approve`，仍走原作者确认边界；普通 submit 不能执行它。

```bash
python3 scripts/novelctl.py project evidence pack --project-root /path/to/书 --snapshot @/path/to/书/工作台/读取快照.json --decisions @/path/to/书/工作台/逐项结论.json --request-id 本轮复核 --view full
```

snapshot 文件保存 prepare 返回的 `snapshot` 对象，decisions 文件保存工位的显式判断。pack 返回 `operation` 与 `arguments`，随后调用原提交入口；复核和正文引证的 requestId 自动附入 arguments。连续性装配无需 requestId，提交仍需原入口的明确作者批准。工具重读并比对整个快照，材料改变返回 EVIDENCE_SNAPSHOT_STALE，重句无范围返回 EVIDENCE_QUOTE_AMBIGUOUS；不偷偷绑定到最新版。

中断重试工件包装时，在既有 artifact-pack 命令中附 `--request-id`。同一请求保留原工件与已有批准；内容变化返回 ARTIFACT_REQUEST_REUSED，依赖变化返回 ARTIFACT_REQUEST_STALE。需要重新核对后使用新请求身份，新工件仍需独立批准。

## 合并已选读取范围

复核材料尚未读完时，可以先登记已处理项，再以原 cursor 和明确的 resumeRequestIds 继续。CLI 每个请求重复一次 flag：

```bash
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind review --record-ids SF-HEIGHT --record-ids SF-LIFE --include-context --cursor "$CURSOR" --resume-request-ids 复核第一批
```

保持原选择、aggregateRef（原来有时）、view 和分页预算。续页逐项验证请求属于同一 snapshot，当前两份复核状态文件的差异恰好由指定回执解释，其他源、授权、规则和历史变化仍拒绝。continuation 标明材料是准备时快照，currentProgressRef 可读取登记后的逐项进度；这不会把 not_checked 变成通过，也不授予批准。不传请求 ID 时保留原严格失效行为，不能用该参数转接其它材料选择。

### 按需复用跨批复核材料

预计下一批有大量共享依赖、且能确认仍持有原材料时，在首次读取开启 `--hold-materials`。普通读取默认不签发该回执，不增加分页成本：

```bash
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind review --record-ids SF-HEIGHT --include-context --hold-materials
```

沿 nextCursor 读完后，把末页的 `materialReadReceipt` 保存为 `MATERIAL_READ_RECEIPT`，下一批显式回传即可启用复用：

```bash
python3 scripts/novelctl.py project evidence prepare --project-root /path/to/书 --kind review --record-ids SF-LIFE --include-context --prior-receipt "$MATERIAL_READ_RECEIPT"
```

每条分页序列固定原 priorReceipt、持有选项和分页参数，只更新 cursor；不要把刚收到的回执替换进同一序列的 priorReceipt。游标绑定实际前一页回执，改偏移、删回执或拼接别的页都拒绝。新批次也可使用部分读取的回执，但只省略确实交付过的字符，Unicode／CRLF 的 start/end 保持原位置。

当前选择、时间与 POV 边界先校验；同版本共享材料可省略，`review-task` 仍返回。`reusedMaterials.characters` 只计省略的材料字符，不是整个响应的节省量；detailsRef 可通过 task snapshot 核对。源、授权或认识范围变化后不继承旧省略。回执丢失、换窗或不能确认仍持有时，去掉 priorReceipt 重新准备；回执本身不代表已审、已理解或获批准。少量共享内容可能抵不过回执开销，实际收益应按当前输入与返回量测。

### 准备报告的差量交付

仍持有上一份完整准备报告及其后续差量时，把 `prepareRef` 作为 `HELD_PREPARE_REF` 回传；换窗、上下文压缩或不能确认持有时不传。按 hash 的入口 `project prepare` 与 `project task prepare` 参数一致：

```bash
python3 scripts/novelctl.py project prepare --project-root /path/to/书 --task editor --chapter-id CH001 --view model --held-prepare-ref "$HELD_PREPARE_REF"
python3 scripts/novelctl.py project task snapshot --project-root /path/to/书 --reference "$HELD_PREPARE_REF"
```

`delivery.mode=delta` 时，用返回字段替换旧值，保留 unchangedFields，删除 removedFields；changedInputs 说明失效来源。就绪、阻塞、作用域与授权边界始终返回当前值。新的 prepareRef 同时可作读取 credential，不能继续使用已失效的旧凭据；读取清单增删导致 readId 变化时，相关清单和映射会重新交付。

缺失缓存、旧版基线、项目或任务／POV 作用域不同会退回完整报告并给原因。完整报告保存在原 prepare 快照中，snapshot 查询只表示历史内容，不证明仍然新鲜。章节看板的 nextActions 提供真实 operation、arguments、missingInputs、效果与执行边界；读操作可直接调用，语义输入或作者批准不会被自动补全。

prepare 的 model 和 station 视图均返回同次生成的 reading.credential，readList 条目提供 readId，contractReads（station 为 promptPlan.contracts）提供 readIds。按需文件与目录入口位于 reading.referenceReads，也提供 readId；未激活或不适用当前工位的资料只返回 available=false 与原因，不分配读取凭据。回传凭据并选择条目即可读取，不必另取 station 视图或自行装配 hash。凭据绑定准备时的任务、章节、POV、文件版本与允许范围；材料变化需重新准备。

```bash
python3 scripts/novelctl.py project task read-batch --project-root /path/to/书 --credential <reading.credential> --read-ids r0 --read-ids r1
```

字符串数组每个值重复一次 flag；例如上面的两个 readId 分别使用 `--read-ids`。也可显式给出 requests，不能与 credential 混用：

```bash
python3 scripts/novelctl.py project task read-batch --project-root /path/to/书 --task editor --chapter-id CH001 --requests @/path/to/书/工作台/选定读取.json
```

每条 request 包含 root、path、sha256，可附 startLine、endLine；范围必须在当前任务读取权限内。Provider 材料以准备结果的 readId 读取，并核验声明摘要。

evidence prepare 同样接受 limit、maxBytes、cursor。materials 的 start/end 是原始材料中的绝对字符位置，fileHash/contentHash 绑定完整材料，chunkHash 绑定该页。coverage.remainingMaterials 包含尚未读完的当前材料，deliveryComplete 只表示交付完成。引文可写成 `{"text":"选中原文","page":{"start":0,"end":100,"chunkHash":"原页指纹"}}`，工具核验页后计算绝对位置；页内重句仍需消歧。复核分批 pack 返回 pendingRecordIds，未处理记录不会获得结论。

## 摘要增量、工具发现与批量返修

summary prepare 返回 inputSnapshot、refresh 和必要正文清单。摘要生成者回传 inputSnapshot、coveredChapterIds 及完整摘要内容；工具只确定重读输入。缺少旧依赖快照、重排、删章或影响无法确定时扩大复核并保留 priorSummary；明确无关的新时间点不触发刷新。旧客户端不携带快照的提交仍兼容，但不会获得伪造的细粒度依赖。

```bash
python3 scripts/novelctl.py project tools list --project-root /path/to/书 --audience writer --task writer
python3 scripts/novelctl.py project tools list --project-root /path/to/书 --view audit
python3 scripts/mcp_server.py --audience writer --task writer
```

MCP tools/list 默认为 model 目录；可用同名 audience、task、view 参数展开，或启动时设置工位。audit 返回完整操作表；发现过滤不改变授权和操作的可调用性。日常未知工具查找优先用 `list_capabilities.query`，受限客户端的调用方式见上文“按意图找工具与导出”。

```bash
python3 scripts/novelctl.py project workflow patch-candidate --project-root /path/to/书 --credential <reading.credential> --patches @/path/to/书/工作台/补丁.json --request-id repair-002 --automatic-repair
```

patches 每项给出 issueId、replacement，并二选一提供 original 引文或 anchorRef。返修页的 location.spans 提供精确问题锚点，contextWindow.anchorRef 提供共享窗口锚点；锚点已绑定项目、章节与原候选版本，不需手抄 hash。可附 `range: {"start": 0, "end": 8}` 选择整个锚点内的左闭右开字符子范围；它不是分页块坐标，CRLF 的两个字符分别计数。缺省范围表示整个锚点；missing 不发位置锚点，ambiguous 保留每次命中及各自锚点，由调用者明确选择，不自动取首个。跨页选择时，range.start 是当前窗口页 start 加页内字符偏移，不加 fileStart。

original 使用与证据相同的精确引文格式，可用唯一原文、显式字符范围或页锚点。此兼容方式用本章 editor/repair 的 credential，或显式 chapterId 与 candidateSha256 绑定基线，不能混用；锚点方式可自行提供基线。所有补丁都在同一旧稿上定位；跨章、旧版本、错误问题身份、越界和重叠拒绝，不能换绑新稿。整批成功后登记新候选并保留补丁收据，原稿不覆盖。补丁成功只说明替换与版本校验成功，之后仍执行完整机械检查与必要语义复核。

## 新建人物或地点

```bash
python3 scripts/novelctl.py project structure entity-create-preview --project-root /path/to/书 --entity-type character --name 新人物 --aliases 别名 --summary 身份待确认
python3 scripts/novelctl.py project structure entity-create-preview --project-root /path/to/书 --entity-type location --name 旧码头
```

预览分配稳定 ID 并保存计划，**不提交实体**。检查返回的 `parameters` 与 `impact`，作者确认后使用该次返回的 ID/hash：

```bash
python3 scripts/novelctl.py project structure commit --project-root /path/to/书 --operation-id <operationId> --plan-hash <planHash> --approve
```

支持人物、道具、地点、事件、势力、规则、秘密、伏笔和软限制。时间锚继续使用 `anchor-preview`，不绕过绝对时间要求。创建后按该稳定 ID 登记属性与片段，阅读卡片由查询生成。

既有实体的事实更新先用 source canon-resolve 定位，再提供旧值、目标字段、当前事实引文和明确批准声明，交 canon-proposal-preview；查看差异后提交。没有可绕过 Canon 门的通用实体字段覆盖或合并入口。

纯显示名、别名及归档／恢复使用后文的受限重构入口，不能夹带摘要或世界事实变化。实体时间事实也须受审源提案，不另设通用实体时间挂载写入口。归档保留身份与引用；影响清单用于人工复核，不等于自动重写推演或重新批准章节。

## 卷、弧线与章节归属

```bash
python3 scripts/novelctl.py project structure volume-create-preview --project-root /path/to/书 --title 第二卷
python3 scripts/novelctl.py project structure volume-update-preview --project-root /path/to/书 --volume-id <volumeId> --changes '{"title":"第二卷·来信"}'
python3 scripts/novelctl.py project structure arc-create-preview --project-root /path/to/书 --title 来信支线 --volume-ids <volumeId> --kind subplot --promise 核实来信署名
python3 scripts/novelctl.py project structure arc-update-preview --project-root /path/to/书 --arc-id <arcId> --changes '{"promise":"核实来信署名与日期"}'
python3 scripts/novelctl.py project structure relink-preview --project-root /path/to/书 --chapter-id <chapterId> --volume-id <volumeId> --primary-arc-id <arcId> --secondary-arc-ids <secondaryArcId>
python3 scripts/novelctl.py project structure move-preview --project-root /path/to/书 --chapter-id <chapterId> --before-chapter-id <otherChapterId>
```

每份 preview 的 commit 都必须处于明确作者授权范围；已有授权涵盖时可连续提交，不要求重复询问。前一次未提交的新 ID 不能被下一次预览引用。多步调整用下面的 batch-preview 合成一个计划。

章节参数（`--chapter-id`、`--before-chapter-id`、`--reader-chapter-id`、`chapterIds` 及 MCP 同名字段）同时接受稳定 ID 与显示编号 `CH001`，大小写和前导零不限。CLI 与 MCP 入口在调用处理器前按当前结构解析：先精确匹配稳定 ID，再在活动章节里唯一匹配显示编号；归档章节保留的旧编号不参与匹配。找不到返回 `CHAPTER_REFERENCE_NOT_FOUND`（同号归档章节列在 `archivedChapterIds`），不唯一返回 `CHAPTER_REFERENCE_AMBIGUOUS`，不猜最近章节。解析过的值在结果的 `chapterReferences` 中列出；预览计划只保存稳定 ID，编号随后变化不影响已生成计划。

```bash
python3 scripts/novelctl.py project workflow chapter-status --project-root /path/to/书 --chapter-id CH001
python3 scripts/novelctl.py project structure batch-preview --project-root /path/to/书 --steps @工作台/批量重排.json
python3 scripts/novelctl.py project structure commit --project-root /path/to/书 --operation-id <operationId> --plan-hash <planHash> --approve
```

`batch-preview` 把有序步骤依次施加到同一份结构快照，生成一个计划：一个 operationId、一个 planHash，一次 `structure commit` 提交，一次 `structure undo` 撤销。步骤是 JSON 数组，长列表写入文件后用 `@路径` 传入：

```json
[
  {"op": "move-chapter", "chapterId": "CH004", "beforeChapterId": "CH001"},
  {"op": "archive-chapter", "chapterId": "CH007", "reason": "并入第六章"},
  {"op": "create-entity", "entityType": "location", "name": "藏书楼", "aliases": ["书库"]}
]
```

- 步骤里的显示编号一律按**预览前**的结构解析，与调用者看到的编号一致；前一步造成的重新编号不影响后一步的引用。
- 任一步失败时整批不生成计划，`STRUCTURE_BATCH_STEP_FAILED` 给出 `stepIndex`、原错误码 `stepCode` 与原细节。结果与当前结构相同返回 `STRUCTURE_BATCH_NOOP`。
- 步数上限读取 `shared/registries/workflow.json` 的 `maintenance.structureBatchMaxSteps`。
- 预览结果的 `batch` 汇总步数、重编号章数、归档章与新建实体；完整重编号列表在 `impact.renumberedChapters`。
- 批量不新增权限：Canon 边界、写集事务与撤销沿用单步计划。批次只接受章节移动、章节归档和实体新建；既有实体事实走受审提案。新章使用 chapter-preview 或 insert-preview 单独预览。

- 新卷追加到卷序列末尾；卷 update 支持 `title`、`status`，不通过 ordinal 偷换章节顺序。
- 弧线 update 支持 `title`、`kind`、`status`、`volumeIds`、`parentArcId`、`promise`、`resolutionTargetChapterId`。可用 JSON `null` 清除父弧线或回收目标。
- `volumeIds=[]` 表示不限制卷范围；非空时，活动关联章节必须在该范围内。父弧线不得成环，引用对象必须存在。
- `relink-preview` **替换**章节的卷和全部主/副弧线关联，省略副弧线参数表示清空副弧线。它不调整显示顺序，不写正文或推演。
- `move-preview` 只调整章节显示顺序。省略 `--before-chapter-id` 表示移动到末尾，稳定 ID 和正文文件名不变。

```bash
python3 scripts/novelctl.py project structure arc-archive-preview --project-root /path/to/书 --arc-id <arcId>
python3 scripts/novelctl.py project structure volume-archive-preview --project-root /path/to/书 --volume-id <volumeId>
```

归档有活动章节/子弧线使用的弧线，或有活动章节/弧线使用的卷，会被拒绝。先显式调整归属或归档依赖项，不隐式级联删除。既有归档章节保留历史关联。新章节不能挂到归档卷/弧线。

## POV 可见性维护

人物认知使用明确的 belief／in_world_text 片段及 perspectiveId 登记；客观事实和作者计划保持各自内容身份。通过当前源提案与批准流程录入，不能将全局事实批量标为公开来解除阻塞。

source query 的 perspective 选择只返回该视角明确登记的信念与世界内文献；章节 prepare 同时携带 povCharacter。角色名命中、地点关联或计划依赖只证明相关，不证明角色知道。完整边界见 [CONTEXT](CONTEXT.md)。

## 定稿、连续性与新事实收尾

continuity 批次绑定最终正文和当时的结构文件，确认只登记工作流证据。定稿不自动处理小说事实或生成摘要，空的状态变化也不能解释成“没有新事实”。

### 按准备快照接续收尾

先用 `workflow.closeout-prepare` 指定 chapterId，以及已选定的 titleCandidateId 或作者明确选择的 skipTitle；返回 prepareRef、阻塞与 runAction。工件批准仍先经原入口完成，准备调用不授予批准。

有阻断时先处理返回的 blockers／recovery，不尝试执行收尾。恢复动作区分可读取的当前证据、须修正或重新审查的材料，以及须已有作者授权的批准。缺状态工件时返回的 state-prepare 动作自带 requestId；诊断重新登记后只核对当前诊断的批准，不把旧批准当成新证据已获认可。阻断消除后重新准备，旧 prepareRef 不会自动换绑。

首次定稿会在事务中创建正式正文并建立归属，候选稿继续放工作台。PROSE_TARGET_NOT_OWNED 表示目标已有文件且归属未成立；回执保留文件位置和冲突依据，不能为通过检查删除、覆盖或接管未知稿件。

```bash
python3 scripts/novelctl.py project workflow closeout-prepare --project-root /path/to/书 --chapter-id CH001 --skip-title
python3 scripts/novelctl.py project workflow closeout --project-root /path/to/书 --request-id ch001-closeout-1 --prepare-ref <prepareRef> --summary-update @/path/to/书/工作台/本章摘要更新.json --approve-continuity
python3 scripts/novelctl.py project workflow closeout --project-root /path/to/书 --request-id ch001-closeout-1
```

以上示例以作者已选择保留工作标题、已授权本章定稿与连续性确认为前提。summaryUpdate 由工位提供非空 storySummary、实际 reason，以及可选的 plotLine／characterSummaries 稳定 ID 增量；也可改传 summary，内容为 `resolution: not_required|refreshed` 与实际 reason，两者不能同传。`refreshed` 只核对已有摘要的新鲜度，不代写摘要。`approveContinuity` 默认 false，只有已有明确授权才传 true；工具参数不意味着还要额外询问一次。

定稿、摘要提交、连续性确认分别使用原领域事务，每一步与自己的执行回执原子写入；后一步失败保留已完成步骤，原 requestId 只接续原输入。缺少摘要处置或确认授权时返回 missingInputs，不补写“无需变化”；补充新内容或授权须重新准备并使用新 requestId。`prepareNext` 默认 true；MCP 可显式设 false，仅完成本次指定收尾。下一步仍受原批准范围与串行门约束，不能越界建章或代写。

prepare 的 factRequestIds 只核对显式列出的本章事实请求；factsAssessed 来自 B 的实际盘点，空请求列表不表示没有新事实，也不清除已知待办。已确认连续性的章节仍可重新准备并传这些请求，核对完成情况后衔接 task.prepare；此时不再传 summary、summaryUpdate 或 approveContinuity。

### 登记已明确的新事实

`source.chapter-facts-preview` 仅在本章 stable 且连续性记录 complete 后开放，当前正文归属与字节也须有效。工位为每条事实提供 ownerId、已有且适用的 topic 字段 field、statement、正文精确 quote（如 `{"text":"逐字引文"}`）、reason 和可选 dependsOn。工具分配属性／片段 ID、绑定当前正文并预览源写集；数值等类型字段和既有事实修订继续走原 source 提案工具，不猜类型或归属。

```bash
python3 scripts/novelctl.py project source chapter-facts-preview --project-root /path/to/书 --request-id ch001-facts-1 --chapter-id CH001 --facts @/path/to/书/工作台/本章事实.json
python3 scripts/novelctl.py project source chapter-facts-complete --project-root /path/to/书 --request-id ch001-facts-1
python3 scripts/novelctl.py project source chapter-facts-complete --project-root /path/to/书 --request-id ch001-facts-1 --register-evidence
```

预览后按 nextAction 调用原 `source.commit`，回传 operationId、planHash 和明确批准；已经覆盖该事实提案的授权直接沿用。complete 缺省只核对进度，提交完成后显式 `registerEvidence=true` 才装配并登记逐条正文 depends_on 引证；同 requestId 续办，不重复提取身份、hash 或偏移。complete 只覆盖本请求列出的事实，不提供语义复核结论，也不证明本章全部新事实已经盘点。

章节回执的 `factInventory.items` 为每项待回流发现列出 `requiredEvidence` 和 `requiredEvidenceCount`。一个 finding 可以需要多处引文，须使用完整集合，不能按 finding 数量每项只取一处。引文包含当前正文中的精确位置，可直接作为事实预览的 quote；工具不代写事实陈述或授予批准。

`view=model` 的回执超过原响应预算时，保留章节身份、正文代次、可定稿／续写状态和下一步，完整报告经已有 `reportRef/readAction` 用 `task.snapshot` 分页读取。快照包含全部引文及阻塞详情，不截断集合，不授予批准，也不会自动改绑新正文。

已有请求与当前义务不同，回执仍返回 `FACT_REQUEST_BINDING_STALE`，并通过 `requestEvidence` 显示正文绑定是否一致、已匹配数量、缺失引文的零起始索引及多余引文。依据完整集合核对原义务，必要时使用新的请求／事项身份；不得修改旧请求绑定，也不能用同一句话在另一处的出现替代原定位。正文、哈希、代次和精确引文集合必须同时匹配。

已识别新事实时，可先以实际理由声明本批次摘要暂不刷新并明确确认连续性，暂缓下一步准备；随后完成事实批准、引证与最后一次源变化后的摘要更新，再交接下一章。不得把源事实提案提前塞入尚未确认的连续性快照，也不得把任一步成功当作整章语义收尾完成。

### 使用独立连续性入口

```bash
python3 scripts/novelctl.py project workflow continuity-preview --project-root /path/to/书 --chapter-id CH001
```

需要逐项 decisions 时，使用 `evidence.prepare(kind=continuity)` 获取绑定快照，再用 `evidence.pack` 传入明确的 decisions 与 summary 处置，由工具补齐 hash；`evidence.confirm-continuity` 必须携带已有明确批准。CLI 对应 `project evidence prepare/pack/confirm-continuity`。批次来源漂移时必须重新核对，不自行替换 hash。

源事实另走 `source change-preview` 与批准提交；事实复核和正文引证用 evidence 入口。摘要在最后一次源事实／引证变化之后准备和提交，避免刚刷新就过期。若当前空批次必须先确认才能修改其绑定结构，应先明确记录本批次不刷新摘要的实际理由，再完成事实、引证和摘要；只有这些语义工作全部完成才能对作者声称整章收尾。

`refreshed` 要求当前摘要新鲜且 hash 相符；`not_required` 须说明理由并保留过期提示。下一章的推演、正文与状态推进在上一章未完成连续性时返回 `CONTINUITY_REQUIRED`。没有当前格式回执的稳定章不能从旧项目历史推断已确认。

## 传输与安全边界

以上新目录操作在 CLI、MCP 和 HTTP 都可用。MCP 名称和 HTTP 路由由 `catalog_api.CATALOG_OPERATIONS` 给出，列表中的 schema 是单一声明；真实 stdio、CLI 子进程和 HTTP socket 冒烟测试逐项执行。Dashboard 本轮新增的是 API 能力，未增加对应网页表单。

所有 preview 都会落盘计划，因此 MCP 的 `readOnlyHint=false`，HTTP preview 也必须通过会话/Origin 校验。正式结构仅在 `commit` 的锁内校验批准、计划 hash、有效期和结构 revision 后变化；未恢复事务阻断新计划与提交。新操作复用现有备份和 undo，不扩展小说工位的创作授权。

```bash
python3 scripts/novelctl.py project structure plans --project-root /path/to/书
python3 scripts/novelctl.py project structure operations --project-root /path/to/书
# 该操作没有 binding 语义变更时，无需代次令牌
python3 scripts/novelctl.py project structure undo --project-root /path/to/书 --operation-id <operationId>
# current-binding 且撤销真实 binding 变更时：必须回显它看到的代次
python3 scripts/novelctl.py project structure undo --project-root /path/to/书 --operation-id <operationId> --binding-generation <g>
```

仅最新且没有后续冲突的操作可直接撤销；人工后改文件、损坏备份或不可验证旧 journal 会阻断自动覆盖。当前正文绑定的撤销是**一次新的正向提交**：新 revision、新 generation、新 integrityDigest，绝不回退旧代次或旧封签；目标 operation 不在可撤销链顶端则 `UNDO_NOT_CHAIN_TOP`。

**`--binding-generation` 在 current-binding 上不是可省的装饰**。撤销会逆转真实 binding 变更时，请求必须回显被撤销章**当前**的 `proseBinding.generation`：先从 `project structure operations` 该行的 `bindingGeneration`（或 `workflow chapter-receipt` 的同名字段）读数，再带着它撤销。省略令牌 → `OWNERSHIP_GENERATION_REQUIRED`（non-zero exit）；令牌过期 → `OWNERSHIP_GENERATION_CONFLICT`；两者都不写任何东西。服务端不会代你查当前代次——「省略即按现状授权」会把请求 CAS 降级成服务端现状观察。被撤销的 operation 没有 binding 语义变更时没有代次可回显，省略才是合法的。MCP `undo_structure_operation` 与 dashboard `/api/operation/undo` 用同一规则、同一字段名（`bindingGeneration`）。契约见 [R1-CURRENT-OWNERSHIP-BINDING.md](R1-CURRENT-OWNERSHIP-BINDING.md)。

## 作者重构（改名 / 移位 / 归档）

稳定 ID 永不因改名、移位、排序、换父、别名、归档或恢复而变化；书名、卷/弧/单元/章标题、文件名、文件路径和项目文件夹都允许反复修改。全部走 preview → commit，范围与写入保护见 [单一真源契约](../shared/contracts/G-SINGLE-SOURCE.md)。

```bash
python3 scripts/novelctl.py project structure book-rename-preview --project-root /path/to/书 --new-name 山城旧事
python3 scripts/novelctl.py project structure chapter-rename-preview --project-root /path/to/书 --chapter-id <chapterId> --new-title 闭店之后 --file-name-strategy sync_safe_paths
python3 scripts/novelctl.py project structure chapter-path-preview --project-root /path/to/书 --chapter-id <chapterId> --file-path 正文/017-闭店之后.md
python3 scripts/novelctl.py project structure entity-rename-preview --project-root /path/to/书 --selector <entityId> --new-name 林岚 --keep-old-name-as-alias
python3 scripts/novelctl.py project structure refactor-references --project-root /path/to/书 --old-name 艾琳
```

`refactor-references` 只读报告旧名在正文与推演中的出现位置；命中只是候选，保留旧称还是改成新称由作者决定，脚本不自动替换正文。`--file-name-strategy` 缺省 `display_only`（只改标题不碰文件）；`sync_safe_paths` 将正文位置与当前绑定一起事务更新；源计划留在结构真源，失败恢复本次写集。

项目文件夹改名/移动是独立的 top-level 文件系统操作（只走 CLI）。预览在 `.gnovel/plans` 落一份带 `operationId` 与 TTL 的计划；执行全程持有 `structure.lock`，锁文件随文件夹一起搬到新位置、释放也跟到新位置：

```bash
python3 scripts/novelctl.py project relocate --project-root /path/to/旧书名 --target /path/to/新书名
python3 scripts/novelctl.py project relocate --project-root /path/to/旧书名 --operation-id <OP> --preview-hash <planHash> --approve
```

执行时末次校验 → 一次 `os.rename` → authored 逐字节复核 → 经写集引擎落 `.gnovel/project-locator.json` 收据并消耗该计划。计划过期、被换掉或项目已变动的，一律拒绝（`PLAN_EXPIRED` / `PLAN_HASH_MISMATCH` / `PLAN_NOT_FOUND`）；改名之后任何一步失败都把文件夹改名回去，改回去也失败就报错，不静默。

## 项目诊断（统一 Doctor）

CLI 与 HTTP 默认 full；MCP 默认 model。model 仍运行完整检查，只分页呈现域状态、问题和建议，底层展开数据保存在 doctorRef 的 detailsAction。对照上次明确保留的 doctorRef 可只读差量：

```bash
python3 scripts/novelctl.py project doctor --project-root /path/to/书 --strict --view model
python3 scripts/novelctl.py project doctor --project-root /path/to/书 --strict --view model --against-ref "$DOCTOR_REF"
```

差量按域、问题码和对象身份归组，不把重复命中假装逐条配对。added、changed、no_longer_reported 是报告差异，消失不等于语义问题已解决。当前 healthy、issueCounts、域状态始终返回，blocking 仍报 DOCTOR_UNHEALTHY；基线丢失、项目或作用域／运行策略变化时标 comparable=false 并交付当前问题。长问题、证据与建议可以 limit/maxBytes/cursor 完整续读；同一分页不重复运行 doctor，输入变化则拒绝。完整历史报告仍可经 task snapshot 阅读，不冒充当前健康。

```bash
python3 scripts/novelctl.py project doctor --project-root /path/to/书
python3 scripts/novelctl.py project doctor --project-root /path/to/书 --strict
python3 scripts/novelctl.py project doctor --project-root /path/to/书 --chapter-id <chapterId>
```

八个域各自给状态：`project`、`planning`、`writerReadiness`、`canon`、`context`、`knowledge`、`workflow`、`tooling`。`--strict` 把警告也计入阻断（如时间线倒挂与未决字段）；`--chapter-id` 指定某一章的上下文与计划就绪度，显式检查已定稿章仍要求当前修订的审查绑定。缺省优先用 `nextAction` 中未完成的章节，再按显示顺序选最早未完成章；stable／archived 不自动成为 Writer 任务。没有待写任务时，Writer 与章节上下文标为 `not_applicable`。已定稿章仍经真实 chapter-receipt 检查当前正文归属、连续性与事实交接，不把历史 A/B 失效当作正在执笔失败。

**按域读，不要按总 `healthy` 读。** 问题的 `status` 说这是什么毛病，域的 `status` 说这个域有多糟，两套词表不能互相比：

| 域状态 | 含义 | 下一步 |
|---|---|---|
| `blocked` | 机械错误 | 按 `recommendedAction` 修；通常是结构、源绑定或不可读文件 |
| `stale` | 输入在解析后变过 | 重新 resolve / preview / 读取材料 |
| `needs_rebuild` | 可重建的派生物 | 重建缓存、重建索引、恢复事务 |
| `author_decision_required` | 作者必须决定 | 补规划、定归属、逐段裁定——**不是去修脚本** |
| `semantic_review_required` | 需要语义复核 | 交 Editor / 作者 |
| `informational` / `not_applicable` | 提示或本域不适用 | 无需处理 |
| `unknown` | 该域没能评估 | 看 `reason`；其余域仍然有效 |

每个问题带 `code`、`domain`、`severity`、`status`、`objectType`、`objectId`、`message`、`evidence`、`affectedOperations`、`recommendedAction`，适用时加 `sourceHash` / `receiptHash` / `chapterId` / `candidateId` / `assetId`。`problems` 字符串列表与 `healthy` 布尔是展示投影，业务真源是结构化 `issues`。

本发行版通过 CLI、MCP 与本地 JSON 服务返回诊断报告。报告建议不授予批准、归档或事实写入权限；可重建类操作使用各自显式维护入口。

CLI 的 `--project-root/--strict/--chapter-id`、MCP tool `doctor` 的 `inputSchema`、HTTP `GET /api/doctor?chapterId=…` 的参数都由同一个 `project.doctor` OperationSpec 生成，不各写一份。HTTP 诊断是只读业务操作，仍遵守服务的会话与同源要求；有 blocking 域时返回 409 并保留完整报告体。

`scripts/aggregate_diagnostics.py` 是**正文机械检查聚合器**（AI 陈词、validate-text、声纹词表、模型退化），属于机械门，与统一 Doctor 是两个概念。

## 本地打包

```bash
python3 scripts/package_toolbox.py --plan                  # 干跑
python3 scripts/package_toolbox.py --output dist           # 目录包 + MANIFEST.json
python3 scripts/package_toolbox.py --output dist/x.zip     # zip 包
python3 scripts/package_toolbox.py --check --manifest dist/MANIFEST.json
python3 scripts/package_toolbox.py --check --manifest dist/x.zip.MANIFEST.json --zip dist/x.zip
```

`localPersonalReleaseReady` 与 `publicReleaseReady` 分开报；后者在无根 `LICENSE`、旧来源权利未清、或存在本机绝对路径引用时为 false 并逐条列 blocker。发布前还须核对实际发布文件、许可证和内容审核结果；构建成功不能代替发布审核。

发布内容审核使用仓库外的 JSON 词表，词表是非空字符串数组。报告同样输出到仓库外：

```bash
python3 scripts/release_audit.py --terms /path/to/local-review/terms.json --output /path/to/local-review/worktree.json
python3 scripts/release_audit.py --history --terms /path/to/local-review/terms.json --output /path/to/local-review/history.json
python3 scripts/release_audit.py --zip dist/x.zip --terms /path/to/local-review/terms.json --output /path/to/local-review/archive.json
```

三个检查分别覆盖当前文件、所有可达 Git 提交中的文件版本、实际压缩包的全部条目。英文按单词及标识符分段匹配，中文按连续文本匹配；检查还拒绝外部链接、二进制内容和混入的数据文件。词面检查不能替代对契约含义、示例和发布范围的审阅。

## 外部稿件入库

```bash
python3 scripts/novelctl.py project import review --source /path/to/外来稿 --target-root /path/to/证据副本
python3 scripts/novelctl.py project import scan --source /path/to/外来稿 --output inventory.json
python3 scripts/novelctl.py project import validate --project-root . --candidate candidate.json
python3 scripts/novelctl.py project import create --project-root /path/to/新书 --candidate candidate.json
```

`import review` 只产只读证据副本（章节边界候选、逐字节定位、内容 hash，`writableStoryTruthEnabled=false`），不创建可写真源、不分配正式 Stable ID、候选一律 `unapproved`。它没有 projectRoot 业务语义，因此是**已登记的 transport exception：只走 CLI**，不进 MCP tool list 也不进 dashboard 路由；surface 与 mutations 见 `shared/registries/tools.json` 的 `external-manuscript-intake`。

## 创作任务入口

作者按设计、写章、修改发起任务，使用 [根入口](../SKILL.md) 取得当前准备结果。本页的维护 preview/commit 仍是独立授权操作，不因内部路由而自动提交。

## 长提交回执与中断恢复

`workflow.submit` 和 `workflow.artifact-pack` 的结果超过响应预算时返回 `reportRef` 与 `readAction`，完整结果通过 `task.snapshot` 分页读取。紧凑回执保留每个阶段、错误码、后续批准提示和恢复入口，未解决内容仍保存在原报告中。提交批次冻结源、授权、上游工件和每个待写目标；若另一次请求已替换目标，原请求拒绝恢复，不把旧稿写回最新候选。创建内联输入时若同路径突然出现外部文件，也会拒绝覆盖。

## 本地调用诊断

```bash
python3 scripts/novelctl.py project logs status
python3 scripts/novelctl.py project logs configure --mode off
```

日志模式以状态返回的当前来源为准。`GNOVEL_LOG_MODE` 可覆盖进程模式，`GNOVEL_LOG_DIR` 指向仓库外诊断目录。完整日志可能保存实际调用参数、返回与错误；凭证脱敏不等于正文内容脱敏。日志只用于本地复盘，不进入发布文件，不改变作品事实、批准或正文归属。

日志缺失或写入失败不替代业务成功或失败判断；缺少结束记录不能自动重放写操作。关闭日志不影响小说项目中的正式证据。
