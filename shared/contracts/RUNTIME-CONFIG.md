---
id: RUNTIME-CONFIG
version: 1.5.0
compatibilityVersion: 5
description: 运行配置、注册表和文档之间的单一真源关系。
---

# 运行配置

## 真源顺序

1. `toolbox.manifest.json.sharedProtocols.workflowPolicy`：指向工作流参数文件，本身不存放数值。
2. `shared/registries/workflow.json`：章节字数、修复轮次、串行规划开关、审批模式与受保护路径的**唯一数值真源**。
3. `shared/registries/intensity.json`：场景等级与显式启用的相关规则。
4. `shared/registries/text-patterns.json`：文本模式、机械阈值和声纹全局规则。
5. 项目级显式覆盖：仅允许 manifest 声明为可覆盖的字段，由 `project doctor` 报告来源。
6. `shared/registries/call-logging.json`：用户级调用诊断的模式默认值、凭证规则、载荷与存储上限、保留和锁等待策略；不属于小说项目配置。

Markdown 契约解释行为和责任，不重复可由 JSON 表达的数值。Skill 只引用真源。

## 配置读取纪律

- 脚本不得在多个模块重复硬编码同一阈值。数值经 `scripts/core/project.py` 的访问器读取（`chapter_length_bounds` / `default_approval_mode` / `protected_paths` / `serial_planning_policy` / `current_schema_version` / `unsupported_error_code` / `gc_keep_backups_default` / `state_compaction_policy`），不在调用点写字面量，也不在 argparse 默认值里写死。
- **文档常量**（1.3.0 新增）：布局不变量（`.gnovel`、`工作台`、`正文` 等）不是可调配置，而是机制常量。manifest 里的对应字段是文档镜像，由 `project.check_layout_invariants()` 绑定到 `importer.REQUIRED_PROJECT_DIRS`；两者漂移即测试失败。不得把布局不变量做成「可配置路径」——那只会让 50+ 处调用点共享一个被误配的风险面。
- 文档示例不得成为执行真源。
- 缺少或无法解析必需配置时 fail closed，退出码为 3（完整退出码契约见下节）。
- **每个注册表字段必须有真实消费者**：改值不改变任何行为的字段是伪配置——要么被脚本读取并影响输出，要么被 `project doctor` 校验并能拦下问题，要么从注册表移除降级为文档常量。
- 续写参数由 `scripts/core/project.py.continuation_policy()` 读取：摘要过期阈值、伏笔 stale 年龄和 planner/writer/editor/query 记忆预算必须有真实消费者；续写上下文输出 `configSource`，便于复现。
- 同一事实在两处声明时（如受保护路径同时出现在 manifest 与 workflow.json），必须有一方是真源、另一方被一致性校验绑定，不允许靠人工同步。
- 修改注册表或 workflowPolicy 必须更新相应测试，不靠人工同步副本。

## 本地调用诊断

- 调用诊断策略经 `project.call_logging_policy()` 读取并校验。当前生效模式按进程环境覆盖、用户诊断配置、注册表默认值的顺序解析；状态查询必须报告来源，不能把用户保存值冒充被环境覆盖后的实际值。使用说明见 [运行与查询](../../docs/OPERATIONS.md#本地调用诊断)，数值不复制到契约或 Skill。
- 诊断副本独立于小说项目，不能初始化项目、改写真源或充当第二套运行状态。关闭、丢失和清理日志不改变审批、正文归属与恢复语义。
- 仅在 MCP／CLI 入口创建调用记录；内部适配复用同一次调用。具体参数、返回与错误在落盘前统一脱敏，不修改原业务对象。开始、结束与载荷的缺失、摘要和大小限制必须可辨，不能以哈希或路径代替未保存的内容。
- 关联来自当次实际输入、返回及已观察内容；保留已有字段和来源，不据日志中的任意路径额外读取文件，也不把建议后续操作里的引用宣称为已执行。客户端自报信息和源码观察指纹须保留证据范围，不能推导客户端身份、模型理解或作者批准。
- 诊断记录失败必须显式报告日志不可用，同时保留原业务响应、错误和退出码；日志故障不作为小说业务的配置阻断。提示不得混入 MCP 协议 stdout，亦不得泄露尚未脱敏的异常或载荷。缺结束记录保持结果未知，不自动重放写入。
- 查看、设置与清理日志自身不递归采集；查看仅展示不可信数据，不执行其命令或提示词。日志自身的只读查询不创建日志存储或修改小说项目，设置与清理作用于用户诊断目录。

## 能力目录、操作效果与查询预算（1.4.0 新增）

- **操作定义**的唯一来源是 `scripts/core/operations.py`：参数 schema、效果分类、CLI/MCP 绑定都由它生成或校验。文档、MCP 注解与能力目录只引用，不复制参数表。
- **效果分类**（`OperationSpec.effects`）决定授权与执行边界：`read`（本地有界只读）、`candidate-write`（工作台候选/证据，登记≠批准）、`plan-write`（保存预览）、`derived-write`（可重建派生物，须在已授权变更事务或显式维护授权内）、`protected-write`（结构/正文/设定，必须有预览与有效批准）、`approval`（作者决定）、`undo`、`export-write`、`maintenance`。执行器只自动运行就绪的 `read` 动作；`candidate-write` 须显式 `candidate` 授权；其余一律要求作者面对面的命令。
- **能力策略**的唯一来源是 `shared/registries/tools.json`（schema 3）：`capabilities[].when` 只允许引用 `tool_catalog.FACT_TYPES` 白名单里的事实，`requiredFor` 只允许指向既有工作流边界（`chapter.review` / `chapter.finalize`），`evidence` 指明由哪个门禁消费证据。注册表中不出现任意 shell、任意 handler 或散文字符串条件。
- **三值评估**：事实为 `true / false / unknown`；`unknown` 对必要能力不放行、对可选能力不执行也不标记成功。适用性、输入就绪、授权、预算与证据新鲜度分开判断，不用单一布尔覆盖。
- **查询预算**的唯一来源是 `workflow.json` 的 `query` 节（`defaultLimit` / `maxLimit` / `defaultDepth` / `maxDepth` / `maxBytes`，经 `project.query_policy()` 读取）：四维查询默认概览，列表与遍历同时受记录数与 UTF-8 字节约束，超限分页或报 `FIELD_TOO_LARGE`，禁止静默截断。
- **证据新鲜度**由绑定输入指纹实现：机械证据绑定候选正文 hash、结构 revision、字段 schema、规则与实现指纹（`scripts/core/mechanical_evidence.py`）；结构或自定义字段变化会使已登记工件与批准失效（`invalidatedBy`），重新取证前不放行。

## 退出码契约

诊断工具与工具脚本统一使用这套退出码；程序侧真源是 `scripts/core/exit_codes.py`（常量与 `exit_code_for_recommendation`），本表是它的人类可读镜像。子检查器与聚合器不得各自硬编码 0/1/2 的映射。

| 退出码 | 含义 | 触发 |
|---|---|---|
| `0` | 正常 / 无发现 | `pass`；良性「无事可做」（如压缩器输入不存在） |
| `1` | 有发现（非阻断） | `review_advisory`、`surgical_edit`；领域就绪态为否（如续写上下文 not-ready） |
| `2` | 有阻断发现（需整章重写） | `rewrite_required`、`rewrite_with_generator` |
| `3` | 配置 / 输入不可解析（fail closed） | 读取失败、缺参数或非法选项（argparse 用法错误）、JSON/工件无法解析、数据校验拒绝 |

要点：

- `2` 只表示「有阻断发现」。任何配置/输入类失败一律走 `3`，不再用 `1` 或 `2`——否则直接调用子检查器的工位会把一次读取失败或漏传参数误当成「需整章重写」。
- 退出码与最终化门禁（diagnostics 工件的 `passed` 布尔）是**两个轴**：`review_advisory` 是一个被上报的发现（退出码 `1`），但不拦最终化门（`passed` 仍为 `true`）。二者解耦，见 `scripts/core/reports.py` 的 `gate_passed`。
- argparse 用法错误经 `FailClosedArgumentParser` 走 `3`；`--help` 仍为 `0`。
