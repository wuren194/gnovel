---
name: gnovel-router
description: 协调状态、事务与作者批准。
---

# Router

协调状态、事务与作者批准。先按根 Skill 取得任务准备结果，读取 manifest 为当前工位装载的契约与材料。
工位职责与交接依据 `shared/contracts/WORKSTATION-LIFECYCLE.md`；审查依据 `shared/contracts/G-MULTI-IDENTITY-REVIEW.md`。

材料读取沿准备结果的 readIds 执行。Provider 规则只在声明条件成立时使用，参考内容不提升为作品事实。
不凭路径、哈希或机械通过推导作者批准；正文正式采用沿核心提交路径。

按 CHAPTER-PIPELINE.md 核对批准范围与唯一 `nextAction`，从当前证据恢复流程。
