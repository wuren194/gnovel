# 本地 CI 与支持策略（LOCAL-CI）

Govel 的完整工程门禁可在 macOS 或 Linux 上执行。本文件说明验收口径；对应平台是否通过，以该环境实际生成的完整报告为准。

## 支持策略

| 维度 | 定位 |
| --- | --- |
| 运行平台 | macOS / Linux |
| Python | 3.11 或更新版本；报告记录实际路径与版本 |
| Linux 文件系统 | 需要 `renameat2(RENAME_NOREPLACE)`；内核或文件系统不支持时拒绝发布 |
| 原生 Windows | 暂不支持；启动时在导入 POSIX 模块前给出说明 |
| GitHub | 源码托管与评审 |
| 外部 CI | 可补充工程证据，不替代本地门禁 |

平台范围不改变稳定身份、事务保全、只读边界、证据绑定和数据完整性要求。更新 Python 或运行环境后，用新环境执行完整门禁，再据实际结果更新支持说明。

Linux 分派的模拟测试不能替代 Linux 内核运行证据。Ubuntu 的实际运行记录见下表。WSL2 尚未单独验收，需在实际项目使用的文件系统上验证；原生 Windows 的文件锁、目录句柄和发布协议尚未实现。外改确认窗口需桌面会话，Linux 另需 `zenity`；工程测试模拟确认结果，不证明真人已完成弹窗确认。

## 已验证的平台基线

2026-11-11，提交 [`1661a5b`](https://github.com/wuren194/govel/commit/1661a5b18128af2273603c0f6ca2c8a4384841d8) 在 [CI 运行 42900393681](https://github.com/wuren194/govel/actions/runs/42900393681) 中通过两端完整 `local_ci.py` 门禁及公开发布内容检查：

| 环境 | Python | 测试结果 |
| --- | --- | --- |
| macOS 15.8.2，ARM64 | 3.11.9 | 2,954 项，0 失败、0 错误、0 跳过 |
| Ubuntu 24.04，x86_64，glibc 2.39 | 3.11.18 | 2,954 项，0 失败、0 错误、4 项跳过 |

Linux 跳过项为 3 项依赖 macOS 整路径 `O_NOFOLLOW_ANY` 的测试，以及 1 项要求文件系统原生 CoW 克隆的测试；该 runner 的文件系统不支持后者。Linux 独占发布、普通副本回退和事务测试在同次完整套件中执行。此记录覆盖上述环境和提交，不代表所有 Linux 文件系统、WSL2、原生 CoW 或真人桌面确认均已验收。后续提交继续以各自的 `macos-ci`、`linux-ci` 结果为准。

## 运行时记录

每次报告的 environment 记录平台、架构、Python 路径与版本；Git 信息记录被验证的版本与工作树状态。本说明不把某台机器过去通过的结果当作当前环境的通过证明。

## 本地 merge gate

仓库内唯一正式入口：

```bash
python3 scripts/local_ci.py
```

无第三方依赖，只用 Python 标准库、现有项目脚本与 git；无 Docker、无 Linux/GNU-only 工具依赖。
它是仓库代码验证，不是项目迁移命令：对真实小说、知识库、项目缓存、收据、manifest、源码一律
**只读**，临时产物写入 `TemporaryDirectory`，运行前后逐字节比对工作树与 Git 身份以自证未改动仓库。

常用形态：

```bash
python3 scripts/local_ci.py                                          # 完整 merge gate
python3 scripts/local_ci.py --focus tests.test_authoring_submission  # 追加专项回归
python3 scripts/local_ci.py --json                                   # 附带机器可读报告到 stdout
python3 scripts/local_ci.py --report-out /tmp/local-ci-report.json   # 指定报告路径
python3 scripts/local_ci.py --jobs 1                                # 同一发现清单串行运行
python3 scripts/local_ci.py --jobs 2 --timeout 600                   # 显式并发和每模块超时
```

默认工作进程数取逻辑 CPU 核数与 `shared/registries/local-ci.json` 的
`defaultMaxWorkers` 的较小值。完整与快速模式
共用 `scripts/local_ci.py` 的 `test_worker_count()`，经 `project.local_ci_policy()`
读取唯一配置。`--jobs` 可显式覆盖默认值，需要隔离的模块仍按串行通道执行。
这个上限控制同时运行的测试模块数，不是 CPU 百分比限额；单个测试及其子进程仍可能占用多个核心。

## 开发迭代的快速入口

已知本次涉及哪些模块时直接指定，多个模块重复 `--focus`；输出只代表选定范围：

```bash
python3 scripts/local_ci.py --quick --focus tests.test_source_plan --focus tests.test_source_proposal_batch
```

也可先查看 Git 改动会选到哪些测试，再运行：

```bash
python3 scripts/local_ci.py --quick --plan
python3 scripts/local_ci.py --quick
```

默认检查相对 HEAD 的 staged、unstaged、删除／重命名两侧及 untracked 文件。Python 改动匹配测试的直接 import、经测试辅助模块的导入链及同名测试；docs、契约等文档改动选对应文档校验。报告的 selection 给出每个模块的选择原因。它不分析生产模块之间的完整依赖，也不证明动态导入和运行时数据的覆盖；共享基础代码收尾必须全量验证。

存在配置、前端资源、知识资产、删除测试等无法映射的改动时，返回 `NEEDS_FULL`（exit 2）且不启动测试。根据本次实现明确指定 `--focus` 可以继续迭代；指定范围与工作树全部改动不是一回事，报告会标记 explicit。未知或零用例模块报失败；模块去重，不重复执行。没有相对 HEAD 的改动返回 `NO_CHANGES`（exit 0），不运行测试，也不核验历史门禁。

快速执行复用全门的模块进程、串行隔离、测试数量和 ID 指纹检查，并执行空白检查、工作树字节指纹和 Git 身份检查。失败或测试改动仓库均 exit 1；通过输出 `QUICK_PASS`（exit 0）。`--plan` 只做发现和选择，输出 `QUICK_PLAN`，不表示测试通过。报告使用 `kind=local_ci_quick_report`，`mergeGate={authoritative:false,result:NOT_RUN}`，不会产生完整门禁 PASS。`--report-out`、`--jobs`、`--timeout`、`--json` 沿用原参数和报告路径规则。

## 必跑检查

开发迭代先跑直接涉及的测试模块，等代码和文档收齐后再跑一次完整门禁。例如修改源计划和批量提案时：

```bash
python3 scripts/local_ci.py --quick --focus tests.test_source_plan --focus tests.test_source_proposal_batch
```

专项通过只证明对应范围；最终仍以完整 `local_ci.py` 为准。全门通过后，没有新修改、失败或未解决风险就不重复执行。`--focus` 是全门之外追加专项，不能用来缩短迭代等待。

核心命题：**进程成功不等于证据有效**。`exit code == 0` 只是必要条件；每个门都必须把工具
真实产出的证据（unittest 摘要、JSON 报告、Git 快照）验到能证明「验证真的发生过」。

| Gate | 内容 | 通过条件 |
| --- | --- | --- |
| 1 完整单元测试 | discover 后按模块分进程运行，默认受 local-ci 注册表上限约束 | 全部进程成功，摘要与模块报告一致，模块集合、测试数与 ID 指纹均匹配发现清单，失败／错误／意外成功为零 |
| 2 包装同步 | `scripts/generate_wrappers.py --check --json` | exit 0 **且** stdout 可解析、`ok` 为布尔真、`drift` 为空数组 |
| 3 检查器基线 | `scripts/evaluate_checkers.py --baseline ... --json --output <temp>` | exit 0 **且** 输出文件存在（非 symlink）、JSON 可解析、`passed=true`、`baselineComparison.regressed=false` 且 `comparable=true` |
| 4 空白 / 冲突标记 | `git diff --check`、`git diff --cached --check` 与 `git show --format= --check HEAD` | 三项均 exit 0，分别展示工作树、暂存区与 HEAD 结果 |
| 5 工作树完整性 | 运行前后工作树的逐字节指纹比对 | tracked / untracked 的存在性、类型、权限位、内容 hash 全部一致 |
| 6 Git 身份 / index | 运行前后 `HEAD`、分支、index 指纹 | 三者都不变 |
| 7 专项回归（可选） | `--focus <module>` 指定的模块 | 指定时必须 PASS，且同样要求摘要证据闭合 |

完整 merge gate 永远包含 Gate 1 完整套件；`--focus` 只是额外的显式隔离证据，专项模块本就在完整套件内。

### Gate 1 的工作队列与隔离

会变异工具箱定义或知识索引的测试模块通过 `tests/toolbox_isolation.py` 在仓库外独占物理副本中执行。
副本来自实际工作树的 Git 枚举路径，包含 dirty 与 untracked 字节，保留内部软链并拒绝外逃，普通文件
不共享 inode；不使用 HEAD 归档取代待验字节。产品导入、真实 CLI 子进程、cwd、catalog、索引和历史
都指向副本。ignored 知识缓存不从主库搬入，由真实消费者在副本重建；Git 指纹不声称覆盖这些 ignored
路径。副本使用独立空 Git 元数据供发行内容检查枚举，没有提交，也不修改主库 index。

标准 unittest 模块 discovery 与 `local_ci.py` 共用这一隔离入口，逐个保留原测试 ID、数量、失败、错误、
skip 和 expected-failure 结果；身份或执行数缺失均失败。直接按类／方法绕过模块隔离的变异测试在
`setUpModule` 拒绝执行。cleanup 只处理自有临时目录；SIGKILL 可留下副本，不把旧源码快照写回主库。
隔离回归包含真实子进程路径、变异完成屏障后的 SIGKILL、并发作者编辑和 ignored 索引哨兵。

发现阶段使用 unittest 的真实 discovery，记录所有模块（含零用例模块）、各模块测试数及完整测试 ID 的指纹。随后先跑串行通道，等它完全结束，再启动普通模块工作队列。每次领取一个模块，开启新的 Python 进程，保留 discovery 的导入环境但不执行其他模块用例；Python 全局变量、补丁与缓存不会跨模块共享。`--jobs 1` 走同一证据流程，工作池并发度为一。

串行策略真源在 `scripts/core/local_test_runner.py`。哪些已发现模块需要串行，由该文件的当前表及 `serial_reason()` 决定；文档不维护第二份模块清单。公开测试在独立临时数据目录运行，不使用作者配置的 Provider 或个人参考目录。

报告的 `fullTests.workerCount` 记录工作池并发度，`moduleTimings` 按实际耗时降序排列，含每个模块的原始摘要尾部、退出码、发现数量与串行理由。`fullTests.durationSeconds` 是 Gate 1 墙钟耗时，报告顶层 `durationSeconds` 是完整 gate 耗时。

worker 报告保存真实 `testIds`、其摘要和 `result.skipped` 的 `testId＋reason` 名单。父层核验
ID 列表唯一且与 discovery 指纹一致、skip ID 属于该模块、无重复、名单长度等于报告及 unittest 摘要计数。
全量与快速报告按模块列出未执行用例及原因，整模块跳过明确标为未执行；允许 skip 不代表对应功能已验收。
缺名单、未知 ID 或计数不合均按证据无效失败，正常 skip 的允许策略保持不变。

worker 缺摘要、缺模块报告、报告与摘要不一致、测试身份或计数变化、缺模块／重复模块均失败。某个 worker 失败后仍收集其他模块证据，不用重跑或重试把失败抹掉。设置 `--timeout` 时，超时会杀掉并回收该模块拥有的进程组；串行与并行通道都使用同一机制。工作树与 Git 身份门的算法保持原样。

**测试数量不是契约**：不写死任何数字，成功只要求 `testsRun > 0`。`Ran 0 tests` + `OK` 判
`ZERO_TESTS_EXECUTED` 失败；摘要缺失或不可解析判 `TEST_SUMMARY_UNPARSEABLE` 失败——不因进程退出 0
就相信。`skipped` 与 `expectedFailures` 允许存在（如实报告，不自动视为失败）；`failures`、`errors`、
`unexpectedSuccesses` 任一非零即失败。

**已有的脏状态允许，新增的脏状态不允许**。作者运行前就有 `M` / `??` 不构成失败——本地 CI 判定的是
`before snapshot == after snapshot`：它自己有没有改动仓库。工作树指纹记录每个 tracked / untracked
路径的存在性、文件类型、权限位与**当前字节的 sha256**；tracked symlink 只记录链接文本的 hash，
不跟随链接去 hash 外部目标。任何新增路径、移除路径、内容变化、类型变化、权限变化、tracked /
untracked 归属变化都判 `repositoryMutatedByCI=true` 并逐项报出 before/after hash。列为 untracked 的
范围由 Git 枚举决定，不递归 hash `.git`、缓存或巨大目录。

**UNKNOWN 一律按 FAIL 处理**。子进程超时结构化报 `timedOut=true`；命令起不来（`OSError` 族）报
`PROCESS_START_FAILED`；门自身抛异常报 `GATE_EXCEPTION`；快照不可用报 `SNAPSHOT_UNAVAILABLE`。
缺失、`None`、非布尔的 `pass` 都不算成功。`verificationReason` 逐门给出上述原因码。

## 平台判定与退出码

运行开始打印 `LOCAL CI ENVIRONMENT`（平台、架构、python 可执行文件与版本、repo 根、HEAD、分支、
工作树状态、`SUPPORTED PLATFORM CHECK`）。

- macOS 与 Linux 使用同一套完整测试、检查器、工作树及 Git 身份门，不能只通过平台检查就取得 PASS。
- 权威 `LOCAL MERGE GATE PASS` 只在 `platform.system()` 属于运行时支持集合且全部门通过时产生。
- 其他原生平台在入口提前退出，stderr 说明运行要求，exit 2，不加载依赖 POSIX 的业务模块。报告判定函数仍保留 `UNVERIFIED`，防止嵌入调用把不受支持的平台标成 PASS。

退出码契约：

| 结果 | 含义 | exit |
| --- | --- | --- |
| PASS | macOS 或 Linux 且全部门通过（权威） | 0 |
| FAIL | 任一门失败 | 1（非零） |
| UNVERIFIED / 不支持运行时 | 平台不在支持集合，或入口拒绝启动（不构成权威 PASS） | 2（非零） |

终端写 FAIL 而进程 exit 0 的情况不会出现。

## 机器可读报告

每次完整执行产生 `local-ci-report.json`，默认写到临时目录（不自动提交仓库）。`--report-out` 只接受
仓库**外**的**新**文件：仓库内路径（含被忽略的缓存目录）一律拒绝，已有文件一律拒绝覆盖。

报告不把成功压成一个模糊布尔——`processSuccess`（进程是否成功）、`evidenceValid`（证据是否可解析
且形状正确）、`pass`（该门是否通过）三者分开，`verificationReason` 给出原因码：

```json
{
  "kind": "local_ci_report",
  "platformPolicy": "macos-linux-local-authoritative",
  "environment": {}, "git": {},
  "fullTests": {
    "exitCode": 0, "processSuccess": true, "evidenceValid": true, "pass": true,
    "verificationReason": "PASS",
    "summary": {"testsRun": 1492, "durationSeconds": 12.3, "failures": 0, "errors": 0,
                "skipped": 1, "expectedFailures": 0, "unexpectedSuccesses": 0, "parsed": true}
  },
  "taskSpecificTests": {}, "wrapperSync": {}, "checkerBaseline": {},
  "whitespace": {},
  "workingTreeIntegrity": {
    "repositoryMutatedByCI": false, "trackedChanged": [], "untrackedChanged": [],
    "pathsAdded": [], "pathsRemoved": [], "pathsContentChanged": [], "changes": [],
    "before": {"head": "...", "branch": "...", "indexSha256": "...",
               "trackedPaths": 1187, "trackedFingerprint": "..."},
    "after": {}, "exclusions": []
  },
  "gitIdentityIntegrity": {"headUnchanged": true, "branchUnchanged": true, "indexUnchanged": true},
  "mergeGate": {"authoritative": true, "result": "PASS"},
  "overall": "PASS"
}
```

`{"processExitCode": 0, "evidenceValid": false, "pass": false}` 这类「进程成功但证据无效」的状态是
可表达的，也不会被当成 PASS。

## 外部 CI

GitHub Actions 的 `macos-ci` 在 `macos-15`、`linux-ci` 在 `ubuntu-24.04` 上使用 Python 3.11 执行 `python3 scripts/local_ci.py`，两者均运行公开发布内容检查；macOS 作业另运行贡献流程脚本回归。工作流使用只读仓库权限，不加载个人 Provider 或审核词表；报告写入 runner 的仓库外临时目录，不作为源码或附件提交。查看具体提交对应的检查结果，已完成的初始平台验证见上方记录。

外部 CI 补充公开干净环境的证据，不替代本机门禁。基础设施不可用与代码测试失败分别报告，远程通过不等于作者本机已验证。签署流程与工程 CI 分开，GitHub 分支保护的配置见 [维护清单](GITHUB-MAINTAINER.md)。
