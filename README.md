# Govel

供 AI Agent 调用的中文长篇小说创作工具箱。

Govel 本身不提供独立的图形写作界面，而是作为一组本地工具接入你的 AI 对话环境。长篇创作过程中，你与 AI 沟通；设定维护、时间线跟踪、推演规划以及文件写入，由 AI 调用 Govel 协同处理。

你负责确立基调、把控节奏与拍板定稿；AI 负责构思情节、试写正文与语义排查；Govel 负责保存项目状态、校验已登记的结构与时间约束，并提供受控写入与事务恢复机制。

本工具不内置任何模型，需搭配支持本地工具调用的 AI 客户端使用。

## 推荐使用方式

**推荐在 Codex 桌面端、Antigravity 或 Claude Desktop 中接入 Govel。** 配置完成后，日常创作以对话为主，无需逐条手动执行底层指令，直接在对话框中说明创作意图：

> 读取当前项目，整理目前的写作进度与待确认事项。

> 根据已确认的设定推演下一章细纲，梳理核心冲突与走位，先提供给我审阅。

> 检查本章的人物认知边界与时间线，附带具体的原文证据列出疑点。

### 推荐实践：Antigravity + Gemini 3.8 Flash / 4argon + 移动端远程控制

若使用 Antigravity，维护者推荐选用 **Gemini 3.8 Flash** 或 **4argon** 作为长篇创作的主力模型，并开启 Remote Control。这是使用推荐，Govel 不绑定特定客户端或模型；可选模型以客户端当前显示为准。

电脑端保持运行、联网且未进入睡眠，用于托管项目文件、执行本地工具与状态校验；移动端通过浏览器连接远程控制页面，便于与 AI 讨论剧情、审阅推演方案并确认改动。

- 开启路径：`Settings → Application → Remote Control → Enable Remote Control`。
- 开启后，扫描设置页中的二维码或使用提供的链接，进入远程控制页面。
- 在这一配置下，移动端用于交互输入与审阅，Govel 的文件读写与事务校验在电脑本地执行。

详细步骤见 [Antigravity Remote Control 官方说明](https://antigravity.google/docs/remote-control)。

## 核心特性

- **设定管理与一致性校验**：统一管理人物状态、时间节点与关系约束，由引擎执行可计算的检查，并为 AI 的因果审查提供依据，减少长篇创作中的前后矛盾。
- **进度记录与上下文接续**：保存已登记的决策与未决事项，中断后可依据项目状态继续创作，减少手动补充背景的负担。
- **循证排查**：为规则核验与语义审校提供证据定位，便于回到对应的设定和原文核对疑点。
- **事务化写入保护**：正式设定变更经预览与明确批准后提交；正文定稿绑定已批准的内容，相关事务提供恢复与受约束的撤销入口，降低误覆盖风险。

> 自动化规则检查帮助发现可计算的约束冲突，不能替代语义审查。作品的文学质感、情绪流动与最终取舍，仍由作者审校与裁断；检查通过不等于作者批准。

本仓库仅包含通用引擎核心、扩展规范与基础中性示例，不预置大模型或第三方语料。个人设定、写作风格库与私有参考材料可在本地自行配置。

> **English summary.** Govel is a local toolkit for AI agents writing long-form Chinese fiction, exposed through MCP and a CLI. Authors work through conversation in an AI client, such as the Codex desktop app, Antigravity or Claude Desktop, and retain creative decisions and approval. The model handles ideation, drafting and semantic review; Govel maintains project state, checks registered constraints, and provides evidence, controlled writes and transaction recovery. It has no standalone writing GUI, bundled model or reference corpus. Runtime: Python 3.11+, standard library only. License: AGPL-3.0-only.

## 安装与接入

本地运行环境为 macOS、Python 3.11 或更新版本。运行时只使用 Python 标准库。

```bash
python3 scripts/novelctl.py --help
python3 scripts/novelctl.py project quick-init --project-root /tmp/govel-example --book-name 山城旧事
python3 scripts/novelctl.py project doctor --project-root /tmp/govel-example
```

项目目录必须为空。当前项目格式由 `toolbox.manifest.json` 声明；不自动转换其他格式，也不继承外部稿件的批准记录。

本发行版提供 CLI、MCP 和本地 JSON 接口。日常对话优先通过 MCP 接入；具备本地命令执行能力的客户端也可由 AI 调用 CLI。

## 接入 MCP 客户端

MCP 服务为 stdio 传输，由客户端作为子进程启动。以下示例中的 `/path/to/govel` 替换为本仓库的绝对路径。

Claude Code：

```bash
claude mcp add -s user govel -- python3 /path/to/govel/scripts/mcp_server.py
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.govel]
command = "python3"
args = ["/path/to/govel/scripts/mcp_server.py"]
```

Claude Desktop、Cursor 等使用 `mcpServers` JSON 配置的客户端：

```json
{
  "mcpServers": {
    "govel": {
      "command": "python3",
      "args": ["/path/to/govel/scripts/mcp_server.py"]
    }
  }
}
```

需要预载给特定工位使用的工具目录时，可在 `args` 末尾追加 `--audience writer --task writer` 等参数。注册后可用只读自检确认 Claude Code 与 Codex 能完成真实握手；该脚本只打印修复命令，不修改客户端配置：

```bash
python3 scripts/check_mcp_registration.py
```

客户端连接后，不熟悉命令时调用 `get_tool_help`（例如 `{"query":"续写这个"}`），按返回的定义和 `invocation` 调用工具；已了解命令时直接执行。`list_capabilities` 继续可用，默认目录不是全部能力清单。

## 查帮助，再调用工具

CLI 和 MCP 提供相同的公开项目操作。模型可查询帮助来了解参数，不需要读取工具箱源码或内部 JSON 推测操作。CLI 命令从工具箱目录执行；在小说目录中使用 `novelctl.py` 的绝对路径（MCP help 的 `view=full` 会返回该路径）。常用 MCP 结果默认尝试精简中文呈现，程序读取 `structuredContent`；CLI 保持 JSON，详见[模型交付](docs/MODEL-OUTPUT.md)。

```bash
python3 scripts/novelctl.py --help
python3 scripts/novelctl.py project tools help --query "续写这个"
python3 scripts/novelctl.py project task prepare --help
python3 scripts/novelctl.py project tools help --command-group workflow
```

“续写这个／继续上次”首先调用 `prepare_task`，传项目路径、`task=continue` 和 `view=model`。工具给出当前任务、材料与下一步；尚无授权任务或发生冲突时，按返回的查询动作核对。初始技能／项目指引可以直接读取，项目数据与运行状态通过工具读取，修改沿预览与批准提交。详细映射见[操作说明](docs/OPERATIONS.md#查找命令与开始任务)。

## 五分钟体验

1. 按上节注册 MCP，在对话中说明新书名称和存放目录。模型调用 `project.create-preview` 展示新建预览，作者确认后调用 `project.create-commit`。CLI 提供相同入口，也保留直接初始化命令：

   ```bash
   python3 scripts/novelctl.py project quick-init --project-root ~/govel-books/山城旧事 --book-name 山城旧事
   ```

2. 在客户端对话中说明：“用 Govel 打开 `~/govel-books/山城旧事`，运行诊断。”
3. 继续用对话补充主角、地点和第一章的构想，让模型通过工具登记设定并生成章节推演。
4. 审阅设定变更预览和章节候选，在对应阶段明确批准后提交或定稿。随时可要求“运行 doctor”查看项目状态，或“导出全部设定”生成阅读快照。

不使用 MCP 时，同样的流程可由具备本地命令执行能力的 AI 客户端通过 CLI 完成，也可手动执行。命令见上文“安装与接入”，完整操作见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 导入已有原稿

已有 TXT/Markdown 小说时，可以直接在对话中说：“把这份原稿导入 Govel 新项目，先让我核对分章。”模型通过 `import.preview` 获取分章候选，可分页查看原文、按行号调整分章；确认后由 `import.commit` 创建含正文与来源证据的项目，无须为每本书编写脚本。

导入会保留原文、自动建立新身份与正文归属，设定和规划继续标为待整理。命令与编码选项见[外部稿件入库](docs/OPERATIONS.md#外部稿件入库)。

导入后可让 AI “校验导入，再把原稿整理到可以接续”。`import.verify` 自动核对全文保存情况；`import.organization-prepare/read/preview` 支持 AI 分批读稿、提交带引文的设定、关系和回溯梗概。作者采用后由工具记录已写原稿的回溯审阅，随后沿现有工具补齐风格和摘要。`import.organization-status` 与 doctor 共用实时待办；无需手写校验脚本或重建旧式规划目录。

已有项目可通过 `import.append-preview` 追加外部章节；章节拆分、合并分别使用 `chapter.split-preview` 与 `chapter.merge-preview`。工具保留来源正文，生成新章节身份和待整理状态，作者核对后提交。

自行修改受管正文后，使用 `workflow.external-preview` 核对旧版、新版与差异，再由 `workflow.external-commit` 请求本机 macOS 确认。接受后重新标记待审，不直接定稿。整本项目的可恢复备份使用 `project.backup`，恢复先调用 `project.restore-preview`，核对后恢复到新目录。以上能力均有 MCP 和 CLI 入口，参数与执行逻辑共用；详见[项目管理与章节整理](docs/OPERATIONS.md#项目管理与章节整理)。

## 接入本地资料

日常只需两个入口：`local/roundtable.md` 是可直接编辑的圆桌，`local/knowledge/` 是个人知识库。程序按固定位置读取，无需配置圆桌路径或手动更新摘要。

```bash
touch local/roundtable.md
python3 scripts/provider.py validate
```

私人圆桌有正文时，相关工位共用这一份；文件不存在或只有空白时使用通用圆桌。保存后下次准备任务自动采用新内容，受影响的旧准备结果失效。无法读取或编码错误会明确报错。

Git 与发行包只保存 `local/README.md` 和空的 `local/knowledge/.gitkeep`，让新克隆具有标准结构。实际圆桌、知识内容、规则包和日志不会被跟踪，需要单独备份。说明不写进空圆桌文件，以免被误认为用户规则。

高级规则包可放在 `local/provider.json` 中自动接入。外部资料可通过 `GOVEL_DATA_HOME` 指定数据根；已有显式 `GOVEL_PROVIDER_CONFIG` 优先。知识格式与高级配置见 [本地资料规范](knowledge/README.md)。

本地存储控制文件位置。若宿主把读取内容交给远程模型处理，内容仍会离开本机；需要完全本地处理时，应使用本地模型和相应宿主配置。

## 版本与兼容

当前处于 0.x 阶段。包括 1.0 及后续版本在内，均不承诺项目格式、导出格式、MCP 工具名与参数稳定；任何版本都可能发生不兼容变化。不兼容变更在 [CHANGELOG](docs/CHANGELOG.md) 中以 `BREAKING:` 标注。产品版本以 `toolbox.manifest.json` 为准，与项目格式 `schemaVersion`、工作流 `workflowVersion` 分开维护。

不提供旧项目格式读取或自动迁移。升级前使用当前安装版本导出正文与设定、规划阅读快照，例如：

```bash
mkdir -p ~/govel-backups
python3 scripts/novelctl.py project export --project-root ~/govel-books/山城旧事 --format md --status stable --status review --status draft --output ~/govel-backups/山城旧事-正文.md
python3 scripts/novelctl.py project source export --project-root ~/govel-books/山城旧事 --selection '{"includeArchived":true}' --output ~/govel-backups/山城旧事-设定与规划.md
```

正文导出会核验归属；请检查导出结果是否完整。阅读快照不包含批准、事务及运行证据，不能代替项目备份。

升级后通过外部稿件导入流程重建项目，先预览分章：

```bash
python3 scripts/novelctl.py project import preview --source ~/govel-backups/山城旧事-正文.md --project-root ~/govel-books/山城旧事-重建 --book-name 山城旧事
```

确认预览后，使用该次返回的 `planId`、`planHash` 创建新项目：

```bash
python3 scripts/novelctl.py project import commit --project-root ~/govel-books/山城旧事-重建 --plan-id "$IMPORT_PLAN_ID" --plan-hash "$IMPORT_PLAN_HASH" --approve
```

导入不会继承旧批准或正文归属证明。之后结合已导出的设定与规划，经现有源预览与批准入口整理。高级结构候选入口与完整导入步骤见 [操作说明](docs/OPERATIONS.md)。

## 开发

```bash
python3 scripts/local_ci.py --quick --focus tests.test_data_provider
python3 scripts/local_ci.py
```

完整门禁检查公开工程测试、包装同步、检查器基线、空白与冲突标记以及工作树完整性。发布扫描使用仓库外的审核词表，检查文件名与内容；该词表不随源码分发。

接入规范见 [knowledge/README.md](knowledge/README.md)，职责边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

外部投稿请先阅读 [贡献规则](CONTRIBUTING.md) 和 [CLA 草稿（需律师审阅）](CLA.md)。漏洞走 [私密报告说明](SECURITY.md)；仓库维护者可按 [GitHub 网页清单](docs/GITHUB-MAINTAINER.md) 配置检查与分支保护。

## 许可证

Copyright (C) 2026 wuren194 and govel contributors.

除 [第三方来源与许可](THIRD_PARTY_NOTICES.md) 单独注明的部分外，本仓库代码、规则、文档及原创示例采用 GNU Affero General Public License 第 3 版，仅限该版本（`AGPL-3.0-only`）。完整条款见 [LICENSE](LICENSE)。

该许可证允许使用、修改、分发和商业使用。分发受许可证约束的作品时，应按条款提供相应源码；修改后的程序支持远程网络交互时，应向与其交互的用户提供获取对应版本源码的机会。具体义务以许可证原文为准。

用户独立的本地小说、参考资料及数据包不会仅因被引擎读取而自动适用本仓库许可证，其权利与授权由各自权利人管理。
