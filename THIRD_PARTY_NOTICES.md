# 第三方来源与许可

本文件记录随 govel 分发内容涉及的第三方来源及其原有许可。govel 自有部分采用 AGPL-3.0-only，范围见 [README.md](README.md#许可证)，完整条款见 [LICENSE](LICENSE)。

## Sepia

`scripts/sepia_bridge.py` 的中文表面检查参考了 [Sepia](https://github.com/Nanako0129/sepia) 的中文表达观察与词表，尤其是 `skills/sepia/references/languages/zh.md`。该脚本提供本项目的 Python 表面检查实现；本发行版不包含完整 Sepia Skill，也不据此宣称完成 Sepia 的语义审阅流程。

原版权声明为 **Copyright (c) 2026 Nanako Tsai**。Sepia 采用 MIT License，完整版权及许可文本保留在 [LICENSES/sepia-MIT.txt](LICENSES/sepia-MIT.txt)。该许可声明的范围限于对应的第三方来源内容，不代表 Govel 其余代码或用户本地资料的许可状态。

## GitHub Actions 开发服务

以下工具由 GitHub Actions 在 runner 上取得并运行，不打包进引擎源码或作为运行时依赖；工作流保留来源并固定完整提交 SHA。各工具及其依赖仍适用上游许可证，本项目的 CLA 不取得这些工具的额外权利。

| 工具 | 用途 | 上游许可证 |
| --- | --- | --- |
| [actions/checkout](https://github.com/actions/checkout) | 检出源码 | MIT |
| [actions/setup-python](https://github.com/actions/setup-python) | 准备 Python | MIT |
| [actions/setup-node](https://github.com/actions/setup-node) | 准备贡献流程回归使用的 Node.js | MIT |
| [actions/github-script](https://github.com/actions/github-script) | 调用 GitHub API 和可信本地辅助脚本 | MIT |
| [manaflow-ai/cla-github-action](https://github.com/manaflow-ai/cla-github-action) | CLA 身份核对及签署记录，源自 SAP 及贡献者的 CLA Assistant | Apache-2.0 |

本项目的 `CLA.md` 是为当前需求起草的中文协议草稿，需律师审阅；没有复制上述工具附带的协议，也不将其工具许可证当作贡献协议。
