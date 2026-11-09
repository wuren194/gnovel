# 当前正文归属与受保护写入契约

本契约适用于 manifest 声明的当前项目格式。当前实现只支持 schema 5 与 current-binding-v1。其他格式不参与当前归属判定，外部稿件按 [人工整理入口](OPERATIONS.md#外部稿件入库) 纳入新项目，不能继承旧身份或批准。

## 当前权威与不变量

- `chapterId` 是稳定章节身份，`filePath` 是可变定位符；只有当前结构登记的 filePath 是权威正文位置。
- `proseBinding` 与盘上观测共同决定正文归属。路径安全、文件存在、名称相同、内容 hash 相同都不能单独证明归属。
- 未知的已有文件不能自动接管；managed 正文缺失不能降格为空的 unbound 槽。
- 作者批准不证明当前目标仍有效。提交在锁内重新验证目标、代次、工件和批准。
- 健康当前归属不依赖历史日志、旧备份、连续性或迁移回执；历史损坏不能撤销当前健康归属，也不能为当前损坏提供回退证明。

## 持久绑定与完整性

`proseBinding` 保存 `state`、`generation`、`expectedContentDigest`、`integrityDigest`。状态来自 source-model 注册表；代次为最近一次正式绑定变更的提交 revision。unbound 的预期正文摘要必须为空；managed 必须持有已经接纳内容的摘要；unresolved 保留待决状态，不能普通覆盖或移动。

`seal_binding()` 是唯一封签实现。完整性摘要绑定 format、projectId、chapterId、filePath、state、generation 和 expectedContentDigest 的规范 JSON。它检测局部损坏，不是数字签名，也不提供抗本机同权限恶意协同改写的密码学来源证明。

普通 load、normalize、save、缓存重建及派生操作不能重封损坏 binding。错误须作为 `OWNERSHIP_BINDING_INVALID` 报告；只有正式受控流转在全部前置校验通过后才能生成新封签。

## 受控流转

| 动作 | 起点与前置条件 | 结果 |
|---|---|---|
| 建章材料化 | 身份尚不存在，目标为空，建章能力精确覆盖新增章节 | 密封 unbound，generation 取本次提交 revision |
| 首次定稿 | unbound、空槽、当前代次与全部审校批准有效 | 写正式正文并密封 managed |
| 再定稿 | managed，盘上正文匹配预期摘要，当前代次与新工件批准有效 | 写新正文并推进代次，即使字节相同也不复用旧 token |
| 正文位置移动 | managed 的正文健康，或 unbound 的两端均为空；新位置无冲突 | 文件与定位符同事务移动，摘要保持、代次推进 |
| 正式撤销 | 可撤销链顶端、反向前置条件与当前代次有效 | 只逆转该操作实际改变的语义字段，提交新的 revision 与 generation |

unresolved 不允许普通 move、rename 或 finalize。纯大小写和 Unicode 规范化形式改名仍按现有路径限制阻断，不通过中间文件名绕过。

外部修改的接受是单独的高风险流转：当前只允许 preview；commit 需要独立特权批准能力。普通 `approve=true`、actor 字符串或重算 planHash 均不是该能力，缺失时返回 `APPROVAL_CAPABILITY_REQUIRED`。Explicit Adopt、Rebind 与 Binding Repair 不能由普通保存或候选登记模拟。

预览保存原请求身份及准确 H1/H2 材料：H1 只从有本章、代次和 locator 关联的登记工件或原提交镜像找回，缺失时明确 unavailable；H2 保存独立字节副本、对象观测和差异。材料准备不证明语义审查完成，也不签发独立能力。原请求只延长期限时仍核验全部原材料，H3、同字节换 inode 或附件变动都不能续用。当前 OS 在场正向确认、可信显示、pin 与 nonce 原子消费仍待完成，因此提交通道保持关闭。

## 通用写者边界

`chapter.filePath` 与完整 `chapter.proseBinding` 是受保护字段。通用 `save_structure` 更新已有结构时必须比较当前状态，未经正式流转能力不得改动它们，也不得新增正式章节。

在磁盘没有 structure 的情况下，通用保存不能自建拥有正文归属的项目。初始创建只由具名初始化通道完成，候选不得自带 proseBinding；导入器统一封签并验证，其目标必须为空。

结构 preview 与 commit 复用同一写者边界。建章时 `generation = proposed.revision`，且 `proposed.revision == current.revision + 1`。建章 capability 必须精确等于实际新增 chapterId 集合，不能少报、多报或换成另一个身份。正式流转的授权集合与实际受保护变更集合也必须精确一致。

## 位置保留与故障隔离

managed、unbound、unresolved 和归档章都排他保留其规范定位符。物理文件缺失不释放位置；unresolved 的位置保留也不等于取得正向归属。

规范定位符唯一性是结构级硬约束，提交时再次强制验证。路径须在项目范围内且规范，拒绝 traversal、symlink、目录目标、折叠冲突和文件系统别名冲突。硬链接等物理别名由真实磁盘观测判定。

单章 binding 损坏属于 chapter-local 错误，应阻断该章的写入而允许其他健康章继续读取与处理。重复定位符、全局身份冲突等 project-fatal 问题阻断相关结构操作。不能为隔离单章错误而放宽全局位置唯一性。

## 代次、撤销与恢复

binding generation 随正式变更的提交 revision 前进，不是正文内容 hash。请求必须回显已经读取的当前代次；服务端不能在缺令牌时自行补当前值。旧请求在改动、撤销、再改动后仍然失效，避免 ABA 复活。

正式撤销是向前提交的反向事务，只恢复该操作实际改变的 binding 语义字段，绝不恢复旧 generation 或 integrityDigest。没有 binding 变化的操作不为章节无理由推进 generation。目标不在链顶端则返回 `UNDO_NOT_CHAIN_TOP`；有冲突、外部改动或证据缺失时拒绝。

事务的持久清单是 `.govel/transactions/<TX-ID>.json`，镜像分别位于 `.govel/transactions/<TX-ID>/before/` 与 `.govel/transactions/<TX-ID>/after/`。`phase != committed` 时恢复精确 before-image（COMPLETE OLD），after-state 尚未取得已提交权威；`phase == committed` 时保留完整 after-state（COMPLETE NEW）并清理。不新增 `prepared → forward replay` 路径。未提交事务恢复可恢复当时 revision；已经提交的业务状态撤销仍必须前向提交。

只读 file guards 与目录成员 guards 不能成为恢复写入目标。被校验的正文状态直接作为写集 before-image；提交期间出现外部修改时否决本次发布并保留外部对象，不能把新出现的文件认领为自己的旧字节。

最后窗口的保护依赖实际对象保全及 exclusive 发布，不依赖“再 hash 一次”。保全路径中的 displaced inode 与不可变镜像分开，成功后仍保留可能被旧 fd 迟写的对象。恢复同样先保全被置换对象，不直接 unlink 预检时的路径；无法安全完成时保持可定位 pending。原 journal 不改写，完成 manifest 留在原 TX 目录。稳定锁 anchor 不删除，实际拥有者与占用由内核锁和线程所有权共同判断。

## 客户端回执

章节回执提供当前所有权观测、bindingGeneration、阻塞和 finalizeAction。调用者回传具体参数，不自己拼归属逻辑。定稿和真实绑定撤销缺少当前代次或代次过期均拒绝。

核心观测包括 UNBOUND、OWNED_CLEAN、OWNED_MODIFIED_EXTERNALLY、OWNED_MISSING、UNRESOLVED、CONFLICT、BINDING_INVALID。错误码与完整字段以运行时 prose_binding 和 OperationSpec 为准；回执就绪是读取时快照，不是取消提交重验的许可。

## 验证责任

当前保护必须由当前格式测试验证：首次与再次定稿、错误目标、外部编辑、空槽竞争、身份改变、精确能力集合、规范位置冲突、损坏隔离、过期 token、单调撤销、异常回滚和中断恢复。测试不再通过旧格式夹具绕开这些边界。完整门禁与主机验收分别报告，不把局部测试或历史 R1 结果升级为当前版本完成证明。
