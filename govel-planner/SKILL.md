---
name: govel-planner
description: 维护设定、规划与章节推演。
---

# Planner

维护设定、规划与章节推演。先按根 Skill 取得任务准备结果，读取 manifest 为当前工位装载的契约与材料。
工位职责与交接依据 `shared/contracts/WORKSTATION-LIFECYCLE.md`；圆桌来源选择按根 Skill 执行，只读取当次计划选中的版本。

材料读取沿准备结果的 readIds 执行。Provider 规则只在声明条件成立时使用，参考内容不提升为作品事实。
不凭路径、哈希或机械通过推导作者批准；正文正式采用沿核心提交路径。

只对当前卷做 hard planning。普通正式续写与约定候选批次分别遵守 CHAPTER-PIPELINE.md，不为续写候选提前正式定稿。CH001 前必须存在已填实的 ARC001。
