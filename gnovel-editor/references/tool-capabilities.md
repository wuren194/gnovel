# editor 工位能力条件段（生成物）

> GENERATED: 由 `novelctl project tools station-cards --apply` 从能力目录生成。
> 手改会被 `--check` 报成漂移；条件、参数与效果的真源是 shared/registries/tools.json
> 与 scripts/core/operations.py，本文件只是它的人读投影。

## 本工位相关能力

| 能力 ID | 何时适用（事实 → 观察值） | 为什么 | 证据/义务 | 绑定 |
|---|---|---|---|---|
| chapter.prepare | project.initialized eq true | 任务开始前需要确定读什么、缺什么、下一步做什么 | — | operation:task.prepare |
| chapter.mechanical | diagnostics.current eq false | 当前正文没有完整且新鲜的机械证据 | chapter.review、chapter.finalize（证据：diagnostics） | operation:diagnostics.aggregate |
| editor.narrative-review | review.current eq false | 当前正文需要阶段 B 语义审校 | chapter.finalize（证据：stage_b_review） | skill:gnovel-editor/SKILL.md |
| story.timeline | timeline.hasInversion eq true | 时间挂载中记录了倒挂，需判断是否为有意倒叙 | — | operation:query.timeline |
| story.logic | project.initialized eq true | 从规则概览开始，按需读取当前规则 | — | operation:query.logic |
| story.attributes | project.initialized eq true | 属性概览与字段投影无需依赖渲染卡片 | — | operation:query.attributes |
| story.nodes | project.initialized eq true | 有界查看本章关联范围，不把未记录当作从未使用 | — | operation:query.nodes |
| chapter.context | project.initialized eq true | 任务需要当前章节与设定上下文 | — | operation:context.read |

## 说明

- 条件为 true/false/unknown 三值；unknown 不放行必要证据，也不当作不适用。
- necessary 的能力缺失时，`project prepare` 会阻塞并给出缺什么；语义审查由本工位执行，系统只验证结论工件。
- 不在本表的能力属于其它工位或维护通道；不要跨工位代跑。
