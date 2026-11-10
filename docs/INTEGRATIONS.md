# 安装与首次接入

Govel 的插件把 Skill 与本地 MCP 引擎一起安装。运行需要 macOS 或 Linux、Python 3.11 或更新版本；引擎和接入脚本只使用 Python 标准库。插件不内置模型，也不替宿主授予文件权限。

## 安装宿主插件

Claude Code：

```bash
claude plugin marketplace add wuren194/govel
claude plugin install govel@govel
```

Codex：

```bash
codex plugin marketplace add wuren194/govel
codex plugin add govel@govel
```

Antigravity：

```bash
agy plugin install https://github.com/wuren194/govel
```

安装后选用 `govel-bind`。Claude Code 另提供 `/govel:init`；Codex 可用 `$govel-bind`。`init` 是接入别名，不是创建小说命令。原生插件已声明 MCP，由宿主管理服务进程，引导只检查该包和独立进程；不额外创建另一份全局 MCP 注册。

如果当前聊天还没有 Govel 工具，按宿主提示刷新或新开会话，再调用 `get_tool_help`。使用完整视图可核对返回的 CLI 入口是否对应本次引擎。工具不可用时，公开 CLI 仍可用于当前已授权任务；不通过直接改小说内部 JSON 代替工具。

## 只安装接入 Skill

已有 Skills CLI 时：

```bash
npx skills add wuren194/govel --skill govel-bind
```

`skills/govel-bind/` 是可单独复制的引导包，包含自己的脚本和策略。它不依赖工作区 Loader 的 `../../../SKILL.md` 路径。安装后让该 Skill 为当前宿主绑定 Govel；Node/npm 只参与这个可选分发步骤。

引导先检查显式指定的完整引擎、包内引擎或已有绑定。没有可用安装时，`install` 从唯一官方仓库解析到一个固定 Git 提交，再下载并校验该提交的源码包；安装目录保留提交身份，已有绑定不在每次启动时自动更新。下载或校验失败不会登记成功绑定。Python 不符合要求时明确报错，不自动安装系统软件。

## 绑定已有 checkout

在工具箱目录执行：

```bash
python3 scripts/govel_bind.py check --host codex --engine-root . --json
python3 scripts/govel_bind.py install --host codex --engine-root . --json
```

其他目录使用脚本与引擎的绝对路径；`--host` 按当前客户端选择 `codex`、`claude` 或 `antigravity`。Claude/Codex 的注册候选由其官方 CLI 生成，再按原配置快照发布；已有同名冲突、并发变化或无法校验的配置不会被自动覆盖。Antigravity 沿用其配置保全与恢复流程。已有环境变量和其他服务器不因绑定 Govel 被丢弃。

离线使用时指定已存在的完整引擎。`check` 不下载、不注册、不创建小说；`serve` 不下载、不重绑，只启动已确定的引擎。它的标准输出只传输 MCP 消息，诊断进入标准错误。

## 接入结果与资料位置

三个结果分开报告：

| 结果 | 能证明什么 |
| --- | --- |
| 配置已就位 | 选定宿主的注册或插件声明存在 |
| 独立进程握手成功 | 此解释器和引擎完成初始化、工具发现及实际帮助调用 |
| 当前聊天工具调用成功 | 此会话已加载对应引擎；需要在宿主内观察 |

脚本不能替当前聊天确认第三项，返回 `hostConnection=not_observed`。另一份 Govel 的响应也不能证明新绑定已加载；路径或身份不可见时继续标为未确认，不把安装成功解释成创作就绪。

显式的 `GOVEL_DATA_HOME`、`GOVEL_PROVIDER_CONFIG` 保持优先。原生插件默认使用稳定的用户数据位置，避免把私人圆桌、知识和日志写到随版本替换的插件缓存。普通开发 checkout 绑定继续使用现有 `local/` 约定。引导不会自动复制或迁移私人资料，实际引擎根与数据根以返回结果为准。

安装授权只覆盖所选宿主的 Govel 接入和工具自身的安装数据。作品创建、设定修改、正文采用和审校仍走原公开操作；接入成功不产生作者批准。Skill 负责指引，事务与证据校验由共享核心执行，不能把 Markdown 指引当成宿主的文件系统隔离。

## 构建可分发的插件包

从当前公共工作树生成包，输出必须是仓库外的新文件：

```bash
python3 scripts/package_plugin.py --plan
python3 scripts/package_plugin.py --output /tmp/govel-plugin.zip
python3 scripts/check_release_contents.py --public --zip /tmp/govel-plugin.zip
```

包包含引擎、契约、技能、宿主声明和许可证。Git 元数据、私人资料、Provider 配置、日志和缓存不进入包；`local/` 只保留公开说明和空目录骨架。构建会复核实际文件和输出，已有 ZIP 不覆盖。

本地测试使用这个公开包的解压目录，不把含私人 `local/` 的开发目录直接交给宿主复制到插件缓存。宿主可能把 `.git` 一起复制进缓存，doctor 因此不按 Git 元数据推断安装方式，只检查运行时包装。发布候选和旧 Antigravity 全局软链在项目诊断中标为不适用，分别用 `scripts/check_release_contents.py --public` 与 `scripts/install_antigravity.py --check` 核验；这些结果不代替当前宿主连接验证。

声明与薄包装由 `scripts/generate_wrappers.py` 生成，版本以 `toolbox.manifest.json` 为准。接入策略来自 `shared/registries/integration.json`，供独立 Skill 使用的 `binding.json` 同步生成。

## 宿主规范

- [OpenAI 插件格式](https://developers.openai.com/plugins/build/plugins)：根 `plugin.json`、`mcp.json`、`skills/`，OpenAI 设置放在 `extensions.com.openai`。
- [Claude Code 插件格式](https://code.claude.com/docs/en/plugins-reference)：`.claude-plugin/plugin.json` 声明本地 MCP，`commands/init.md` 提供命名空间内的接入别名。
- [Antigravity 插件格式](https://antigravity.google/docs/build-plugins)：使用 `.antigravity-plugin/plugin.json` 与 `mcp_config.json` 适配宿主加载。

原生安装仍受宿主版本、权限、刷新机制和可用 Python 环境影响。静态清单校验、独立握手与真人客户端使用分别验证，不互相代替。
