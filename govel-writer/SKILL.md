---
name: govel-writer
description: 按已批准规划产出候选正文。
---

# Writer

按已批准规划产出候选正文。先按根 Skill 取得任务准备结果，读取 manifest 为当前工位装载的契约与材料。
工位职责与交接依据 `shared/contracts/WORKSTATION-LIFECYCLE.md`；圆桌来源选择按根 Skill 执行，只读取当次计划选中的版本。

材料读取沿准备结果的 readIds 执行。Provider 规则只在声明条件成立时使用，参考内容不提升为作品事实。
不凭路径、哈希或机械通过推导作者批准；正文正式采用沿核心提交路径。
