# Govel 开发规则

公开仓库只跟踪通用引擎、规范和原创中性示例，以及 `local/README.md`、空的 `local/knowledge/.gitkeep` 目录骨架。用户资料可放在 `local/` 或仓库外；私人圆桌、知识内容、Provider 配置及日志均由 Git 忽略，不得进入发行包或复制到公开源码和测试。`local/roundtable.md` 是可直接编辑的唯一私人圆桌，缺失或空白时使用通用版。

仅支持 manifest 声明的当前项目格式。语义归契约，数值归注册表，代码必须有真实消费者。修改作者体验前读取 `docs/PRODUCT-INTENT.md` 与 `docs/INTENT-ALIGNMENT.md`。

保留事务、正文归属、作者批准及证据依赖。参考资料不提升为作品事实，机械通过不替代语义审阅。多视角文学审查在同一会话内完成，不拆成独立代理。

先运行直接相关的快速检查，改动收齐后运行一次 `python3 scripts/local_ci.py`。未配置 Provider 的干净环境必须可验证通用能力；私有包的回归在仓库外执行。版本以 manifest 为准，新增机制同时更新变更记录。
