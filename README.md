# gnovel

中文长篇小说结构化辅助系统。提供设定与规划管理、时间和关系约束、章节工作流、证据定位、受控写入与事务恢复。

本仓库仅包含通用引擎、扩展规范及少量原创演示样例，不附带内置创作参考数据集。用户可根据自身创作需求，在本地目录自行填充特定题材的参考资料，并通过本地数据提供器接入。

## 开始使用

本地运行环境为 macOS、Python 3.11 或更新版本。运行时只使用 Python 标准库。

```bash
python3 scripts/novelctl.py --help
python3 scripts/novelctl.py project quick-init --project-root /tmp/gnovel-example --book-name 山城旧事
python3 scripts/novelctl.py project doctor --project-root /tmp/gnovel-example
```

项目目录必须为空。当前项目格式由 `toolbox.manifest.json` 声明；不自动转换其他格式，也不继承外部稿件的批准记录。

本发行版以 CLI、MCP 和本地 JSON 接口为入口，不附带模型、参考文库或完整图形工作台。模型负责构思、写作与语义审查；工具提供机械检查与可追溯的执行证据。检查通过不等于作者批准。

## 接入本地资料

```bash
python3 scripts/provider.py create --root /tmp/gnovel-example-data
export GNOVEL_PROVIDER_CONFIG=/tmp/gnovel-example-data/config.json
python3 scripts/provider.py validate
python3 scripts/novelctl.py project knowledge pack --planning-intent 伏笔 --station planner
```

示例数据由 `knowledge/example.json` 创建到外部目录。未配置 Provider 时，知识目录为空；显式配置不可读、资源缺失或内容摘要不符时会报错。资料、索引和编辑历史均存放在仓库外。

本地挂载控制文件存储与装载范围。若宿主把读取的内容交给远程模型处理，内容仍会离开本机；需要完全本地处理时，应使用本地模型和相应宿主配置。

## 开发

```bash
python3 scripts/local_ci.py --quick --focus tests.test_data_provider
python3 scripts/local_ci.py
```

完整门禁检查公开工程测试、包装同步、检查器基线、空白与冲突标记以及工作树完整性。发布扫描使用仓库外的审核词表，检查文件名与内容；该词表不随源码分发。

接入规范见 [knowledge/README.md](knowledge/README.md)，职责边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

第三方表面文本指标的来源及许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
