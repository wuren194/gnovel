---
name: gnovel
description: 中文长篇小说结构化辅助系统。
---

# gnovel

作者表达创作目标，模型承担构思、规划、写作与语义判断，工具维护资料、证据、版本和受控写入。
优先使用已连接的 MCP 工具目录；CLI 入口为 `python3 scripts/novelctl.py`，命令详情由 `--help` 查询。

准备任务后读取当次契约、来源、缺项与下一步动作。执行范围以作者授权和当前机器状态为准。
文学审查的多种视角在同一会话内切换。局部修订保留无关成果；可选建议随稿呈现，不冒充必修问题。
参考知识不修改作品事实。配置的 Provider 不可用时说明缺项；不借关闭规则取得通过。

准备结果优先使用 `--view model` 的紧凑投影；缺字段时查询当前操作定义。

## 建章、审校与提交

| 操作 | 用途 |
| --- | --- |
| `workflow.chapter-preview` | 预览章节及计划 |
| `source.chapter-plan` | 读取当前章节计划 |
| `workflow.review-form` | 生成绑定当前输入的审查表单 |
| `workflow.submit` | 提交约定范围的工件 |
| `workflow.state-prepare` | 准备当前状态候选 |
| `workflow.closeout-prepare` | 检查正式采用前提 |
| `workflow.closeout` | 在有效批准范围内完成收尾 |
