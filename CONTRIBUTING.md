# 参与贡献

感谢关注 Govel。提交前请先阅读 [AGENTS.md](AGENTS.md) 与 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，它们规定了真源、契约和注册表的分工。

## 欢迎的贡献

- 缺陷报告：附复现命令、项目格式版本（见 `toolbox.manifest.json`）和完整错误输出。
- 文档修正：命令、参数或行为描述与实际实现不一致。
- 检查器误报与漏报：附最小可复现的原创文本片段。
- 新 MCP 客户端的接入验证记录。

以下改动需要先开 issue 讨论：新增契约、新增注册表字段、改变项目格式、改变事务或正文归属语义，以及改变导出格式或 MCP 工具名、参数。改变导出格式或 MCP 工具名、参数的改动须标注 `BREAKING`，并在 `docs/CHANGELOG.md` 中以 `BREAKING:` 开头记录。

## 不接受的内容

- 受版权保护的第三方小说原文或未获授权的拆文资料。参考资料应通过不受 Git 跟踪的本地 Provider 接入，可放在 `local/` 或仓库外，见 [knowledge/README.md](knowledge/README.md)。
- 旧项目格式的读取兼容或自动迁移链。
- 在多个模块硬编码同一阈值；数值应放入 `shared/registries/` 并由访问器读取。

## 开发流程

运行环境为 macOS、Python 3.11 或更新版本，运行时只使用标准库。

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/local_ci.py --quick
python3 scripts/local_ci.py
```

迭代时使用 `--quick`，提交前跑一次完整门禁并确保通过。新增机制时在 `docs/CHANGELOG.md` 顶部追加记录；修改注册表需同步更新相应测试。

## 许可

提交即表示同意你的贡献以 AGPL-3.0-only 发布。
