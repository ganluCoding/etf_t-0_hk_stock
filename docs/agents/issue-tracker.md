# Issue tracker: GitHub

本仓库的需求、研究任务、风险项和验收记录均使用 GitHub Issues 管理。

- 仓库：`ganluCoding/etf_t-0_hk_stock`
- 创建：`gh issue create --title "..." --body "..."`
- 阅读：`gh issue view <number> --comments`
- 列表：`gh issue list --state open`
- 标签：`gh issue edit <number> --add-label "..."`
- 关闭：`gh issue close <number> --comment "..."`

PRD 及重要决策先保存在 Git 中，再用 Issue 追踪其实施、讨论和验收。提交和 Pull Request 必须引用相关 Issue 编号。

## 当前私有仓库的合入补偿控制

本仓库当前 GitHub 套餐不能对私有仓库启用分支保护规则（见 Issue #2）。在升级套餐或调整仓库可见性前，所有开发必须在功能分支完成、创建 Draft Pull Request、等待 `test` CI 通过，并仅由仓库所有者人工合入 `main`。不得直接推送 `main`。
