# 本地资料目录

仓库只跟踪本说明和 `knowledge/.gitkeep` 空占位文件。实际私人资料不受 Git 跟踪，也不进入发行包，请单独备份。

- `roundtable.md`：唯一私人圆桌，可直接编辑 UTF-8 Markdown；非空时使用用户版，缺失或空白时使用通用版。
- `knowledge/`：个人知识库，知识资产放在 `assets/`，参考原文与索引按知识库规范管理。

新克隆可在仓库根目录创建空圆桌文件：

```bash
touch local/roundtable.md
python3 scripts/provider.py validate
```

保存圆桌后，下次准备任务自动读取新内容，不用更新摘要或版本号。受影响的旧准备结果需要重新准备。说明和占位提示不要写入空圆桌文件。

高级规则包可以使用本目录的 `provider.json`、`rules/`、`materials/`；它们仍属私人内容。数据格式与外部位置配置见 [本地资料规范](../knowledge/README.md)。
