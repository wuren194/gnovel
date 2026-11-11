# 实现与验证

原稿整理通过 `import.organization-prepare/read/preview` 与原 `source.commit` 接入受控源事务，保存 AI 提供的引文、事实、关系和回溯审阅。`tests/test_import_organization.py` 验证中性原稿从导入到摘要、风格与前章交接的链路，以及未决项、缺证据、原文变化、请求重试和事务失败恢复。回溯采用不生成 A/B 批准。`import.verify` 复核冻结原文覆盖与当前正文，`import.organization-status` 和 doctor 共用实时待办；原导入回执仍是历史记录。

`project tools help` 与 MCP `get_tool_help` 共用实际操作目录，提供分组分页、自然语言发现、参数和 CLI 示例；帮助无需读取小说项目。续写首先进入 `task.prepare(task=continue)`，失败保留错误并给公开恢复查询。口语路由、help 参数、无项目帮助和续写边界由 `tests/test_command_help.py` 与现有发现／工作流回归验证；工程检查不证明外部宿主中的模型一定遵循指引。

完整原稿导入使用 `import.preview / inspect / read / commit`，共用 CLI 与 MCP 定义。`tests/test_manuscript_import.py` 检验原文覆盖、显式编码、分章调整、正文归属、来源漂移、目标冲突、分页和失败续办；测试采用原创中性短文，不读取真实小说。导入后的章节保留待规划状态，不把导入批准当成文学审校通过。

公开核心保留当前格式的结构、时间、身份、事务、候选和审查实现。题材数据从源码常量和内置文库移到外部 Provider；公开默认配置不启用题材词面检查。

项目新建、备份恢复、已有项目追加正文与章节拆合分别由 `tests/test_project_lifecycle.py`、`tests/test_project_archive.py`、`tests/test_manuscript_append.py`、`tests/test_chapter_reshape.py` 验证；共享材料化边界由 `tests/test_manuscript_materialization.py` 验证。`tests/test_cli_mcp_parity.py` 核对公开命令覆盖及跨通道执行，原维护入口沿同一权限边界工作。外改采用由 `tests/test_external_adoption.py` 覆盖确认取消、输入变化、一次性能力、原文保全与证据失效；工程测试替换系统确认函数，真实 macOS 窗口的人手点击另行验收。

Provider 的未配置、缺失、摘要失配、路径范围和任务依赖由 `tests/test_data_provider.py` 验证，覆盖仓库内 `local/`、配置相对路径和整体搬移。`tests/test_release_contents.py` 与 `tests/test_release_audit.py` 检查 Git 忽略、强制添加反例以及发行包泄漏反例。通用引擎沿 `scripts/local_ci.py` 执行完整公开门禁。实际测试结果由门禁生成，不在本文预先声明通过。

文学生成质量、真人作者体验和外部宿主的运行中装载状态需要单独验证。此发行范围不包含图形工作台，不用工程测试替代这些结论。README 中的客户端、模型与移动端远程使用组合属于维护者推荐，不代表完整兼容性或模型性能评测结论；移动端交互由宿主提供，Govel 继续在电脑端执行本地工具。

文件发布按 macOS/Linux 分派到内核独占重命名，缺失原语仍拒绝写入。事务快照使用独立 inode 的写时复制减少物理重复，保留完整镜像和旧 fd 可写的 displaced 对象；相关并发、源漂移及恢复回归见 `tests/test_file_preservation.py`、`tests/test_snapshot_files.py` 与既有事务套件。macOS 与 Ubuntu 的实际完整门禁结果、提交和跳过项见 [平台验证记录](LOCAL-CI.md#已验证的平台基线)。外改桌面确认的会话预检、取消、超时和 Linux 后端由 `tests/test_external_approval.py` 验证；该部分模拟系统接口，真人窗口确认仍须单独验收。

上下文构建在同次调用内共用依赖图与计划，`tests/test_context_builder.py`、`tests/test_context_preheat.py` 检查输出一致及错章拒绝。全源快照失效、正文依赖与审查阻断保持原有范围，不用性能优化跳过证据检查。

首次接入使用同一 `govel-bind` 引导包，宿主声明与薄入口由包装生成器维护。`tests/test_govel_bind.py`、`tests/test_plugin_shells.py` 验证独立复制、运行时与完整引擎检查、真实帮助调用、注册保全、固定提交下载及授权范围。配置、独立进程握手与当前会话连接分别报告；脚本不能把前两项当成宿主已加载的证明。

`tests/test_plugin_distribution.py` 将公共包解压到只读、含空格的陌生目录，验证 MCP、根 Skill、契约与 doctor 的实际消费者；`tests/test_packaged_tooling.py` 确认运行时保留包装检查，且 `.git` 目录或文件不会触发发布候选、无关客户端的全局登记检查。这些维护检查仍由原命令单独执行。发布包不携带私人资料，插件升级不迁移现有作品或重写批准。真实宿主的安装与当前会话验证另行记录，不以静态 manifest 校验代替。

可编辑圆桌由 `tests/test_local_roundtable.py` 验证：固定入口无须配置、缺失和空白使用默认版、五个审校工位共用用户版、保存后旧回执失效、知识库与未消费圆桌的任务不受影响。公开目录骨架及其空占位约束由发布检查测试验证。

公开契约已改为按作品声明判断场景、表达与故事结构，不预设题材或固定身体描写方式。章节语义审阅由现有 Editor 职责承担；第三方方法仅能通过显式 Provider 进入相应材料范围。事实因果、十视角结论、组织过满的反误判、作者批准与事务边界继续保留。文档链接、命令存在性和契约 frontmatter 由对应工程检查核验，实际运行状态仍以当前门禁报告为准。

风格诊断将形式定位与语义裁决分开，公开规则及 S-10q 默认提供非阻断候选，不按计数推断作者身份或强制全章重写。`tests/test_ai_style_rules.py` 验证密度、章尾、对白、模式与聚合门的一致性；`tests/test_degeneration_rules.py` 验证行内长句复读、短句提示及生成说明与正常第一人称的区别。合成评估只证明控制样例上的行为，未建立真实文稿的文学质量校准。
