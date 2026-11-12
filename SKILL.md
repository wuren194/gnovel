---
name: govel
description: 使用 Govel 工具箱管理中文长篇小说；续写、读稿、进度查询、设定修改、导入与审校。
---

# Govel

作者表达创作目标，模型承担构思、规划、写作与语义判断，工具维护资料、证据、版本和受控写入。
不熟悉命令时先查 help，已持有当前定义时直接调用工具箱。MCP 与 CLI 均为正式入口；CLI 的 `scripts/novelctl.py` 相对于本 Skill 所在工具箱目录，不相对于小说目录。

## 首次调用与帮助

- MCP：调用 `get_tool_help`，用 `query` 描述任务，或用 `operation` 查询操作 ID、MCP 工具名、CLI 命令路径。已熟悉 `list_capabilities` 时也可继续使用同一目录。
- CLI：`python3 scripts/novelctl.py --help` 查看入口；`python3 scripts/novelctl.py project tools help --query "续写这个"` 找命令；`python3 scripts/novelctl.py project task prepare --help` 查具体参数。`project tools help --command-group workflow` 分页列出该组公开操作；搜索没有匹配时按分组查找，不推断工具不存在。
- 作者说“续写这个／接着写／继续上次”，先调用 `task.prepare`（MCP `prepare_task`），传入明确的 `projectRoot`、`task=continue`、`view=model`。先让工具解析当前进度，不扫描项目目录或直接打开 `structure.json`、`workflow.json` 猜下一步。已有明确的修改或设计任务使用相应 task，不把所有请求都路由为续写。
- 只问进度用 `project.overview`；读正式正文用 `project.prose`；读计划用 `source.chapter-plan`；审查意见用 `review.report-view`。需要更多操作先查 help。CLI 与 MCP 没有偏好的强制切换；一个入口不可用时使用另一个公开入口。

项目状态、设定、正文与运行证据均通过公开查询和材料入口读取；保存与修改通过领域命令完成。工具返回 `readAction`、`nextAction` 或恢复动作时，按其参数与授权条件继续；分页读到所需范围，不能把第一页当成完整材料。续写返回无已授权任务或冲突时，沿错误中的 `recovery.readActions` 查项目概览与诊断，核对作者范围和待办，不用改内部 JSON 或临时脚本绕过。

本 Skill、项目 AGENTS 和宿主提供的工具说明可用于初始指引；用户交来的原始资料可作导入输入。正文创作、语义判断与自定义圆桌编辑仍由模型／作者完成。只有作者明确要求开发或故障排查时才检查工具箱源码；日常创作不反向分析实现来拼接操作。确实缺少能力时说明具体缺口，已有明确授权继续有效，不反复申请相同批准。

准备任务后读取当次契约、来源、缺项与下一步动作。执行范围以作者授权和当前机器状态为准。
圆桌只使用当次读取计划选中的真源：默认读取 `local/roundtable.md`，非空时使用用户版，缺失或空白时使用通用版。准备结果说明本次来源。通过准备结果的 readIds 读取，不另外打开默认圆桌文件，也不在工位目录复制席位职责。用户保存修改后重新准备受影响任务，不要求手动更新摘要或重启知识库。
文学审查的多种视角在同一会话内切换。局部修订保留无关成果；可选建议随稿呈现，不冒充必修问题。
参考知识不修改作品事实。配置的 Provider 不可用时说明缺项；不借关闭规则取得通过。

准备结果优先使用 `--view model` 的紧凑投影；缺字段时查询当前操作定义。

## 导入外部小说

收到 TXT/Markdown 原稿转项目请求时，先发现并调用 `import.preview`，`projectRoot` 指向尚不存在的新目录；资料放在 `local/` 或仓库外。工具冻结原文、提出分章并自动生成后续所需身份，不要求编写导入脚本或手工组装 `candidate.json`。

沿 `import.inspect` 的分页核对分章列表，可用 `anomaliesOnly` 只看异常，`search` 按标题、文件名或行号查找；用 `import.export-plan` 导出 CSV／JSONL。预览会识别窗口内重复标题、引子／楔子／序章／尾声／后记／番外，扫描缺“第”、章／掌误写、短章与编号异常。`ignoreHeadingPattern` 只影响标题识别，不删除原文；`headingPatterns` 增加标题前缀，`dedupeHeadingWindow` 设置重复边界合并的行窗口。疑似 OCR 标题仅作为异常候选，不自动增加边界。需要改分章时重新 preview，`boundaries` 只填写源文件相对路径、起始行和标题；首个边界之前的内容自动保留为卷首。章节边界及书名经作者明确认可后，原样使用本次 `planId`、`planHash` 调用 `import.commit`；既有授权已涵盖具体预览时沿用。

导入会保存原稿正文及来源，建立新项目归属，章节保持待规划；它不证明设定已提取、文学审校已完成或续写已获批准。原稿变化重新预览；调用中断以同一计划重试。不要手工调用 `seal_binding`、改结构真源或缓存来补导入。

### 导入后由 AI 整理到可接续

1. 调用 `import.verify` 核验冻结原稿与当前正文完整性；`checkSource=true` 可比较外部源文件，它后来变化不等于保存的导入副本丢失。查看 `import.organization-status` 的同一份实时待办，不自行对照旧项目目录推断缺文件。
2. 按作者已授权的读稿整理范围，调用 `import.organization-prepare` 选择小批章节，沿 `import.organization-read` 分页读完。先查询已有实体、事实和关系；新对象使用 `$` 局部引用，已有对象用工具返回的稳定 ID。
3. 实际提炼人物／对象、事实、关系和本章回溯梗概，逐项提供原文引文。填写 facts、relationships、continuity、structure 四项审阅结论；未知和冲突写入 unresolved，不以空数组替代实际盘点。引文匹配不证明语义正确，AI 必须核对归属、时间与因果。遇到内容边界问题可为该章传 `adopt=false`、`quarantineReason` 和 `quarantineCategory`；隔离章不提炼摘要、事实或关系，不把引文和审阅文本写入事务记录。涉及未成年角色的露骨内容使用 `underage_explicit_content` 分类。
4. 将 fields 填入 `import.organization-preview` 的 content。只保存整理结果用 `acceptHistory=false`；确认无未决项并拟采用的章节设 `adopt=true`（默认），其他可隔离，单批其他章仍可采用。紧凑预览后的 entity ref→ID 映射沿 `entityReferencesAction` 或 `import.organization-plan-inspect` 分页读取。预览后沿 `source.commit` 消费实际作者授权；可沿用已经明确覆盖的批准，不逐章重复询问。隔离章保留原始身份和正文供审计，但不进入历史采用、摘要、续写上下文、事实／连续性和默认导出。作者复核后通过 `chapter-quarantine-release-preview` 提交解除，章节回到 `planning_required` 并重新组织；原稿采用的 stable 状态来自当前回溯审阅，不能解释为历史 A/B 已完成。
5. 每批提交后重新准备剩余章（默认跳过有效采用的章，不继续复用旧 offset）。已登记事实用 existingRecordIds 交代依赖；相同梗概可复用，改事实仍走受审源提案。卷、弧和分卷归属通过结构工具整理，语义归 structure.json，不要求创建规划 Markdown。最后由 AI 依据原文提供 `source.voice-profile-preview` 的风格候选、按实际批准激活，再通过 `summary.prepare → summary.commit-delta` 完整重述故事与角色末态。

组织与审阅没有内置模型：这些语义工作由当前会话的 AI 完成，不是让作者手工填表，也不能批量填“已核对”占位通过。模型不能擅自补全未写的未来情节。回溯审阅改变或原稿归属失效时保持待复核；原稿文字改写进入正常修订通道。新章节继续走正常阶段 A/B、候选与作者批准。

动作里的 arguments 是 JSON 参数名。CLI 直接使用动作的 cli／cliArgv；没有该字段时用 `project tools help --operation <操作ID> --arguments <原arguments JSON>` 生成命令，不把 projectRoot 猜成旗标 `--projectRoot`。

## 建章、审校与提交

新建空项目使用 `project.create-preview → project.create-commit`。已有项目追加原稿使用 `import.append-preview / append-inspect / append-read / append-commit`；先明确所在卷或项目范围、主弧及插入点。拆分和合并已有正文使用 `chapter.split-preview`、`chapter.merge-preview`，拆章可用 `cutBefore` 指定唯一原文片段，工具计算字符分界；沿返回动作核对并提交。原章和原文保留，新章重新整理规划。

作者在外部编辑器修改正文后，使用 `workflow.external-preview / external-read` 核对冻结材料，已有明确授权后沿 `approvalAction` 请求本机确认。确认由作者完成；不能模拟点击或把普通批准参数当成本机确认。系统确认仅采用该版本，后续语义审查仍须实际完成。

完整项目备份使用 `project.backup`，用 `project.backup-verify` 校验；恢复先 `project.restore-preview`，核对目标和原宿主任务处理后执行 `project.restore-commit`。恢复保留项目身份，目标必须为新目录。阅读导出不能替代完整备份。所有公开项目操作在 MCP 与 CLI 可用；找不到默认目录项时按意图或 operation 查询，不自行拼接内部写者。

| 操作 | 用途 |
| --- | --- |
| `workflow.chapter-preview` | 预览章节及计划 |
| `source.chapter-plan` | 读取当前章节计划 |
| `workflow.review-form` | 生成绑定当前输入的审查表单 |
| `workflow.submit` | 提交约定范围的工件 |
| `workflow.state-prepare` | 准备当前状态候选 |
| `workflow.closeout-prepare` | 检查正式采用前提 |
| `workflow.closeout` | 在有效批准范围内完成收尾 |
