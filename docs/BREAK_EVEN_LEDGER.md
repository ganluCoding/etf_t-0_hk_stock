# 回本账本（Break-even Ledger）

回本账本是所有策略回测之前的成本门禁。它逐行计算：

`目标 ETF × 单笔金额 × 网格间距 × 费用情景 × 执行证据层级`

它回答的只是：在合法 100 份整手、指定价格和已声明成本下，拟定网格能否至少覆盖一次完整买卖的现金流。它不预测价格，不模拟账户，也不证明限价单成交。

## 当前运行方式

在项目根目录运行：

```bash
PYTHONPATH=src .venv/bin/python -m etf_t0.break_even_report \
  --order-amounts-cny 10000,30000 \
  --grid-spacings-bps 10,20,50,100,200
```

结果保存到本机（默认）：

`reports/generated/break_even_ledger/<run_id>.json`

该目录被 Git 忽略。每次默认运行生成新的不可变 run ID 文件，而不覆盖上一份；报告同时保存当前 Git revision、标的池配置哈希、输入分钟文件哈希、费用情景完整快照和输入参数。报告读取每只标的本机保存的最后一个原生 5 分钟收盘价，且每一行强制为 `OHLC_CONSERVATIVE` / `NO_EXECUTION_CLAIM`；它不是实时盘口，也不能作为下单价格。

## 每行的字段和门禁

- `quantity`：买入费用后仍可用指定金额支付的最大 100 份整数数量。
- `unused_cash_cny`：支付入场名义金额和买入端声明成本后未使用的现金。
- `buy_charged_commission_cny` / `sell_charged_commission_cny`：双边实际计提的佣金，可能高于最低收费；`declared_minimum_commission_per_side_cny` 单列费用表所声明的最低收费，绝不将两者混淆或合并为一笔。
- `other_explicit_cost_cny`：经手费、印花税和过户费等显性项目；已含在佣金的费用不会重复计入。
- `spread_cost_cny`、`slippage_cost_cny`、`queue_partial_fill_haircut_cny`：分别显示的经济成本。未获得证据时为零只表示下限情景，不表示真实成本为零。
- `minimum_round_trip_price_delta_cny`、`minimum_round_trip_ticks`、`minimum_round_trip_bps`：覆盖全部已声明成本的最小合法价格移动。
- `proposed_grid_price_delta_cny`：将输入 bp 向上对齐到 0.001 的实际价格间距。
- `total_capital_cny`、`planned_round_trip_count` 与 `aggregate_declared_cost_bps_of_total_capital`：当把总资金拆成多笔独立往返时，显示累计声明成本相对总资金的 bp。示例：总资金 ¥10,000、每笔 ¥2,000、计划 5 次独立往返，在仅双边最低 ¥10 的下限下累计费用就是 ¥50 / 50 bp；实际值仍会随整手、价差和滑点变化。

`RESEARCH_BLOCKED_LOT` 表示单笔金额买不起一手；`RESEARCH_BLOCKED_COST` 表示拟定网格小于完整成本阈值。两者均不得进入策略回测或参数比较。

`COST_FLOOR_COVERED` 仅表示声明的算术下限被覆盖，不表示可成交或可盈利。`LOWER_BOUND_ONLY` 表示当前缺少执行成本证据；它不会进入策略回测。

## 证据层级

| 层级 | 可做什么 | 不能做什么 |
| --- | --- | --- |
| `OHLC_CONSERVATIVE` | 因果、最不利路径的粗筛 | 声称盘口、限价成交或盈利能力 |
| `QUOTE_AWARE` | 仅在目标 ETF 的同时 bid/ask 与一档数量、来源、带时区时间戳与服务时钟校验出的 ≤120 秒时效、深度、明确滑点和排队折损均合格时，讨论成本/限价假设；主动买入以 ask 作为入场现金价格，目标退出以 bid 计价 | 凭公开或缺深度报价推断券商成交，或把 ask 入场再重复加一遍同一盘口价差 |
| `PAPER_EXECUTION` | 记录人工观察的可下单价格、成交、部分成交、撤单和未成交 | 连接券商或替代正式样本外验证 |

纸面执行需要至少 20 个有效正常重合交易日。它只是 G7 的执行可行性数据，不能取代 G5 的冻结 60 日留出集、100 次完整往返和 40 个活跃日。

每条纸面执行记录保存在本机 SQLite 研究库中，字段包括目标代码、带时区时间、正常重合日标记、意图方向/价格/数量、当时 bid/ask 与来源、费用证据、完整/部分/未成交/撤单结果、实际成交量/价格和原因。计数时还必须匹配版本化的正常重合日清单，并落在 09:30–11:30 或 13:00–15:00 的连续竞价窗口；未成交、部分成交与撤单不会被删除或以“成交”替代；系统不连接券商。

## 当前费用边界

用户已报告招商 A 股账户交易 **159567** 的已知下限：每次成交买、卖分别最低 ¥5，无印花税、无过户费。因此报告同时运行 `cmb_user_reported_minimum_all_in_assumption` 和 `cmb_user_reported_minimum_plus_handling_assumption` 两个边界：前者假定经手费已含，后者将经手费另计，避免只输出较低成本情景。

佣金比例、经手费是否含在全佣、部分成交收费，以及该费率对其他 ETF 的适用性尚未经交割单校准。因此账本会保留 `WAIT_FEE_EVIDENCE`；对 159567 之外的标的还会保留 `WAIT_FEE_SCOPE`。这些状态阻断实际成本和实盘准入结论，但不会掩盖一个本就无法覆盖最低成本的参数组合。
