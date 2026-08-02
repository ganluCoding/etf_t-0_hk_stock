# 项目活动与验证记录

本文件记录已经合并到 `main` 的需求、实现和验证活动。它不记录原始行情、券商交割单、账户信息或本机生成的大型数据；这些数据按 DVC 与本地数据规则保留在 Git 外。

## 当前基线

- 仓库：`ganluCoding/etf_t-0_hk_stock`
- 功能与文档基线：`dae0bb7`（2026-08-02，PR #38）；后续合并提交按本表继续追加。
- 当前产品状态：研究与人工决策支持；**不是**自动交易程序，也不是实盘策略准入。
- 当前实盘准入：No-Go。G0、G2、G3 及后续验证门未通过。
- 本机生成的最新 OHLC 回本账本：16 个已确认研究标的、1 万/3 万资金档、3 个网格间距和 2 个费用情景共 192 行；可进入回测/执行的行数为 0。该结果是缺少费用与可执行盘口证据时的预期 fail-closed 行为。

## 已合并活动

| 时间（Asia/Shanghai） | 范围 | 结果 | 验证/记录 |
| --- | --- | --- | --- |
| 2026-07-26 | M1 单标的桌面观察固定夹具 | 目标 ETF 单代码纸面观察原型；不连接券商 | PR #27，Issue #26 |
| 2026-07-26 | M2 当前纸面观察 | 本地目标快照、因果数据门禁、策略 lineage、失效清空与审计日志 | PR #29，Issue #28 |
| 2026-07-28 | M3 多 ETF 本地工作台 | 16 标的能力列表、SQLite 标准化行情与趋势区间研究 | [PR #32](https://github.com/ganluCoding/etf_t-0_hk_stock/pull/32)，Issue #30 |
| 2026-07-28 | M3 可用性修复 | 左侧显示代码/名称；详情页趋势、数据与说明分区 | [PR #34](https://github.com/ganluCoding/etf_t-0_hk_stock/pull/34)，Issue #33 |
| 2026-08-01 | 回本账本与执行证据门禁 | 合法整手/现金检查、费用拆分、网格成本拒绝、总资金拆网格成本压力、三层执行证据、SQLite 人工纸面记录、不可变本地报告 | [PR #35](https://github.com/ganluCoding/etf_t-0_hk_stock/pull/35)，Issue #31；CI `test` 成功；本地 155 项测试和 `ruff` 通过 |
| 2026-08-02 | M3 可靠性与执行证据方案 | SQLite 真只读、台账身份 fail-closed、最新日 x/16 覆盖、无数据/刷新/最小窗口修复，独立行情与招商盘口留证设计 | [PR #38](https://github.com/ganluCoding/etf_t-0_hk_stock/pull/38)，Issue #37；UX/数据/PRD 审计无 P0/P1；160 项测试与 `ruff` 通过 |

## 当前未完成事项

| 优先级 | 事项 | 当前限制 | 跟踪 |
| --- | --- | --- | --- |
| P0 | 券商费用账单校准 | 仅有 159567 的用户报告最低佣金下限；佣金比例、经手费包含关系、部分成交收费及跨标的适用范围未校准 | [Issue #7](https://github.com/ganluCoding/etf_t-0_hk_stock/issues/7)（需要用户材料） |
| P0 | 前向采集与首日/持续质量验收 | 免费数据仍是 `UNVERIFIED RESEARCH FEED`；需要按版本化日历和数据质量规则积累证据 | [Issue #21](https://github.com/ganluCoding/etf_t-0_hk_stock/issues/21) |
| P1 | 交易日及跨市场错配标记 | 需要完成/复核正常重合日和错位日的可用数据验收 | [Issue #5](https://github.com/ganluCoding/etf_t-0_hk_stock/issues/5) |
| P1 | 保守策略仿真与准入 | 只能在 G0.5 费用/执行证据条件满足后推进；30 日样本不可作为盈利证明 | [Issue #9](https://github.com/ganluCoding/etf_t-0_hk_stock/issues/9) |
| P1 | 独立行情与招商盘口留证 | 三源证据设计已由 Issue #37 / PR #38 完成；待用户确认数据权限/成本、完成券商首日探针，再实施 adapter、3日跨源核验与20日纸面执行 | [Issue #39](https://github.com/ganluCoding/etf_t-0_hk_stock/issues/39) |

## 交接与变更规则

1. 新开发从 `agent/<scope>` 分支开始，必须引用 GitHub Issue，使用 PR 合入 `main`。
2. 每个 PR 合并前运行 `ruff check src tests` 与 `pytest -q`；每次合并和每五条用户对话调用 `prd_consistency_audit`。
3. 原始行情、缓存 API 载荷、SQLite 工作库、券商材料和生成报告不得提交；使用 DVC 指针与本机文件哈希保留数据 lineage。
4. 不得添加券商凭据、下单、撤单、自动交易或收益承诺。
5. 任何“可执行”“可回测放行”结论必须同时满足 PRD G0.5 与相关 G0–G8 门禁；OHLC 成本屏幕和 30 日样本均不构成实盘证据。
