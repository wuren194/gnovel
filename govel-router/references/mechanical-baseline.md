# 机械证据基线

文本模式、范围与数值来自 shared/registries/text-patterns.json 的 ai、diagnosticPolicy 等对应字段，以及明确启用的本地覆盖。使用 project diagnostics rules 查询当前规则卡，按当前任务范围运行检查。

机械检查返回范围、定位、严重度和未评估内容。检查结果不证明文学质量或作者批准，未启用的题材规则报告不适用。候选变更后必须重新核验旧证据的有效性。

风格候选的 `review=semantic` 与 `gate=false` 表示交由 Editor 复核，`review_advisory` 不阻断机械门。不能按命中数推定模型作者身份，不能把可选提示转换成“全部清零”的要求。S-10q 默认只报告启发式数值，不要求正文凑分。实际成立的语义缺陷仍在阶段 B 处理。

密度提示附原始命中位置及统计口径；成组提示内的每个原句都能回查。具体字段、CLI 退出码与校准边界见 [风格诊断说明](../../docs/STYLE-DIAGNOSTICS.md)。
