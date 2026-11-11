---
name: govel-bind
description: 安装、绑定或检查当前宿主的 Govel 接入；适用于首次使用、工具缺失或引擎路径失效。日常创作沿已连接的 Govel 工具与根 Skill 执行。
---

# Govel 接入

本 Skill 自带接入脚本 `scripts/bind.py` 与 `binding.json`，可以独立安装。以本 `SKILL.md` 所在目录定位脚本，使用其绝对路径调用；不要相对小说目录猜路径。脚本需要 Python 3.11 或更新版本。

Windows 桌面宿主提供实验性的 WSL2 接入，真实 Windows 客户端、写入／恢复与 WSLg 窗口尚未验收。引擎与项目放在 Linux 文件系统，在对应发行版内运行 `scripts/bind.py wsl-config --engine-root <完整引擎绝对路径> --json`。该操作检查 Windows→WSL 的实际 MCP 握手，输出 `mcpConfig` 供加入 Windows 客户端；不会修改宿主配置。直接 `python3` 插件入口和下面的 `install --host` 不会替 Windows 宿主配置 WSL 桥接。先按完整引擎的 `docs/WINDOWS.md` 安装，再核对实际聊天中的帮助调用；WSL 内运行的宿主 CLI 仍按下面流程接入。

1. 先明确当前宿主。原生 Govel 插件已安装时用 `--host plugin`；单独安装本 Skill 时使用当前的 `--host codex`、`--host claude` 或 `--host antigravity`。只处理作者选定的宿主。
2. 运行 `scripts/bind.py check --host <宿主> --json`。已有完整引擎路径时加 `--engine-root <绝对路径>`；检查不会下载或改配置。
3. 作者要求安装、连接或绑定且检查显示未就绪时，运行同一脚本的 `install`，保留相同的宿主与显式路径参数。此授权仅涵盖 Govel 安装目录和所选宿主的 Govel 注册。独立 Skill 缺少引擎时，安装器从登记的官方仓库下载并记录具体提交；不要另写下载程序或覆盖冲突注册。原生插件已能启动 MCP 时，无须追加全局注册。
4. 分别报告引擎路径与版本、注册状态、独立进程握手结果。脚本的 `hostConnection=not_observed` 不表示当前聊天已经连接；继续在当前会话中调用实际暴露的 Govel `get_tool_help`，传 `operation=project.overview`、`view=full`。核对响应中的 CLI 入口属于检查报告的同一 `engineRoot`，不能把另一份同名 Govel 的响应算成本次接入。宿主未提供可核对身份时，仍报告当前连接未确认。需要宿主刷新或新会话时，按检查结果说明。暂时没有 MCP 工具时，可以使用报告指定的公开 CLI，不改小说内部文件。

接入完成后，读取已验证 `engineRoot` 下的 `SKILL.md`，后续 MCP／CLI 选择按根 Skill 执行。不要从当前工作目录寻找同名文件。接入不创建小说、不批准作品变更，也不替代审校；正文与设定仍由 Govel 的公开领域流程管理。此 Skill 不提供宿主文件权限隔离。

保留用户已有的 `GOVEL_DATA_HOME`、`GOVEL_PROVIDER_CONFIG` 和其他 MCP 注册。私有资料不能放进插件的版本缓存或分发包；安装器给出的稳定数据位置与原 checkout 的 `local/` 依各自模式使用，不自动搬迁资料。
