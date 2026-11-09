# Govel 架构

CLI、MCP 和本地 HTTP JSON 接口共用 `scripts/core`。模型承担构思、规划、正文创作与语义审阅；工具维护项目身份、结构、证据与受控写入。

项目使用 manifest 声明的当前格式。设定与规划位于 `.govel/structure.json`，正文与运行证据保留各自身份。结构修改沿预览、作者批准、提交执行；批准绑定当次内容。事务保全、当前正文归属及候选依赖由核心实现，扩展包不能替换。

通用语义归 `shared/contracts`，数值与机械策略归 `shared/registries`，装载声明归 manifest。六个工位共享这些来源，客户端包装只提供发现与路由信息。

本地 Provider 是仓库外或仓库内 `local/` 中的数据包。`data_provider.py` 校验配置、资源摘要和路径范围；知识存储及索引使用同一本地数据根。仓库内其他源码目录不能作为数据根。`local/` 默认提供数据根，Git 与发行包只包含公开说明及空占位，其余私人内容由打包与发布检查独立排除。私有规则通过声明的注册表和材料接口接入。材料读取沿任务准备、版本绑定及分页读取通道执行。

Provider 身份与声明摘要进入计划和诊断依赖。切换包或变更规则后，旧准备结果及旧诊断不能当作当前结果。参考知识、规则材料、作者决定和作品事实分别保留身份；材料交付不证明模型已经理解。

圆桌固定读取数据根下的 `roundtable.md`，与可选的固定规则包清单独立。`prompt_plan.py` 根据实时内容选择用户版或通用版，`review_plan.py` 绑定同一资源。`prepare_dependencies.py` 单独观察圆桌依赖，包括从缺失或空白切换到非空的情况；知识存储仍只绑定规则包及数据根。相关读取计划改变后旧回执失效，未消费圆桌的任务不受影响。文件摘要由工具计算，用户不维护清单。引擎的十席编号、阶段组席、证据及批准校验不由文本材料覆盖。

公开发行包含通用引擎与中性工程用例。完整图形工作台及用户参考数据不在此发行范围内。

## 传输入口

- `scripts/novelctl.py`：当前格式项目、源查询与事务、工位准备、正文及工件提交、证据、摘要、定稿与连续性；参数以 operations 注册定义为准；project export 成品导出与 preview → commit 原子事务；workflow finalize-chapter 与 continuity-complete。
- `scripts/mcp_server.py`：当前格式项目、源查询与事务、工位准备、正文及工件提交、证据、摘要、定稿与连续性；参数以 operations 注册定义为准；project export 成品导出与 preview → commit 原子事务；workflow finalize-chapter 与 continuity-complete。
- `scripts/dashboard_server.py`：兼容结构查询与事务；高级任务能力通过 CLI/MCP。
- `scripts/core/importer.py`：把外部 .txt/.md 稿件变成只读证据副本（章节边界候选、逐字节定位、内容 hash，writableStoryTruthEnabled=false）；或把已校对的 candidate JSON 建成新项目。入口参数是 --source 与 --target-root/--project-root，不是项目内业务操作。
- `scripts/core/project_relocate.py`：把项目文件夹改名或移动到同一文件系统内的新绝对路径。参数是当前项目根与目标绝对路径，不是项目内业务操作：它改变 projectRoot 本身。
