# GitHub 维护清单

本页面向仓库管理员。文件合并不会自动开启分支保护或私密漏洞报告；完成下面的网页设置后，GitHub 才会强制拦截不符合要求的合并。CLA 仍是**草稿，需律师审阅**，不要把机器人通过当成法律审阅或权属核验。

## 先了解检查怎样运行

| 检查 | 做什么 | 权限与数据 |
| --- | --- | --- |
| `macos-ci` | 在 `macos-15`、Python 3.11 上执行公开发布范围检查、贡献流程回归和完整 `local_ci.py` | 只读仓库权限，不使用个人 Provider、审核词表或凭据；报告在 runner 临时目录 |
| `license/cla` | 核对当前 PR 的签署结果，再把结果写到该提交 | 独立签署工作流；只检出可信的基准分支提交，不执行外部 PR 代码 |
| 维护者审查 | 核对来源、实际作者、核心语义、公开内容与法律材料 | 不能由自动检查替代 |

必须检查的是 **`license/cla` 提交状态**，不是名为 `cla` 的 Actions 作业。评论触发的签署会更新同一个 PR 提交的状态，不需要个人访问令牌或重新运行之前失败的作业。`macos-ci` 使用 `pull_request` 检查合并结果；`CLA` 使用 `pull_request_target` 和新评论事件操作签署记录，两者不能混用权限。

## 1. 检查 Actions 是否能运行

1. 打开仓库，点击 **Settings → Actions → General**。
2. 在 **Actions permissions** 中允许本项目引用的 Actions。若限制到指定作者，需要允许 `actions/*` 和 `manaflow-ai/cla-github-action`。
3. 在 **Workflow permissions** 保持默认只读即可；CLA 作业在文件中单独申请 `contents`、`issues`、`pull-requests` 和 `statuses` 的写权限。不要开启“Allow GitHub Actions to create and approve pull requests”。组织级策略若禁止这些权限，需要管理员处理。
4. 外部 Fork 的工作流审批建议选择 **Require approval for all outside collaborators**。批准前看 PR 改动，尤其是工作流和脚本；不要把私密 Secret 交给 PR 测试。
5. 点击顶部 **Actions → CI**，查看一次实际运行。也可点 **Run workflow → main → Run workflow**，等待 `macos-ci` 完成。

CLA 工作流首次出现在主分支前，首次安装 PR 可能没有 `license/cla`。先核对本次是权利人自己的安装变更；合并后在后续 PR 观察 CLA 运行。没有实际出现的检查名称可能无法在分支保护搜索框中选中。

## 2. 保护 main 分支

1. 点击 **Settings → Branches → Add classic branch protection rule**。如果页面引导使用规则集，选择 **Rules → Rulesets → New branch ruleset**，目标限定 `main`，效果相同。
2. 分支名称填 `main`。
3. 开启 **Require a pull request before merging**。
4. 开启 **Require approvals**，至少 1 人；开启 **Require review from Code Owners**。本仓库的 `.github/CODEOWNERS` 指向 `@wuren194`。
5. 开启 **Dismiss stale pull request approvals when new commits are pushed** 和 **Require conversation resolution before merging**。
6. 开启 **Require status checks to pass before merging**，添加 **`macos-ci`** 和 **`license/cla`**。如果可以选择来源，限定为 **GitHub Actions**。
7. 开启 **Require branches to be up to date before merging**。这样主分支或协议更新后，旧 PR 需要更新并重新检查。
8. 保持 **Allow force pushes**、**Allow deletions** 关闭，保存规则。

**只有你一位维护者时需要决定一件事：**GitHub 不允许给自己的 PR 做批准审查。外部贡献可以由你审查；你自己的 PR 要么由另一位可信维护者审查，要么通过明确配置的管理员例外处理。不要为了方便外部投稿而关闭全仓库的审查要求。如果有第二位维护者，再考虑启用 **Do not allow bypassing the above settings**；如果保留管理员例外，自有 PR 也应人工核对两项检查后再合并。

不要把 `cla-signatures` 分支纳入必须走 PR 的 `main` 规则；签署 Action 需要直接追加记录。能写这条分支的人可以改变签署账本，因此不要向未经信任的人授予仓库写权限。若以后启用“所有分支”规则，应为签署自动化单独配置最小例外，不要开放给普通贡献者。

## 3. 开启私密漏洞报告

1. 点击 **Settings → Security → Code security**；某些界面显示为 **Advanced Security**。
2. 找到 **Private vulnerability reporting**，点击 **Enable**。
3. 返回仓库顶部 **Security → Advisories**，确认出现 **Report a vulnerability** 入口。
4. 按需要检查 Secret scanning 和 Push protection 是否开启；这些设置不能代替人工检查私人资料。

如果组织策略或仓库可见性使选项不可用，先核对 GitHub 的[私密漏洞报告说明](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)。不要用公开 issue 接收未披露的漏洞细节。

## 4. 查看、维护签署记录

1. 打开 **Code**，点击左上角分支选择器。
2. 选择 **cla-signatures**。该分支在首次有效检查时自动创建，与 `main` 共用初始历史，不需要单独建仓库。
3. 打开 `signatures/`，再进入对应协议 SHA-256 摘要目录，查看 `cla.json`。
4. 记录中的账号 ID、评论 ID、时间和 PR 编号用于核对签署；机器人的协议链接指向固定 Git 提交，可找回当时原文。不要手填别人的签名或把记录合并回 `main`。

维护者自己的 PR 仅在发起人数字 ID 是 `143167886`（`wuren194`）时豁免该账户；其他 PR 没有这个白名单，其他作者仍须签署。没有通配机器人白名单。

当前上游自动要求 PR 发起人和主提交作者签署，**不对仅列在 `Co-authored-by` 中的共同作者提出签署要求**。审查时核对实际贡献者，要求每人有自己的实际贡献提交并进入自动检查。机器人、雇主拥有的代码、代投补丁和第三方移植都需核对来源，不能仅凭账号匹配认定权属。

## 5. 更新 CLA 或签署工具

协议内容的任何修改都会产生新摘要、新签署句和新的记录目录。保留旧记录，不把旧签署批量复制到新版本。律师确认后的正式文本也需要重新签署。合并协议更新后，让尚未合并的贡献更新到最新 `main`，重新查看机器人提示。

2026-11-10 核实的工具状态：

- 原示例 [contributor-assistant/github-action](https://github.com/contributor-assistant/github-action/commit/58daaf842ac3e26d08c2bdf26770d6e9c5b965a9) 已在 2026-03-23 明确停止维护并归档，因此未采用。
- 采用 [manaflow-ai/cla-github-action](https://github.com/manaflow-ai/cla-github-action)，未归档；所选默认分支提交 [44731e8](https://github.com/manaflow-ai/cla-github-action/commit/44731e8bc2120b726d651a1c1d90721411c75eb7) 发布于 2026-09-02，包含签署评论换行修复。它由 Manaflow 为自有仓库维护，不是 GitHub 官方服务，也没有面向本项目的支持承诺。
- 所有 Action 固定到完整提交 SHA。升级时检查源码、运行时和权限变化，再更新 SHA；不自动跟随浮动标签。

本项目补充了协议内容摘要、固定文档链接和 `license/cla` 状态发布。只有签署 Action 成功且明确输出 `cla_passed=true`，并再次确认 PR 提交和基准提交未变化，才发布成功。API 错误、未签署或旧提交结果不能通过。签署过程中发生推送或主分支更新时，等待新检查或评论 `recheck`。首次创建一个版本的空记录后，如果已有签署尚未登记，也可评论 `recheck`。

这是 GitHub API 上的检查流程，不是跨多个 API 的原子事务；维护者仍要看最终 PR、实际贡献者和两个检查结果。并发队列可能替换尚未开始的旧任务，遇到未刷新时重新评论即可。

## 6. 发布前检查和律师审阅

公开 CI 不接触本地审核词表。维护者在 macOS 跑完整本地门禁，并用自己的仓库外词表检查候选源码、发行包与要推送的历史。以下为命令格式，示例路径需替换成自己的仓库外文件：

```bash
python3 scripts/release_audit.py --terms /path/outside/review-terms.json
python3 scripts/release_audit.py --terms /path/outside/review-terms.json --history
python3 scripts/release_audit.py --terms /path/outside/review-terms.json --zip /path/outside/source.zip
```

词表为非空 JSON 字符串数组。词面扫描和公开目录检查不能证明所有内容合规，也不能确认第三方版权来源；报告不提交、不作为公开附件。

请律师确认：权利人的法律主体身份、额外授权和商业再许可范围、既有贡献是否需要补授权、雇主授权与专利条款、适用法律和争议处理、电子签署及公开记录的个人信息安排。当前没有替你确定这些法律事项。网页保护设置、第二位审查人和实际接受外部贡献的时点，由维护者决定。
