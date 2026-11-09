# 本地圆桌与知识库

## 日常入口

默认数据根是当前引擎仓库的 `local/`：

```text
local/
├── README.md             # 公开使用说明
├── roundtable.md         # 用户自行编辑，不受 Git 跟踪
└── knowledge/
    ├── .gitkeep          # 公开空占位文件
    ├── assets/           # 知识资产 Markdown
    ├── sources/          # 参考原文
    ├── .cache/           # 派生索引
    └── .history/         # 资料编辑历史
```

Git 与发行包只保留说明及空占位文件，实际私人内容需要单独备份。不要把资料写进 `.gitkeep`，检查器会拒绝带内容的占位文件。

## 编辑圆桌

创建或打开 `local/roundtable.md`，用普通文本编辑器保存 UTF-8 Markdown。文件非空时使用用户圆桌，不存在、空文件或只有空白时使用通用圆桌；说明和占位提示放在旁边的 README，圆桌文件可以真正留空。

不需要声明文件路径、手动计算摘要或修改版本号。程序记录每次实际使用的内容版本，修改后重新准备受影响任务即可；知识库不因圆桌修改而要求重启。切换到用户版、修改用户版、清空回到通用版，都会使之前依赖不同版本的准备及读取回执失效。不使用圆桌的执笔准备不因此失效。

文件不可读、不是 UTF-8 或路径包含符号链接时报告具体错误。私人圆桌仍使用引擎当前的十席编号及阶段组席规则，事务、作者批准和证据校验由引擎执行。

## 维护知识库

每份知识资产使用 JSON frontmatter 与 Markdown 正文；`schema.json` 描述其 JSON 导入表示。`source.path=authored` 表示自行编写，来源摘要为空；有原件的条目保留来源路径与 SHA-256。阅读知识不会将其提升为作品事实。

```bash
python3 scripts/provider.py validate
python3 scripts/novelctl.py project knowledge catalog --status active
python3 scripts/novelctl.py project knowledge index-rebuild
```

索引和编辑历史是本地派生资料。编辑与恢复继续使用已有版本检查，知识原文不会因为移动目录而改变身份。

## 可选规则包与外部位置

数据根存在 `provider.json` 时自动装载规则包，没有该文件也可以使用圆桌和知识库。清单包含 `schemaVersion`、`providerId`、`version`、`registries` 和 `materials`，不包含圆桌配置。

可替换的机械注册表为 `intensity`、`text-patterns` 和 `knowledge`。每项声明包内相对路径与 SHA-256。材料包含 `id`、`role`、`stations`、`resource`、`when`；`role` 为 `reference` 或 `rule`，`resource` 为路径与摘要，`when=null` 表示无条件使用。条件不明时保留缺项，不视为不适用。`roundtable.md` 不得列为固定摘要材料，`GOVEL-ROUNDTABLE` 为保留 ID。

可在尚未使用的子目录创建原创中性示例：

```bash
python3 scripts/provider.py create --root "$PWD/local/example"
GOVEL_DATA_HOME="$PWD/local/example" python3 scripts/provider.py validate
```

数据位置依次由显式 `GOVEL_PROVIDER_CONFIG`、`GOVEL_DATA_HOME`、仓库 `local/` 决定。显式配置仍是包含 `schemaVersion: 1` 与 `root` 的 JSON；`root` 可为绝对路径或相对于配置文件的普通路径。所有位置必须在仓库 `local/` 或仓库外，包内路径不接受逃逸或符号链接。

配置损坏、固定规则资源缺失或摘要不符时报错。变更规则包声明或移动数据根后重启长驻进程；日常只编辑圆桌文件不需要这一步。Provider 不加载可执行扩展，也不能替换项目事务、批准或正文归属机制。
