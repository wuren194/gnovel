# Govel 架构

CLI、MCP 和本地 HTTP JSON 接口共用 `scripts/core`。模型承担构思、规划、正文创作与语义审阅；工具维护项目身份、结构、证据与受控写入。

公开 `novelctl project` 操作全部通过 OperationSpec 暴露给 MCP；新接口由同一 schema 生成 CLI 参数，原有接口由枚举后的 CLI 解析器派生。默认发现目录仍可按意图或工位收窄，目录展示不改变实际调用权限。维护工具也必须通过其原领域校验，任务自动执行器不会因此获得作者批准。

`command_help.py` 消费同一目录，提供无需项目的命令帮助、分组分页和实际 CLI 示例；位置参数从原 CLI 解析器派生，避免帮助生成不存在的选项。自然语言只负责匹配入口，不决定授权或替代当前工作流。续写统一交 `task.prepare(task=continue)`；失败附公开恢复查询，模型不自行解析结构与运行 JSON 推断状态。

项目使用 manifest 声明的当前格式。设定与规划位于 `.govel/structure.json`，正文与运行证据保留各自身份。结构修改沿预览、作者批准、提交执行；批准绑定当次内容。事务保全、当前正文归属及候选依赖由核心实现，扩展包不能替换。

原稿回溯整理由 `import_organization` 组装模型的实际语义输入，`import_organization_model` 重建精确源变更；源事务同时保存引文与历史审阅。`import_history` 从已提交操作证明及当前正文／依赖判定适用性，供连续性、事实交接与诊断共同使用，不生成 A/B。`import_verification` 复核原始字节覆盖；`import_status` 提供唯一实时导入待办。原导入回执是历史来源，不是可覆盖的当前状态。

通用语义归 `shared/contracts`，数值与机械策略归 `shared/registries`，装载声明归 manifest。六个工位共享这些来源，客户端包装只提供发现与路由信息。

本地 Provider 是仓库外或仓库内 `local/` 中的数据包。`data_provider.py` 校验配置、资源摘要和路径范围；知识存储及索引使用同一本地数据根。仓库内其他源码目录不能作为数据根。`local/` 默认提供数据根，Git 与发行包只包含公开说明及空占位，其余私人内容由打包与发布检查独立排除。私有规则通过声明的注册表和材料接口接入。材料读取沿任务准备、版本绑定及分页读取通道执行。

Provider 身份与声明摘要进入计划和诊断依赖。切换包或变更规则后，旧准备结果及旧诊断不能当作当前结果。参考知识、规则材料、作者决定和作品事实分别保留身份；材料交付不证明模型已经理解。

圆桌固定读取数据根下的 `roundtable.md`，与可选的固定规则包清单独立。`prompt_plan.py` 根据实时内容选择用户版或通用版，`review_plan.py` 绑定同一资源。`prepare_dependencies.py` 单独观察圆桌依赖，包括从缺失或空白切换到非空的情况；知识存储仍只绑定规则包及数据根。相关读取计划改变后旧回执失效，未消费圆桌的任务不受影响。文件摘要由工具计算，用户不维护清单。引擎的十席编号、阶段组席、证据及批准校验不由文本材料覆盖。

公开发行包含通用引擎与中性工程用例。完整图形工作台及用户参考数据不在此发行范围内。

## 传输入口

- `scripts/novelctl.py`：当前格式项目、源查询与事务、工位准备、正文及工件提交、证据、摘要、定稿与连续性；参数以 operations 注册定义为准；project export 成品导出与 preview → commit 原子事务；workflow finalize-chapter 与 continuity-complete。
- `scripts/mcp_server.py`：当前格式项目、源查询与事务、工位准备、正文及工件提交、证据、摘要、定稿与连续性；参数以 operations 注册定义为准；project export 成品导出与 preview → commit 原子事务；workflow finalize-chapter 与 continuity-complete。
- `scripts/dashboard_server.py`：兼容结构查询与事务；高级任务能力通过 CLI/MCP。
- `scripts/core/manuscript_import.py` 与 `import_commit.py`：冻结原稿、预览／调整分章、分页读取与批准创建，CLI/MCP 共用 `import_operations.py`。完整新项目在独立暂存目录中建立，通过独占改名发布，失败不留下半个可用项目；重复提交返回原导入回执。新正文具有归属，章节保持待规划，导入不产生文学审校或续写批准。
- `scripts/core/importer.py`：高级 CLI/MCP 只读证据副本与结构候选创建；前者不创建可写真源，后者不复制正文。日常全文导入使用上述 manuscript-import 入口。
- `scripts/core/project_relocate.py`：把项目文件夹改名或移动到同一文件系统内的新绝对路径。参数是当前项目根与目标绝对路径，不是项目内业务操作：它改变 projectRoot 本身。
- `scripts/core/manuscript_append.py` 与 `chapter_reshape.py`：分别重推追加原稿、已有章节拆合的完整允许变化。`manuscript_materialization.py` 将精确的新章节集合、冻结正文和来源保护记录交给原结构事务，正文与来源证据同一写集提交；已有正文仅作只读 guard。
- `scripts/core/external_approval.py`：显示冻结计划的本机确认，签发不进入 MCP/CLI 载荷的一次性能力。外改采用在原提交锁内消费能力、重验全部输入并更新归属，正文始终只读，相关工件退回待审。
- `scripts/core/project_lifecycle.py` 与 `project_archive.py`：新建和恢复先在独立暂存目录完成，再独占发布；备份保留完整当前格式项目文件与证据，并逐文件校验。恢复保留项目身份，停止原宿主的活跃交接，新增定位历史并保存原元数据副本。
- `scripts/core/operation_receipts.py`：新导入和拆合操作的历史成功声明需要匹配已提交事务的不可变镜像。当前健康正文归属仍不依赖历史回执。
