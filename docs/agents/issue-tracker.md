# Issue tracker: GitHub

本仓库的需求、研究任务、风险项和验收记录均使用 GitHub Issues 管理。

- 仓库：`ganluCoding/etf_t-0_hk_stock`
- 创建：`gh issue create --title "..." --body "..."`
- 阅读：`gh issue view <number> --comments`
- 列表：`gh issue list --state open`
- 标签：`gh issue edit <number> --add-label "..."`
- 关闭：`gh issue close <number> --comment "..."`

PRD 及重要决策先保存在 Git 中，再用 Issue 追踪其实施、讨论和验收。提交和 Pull Request 必须引用相关 Issue 编号。

## main 合入规则

自 2026-07-25 起，公开仓库的 `main` 已启用 GitHub 技术保护（Issue #2 已完成）：

- 必须经 Pull Request 合入，且 `test` CI 在当前 main 基线通过。
- 必须解决 PR 对话；规则同样对管理员生效。
- 禁止 force push 和删除 main。

开发从 `agent/<scope>` 功能分支开始；创建 Draft PR，完成检查与审计后转为 ready for review。合并由拥有相应 GitHub 权限的人在规则满足后执行。
