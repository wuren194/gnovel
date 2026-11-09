# Govel
中文长篇小说结构化辅助系统

提供设定与规划管理、时间和关系约束、章节工作流、证据定位、受控写入与事务恢复。

本仓库仅包含通用引擎、扩展规范及少量原创演示样例，不附带内置创作参考数据集。用户可根据自身创作需求，在本地目录自行填充特定题材的参考资料，并通过本地数据提供器接入。

> **English summary.** Govel is a structured engine for long-form Chinese fiction, exposed as an MCP server and a CLI. It keeps a single source of truth for settings, plans, timeline and chapters, binds prose ownership to approved content, and records every write as a recoverable transaction. The chat model does the ideation, drafting and semantic review; Govel supplies state, constraints, evidence and controlled writes. It ships no model and no reference corpus. Runtime: Python 3.11+, standard library only. License: AGPL-3.0-only.

## 适合解决什么问题

- 写到几十章后设定漂移：人物年龄、关系、道具、时间线前后矛盾。
- 模型改稿覆盖了已确认的正文，或多个会话同时改同一本书。
- 写作中断后难以恢复上下文：上一章写到哪、哪些伏笔未回收、哪些决定已经确认。
- 需要可追溯的审校证据，而不是一句“检查通过”。

## 开始使用

本地运行环境为 macOS、Python 3.11 或更新版本。运行时只使用 Python 标准库。

```bash
python3 scripts/novelctl.py --help
python3 scripts/novelctl.py project quick-init --project-root /tmp/govel-example --book-name 山城旧事
python3 scripts/novelctl.py project doctor --project-root /tmp/govel-example
```

项目目录必须为空。当前项目格式由 `toolbox.manifest.json` 声明；不自动转换其他格式，也不继承外部稿件的批准记录。

本发行版以 CLI、MCP 和本地 JSON 接口为入口，不附带模型、参考文库或完整图形工作台。模型负责构思、写作与语义审查；工具提供机械检查与可追溯的执行证据。检查通过不等于作者批准。

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

客户端连接后，先调用 `list_capabilities` 并用自然语言描述意图（例如 `{"query": "导出全部设定"}`），按返回的 `invocation` 调用对应工具。默认目录不是全部能力清单。

## 五分钟体验

1. 按上节注册 MCP。新建项目目前只提供 CLI 入口，先在终端创建一本书：

   ```bash
   python3 scripts/novelctl.py project quick-init --project-root ~/govel-books/山城旧事 --book-name 山城旧事
   ```

2. 在客户端对话中说明：“用 Govel 打开 `~/govel-books/山城旧事`，运行诊断。”
3. 继续用对话补充主角、地点和第一章的构想，让模型通过工具登记设定并生成章节推演。
4. 每个正式写入都会先返回预览，确认后才提交。随时可要求“运行 doctor”查看项目状态，或“导出全部设定”生成阅读快照。

不使用 MCP 时，同样的流程可直接用 CLI 完成，命令见上文“开始使用”，完整操作见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

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

升级后通过外部稿件导入流程重建项目。先建立只读核对副本：

```bash
python3 scripts/novelctl.py project import review --source ~/govel-backups/山城旧事-正文.md --target-root ~/govel-books/山城旧事-导入核对
```

结合导出的设定与规划，人工整理并校对 `candidate.json` 后，再创建新项目：

```bash
python3 scripts/novelctl.py project import create --project-root ~/govel-books/山城旧事-重建 --candidate ~/govel-backups/candidate.json
```

导入不会继承旧批准或正文归属证明。候选文件要求与导入步骤见 [操作说明](docs/OPERATIONS.md)。

## 开发

```bash
python3 scripts/local_ci.py --quick --focus tests.test_data_provider
python3 scripts/local_ci.py
```

完整门禁检查公开工程测试、包装同步、检查器基线、空白与冲突标记以及工作树完整性。发布扫描使用仓库外的审核词表，检查文件名与内容；该词表不随源码分发。

接入规范见 [knowledge/README.md](knowledge/README.md)，职责边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 许可证

Copyright (C) 2026 wuren194 and govel contributors.

除 [第三方来源与许可](THIRD_PARTY_NOTICES.md) 单独注明的部分外，本仓库代码、规则、文档及原创示例采用 GNU Affero General Public License 第 3 版，仅限该版本（`AGPL-3.0-only`）。完整条款见 [LICENSE](LICENSE)。

该许可证允许使用、修改、分发和商业使用。分发受许可证约束的作品时，应按条款提供相应源码；修改后的程序支持远程网络交互时，应向与其交互的用户提供获取对应版本源码的机会。具体义务以许可证原文为准。

用户独立的本地小说、参考资料及数据包不会仅因被引擎读取而自动适用本仓库许可证，其权利与授权由各自权利人管理。
