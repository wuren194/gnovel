# Windows 使用 Govel（WSL2 实验性接入）

Govel 提供 Windows＋WSL2 的实验性接入路径，尚未完成 Windows 实机验收。引擎与小说存放在 WSL 的 Ubuntu 内，Windows AI 客户端通过自定义 stdio MCP 配置调用 `wsl.exe`。Windows 端无需安装 Python，原生 Windows 引擎暂不支持。

建议使用 Windows 11、最新版 WSL2 和 Ubuntu 24.04。Windows 10 的 WSLg 图形窗口要求 Build 19044 或更新版本，具体系统要求见 [Microsoft 的 WSLg 说明](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps)。

当前验证情况：

| 范围 | 状态 |
| --- | --- |
| macOS／Ubuntu 引擎 | 已有完整工程门禁通过记录，见[平台基线](LOCAL-CI.md#已验证的平台基线) |
| WSL 配置生成、参数传递与 MCP 调用 | 已通过模拟桥接连接真实 Govel MCP 的自动化测试 |
| 真实 Windows＋WSL2 | 客户端连接、小说写入与恢复、WSLg 确认窗口均待实测 |
| 原生 Windows、Windows 挂载盘上的活跃项目 | 不在本次支持范围 |

`wsl-config` 在用户的 WSL 环境中执行桥接握手检查；命令通过后仍需在实际客户端中确认工具可用。握手结果不代替小说写入／恢复或真人窗口验收。

## 1. 安装并打开 Ubuntu

在**管理员 PowerShell** 中执行：

```powershell
wsl --install -d Ubuntu-24.04
```

按系统要求重启，首次打开 Ubuntu 时创建 Linux 用户与密码。已有 WSL 的电脑可以复用安装；在 **PowerShell** 中检查：

```powershell
wsl --update
wsl --list --verbose
```

确认准备使用的发行版 `VERSION` 为 `2`。如果为 `1`，先备份该发行版内的已有资料，再按 [Microsoft 的 WSL 命令说明](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)转换。已有其他发行版时，可以沿用，但需要 Python 3.11 或更新版本。

打开 Ubuntu：

```powershell
wsl -d Ubuntu-24.04
```

下文 Ubuntu 命令都在这个窗口中执行。所用发行版名称以 `wsl --list --verbose` 的输出为准。

## 2. 安装 Govel

在 **Ubuntu** 中执行：

```bash
sudo apt update
sudo apt install -y python3 git zenity
git clone https://github.com/wuren194/govel.git ~/govel
cd ~/govel
mkdir -p ~/govel-books
python3 scripts/govel_bind.py wsl-config --json
```

已有 Govel 时进入现有目录执行最后一条命令，无须重复克隆。命令会通过 `wsl.exe` 再启动本发行版中的 Govel，检查初始化、工具列表与真实帮助调用，然后输出 Windows 客户端配置。它只生成配置，不修改客户端文件或小说。

输出中的 `code` 应为 `WSL_CONFIG_READY`，`processHandshake.ok` 应为 `true`。它会记录实际发行版、Linux 用户、Python、引擎及资料目录，路径含中文或空格时也按独立参数传递。已有 `GOVEL_DATA_HOME`／`GOVEL_PROVIDER_CONFIG` 会按当前 Linux 绝对路径写入配置；默认 checkout 继续使用自己的 `local/`。

如果看到 `WSL_REQUIRED`，说明命令没有在 WSL 中运行。如果看到 `WSL_BRIDGE_UNAVAILABLE`，检查 WSL 的 Windows 程序互操作与 Windows PATH 是否启用。`WSL_MCP_HANDSHAKE_FAILED` 表示尚未生成可用配置，先处理发行版启动、互操作或解释器问题。不要将这些错误报告当作 MCP 配置粘贴。

## 3. 接入 Windows 客户端

在 Windows 客户端的 MCP 设置中添加输出的 `mcpConfig`。使用 `mcpServers` JSON 格式的客户端，可以合并其中的 `govel` 条目；保留原有其他服务器。手动填写表单时，将 `command` 填为 `wsl.exe`，将 `args` 中的每项保持为独立参数。

只复制 `mcpConfig` 内的配置对象，不复制整份检查报告。输出已包含真实路径，无须手工替换用户名，也不要把 Linux 路径改成 `C:\...`。使用其他配置格式的客户端，保留相同的 `command` 与 `args`。

当前插件声明直接启动 `python3`，所以此路线使用手动 MCP 接入。不要把 Windows 端插件安装成功当成 WSL 引擎已连接，也不要同时留下两条同名 `govel` 注册。在 Ubuntu 内运行宿主 CLI 的用户，则可沿用[普通接入流程](INTEGRATIONS.md)；它配置的是 Linux 侧宿主。

保存并按客户端要求刷新后，在对话中说：

> 调用 Govel 的 get_tool_help，查询 project.overview，使用 view=full。核对返回的 CLI 路径与刚才 WSL 配置中的 engineRoot 一致。

工具实际返回后，当前客户端连接才算确认。终端报告的 `hostConnection=not_observed` 会保留，因为脚本看不到聊天是否已加载新配置。

## 4. 开始写作与导入

小说目录使用 WSL 的 Linux 路径。在 Ubuntu 中运行 `realpath ~/govel-books` 得到准确位置，然后告诉 AI：

> 在刚才的 govel-books 目录下，用 Govel 为《山城旧事》生成新建项目预览，先让我核对。

后续设定、规划、续写与定稿仍通过对话完成，预览与作者批准沿用现有流程。AI 调用 Govel 时传 `/home/...` 路径；如果需要 CLI，配置报告的 `cliCommand` 已给出从 Windows 启动它的参数。

引擎、活跃小说和私有资料先放在 Ubuntu 家目录中。`/mnt/c`、`/mnt/d` 是 Windows 挂载盘，不在本路线的验收范围。已有 TXT/Markdown 原稿可以从这些路径读取，经 Govel 预览、批准后导入 Linux 目录。不要将运行中的项目放在 Windows 同步盘，再让两端同时改写。

从 Windows 文件资源管理器查看文件时，在 Ubuntu 对应目录执行 `explorer.exe .`，或在资源管理器输入 `\\wsl$` 选择发行版。文件存放建议见 [Microsoft 的跨文件系统说明](https://learn.microsoft.com/en-us/windows/wsl/filesystems)。

## 5. 采用外部修改时的确认窗口

直接用编辑器改过受管正文后，外改采用会通过 Linux 的 `zenity` 请求确认。WSLg 负责把窗口显示到 Windows 桌面；这一步需要可用的图形会话。

可在 Ubuntu 中单独检查窗口：

```bash
zenity --info --text="Govel 确认窗口可以显示"
```

这个窗口仅检查图形通道，不批准任何作品修改。若窗口不可用，外改采用保留原计划并返回 `APPROVAL_CHANNEL_UNAVAILABLE`；先修好 WSLg 再重试，输入已变化时重新预览。查询和其他不依赖这个窗口的工作仍可使用。

## 验收范围

`wsl-config` 验证的是当前 Windows→WSL 桥接进程的 MCP 初始化、工具发现和帮助调用。它不替代真实客户端连接、小说写入／恢复或真人窗口验收。

维护者或首次验证新环境时，在 Ubuntu 的 Govel 目录运行完整工程门禁：

```bash
python3 scripts/local_ci.py
```

测试临时目录应与拟使用的项目位于同类 Linux 文件系统，报告保留实际环境与提交。随后用中性样例在实际客户端完成新建、导入、查询、批准提交、恢复及外改窗口确认。只有实际完成的部分才能记为通过；普通 Ubuntu CI 与模拟 `wsl.exe` 的单元测试均不能充当 WSL2 实机记录。
