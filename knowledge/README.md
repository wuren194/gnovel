# 接入本地资料

仓库内仅存放规范、Schema 与一个原创示例。运行时资料目录由 `GNOVEL_PROVIDER_CONFIG` 指向的本地 JSON 配置确定。配置包含 `schemaVersion: 1` 和外部绝对目录 `root`。

`scripts/provider.py create` 可在空目录创建完整样例。该目录包含 `provider.json`、`config.json` 和 `knowledge/assets/example.md`。每份资产采用 JSON frontmatter 与 Markdown 正文；`schema.json` 描述其 JSON 导入表示。`source.path=authored` 表示自行编写，来源摘要为空；有原件的条目须保留来源路径与 SHA-256。

## Provider 声明

`provider.json` 包含 `schemaVersion`、`providerId`、`version`、`registries` 和 `materials`。

可替换的机械注册表只有 `intensity`、`text-patterns` 和 `knowledge`。每项声明一个包内相对路径和 SHA-256；规则数据变动后，需要审阅变动并更新声明。Provider 不加载 Python、JavaScript 或其他可执行扩展，也不能替换项目事务、批准和正文归属机制。

材料条目包含 `id`、`role`、`stations`、`resource` 和 `when`。`role` 为 `reference` 或 `rule`；`stations` 指定消费工位；`resource` 包含相对路径和 SHA-256。`when=null` 表示无条件装载，也可声明由 `field`、`op`、`value` 组成的条件，其中 `op` 为 `eq` 或 `includes`。未知条件保留为未完成，不能当作不适用。

参考资料不会转为作品事实。规则材料仅在作者显式启用的包及适用范围内参与审阅；不授予写入、批准或执行代码的权限。

## 读取与编辑

```bash
python3 scripts/provider.py validate
python3 scripts/novelctl.py project knowledge catalog --status active
python3 scripts/novelctl.py project knowledge index-rebuild
```

检索返回资产身份、内容摘要、来源和选中小节。编辑与恢复继续使用原有版本检查；索引和编辑历史位于外部数据目录。切换包或修改包声明后需要重新启动长驻进程，并重新准备受影响的任务。

所有包内路径必须是普通相对路径，不接受目录逃逸或符号链接。已声明的资源不可用时，系统报告错误，不退回空包。发布包不包含用户数据目录。
