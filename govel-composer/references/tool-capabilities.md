# composer 工位能力条件段（生成物）

> GENERATED: 由 `novelctl project tools station-cards --apply` 从能力目录生成。
> 手改会被 `--check` 报成漂移；条件、参数与效果的真源是 shared/registries/tools.json
> 与 scripts/core/operations.py，本文件只是它的人读投影。

## 本工位相关能力

| 能力 ID | 何时适用（事实 → 观察值） | 为什么 | 证据/义务 | 绑定 |
|---|---|---|---|---|
| story.logic | project.initialized eq true | 从规则概览开始，按需读取当前规则 | — | operation:query.logic |
| story.attributes | project.initialized eq true | 属性概览与字段投影无需依赖渲染卡片 | — | operation:query.attributes |

## 说明

- 条件为 true/false/unknown 三值；unknown 不放行必要证据，也不当作不适用。
- necessary 的能力缺失时，`project prepare` 会阻塞并给出缺什么；语义审查由本工位执行，系统只验证结论工件。
- 不在本表的能力属于其它工位或维护通道；不要跨工位代跑。
