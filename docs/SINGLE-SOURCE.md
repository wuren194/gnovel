# 单一真源接口与数据约定

只支持 manifest 声明的当前格式（schema 6）。设定与规划统一保存在 structure.json，正文由 current-binding-v1 管理。其他格式拒绝进入项目运行时；旧项目按外部小说人工整理迁入，见 [外部稿件入库](OPERATIONS.md#外部稿件入库)。架构见 [ARCHITECTURE](ARCHITECTURE.md)。

## 创建新源项目

```bash
python3 scripts/novelctl.py project quick-init --project-root /path/to/empty-project --book-name 新书
```

目标必须为空目录。初始化不生成维护型设定卡和推演文件；quick-init 放置当前项目指令，`init` 只创建项目数据。没有格式切换参数。

空项目通过结构诊断，只证明初始化和登记有效；尚未填写的设定、章节计划及审批仍需按任务补齐。工具箱合成验收与真实小说迁移分别授权，长篇规模边界见本文的规模说明。

## 稳定身份与内容类型

ID 使用完整英文类型前缀和工具生成的随机后缀。新增记录省略 ID，或用 `$person` 等临时句柄连接同批记录；预览返回实际 ID 映射。更名、调整阅读顺序、修订故事时间或登记核验都不改已有 ID。新内容不能借用删除或撤销过的旧 ID；恢复原记录使用正式撤销。

| 对象 | 前缀 |
| --- | --- |
| 人物、道具、地点、事件、势力 | `character-`、`prop-`、`location-`、`event-`、`faction-` |
| 规则、秘密、伏笔、软限制、时间锚 | `rule-`、`secret-`、`hook-`、`limit-`、`time-` |
| 卷、弧线、章节、关系、配对 | `volume-`、`arc-`、`chapter-`、`relation-`、`pair-` |
| 字段定义、对象属性、时态取值 | `field-`、`attribute-`、`value-` |
| 结构单元、事件呈现、规划候选、作者决策 | `unit-`、`presentation-`、`candidate-`、`decision-` |
| 作者事实、人物信念、世界内文本、作者规划 | `fact-`、`belief-`、`text-`、`plan-` |
| 核验、人格召回池、抽样回执、正文引证 | `check-`、`pool-`、`sample-`、`evidence-` |

片段继续共用 `fragments` 集合。前缀只编码固定内容角色，不编码真假、知情结果或批准状态。`belief-` 对应 `contentRole=belief`，`text-` 对应 `in_world_text`。证实信念时保留原信念 ID，新增核验；需要建立作者事实时另建 `fact-` 并显式登记依赖。不能在同一 ID 下切换角色。文学检查规则的 `C-01` 等编号属于独立规则注册表。

`field-` 是字段的定义，`attribute-` 是字段绑定到某对象后的槽位，`value-` 才承载具体取值和有效区间。`state=unknown` 表示作者尚未给出取值，`absent` 表示明确不存在，`unfilled` 表示尚未填写，`not_applicable` 表示不适用；这些状态不携带 `value`。已知数值 `0` 和布尔值 `false` 仍是实际取值。上述状态均不表示人物是否知情。

## 读取模式

```bash
python3 scripts/novelctl.py project source schema
python3 scripts/novelctl.py project source query --project-root /path/to/project --view overview
python3 scripts/novelctl.py project source query --project-root /path/to/project --view detail --selection '{"ownerTypes":["character"],"excludeFields":["appearance.height"]}'
```

`schema` 返回当前校验器消费的必填字段、可选字段、ID 格式、类型、时间约定、placementScopes 与 unitMemberTypes。项目版本由 manifest 单点声明；读取和写入均要求当前格式，拒绝把旧结构猜成当前资料。

`source schema` 的 `mutationPolicy` 还列出 Canon、语义契约、作者规划与叙事呈现各集合的写入方式；字段与记录的实际写入范围以本页说明和返回的当前操作定义为准。

结构版本使用 `fieldDefinitions`、`attributes`、`fragments`、`units`、`presentations`、`checks`、`recallPools`。事件与时间锚沿用现有实体集合，避免建立第二条时间线。具体属性以 `(ownerId, fieldId)` 唯一；`values` 内每个值段都有 `valueId`。`topic` 类型不存拼接全文，长文只保存到片段。

正式章节的结构归属由 `placement` 表达，只有两种合法形状：`{"scope":"volume","volumeId":"volume-…"}` 或 `{"scope":"project"}`（卷外正式章）。当前章节不得再持久化 `volumeId` 或 `unitId`，二者与 `placement` 并存即校验报错——避免第二真源。`chapterArcLinks` 仍是章节参与哪些故事弧的唯一真源，`displayOrdinal` 表示阅读顺序，三者职责分离；Arc 不进入 `placement`。Unit 是带 `placement` 与有序 `members` 的结构容器，成员类型限 `chapter` 与 `planning_candidate`：同一 Unit 内 `order` 为正整数且唯一、`(memberType, memberId)` 唯一、目标必须存在，一个活动章节/候选最多属于一个活动 Unit。卷域 Unit 的成员必须同卷；项目域 Unit 可跨卷、可含卷外章、可含尚未定位的候选。

可选 `planningCandidates` 与 `authorDecisions` 也保存在同一结构文件中，但不进入 Canon 内容索引。`source planning-candidates` 查询未来规划节点；`source candidate-preview` 预览创建、修订、拆分、合并、批准、撤回和物化，物化提交才生成正式章节 ID。候选用 `placement`（`null`、卷域或项目域）表达归属；卷域候选的弧线须覆盖该卷，项目域候选只能绑定全局弧线（`arc.volumeIds == []`）。物化把候选的 `placement` 原样复制给新章；若候选是某 Unit 的成员，物化在同一事务内把该成员从 `planning_candidate` 原子转换为新章 `chapter`，不留指向已物化候选的成员。拆分使用 `record.parts`（至少两份位置记录）；合并使用 `record.sourceCandidateIds` 与 `record.placement`。旧候选转为撤回并保留双向来源关系，新候选回到草稿，原批准失效。对已属于某 Unit 的候选做拆分、合并、撤回时 fail closed（`PLANNING_CANDIDATE_UNIT_MEMBERSHIP_CONFLICT`），因为没有无歧义的成员继承规则。已物化候选的预览重试只返回同一个正式章 ID，不再分配新章。`source author-decisions` 查询作者规划决定及作用域过期状态；决定并非角色世界内事实。既有正式章节保持原 ID，不批量反物化。声明的批准者与证据由客户端提供，回执中的 `verifiedHuman=false` 明示本地接口没有独立认证人类身份。
归属尚未确定的未来章节可创建 `placement=null`、`primaryArcId=null` 的 draft 候选；确认位置与弧线之前不能批准或物化，避免为序章等材料编造位置或假卷。

已有实体的姓名、别名、状态、摘要、自定义字段及受支持的时间／规则／声纹字段调整，先通过 `source canon-resolve` 得到当前源 sessionRef，再调用 `source canon-proposal-preview` 指明字段、`expectedOldExists`、旧值、新值、原因和当前源事实片段的原文引证，最后按原结构事务提交。至少一条引证须来自目标实体或解析时绑定的章节；规划片段不作事实证据，回执保留 `sourceChapterId`。旧值存在性、目标引用或引证过期时拒绝；泛用源变更入口不接受已有实体的任何改动。
解析器可按实体类型和当前章节已登记的 `references` 限定范围；未登记的暗示关系不被猜成命中。

既有属性值或事实片段的更新、删除使用 `source content-resolve --record-id ID --kind attribute|fact`，取得完整旧记录和绑定当前源的 sessionRef，再将 `expectedOldRecord`、新记录（删除时为 `null`）、原因、事实引证与声明的批准交给 `source content-proposal-preview`。新记录不能变更 ID、owner 或事实角色。若属性尚无绑定的事实片段，引证可为该旧记录的 `recordHash`，这仅证明当前记录快照，不替代作者的语义判断。已有事实片段须引用其自身原文；超出单次读取预算的旧记录会明确拒绝，不能静默截断。泛用批量入口仍可创建新属性和新片段、修改规划片段，但不能改动已有属性或事实片段。

`relations` 的作者关系进入 `source graph --graph canon`；可选 `perspectiveId` 表示特定人物或势力的主观判断，不进入 Canon 图，`validDuring` 和 `basisIds` 分别表示有效期与登记依据。修改或删除既有关系先用 `source relation-resolve --relation-id ID`；也可用 `--from-entity`、`--to-entity`、`--kind` 精确查找，多个匹配只返回 `ambiguous` 而无可写 sessionRef。`source relation-proposal-preview` 需要这个绑定源 hash 的 sessionRef、完整 `expectedOldRecord`、仅修订 `kind`、`validDuring`、`perspectiveId`、`basisIds` 的 `newRecord`（删除为 `null`）、原因和声明批准。端点、ID 与来源字段不可变；需要不同端点时另建新关系，同时对旧关系走受审删除。`evidenceMode=source` 要求当前、未归档、确实包含引文且通过登记绑定关联**两端点**的事实片段；没有此类正文证据时用 `evidenceMode=author_declaration` 和空 `evidence`，由作者直接声明，不能伪造原文。`verifiedHuman=false` 保持本地身份边界。既有关系的通用覆盖／删除会返回 `RELATION_PROPOSAL_REQUIRED`；提案、提交、撤销共用结构事务与登记依赖影响。

被现有 Attribute 引用的 FieldDefinition 属于 Canon 语义契约，通用改动或删除仍返回 `FIELD_DEFINITION_MIGRATION_REQUIRED`。名称与说明更正使用既有 `source proposal-batch-preview` 的 `kind=field_definition`；语义键、类型或适用对象修订使用 `kind=field_migration`，列全该字段的属性及每个已知值，并提供各属性沿原规则所需的证据。两种字段项顶层 `evidence` 均为空，字段快照和属性旧新记录由工具绑定。迁移仅在类型变更时允许转换值，保留字段、属性、值段身份及归属、状态、时效；不自动删造值或跨字段搬值。预览和提交重验完整范围、最终源合法性与原批次预算，沿字段→属性／值段→片段／章节的登记依赖使相关证据待复核。新定义与未引用定义仍受结构校验和事务约束。实际输入形状通过当前 `source schema` 和操作定义查询。

已有 plan 片段不能原 ID 直接升格为 fact；新增事实可用新稳定 ID 进入通用预览与批准。`presentations` 表示 reveal、flashback、mention 的叙事呈现，不是世界事实，仍用普通结构事务与影响失效。

`validDuring` 是半开区间 `[fromId, toId)`，两端可缺省或为 null；端点只能是事件或时间锚。只登记阶段而没有数值次序时，查询明确返回时间未知，不补造日期。

## 人物认知与独立核验

事实或信念片段可附带 `cognition`：`holderId` 是人物，`targetId` 指向具体 `valueId` 或命题片段，`attitude` 为 `knows`、`believes`、`suspects`、`unaware`，`acquiredAtId` 是获取信息的事件或时间锚，`basisIds` 是已登记依据。未定获取时间显式填写 null。人物信念只能记录 `believes` 或 `suspects`，其持有人必须与 `perspectiveId` 一致；作者事实才能明确建立知道或不知情。属性槽位和字段定义不能充当具体命题。

```bash
python3 scripts/novelctl.py project source cognition --project-root /path/to/project --holder-id character-… --target-id value-… --as-of time-…
python3 scripts/novelctl.py project source checks --project-root /path/to/project --target-id belief-… --as-of time-…
```

认知查询计算半开区间与获取时间的交集，分别返回 `recorded_knowledge`、`recorded_belief`、`recorded_suspicion`、`explicitly_unaware`、`no_record`、`time_unknown` 或 `inconsistent`。不把旧信念与新失忆记录的重叠静默裁决成某一方。获取事件、具体命题和依据进入原依赖图；修改获取时点使相关审查失效，不自动移动客观物品或推断人物遗忘。

`source check-preview` 接受目标、核验状态、`checkedAtId`、理由和实际引文；`holderId` 省略表示作者核验，填写人物表示该人物的核验。工具填充 `targetHash`、`inputHashes` 和 `recordedAt`，沿原 `source commit` 批准追加 `check-`。状态为 `unverified`、`supported`、`refuted`、`inconclusive`；证实与反证必须有现存片段的准确引文。人物核验只接受该人物在核验时点已获得的证据。

命题的 `validDuring`、信息获取的 `acquiredAtId`、核验的 `checkedAtId` 属于故事时间，`recordedAt` 属于系统记录时间。核验记录不可覆盖、删除或用撤销抹去；后续结论另加核验。目标或证据被修改、删除后，历史记录仍保留原结论，查询单独返回 `evidenceState=stale`。这一状态不表示人物失忆或客观事实被推翻。核验查询的时间匹配表示核验是否已经发生，历史结论不替代当前命题的有效区间。核验不自动提升内容角色或替代作者事实。

实体可登记 `designations=[{label,perspectiveIds,validDuring}]`，表示某些人物在一段故事时期知道的称呼。名称元数据在视角中不可见时，仅在称呼的受众和时间明确匹配时使用该称呼；存在歧义时返回稳定 ID。作者用的真实姓名和别名不会因此成为视角人物已知信息。

## 人物多面资料与可选召回

人物片段的可选 `topic` 为 `trait`、`preference`、`tendency`、`goal`、`priority`、`state`、`voice`。它与 `contentRole` 分别表达资料维度和内容身份：未来成长仍用 `plan-`，暂时状态仍须有故事有效区间，实际发生的行为仍通过事件和事实登记。共同成立的复杂侧面保存在同一片段，避免把“好胜且欣赏他人”抽成互相排斥的两种人格。

`recallPools` 引用人物已有 `fact-` 片段，只允许长期性格、偏好和基础反应三个 topic。池记录提供 `poolId`、`characterId`、`count`、`facets=[{fragmentId,weight}]`；权重必须为有限正数，数量不能超过不同候选数。池的创建、调整和删除沿普通源预览与作者批准，不另存一份人格真源。

```bash
python3 scripts/novelctl.py project source profile --project-root /path/to/project --character-id character-…
python3 scripts/novelctl.py project source profile-sample --project-root /path/to/project --pool-id pool-… --request-id 本次写作任务
```

抽样使用固定权重、不放回，保存 `sample-` 回执。输入只包含池和请求，没有当前场景或情绪参数。权重表示提醒频率，不表示行为概率、真实性或强弱。返回正文侧面时不附权重；被抽中无需强制安排对白或动作，未抽中仍属于人物。目标、暂时状态、知情边界、硬限制和其他必要事实不随机删减；使用了某侧面也不会自动加权。

同一 `requestId` 重试返回原抽样；池或资料变化时原请求明确失效，保留旧回执。新的任务读取新资料后使用新请求。`task prepare` 为已登记章节范围内的人物提供认知查询和可选抽样动作，不在准备阶段抽样或改写人物。

## 选择规则

- 对象范围：`ownerIds`、`ownerTypes`。
- 内容范围：`includeFields`／`excludeFields`（稳定字段 ID 或语义 key）、`includeAttributes`／`excludeAttributes`（对象属性 ID）。同一内容维度内包含项取并集，排除优先。
- 内容身份：`roles`；归档：`includeArchived`。
- 时点：`asOf` 指向事件／时间锚；省略表示全时期，显式 null 表示时点未定。不确定项：`unknownTime` 为 `include` 或 `exclude`，无界内容在时点未定时仍可交付。
- 未提供包含列表表示范围内全部内容；显式空包含列表表示不选内容。

一个片段只要有一个被选择、且未排除的 `contentBindings` 就整段保留。`ownerId`、`validDuring`、`dependsOn` 不参与该并集。查询不是脱敏：同时讲身高与经历的段落可能在排除身高后保留“一米九”字样。

## 修改与重试

`project source change-preview` 的 `--changes` 是 JSON 变更数组；每项包含 `action`、`collection` 与 `record`（put），或 `id`（delete）。put 是完整替换该记录，不是忽略缺项的浅合并。已有记录保持 ID；新记录可省略 ID，或用 `$handle` 声明同批临时句柄。仅标识符字段替换句柄，正文文本不插值。

删除不释放已提交使用过的稳定 ID；再次创建内容应由工具分配新身份。预览和提交均沿既有 operations 记录核验，包含属性内的值段身份；未提交预览不占用 ID，原操作合法撤销仍可恢复原身份。相同内容或原名不构成重新使用历史身份的依据。

调用方必须提供 `--base-revision`、`--request-id` 和 `--reason`。相同请求重试返回同一预览或已提交回执；同一个请求 ID 携带不同载荷会被拒绝。未确认的创意不进入这份变更数组。

预览返回 `nextAction`，包含当前操作 ID、计划 hash 和完整 CLI/MCP 提交参数。源修改使用同命名空间入口，仍复用结构事务：

```bash
python3 scripts/novelctl.py project source commit --project-root /path/to/project --operation-id OP_ID --plan-hash PLAN_HASH --approve
```

其中 `OP_ID`、`PLAN_HASH` 必须来自预览回执。提交结果的 `impact` 区分登记的依赖和语义检查覆盖，不宣称自然语言内容已全部同步。设定修改不改写正文。

## 长段与阅读快照

只记得任务而不知道命令时，先用 `project tools list --query '导出全部设定'`；MCP 使用 `list_capabilities` 的 `query` 参数。返回当前操作定义、参数和可调用入口，详见 [按意图找工具与导出](OPERATIONS.md#按意图找工具与导出)。这里的 `source export` 保存设定与规划，`project export` 保存已入库正文，两者不互相替代。

```bash
python3 scripts/novelctl.py project source export --project-root /path/to/project --output 工作台/设定与规划.md
python3 scripts/novelctl.py project source read-fragment --project-root /path/to/project --fragment-id SF_ID
python3 scripts/novelctl.py project source render --project-root /path/to/project --selection '{"ownerTypes":["character"]}'
python3 scripts/novelctl.py project source export --project-root /path/to/project --output 工作台/人物阅读.md --selection '{"ownerTypes":["character"]}'
```

长段续读使用返回的 `endOffset` 作为下一次 `--offset`，同时传回 `--source-hash`。偏移以 Unicode 字符为单位；UTF-8 响应仍受字节预算限制。源变化则拒绝拼接不同快照。

`render` 只返回文本；内容超过响应预算时明确要求 `export`。`export` 输出全部命中内容，不做摘要或语义改写。不指定 selection 或 presetId 时选择全部已登记设定与规划，默认不含归档；需要归档时显式传 `selection.includeArchived=true`。有预设时沿用其选择与审查要求，不与 selection 混用。文件带隐藏的快照来源标记，可删除，不加入自动维护视图。项目内输出必须位于 `工作台/`；项目外交付物须显式指定绝对路径。

目标已有不同内容时，默认拒绝覆盖。只有 `--overwrite` 和与当前文件匹配的 `--expected-file-hash` 同时提供才会覆盖。`--require-reviewed` 拒绝仍有未审／过期／未知内容的选择；明确的 `not_applicable` 是已填写的“不适用”，无需虚构普通值；工具校验版本和记录，不代替 App 做语义判断。

## 语义检查与正文证据

```bash
python3 scripts/novelctl.py project source reviews --project-root /path/to/project --statuses pending --statuses stale --statuses unreviewed
python3 scripts/novelctl.py project source review-receipt --project-root /path/to/project --receipt-id SR_ID
python3 scripts/novelctl.py project source evidence --project-root /path/to/project --chapter-id CH_ID
python3 scripts/novelctl.py project source change-report --project-root /path/to/project --operation-id OP_ID --section impact
```

`reviews` 返回片段 ID、内容 hash、登记依赖的输入 hash 和 `read-fragment` 可直接消费的定位。App 阅读后通过 `source review-submit` 的 `--reviews` 数组登记逐项判断，另提供 `--request-id`。每项结构如下；hash、ID 与引文须来自实际查询，不照抄示例占位符：

```json
{
  "recordId": "SF_ID",
  "inputHash": "INPUT_HASH",
  "contentHash": "CONTENT_HASH",
  "decision": "compatible",
  "reason": "此处描述成年后的身高，与当前值段一致。",
  "evidence": [{"start": 0, "end": 7, "text": "他身高一米九。"}]
}
```

`decision` 支持 `compatible`、`conflict`、`needs_information`、`not_checked`。除明确未检查外，必须提供与当前片段吻合的非空引文列表。字符范围前闭后开，使用解码后的 Unicode 字符序号。工具验证文本存在、输入仍有效；判断理由是否正确归 App 负责。历史回执和当前状态同时原子保存，重试不重复登记。相关输入变更后回执变旧，无关对象变化不整批作废语义结论。

`source evidence-register` 接收 `--chapter-id`、`--file-hash`、`--source-hash`、`--request-id` 及 `--citations` 数组。每项含 `start`、`end`、`text`、`targetIds`、`kind`、`reason`；位置绑定该章的实际 `filePath`，以 UTF-8 文件原始字节计算稿件 hash，字符偏移保留原始 CRLF。`kind` 为 `mentions` 或 `depends_on`，只登记 App 明确判断的关系。工具不把人名匹配提升为事实依赖，也不把几处引文登记称为整章已审查。

稿件改动后，`evidence` 返回 `stale_location`，要求重新定位；规范输入变更而稿件未变时返回 `needs_review`。目前正文证据负责引用定位与影响传播，正文语义问题的完整关闭／延期及发布闸门仍在后续接入范围内。

较大的修改回执返回计数和 `change-report` 入口；用同一 `operationId` 分页读取 `impact`、`writes` 或 `identities`。它是冻结或历史报告，当前状态应另查 `reviews` 和 `evidence`。新源撤销追加历史回执并重算当前影响；查询撤销影响使用 `change-report --undo-revision` 与撤销返回的修订号。撤销不抹掉已有的语义审查历史。

## 章节计划与审批输入

```bash
python3 scripts/novelctl.py project source chapter-plan --project-root /path/to/project --chapter-id CH_ID --view list
python3 scripts/novelctl.py project source chapter-plan --project-root /path/to/project --chapter-id CH_ID --view detail
```

列表给出实际输入身份和指纹，详情按页返回原始源记录。`sourcePlanHash` 绑定本章有效片段、项目／卷／单元／弧线祖先的有效 plan、适用规则，以及显式引用和依赖闭包。章→实体引用含该实体的属性、所属及关联有效片段；其他祖先事实不因归属而自动进入每章。章节自身必须有有效的 `contentRole: plan` 片段，不能只靠上层大纲取得阶段 A 就绪。来源快照仍绑定全源版本，输入摘要只绑定本章选择结果。

`stage_a_review` 与 `state_candidate` 工件使用 `sourcePlanHash`。阶段 A 推进和发布预检使用同一个绑定函数；删除阅读文件不影响源计划审批，新增相关片段或修改依赖事实会改变摘要。阶段 A 的批准不能仅凭工件路径继续复用，文件内容也必须与批准的工件 hash 一致。

本接口提供已登记范围内的输入清单，不证明隐含依赖都已找到，也不替代其他写作／审校／发布条件。完整范围规则、续写上下文和正式发布链的迁移仍在实施中。

## 原有查询入口

源模式下，`settings show` 和四维查询直接读取规范属性与片段，忽略旧 Markdown 中的设定值。字段 key 是输出的稳定标识；输入可用字段 ID、key 或唯一显示名。显示名重复时需要改用 ID 或 key。

兼容字段摘要中的 `source-fragment:<fragmentId>` 指向同次结果的片段清单；正文按片段 ID 只返回一次。带时效的规范值保持值段结构，不把多个故事时期压成一个无时间说明的值。超过兼容响应预算时使用 `source query` 分页读取。

`settings check` 校验当前源结构和绑定。显式生成阅读文本使用 `source export`。

## 预设、关联与正文候选

`source preset-save` 保存 `name`、`selection`、`requireReviewed` 和幂等 `requestId`。之后可在 `source query|render|export` 中传 `--preset-id SP_ID`；每次都按当前真源重新查询。预设只保存参数，不复制设定正文。更新同一预设时给出其稳定 ID 和新的请求 ID；同名的不同 ID 会被拒绝。

`source links --record-id ID` 按 `direction`、`edgeKinds`、`depth`、`limit` 和 `maxBytes` 遍历已登记关系。结果区分属性、依赖、章引用、时间、规则输入和实体关系，返回深度边界数、可能的下一深度查询参数及是否发生分页。零登记路径只证明覆盖过的关系图没有该路径。

`source graph --record-id ID --graph canon|planning` 沿用同一有界遍历和游标。`canon` 只展示登记的事实片段相关依赖、实体章引用及实体关系；`planning` 展示候选位置、拆并来源、物化链接、作者决定作用域与决定依赖。结果的图类型与覆盖字段标明查询边界；规划边另标 `epistemicRole=authorial_plan`。两种视图都不从散文推断隐式关系，也不把候选或决定当作世界事实。

`source candidate-scan --terms 救了 --chapter-ids CH_ID` 对明确章节的当前正文做字面扫描，返回稿件 hash、Unicode 字符范围和原文。多个词或章节可重复传相应参数。每条结果标记为候选提及；App 阅读后才能登记依赖或判定规则冲突。未提供章节范围时扫描已登记的全部章，但覆盖报告会标明这并非明确的全章完成声明。缺正文的章节会列出数量与有限样本，不能被默认为“没有命中”。

## 外部稿件整理

旧项目按外部小说稿件逐本人工整理，不提供格式专用识别、原地升级或兼容运行。现有 `import review` 可保存文本阅读证据，所有语义分类、结构与纳入范围由作者和工位确定；当前项目从新初始化开始，审批和归属重新建立。具体步骤见 [外部稿件入库](OPERATIONS.md#外部稿件入库)。本发行版不包含历史格式转换器。

## 规模边界

规模评估使用 `scripts/benchmark_single_source.py` 的 source 与 prose 子命令，具体参数可查询 `--help`。评估输出需记录实际平台、输入规模、耗时、内存与正确性；不能用历史环境的数字替当前版本背书。

## 规则范围与机械检查

`source rules`（MCP：`query_source_rules`）接受 `chapterId`、`action`、`limit`、`maxBytes`、`cursor`。action 为 prepare、draft、review、publish。分页中的 ready 对全部适用规则计算，不仅针对当前页。它仅表示登记规则就绪，不能替代完整工作流的就绪结论。

规则保存在 entityType 为 rule 的实体的 ruleSpec 中；requiredFields 与枚举可通过 source schema 查询。规则必须有版本、方法、级别、范围、动作、输入。scopeIds 按当前章及其上层范围、实际登记输入匹配；多个范围取并集。未登记关系不被推测为确定适用。

机械 evaluator 当前支持 count_at_most：eventIds 是作者明确登记的成功事件，limitValueId 引用规范整数上限，complete 表明作者是否已确认清单完整。事件 ID 不得重复。超限直接报告 conflict；未超限但清单不完整或上限未知时为 pending；仅完整且不超限时 passed。这不验证作者对“成功事件”的语义判断，也不扫描正文。semantic 规则在没有当前 App 回执时保持 pending。

required 且未通过的规则按动作阻断；advisory 保留报告。规则检查不修改正文。

`source rule-issues --chapter-id CH_ID` 返回当前章各动作的语义问题；尚未登记回执的适用规则也会以 pending 出现。`source rule-issue-submit` 登记 pending、confirmed、deferred、resolved 或 exception，绑定规则版本、输入 hash、动作、影响 ID、理由与证据。源片段证据使用 `source_quote` 的 recordId、字符范围和原文；正文证据使用已登记且仍有效的 `prose_evidence` ID。`source rule-issue-receipt` 用于读取历史回执并核对当前有效性。

resolved 只对同一规则版本、输入闭包和当前证据放行。confirmed 与 deferred 对当前适用的 required 动作继续阻断；advisory 始终只报告。延期范围必须在当前章节范围之外。exception 只有规则显式声明 exceptionsAllowed 且回执提供当前证据时才放行，包括机械规则的例外。历史回执不会被覆盖；输入恢复到旧版本时，相符的旧回执可以重新成为当前依据。

设定变更预览、提交与撤销的影响报告含 ruleChecks，给出受影响规则的当前机械结果；此处不声明动作阻断，实际阻断由章节与动作查询计算。

## 故事时间、阅读顺序与信息身份

时间实体可保留 `timeOrdinal` 作为已知排序点，或使用 `timeSpec`：`relative` 引用锚点和偏移，`range` 登记 afterId／beforeId，`coarse` 保存阶段或季节原文，`unknown` 明确未知。一个实体不能同时声明绝对排序点与 timeSpec。工具将相对等式、严格先后和显式起止共同求解；无数值依据时保持 unknown／coarse，确定无解的组合拒绝写入。`range` 是可能发生窗口，输出保留开闭端点；起止挂载满足开始不晚于结束，持续活动自身不自动等同某个端点。反向约束纳入实际依赖闭包，无关时间线保持隔离。

`source timeline`（MCP：`query_source_timeline`）分页返回故事时间解释和 event 的 presentation 清单。指定 `readerChapterId` 时，事件按登记呈现分为 revealed、unrevealed、unregistered；同一事件可在多章 reveal、flashback 或 mention，无需复制事件事实。

统一 source query／render／export 选择支持 `knowledgeMode: author|perspective` 和 `perspectiveIds`。author 读取作者源；perspective 只返回明确登记为这些人物／势力的 belief 或 in_world_text 片段，并排除规范客观值和未登记认知。belief 必须绑定人物，in_world_text 必须绑定人物或势力，fact／plan 禁止伪装成某角色认知。覆盖信息会说明排除数量及“只依据显式登记”，不能据此宣称未登记秘密已经通过保密审查。

视角读取的元数据使用同一选择边界。overview 只列实际入选信念／文献的 owner；人物名、章节／卷标题、书名、属性标签和时间标签若没有明确可见声明，以稳定 ID 显示。render、export、search 不再通过标题或 ownerLabel 暴露作者侧名称。空的视角选择不会返回其他对象清单；author 模式仍按作者权限显示登记名称。此保护不等于对片段自由文本做语义脱敏，输入内容的认识身份仍由工位复核。
