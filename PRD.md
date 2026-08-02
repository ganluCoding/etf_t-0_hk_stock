# 港股 ETF T+0 日内网格研究系统 PRD

- 文档状态：Active v1.2
- 更新日期：2026-08-02
- 项目阶段：M1–M3 本地研究工作台与 G0.5 回本账本均已实现。当前公开网络数据固定标记为 `UNVERIFIED RESEARCH FEED`；G0 费用账单校准、G2 跨源/日历和 G3 券商可执行性/深度未通过，因此实盘准入始终为 No-Go。只有当前快照、连续因果 L48、资格、策略 lineage 与保守成本门禁全部通过时，才可显示带水印的纸面观察价
- 目标用户：使用境内 A 股账户、人工完成买卖操作的个人投资者

## 变更记录

| 日期 | 版本 | 变更 | 可追溯记录 |
| --- | --- | --- | --- |
| 2026-07-28 | v0.9 | 合并 16 只 T+0 ETF 的 SQLite 本地研究工作台与收盘趋势研究 | PR #32 / Issue #30 |
| 2026-07-28 | v0.9 | 修复工作台的 ETF 代码/名称可见性与数据分区 | PR #34 / Issue #33 |
| 2026-08-01 | v1.0 | 增加 G0.5 回本账本、三层执行证据边界与 20 日人工纸面执行要求 | PR #35 / Issue #31 |
| 2026-08-01 | v1.1 | 固化交接基线、当前阶段门状态与版本化项目活动记录 | `docs/PROJECT_ACTIVITY.md` |
| 2026-08-02 | v1.2 | 增加桌面只读/最新完整日验收与独立行情、招商盘口留证方案 | PR #38 / Issue #37 |

完整的合并、验证和运行活动见 `docs/PROJECT_ACTIVITY.md`；原始行情和生成报告继续保留在本机，不写入 Git。

## Problem Statement

用户希望通过境内 A 股账户，研究并人工交易支持当日回转交易的港股或港股通 ETF，例如 159567。初步设想是围绕一个可解释的价格锚点，在日内价格波动中分批买入和卖出，以较小价差反复获取收益。

当前存在以下问题：

1. 不能仅凭基金名称判断一只 ETF 是否支持当日回转交易，需要逐只保存交易所的正式证据。
2. “锚定价格来回套利”并非无风险套利，而是带有方向敞口、隔夜风险和库存风险的日内均值回归交易。
3. 用户计划投入 10,000 元，券商佣金可能存在买卖双边最低收费。资金拆成多个小网格后，最低佣金可能高于策略可捕获的波动。
4. 买卖价差、滑点、排队、部分成交、折溢价变化和未平仓库存浮亏可能显著高于账面佣金。
5. 免费数据可能无法同时满足最近 30 个交易日、1 分钟历史、历史盘口和历史 IOPV 的要求。
6. 5 分钟 OHLC 数据不能证明限价单真实成交，也无法确定同一根 K 线内价格触及买卖网格的先后顺序。
7. 最近 30 个交易日只覆盖很短的市场状态，不能用于证明策略长期具有正期望。
8. 内地、香港、港股通和 ETF 申购赎回日历并不完全一致。错位交易日的定价和折溢价行为不能与正常重合交易日混合分析。
9. 用户将自行操作买卖，因此系统应服务于研究、数据分析和决策支持，不应自动生成或提交真实订单。
10. 用户只计划观察和交易输入的目标 ETF，不应被要求同时理解、盯盘或交易后台使用的主题、指数或价格代理。

项目需要建立一条可审计、可复现、成本完整且对成交持保守假设的研究流程，先回答“哪些 ETF 值得研究、数据是否可靠、波动能否覆盖真实成本”，再回答“采用何种锚点和网格参数”。

## Solution

建设一个面向人工交易的港股 ETF T+0 研究系统，按以下顺序推进：

1. 建立支持当日回转交易的港股及跨境 ETF 证据台账。
2. 以 159567 验证单标的采集方法，再扩展到有交易所证据的多标的首批研究池；159567 不被预设为最终交易标的。
3. 默认采用免费数据路径，先取得最近 30 个完整交易日的原生 5 分钟 OHLCV，并实测免费 1 分钟数据的可回溯范围。
4. 若免费 1 分钟历史不足，则从项目启动日起持续积累 1 分钟数据和实时盘口快照；不得将 5 分钟数据伪造上采样为 1 分钟数据。
5. 显式对齐内地、香港、港股通及基金申赎日历，区分正常重合日与错位交易日。
6. 建立由真实交割单驱动的费用模型。在券商费率未确认前，采用买入 5 元、卖出 5 元的保守占位假设，并避免重复计算已经包含在全佣中的经手费。
7. 将“检查频率”和“价格网格间距”建模为两个独立概念。
8. 先进行 30 日数据质量和描述性波动分析，不在该样本上宣称盈利能力。
9. 在获得更长历史和足够成交信息后，研究 IOPV、同指数代理价差、滚动 VWAP、EMA 等候选锚点。
10. 使用保守的成交模拟、完整的现金与持仓约束以及账户总权益核算进行回测。
11. 通过样本外验证、成本压力测试和模拟盘后，才允许以最小交易单位进行受控人工实盘验证。
12. 提供本机桌面观察应用：首页展示首批16只研究标的及其数据能力，用户进入任一目标 ETF 后只观察该目标；应用读取已保存的本地费用与资金档案，输出趋势、数据质量、观察买入上限、观察卖出下限、有效期、成本覆盖和 No-Go 原因。
13. 建立本机 SQLite 研究数据库保存标准化行情、数据质量、采集运行和可复现的趋势区间；原始供应商载荷继续保留为本机不可变文件及 DVC 指针。
14. 收盘后从原生分钟数据识别连续上涨区间，并将“价格趋势”与“已由 bid/ask 和成本验证的可执行空间”明确分开显示。
15. 桌面先验证 SQLite 标的集合与版本化 16 只台账完全一致，再默认打开最新至少一只 ETF 具有完整原生数据的交易日，显示截止日、覆盖度 x/16 与只读刷新结果，并区分“识别未运行”与“运行后无区间”。标的身份缺失、多余或错配时 fail-closed，禁用最新日导航并阻断所有目标的图表/趋势计算；左侧实际身份列表可保留供数据诊断。
16. 将数据库建表/迁移限定在显式 bootstrap/采集路径；桌面查询使用 SQLite `mode=ro`，不得因打开或刷新而修改研究数据库。
17. 建立当前公开源、独立行情源和招商证券可见盘口三条分离证据链；详细采集、脱敏、对齐和门禁见 `docs/EXECUTION_EVIDENCE_PLAN.md`。

本项目的最终产物不是自动交易程序，而是：

- 可审计的 ETF 标的池。
- 可复用的数据采集和质量检查流程。
- 完整费用及盈亏平衡计算。
- 描述性波动与可交易性报告。
- 具有明确适用条件和停手机制的研究策略。
- 供用户人工判断和下单使用的研究结论。
- 只聚焦目标 ETF、明确区分纸面观察与实盘准入的本机桌面界面。

## User Stories

1. As an A 股账户投资者, I want to see which港股 ETF 有交易所当日回转证据, so that I do not mistakenly trade a T+1 product as T+0.
2. As an A 股账户投资者, I want each ETF record to include the exchange-issued announcement link and review date, so that the T+0 conclusion is auditable.
3. As an A 股账户投资者, I want 159567 to be used as the first benchmark instrument, so that the project starts from a concrete product.
4. As an A 股账户投资者, I want similar T+0 ETFs to be compared with 159567, so that I can choose based on tradability rather than familiarity.
5. As an A 股账户投资者, I want candidate ETFs ranked by liquidity, spread, volatility, fund size and premium/discount risk, so that high apparent volatility is not confused with good execution quality.
6. As an A 股账户投资者, I want the system to record whether an ETF is listed, suspended or at risk of termination, so that stale products are excluded.
7. As an A 股账户投资者, I want the system to distinguish secondary-market T+0 trading from primary-market subscription and redemption, so that unrelated rules and fees are not mixed together.
8. As an A 股账户投资者, I want the latest 30 complete trading days of native 5-minute data, so that I can inspect recent intraday behavior.
9. As an A 股账户投资者, I want the system to test the actual depth of free 1-minute history, so that the data plan reflects what is really available.
10. As an A 股账户投资者, I want unavailable 1-minute history to be marked explicitly, so that 5-minute bars are never presented as genuine 1-minute observations.
11. As an A 股账户投资者, I want future 1-minute data and real-time quote snapshots to be accumulated, so that execution research improves over time.
12. As an A 股账户投资者, I want every dataset to identify source, timezone, units, adjustment method and acquisition time, so that results are reproducible.
13. As an A 股账户投资者, I want duplicate timestamps, missing bars, zero-volume bars and unexplained gaps reported, so that phantom trading opportunities are not created by bad data.
14. As an A 股账户投资者, I want at least three trading days cross-checked against broker quotes, so that the free data source is independently validated.
15. As an A 股账户投资者, I want opening auction, continuous auction, closing auction and after-hours fixed-price records distinguished, so that incompatible trading mechanisms are not merged.
16. As an A 股账户投资者, I want after-hours fixed-price trading excluded from the initial strategy analysis, so that the first model has a clear market-session boundary.
17. As an A 股账户投资者, I want Chinese, Hong Kong, Stock Connect and ETF subscription/redemption calendars aligned, so that holiday mismatch risk is visible.
18. As an A 股账户投资者, I want normal overlap days analyzed separately from mismatch days, so that stale underlying prices do not distort mean-reversion estimates.
19. As an A 股账户投资者, I want my actual ETF fee schedule or anonymized trade statement to replace placeholder fees, so that backtests reflect my account.
20. As an A 股账户投资者, I want every buy and every sell charged independently, so that minimum commissions are not understated.
21. As an A 股账户投资者, I want exchange handling fees identified as included or excluded from broker commission, so that they are not counted twice.
22. As an A 股账户投资者, I want stamp duty, transfer fees and subscription/redemption charges classified correctly, so that irrelevant fees are not added.
23. As an A 股账户投资者, I want spread, slippage, queueing and partial fills included as economic costs, so that nominal price movement is not mistaken for profit.
24. As an A 股账户投资者, I want the break-even movement shown for each proposed order size, so that I understand how minimum commission changes with grid allocation.
25. As an A 股账户投资者, I want decision frequency separated from price grid spacing, so that increasing observation frequency does not silently change the strategy.
26. As an A 股账户投资者, I want candidate anchors ranked by economic interpretability, so that a visually attractive moving average is not automatically treated as fair value.
27. As an A 股账户投资者, I want every anchor to use only information available at signal time, so that the research contains no look-ahead bias.
28. As an A 股账户投资者, I want a signal generated at bar close to execute no earlier than the next available quote, so that the backtest respects causality.
29. As an A 股账户投资者, I want 5-minute bar touches treated conservatively, so that a brief high or low is not assumed to fill my order.
30. As an A 股账户投资者, I want same-bar buy and sell triggers processed in an adverse sequence, so that ambiguous paths do not create artificial profits.
31. As an A 股账户投资者, I want active orders modeled at the executable bid or ask, so that mid-price profits are not overstated.
32. As an A 股账户投资者, I want passive orders to model queueing and partial fills when quote data is available, so that limit-order execution is realistic.
33. As an A 股账户投资者, I want orders constrained to valid 100-share lots, available cash and sellable inventory, so that simulated trades could actually be placed.
34. As an A 股账户投资者, I want overnight base inventory to be allowed, so that the strategy can study both sell-then-buy and buy-then-sell intraday paths.
35. As an A 股账户投资者, I want an initial research baseline of 50% base inventory and 50% cash, so that overnight inventory and downside capacity are represented without committing the full account to one side.
36. As an A 股账户投资者, I want inventory allocation treated as a risk setting rather than endlessly optimized, so that the model does not overfit.
37. As an A 股账户投资者, I want portfolio equity to include unrealized inventory gains and losses, so that completed profitable grids do not hide a large losing position.
38. As an A 股账户投资者, I want results compared with holding the ETF, not trading, and simple baseline rules, so that activity is not confused with added value.
39. As an A 股账户投资者, I want the 30-day sample used only for exploration and data acceptance, so that recent noise is not presented as validated alpha.
40. As an A 股账户投资者, I want the final research to use at least approximately 12 months when available, so that multiple market conditions are represented.
41. As an A 股账户投资者, I want the final 60 trading days held out from parameter selection, so that there is a genuine out-of-sample result.
42. As an A 股账户投资者, I want tested anchors and parameter combinations logged, so that unsuccessful experiments are not silently discarded.
43. As an A 股账户投资者, I want zero-cost, baseline-cost and stress-cost results, so that strategy fragility is visible.
44. As an A 股账户投资者, I want strategy conclusions based only on baseline and stress costs, so that idealized results cannot justify trading.
45. As an A 股账户投资者, I want net return, maximum drawdown, worst day, turnover, cost-to-gross-profit ratio and maximum inventory reported, so that profitability and risk are evaluated together.
46. As an A 股账户投资者, I want a daily loss stop, inventory cap, spread stop and stale-data stop, so that abnormal conditions suspend trading.
47. As an A 股账户投资者, I want martingale position increases prohibited, so that losses cannot trigger uncontrolled risk escalation.
48. As an A 股账户投资者, I want overnight treatment fixed before a test begins, so that a losing intraday trade cannot be reclassified as a long-term investment.
49. As an A 股账户投资者, I want at least 20 trading days of simulated execution before live validation, so that model and actual market behavior can be compared.
50. As an A 股账户投资者, I want live validation to begin with the minimum trading unit, so that unverified assumptions do not immediately risk the full 10,000 yuan.
51. As an A 股账户投资者, I want all live orders placed manually by me, so that the research system never exercises account authority.
52. As an A 股账户投资者, I want clear Go/No-Go stage gates, so that failed data, cost or robustness checks stop the project from advancing.
53. As an A 股账户投资者, I want the same rule compared at one, two and four maximum daily round trips, so that I can see whether lower frequency improves cost-adjusted results.
54. As an A 股账户投资者, I want a finite, predeclared set of mean-reversion, trend, breakout, volatility-filter and proxy-residual hypotheses, so that the research is not limited to one EMA rule.
55. As an A 股账户投资者, I want every tested symbol and parameter combination retained, including failures, so that positive 30-day results are not cherry-picked or presented as validated profitability.
56. As an A 股账户投资者, I want the selected 159570/513780 hypothesis frozen before forward collection begins, so that future observations are not contaminated by continued tuning.
57. As an A 股账户投资者, I want synchronized one-minute bars, bid/ask, IOPV and provider timestamps accumulated from 2026-07-27 onward, so that execution and fair-value assumptions can be tested with point-in-time data.
58. As an A 股账户投资者, I want stale, weekend and pre-freeze observations retained but excluded from the forward sample, so that a successful request is not confused with valid live-session evidence.
59. As an A 股账户投资者, I want unavailable depth fields preserved as missing rather than inferred from last price, so that execution-data quality remains fail-visible.
60. As an A 股账户投资者, I want to enter only the six-digit code of the target T+0 ETF, so that I do not need to operate a proxy instrument.
61. As an A 股账户投资者, I want capital, commission and conservative execution assumptions saved in a local research profile, so that daily use does not require repeated configuration.
62. As an A 股账户投资者, I want the screen to show only the target ETF's observation buy ceiling and observation sell floor, each with a calculation time and expiry, so that stale prices cannot be mistaken for current values.
63. As an A 股账户投资者, I want an optional manual entry-fill price and quantity after I independently trade, so that the sell observation floor can be recalculated from my actual target-ETF fill without connecting the application to a broker.
64. As an A 股账户投资者, I want any IOPV, same-index, theme or market proxy to remain an internal research input rather than a second order leg, while remaining available in an audit trace, so that the output stays simple without becoming an opaque black box.
65. As an A 股账户投资者, I want ineligible symbols, stale data, invalid quotes, unavailable anchors, uncovered costs or failed gates required by the requested mode to return No-Go with no reusable price, so that the interface fails closed.
66. As an A 股账户投资者, I want paper-observation prices visually and semantically separated from controlled-live-validation prices, so that exploratory output is never presented as an approved recommendation.
67. As an A 股账户投资者, I want a desktop home screen to list every ETF in the confirmed research universe with its data and strategy readiness, so that entering one code does not falsely imply that all confirmed ETFs already have the same capability.
68. As an A 股账户投资者, I want to open one ETF at a time and inspect its current or completed-day one-minute and five-minute price trend, so that I can study intraday movement without trading an internal reference.
69. As an A 股账户投资者, I want all crawled bars, quote snapshots, data quality checks and collection runs stored in a local database with source lineage, so that the desktop can query them reliably and I can reproduce a result.
70. As an A 股账户投资者, I want raw provider responses retained outside Git and linked by immutable path and hash, so that database convenience does not weaken data provenance or expose large market data in version control.
71. As an A 股账户投资者, I want the application to identify completed continuous-uptrend intervals with their duration, rise and maximum pullback, so that I can study recurring intraday patterns after the close.
72. As an A 股账户投资者, I want a separate conservative cost-coverage annotation only when executable bid/ask evidence exists, so that a bar-level upward move is not represented as an achievable short-term profit.
73. As an A 股账户投资者, I want every trend interval to retain its detection parameters, input data vintage and calculation time, so that I cannot unknowingly tune the definition after seeing the day’s result.
74. As an A 股账户投资者, I want missing or incomplete current-day data shown as WAIT-DATA rather than filled by another ETF or an interpolated bar, so that cross-symbol and fabricated evidence cannot drive a decision.
75. As an A 股账户投资者, I want the app to remain research-only while showing these trends, so that a completed rising interval is never treated as a guaranteed next trade or automatic order.
76. As an A 股账户投资者, I want a break-even ledger to screen every target ETF, order amount and grid spacing before any backtest, so that a parameter group whose minimum commissions consume the available movement is not optimized into a misleading result.
77. As an A 股账户投资者, I want that ledger to separately show buy and sell minimum commission, spread, conservative slippage and queue/partial-fill haircut, so that I can tell observed costs from provisional assumptions.
78. As an A 股账户投资者, I want the ledger to show the tick-aligned minimum round-trip movement in yuan and basis points, so that I can compare a proposed grid distance directly with its cost floor.
79. As an A 股账户投资者, I want OHLC-conservative, quote-aware and paper-execution results kept in separate tiers, so that an assumed bar path is never mistaken for a fill probability or a manual execution record.
80. As an A 股账户投资者, I want paper execution to retain intended price, observed executable bid/ask, fill quantity, unfilled or cancellation reason and fee evidence for every manual observation, so that 20 valid days can test the model without connecting to my broker.

## Implementation Decisions

- The product is a research and decision-support system. It will not connect to a broker for order submission, cancellation or account control.
- The initial collection template is 159567, but it is not a preferred or final trading instrument. The first multi-symbol research batch contains 16 evidence-backed Hong Kong or Hong Kong Stock Connect equity ETFs across SZSE and SSE; future expansion remains fail-closed on exchange evidence and current listing status.
- The desktop research workbench displays all 16 confirmed research instruments, but a visible instrument is not automatically an eligible paper-observation target. The capability registry must separately state T+0 evidence, current status review, historical coverage, current-day data, fee profile, frozen policy, permitted mode and blocking gates.
- Every universe entry will include an exchange-issued announcement URL or a current exchange official-list designation, evidence date, last review date, enumerated current security status, legal fund name, trading code, manager and tracked index. The eligibility gate accepts only the exact `listed` state and binds an affirmative same-day-turnaround statement to a single target security record. An original exchange URL is preferred; when only an exchange-issued mirrored copy is accessible, the ledger must record its content SHA-256 fingerprint, issuer, exact T+0 quote and why the original URL was unavailable.
- The universe service, market-data adapter, calendar service, fee engine, portfolio ledger, execution simulator, analysis engine and reporting layer will be separate modules.
- External data providers will be isolated behind adapters because free endpoints, field names and retention periods can change.
- The initial free-data path will prioritize native 5-minute OHLCV for the latest 30 complete trading days.
- The system will probe, record and report the actual available range of native 1-minute data. It will never interpolate or expand 5-minute bars and label them as 1-minute data.
- If historical 1-minute quotes are unavailable for free, the system will support forward accumulation of native 1-minute bars and real-time bid/ask snapshots.
- Historical Level-1, Level-2, tick and IOPV data will be optional capabilities. Strategy claims requiring those data will remain blocked until an appropriate source is available.
- Raw source data will be immutable after ingestion. Cleaning, session labeling and resampling will produce derived datasets with lineage back to the raw observation.
- SQLite is the local operational database for normalized instruments, immutable raw-payload references, acquisition runs, quote snapshots, bar vintages, quality results, trend intervals and desktop research decisions. Raw JSON and larger derived datasets remain local files managed by DVC pointers; neither belongs in ordinary Git.
- The canonical timestamp timezone will be Asia/Shanghai. Each record will retain source acquisition time and session classification.
- The initial core analysis session will be 09:30–11:30 and 13:00–15:00. Opening auction will be stored as an event. Closing auction will be flagged within the final interval. After-hours fixed-price trading will be stored separately and excluded by default.
- A complete normal core trading day is expected to contain 48 native 5-minute bars. Exceptions must be explained by suspension, market calendar, provider failure or another explicit status.
- Trading calendars will represent mainland exchange availability, Hong Kong exchange availability, southbound Stock Connect availability, ETF subscription availability, ETF redemption availability and whether the day is a normal overlapping day.
- Normal overlap days will be the primary analysis set. Calendar mismatch days will be reported as a distinct regime.
- Historical prices for intraday execution research will remain unadjusted. Corporate actions or fund events that change price comparability will be represented explicitly rather than hidden by future-informed adjustments.
- The fee engine will accept broker commission rate, minimum charge, whether handling fees are included, and per-order rounding rules.
- Until the user's actual ETF fee schedule is confirmed, the baseline placeholder will charge 5 yuan on each buy order and 5 yuan on each sell order. This assumption will be clearly marked provisional.
- Exchange handling fees will not be added twice when already included in broker all-in commission.
- ETF secondary-market trading will exclude stock transaction stamp duty and ordinary secondary-market transfer fees unless an actual broker statement demonstrates a different account treatment.
- Fund management and custody fees will be treated as fund-level NAV drag rather than per-order cash charges.
- The cost model will separately report explicit fees, bid/ask spread, slippage, impact, queueing assumptions and missed or partial execution.
- Decision interval and grid price spacing will be separate strategy parameters.
- Before any strategy backtest or parameter comparison, the system will produce a versioned break-even ledger for every requested `target ETF × order amount × grid spacing × fee scenario × execution tier` combination. It will lot-round the affordable quantity, show unused cash, and reject a configuration that cannot fund one lot or whose declared grid spacing is below its tick-aligned full-cost movement.
- The ledger will report, separately, buy-side minimum commission, sell-side minimum commission, handling and other explicit fees, observed or declared spread, conservative slippage and queue/partial-fill haircut. Missing execution inputs remain missing: zero may be used only as a clearly marked lower-bound assumption and must not be presented as an observed or broker-executable cost.
- The `minimum round-trip movement` is the smallest legal-tick gross price movement that covers the two order cash flows and all declared economic-cost components. It must be shown in yuan per unit, ticks and basis points, together with the proposed grid movement and a `RESEARCH_BLOCKED_COST` result when the latter is insufficient. Passing this screen only means the declared arithmetic is covered; it does not establish a trade, fill, profitability or a Go decision.
- For a fixed 10,000-yuan notional split into `N` independently charged grid round trips, the known minimum-fee lower bound alone is `10 × N` bp before lot rounding, spread, slippage or unfilled orders. The 30,000-yuan comparison lower bound is `3.33 × N` bp. The ledger, rather than this approximation, is authoritative because it uses the target's price, lot size and declared costs.
- Execution tiers are strictly separated: `OHLC_CONSERVATIVE` permits only causal bar-level, adverse-order screening and returns `NO_EXECUTION_CLAIM`; `QUOTE_AWARE` requires same-target qualified bid/ask, source, timestamp freshness and depth/queue fields before it may discuss limit-order fill assumptions; `PAPER_EXECUTION` is a manual journal of observed orderable prices and outcomes, never a broker connection.
- `PAPER_EXECUTION` requires at least 20 valid normal-overlap trading days. Each observation records target code, timestamp, intended side/price/quantity, contemporaneous bid/ask and source, actual filled price/quantity or unfilled/partial/cancelled outcome, fee evidence and reason. These 20 days are an execution-feasibility dataset only; they cannot replace G5's frozen 60-day holdout, 100 completed round trips and 40 active days.
- Anchor candidates will be evaluated in this priority order: contemporaneously reconstructable IOPV or constituent-basket fair value, residual versus a liquid same-index proxy, causal rolling VWAP or EMA, and fixed reference prices such as previous close or opening range as baselines.
- No end-of-day VWAP, closing NAV or future bar information may be used by an intraday signal.
- A signal created after a completed bar may execute no earlier than the next available executable quote.
- Five-minute OHLC data will support exploration and conservative coarse screening only.
- When only bar data are available, touching a limit price will not guarantee a fill. Same-bar conflicting triggers will use adverse ordering and will not permit repeated round trips within one bar.
- When quote or tick data are available, active buys will execute at the ask and active sells at the bid. Passive orders will require a queue, available-volume and partial-fill model.
- The portfolio ledger will enforce 100-share lot constraints for purchases, available-cash constraints, sellable-inventory constraints and no uncovered short selling.
- Overnight base inventory is permitted.
- The initial research baseline will allocate 50% of the 10,000-yuan capital to base inventory and retain 50% as cash. This is a research baseline, not a live allocation recommendation.
- The user-requested 30,000-yuan, maximum-20-round-trips-per-day calculation is a separate 30-day cost pressure scenario for Issue #8. It does not replace the frozen 10,000-yuan, 50% inventory / 50% cash research baseline, does not qualify as an execution backtest, and cannot support a strategy Go decision.
- The first cross-sectional grid screen standardizes total capital at 30,000 yuan and limits the tactical comparison layer to 25% of total capital. This is an exploratory cross-symbol comparison requested by the user, not a PRD allocation change, live position recommendation or replacement for the 10,000-yuan baseline.
- The Issue #17 multi-strategy exploration keeps the total comparison capital at 30,000 yuan. Its candidate families use a 15,000-yuan long-only tactical sleeve to match the approved 50% cash capacity; its frequency-isolation rows retain the earlier 7,500-yuan layer so that only the daily round-trip cap changes.
- Reusing the 30-day sample is permitted for finite, explicitly logged hypothesis generation and for answering sensitivity questions. It is not permitted to search indefinitely, discard failed trials, call the best row validated, or use the reused sample to advance G4-G8.
- A strategy combination in Issue #17 means a declared instrument, anchor or proxy, causal rule and parameter set. It does not mean that multiple rows can be added together as a simultaneously executable portfolio without a separate capital-allocation and overlap simulation.
- Total portfolio equity will equal cash plus inventory marked at a conservative executable liquidation price, less accrued costs.
- Completed-grid profit will never be reported without unrealized inventory profit or loss and total capital usage.
- The first 30 trading days will be used only for data acceptance, recent-volatility description, cost coverage and hypothesis generation.
- Formal validation will target all available history, preferably at least approximately 12 months, and reserve the last 60 trading days as a final untouched holdout.
- Effective strategy freedom will be limited to approximately four principal choices: anchor, entry/exit deviation, per-layer size and maximum inventory layers. Risk settings will be frozen rather than optimized wherever possible.
- Every examined anchor and parameter family will be logged to expose multiple testing.
- Exploratory positive rows will be separated from priority hypotheses. A priority label still requires positive baseline and stress tactical P&L in both reused chronological descriptions, carries a small-sample warning below 30 paths, and never substitutes for the final 60-day holdout.
- Issue #19 freezes `PROXY_RESIDUAL_L48_Z150_H12_MAX1` before forward observation. The target is 159570, the theme proxy is 513780, and no parameter may change without a new version and a new forward start date.
- Forward collection v1 begins exactly on 2026-07-27. Its complete strategy definition, source Git commit and source blob are frozen; a change requires a new collection ID and start date. The default public-data capture interval is 15 seconds, depth is probed every 60 seconds and the provider-native one-minute window is synchronized every 300 seconds.
- Candidate quote eligibility requires a mainland core session, matching provider data date, provider timestamp age no greater than 120 seconds, valid last/bid/ask/IOPV and both instruments in the same capture. This does not prove real-time delivery. Weekend, stale and pre-freeze observations are retained for lineage but excluded from the candidate forward sample.
- One-minute payload vintages are immutable. Only completed, recent, valid-OHLC bars whose timestamp belongs to the corresponding core session, and which are first observed during that session or its predeclared 11:30–11:37/15:00–15:07 completion window for both instruments, may be candidate paired bars; later backfill is retained but not promoted to point-in-time evidence.
- Public bid/ask and IOPV snapshots do not establish broker executability. Missing depth, source delivery latency, queueing and partial fills keep G3 blocked and must not be imputed.
- The desktop application is a local target-ETF observation client. Its primary workflow accepts one target ETF code; it does not ask for a proxy code and does not expose any proxy order, position or trading action.
- The M3 desktop home screen is a master list of the 16 research instruments. A detail screen is still target-only: selecting an ETF never exposes an internal reference as a second tradeable leg.
- End-of-day trend research uses native one-minute bars when available, otherwise clearly labelled native five-minute bars. A completed uptrend interval is descriptive: it records a predeclared rise, duration and drawdown rule and never becomes a next-day recommendation. An executable cost-coverage annotation requires contemporaneous bid/ask evidence and conservative full transaction costs; OHLC alone cannot prove fills.
- The M3 collector must ingest each instrument independently. It must never reuse a quote, IOPV, bar, fee profile, policy or data-quality result from a different ETF. Missing current-day data remains `WAIT_DATA`.
- Background anchors may use point-in-time IOPV, the target ETF's own causal history, or an approved internal reference instrument. Every component must remain versioned and auditable, but only the target ETF's prices appear on the primary decision card.
- The first desktop release will expose a paper-observation mode. Controlled-live-validation wording and state remain disabled until G0–G7 pass; a research price is never silently promoted when a gate fails.
- Paper observation and controlled live validation use separate gate matrices. Paper mode requires G1, a registered frozen policy, an eligible current snapshot and a conservative cost profile; G0 and project-wide G2–G7 remain visible blockers of live mode but do not hide explicitly labelled paper prices. Live mode requires G0–G7 without exception.
- A target ETF without a registered, versioned observation policy returns No-Go. The application will not invent a generic EMA or fixed range merely because a code exists in the T+0 universe.
- Observation prices are nullable, versioned and short-lived. No-Go, expiry, application resume, network recovery or critical input changes clear the displayed values rather than retaining the last valid result.
- The application will report the causal strategy exit threshold separately from the break-even reference calculated from a planned ask fill or user-reported actual fill. Cost coverage is an entry gate and a P&L status, never a reason to postpone a maximum-hold, end-of-day, stale-data or risk exit while waiting to break even.
- Production desktop evaluation uses a service-owned trusted clock. Historical replay and tests use a separate interface; the production UI cannot supply or override `as_of` for freshness or expiry decisions.
- Desktop paper prices are available only during continuous auction windows, initially 09:30–11:30 and 13:00–14:57 Asia/Shanghai. Opening auction, lunch, closing auction and post-close states clear all prices.
- Every registered observation policy must retain its target, anchor formula, causal bar timing, training window, parameter-search log, freeze time, forward start, code/data hashes, validation status and allowed mode. Any target, anchor or parameter change creates a new policy version and forward start.
- The recommended first implementation is a Python `PySide6` local desktop client over a small target-observation application service, subject to design review before implementation begins.
- M1 uses `PySide6 Essentials` and a versioned 159570 fixture with a service-owned, monotonically advancing demo clock. The fixture binds the confirmed T+0 ledger evidence, eligibility review date, legal 0.001 ticks and frozen policy lineage. It validates the target-only UI, provisional cost floor, continuous-session gate, non-replayable expiry clearing and unsupported-symbol No-Go; it must never be described as current market data.
- Evaluation baselines will include no trading, passive ETF holding, a simple fixed rule and a random-timing strategy with comparable turnover.
- Performance reporting will include net return, daily Sharpe ratio, maximum drawdown, worst day, turnover, cost as a percentage of gross profit, profit factor, partial-fill rate, maximum inventory, forced-exit share and active trading days.
- Reports will show zero-cost, baseline-cost and stress-cost scenarios. Only baseline and stress results may support a Go decision.
- Initial research risk controls will stop new trading after a 1% daily total-equity loss, prohibit additional downward grid entries above 80% invested capital, pause the strategy at a 3% equity drawdown, and treat a 5% drawdown as version failure.
- Exploratory cost pressure calculations that intentionally omit these controls must say so in code and reports, and must not be called a strategy baseline or be used to advance G4–G8.
- New orders will be suspended when the spread is approximately twice its normal comparable-session median or above its comparable-session 95th percentile.
- New orders will be suspended when anchor data, IOPV, constituent prices or quotes are stale, or when the Hong Kong underlying market is closed and the model depends on live underlying prices.
- Martingale sizing is prohibited.
- The user will make every live trading decision and place every live order manually.

## Testing Decisions

- Tests will target externally observable research behavior and financial invariants, not internal class structure or implementation details.
- The highest test seam for universe eligibility is: given official evidence and a review date, the system either includes an ETF as confirmed T+0 or rejects it as unverified.
- The universe test suite will verify that no ETF can enter the confirmed pool without a source URL, an issuer, explicit affirmative same-day-turnaround language, and a SHA-256 fingerprint for any exchange-issued mirrored copy.
- The highest test seam for data ingestion is: given a provider response, the system produces an immutable normalized dataset plus a quality report containing date range, source, units, timezone, session coverage and anomalies.
- Data tests will cover duplicates, missing bars, zero prices, impossible values, unexplained gaps, stale observations and mixed units.
- Session tests will verify the 48-bar expectation for a normal full core session and explicitly test opening auction, closing auction, lunch break and after-hours separation.
- Calendar tests will cover normal overlap days, mainland-only trading days, Hong Kong holidays, Stock Connect closures and subscription/redemption suspensions.
- Cross-source acceptance tests will compare at least three sampled dates against broker-visible market data for OHLC, volume and turnover.
- Resampling tests will verify that any derived interval is produced only from finer native data and that no finer-resolution series is constructed from coarser data.
- The highest test seam for fees is: given an anonymized broker statement or a fully specified commission schedule and a list of orders, the engine reproduces each fee and total cash movement to 0.01 yuan.
- Fee tests will cover minimum commission, percentage commission above the minimum, both sides of a round trip, partial fills under the broker's charging rule, included handling fees and excluded handling fees.
- Fee tests will assert that ETF secondary-market trades do not receive stock stamp duty by default and that exchange fees are not double counted.
- Break-even tests will show the required price movement for several order sizes under the provisional 5-yuan-per-side assumption.
- Break-even-ledger tests will cover affordable lot rounding, the one-lot funding failure, independent buy/sell minimum commissions, explicit separate cost components, tick-aligned minimum movement, rejected proposed grids, and a visible lower-bound status whenever broker fee or execution evidence is incomplete.
- Execution-tier tests will prove that an OHLC row cannot be promoted to quote-aware or paper-execution status; quote-aware rows require qualified target bid/ask and declared freshness/depth; paper-execution readiness requires 20 valid manual-record days with preserved partial/unfilled outcomes.
- The highest test seam for portfolio accounting is: given a sequence of orders, fills, fees and market quotes, the ledger returns cash, inventory, realized P&L, unrealized P&L, total equity and capital usage that reconcile exactly.
- Portfolio tests will cover 100-share lots, insufficient cash, insufficient sellable inventory, base inventory, overnight carry, partial exits and liquidation valuation.
- The highest test seam for execution is: given signals and market observations, the simulator returns only fills permitted by causal timing and the configured conservative execution policy.
- Execution tests will reject same-bar perfect round trips when price path is ambiguous.
- Execution tests will cover touch versus trade-through behavior, adverse same-bar ordering, next-quote execution, bid/ask execution, partial fills and stale quotes.
- Signal tests will assert that rolling anchors use only information available through signal time.
- Look-ahead tests will ensure that final daily VWAP, final NAV, closing prices and future bars cannot influence earlier signals.
- Analysis tests will verify that the 30-day dataset is labeled exploratory and cannot be promoted as a final validation set.
- Validation tests will enforce chronological train/validation/holdout separation and prevent the final 60-day holdout from being used for parameter selection.
- Performance tests will reconcile gross P&L to explicit fees, spread/slippage, net P&L and total-equity change.
- Risk-control tests will verify daily loss stops, inventory caps, drawdown pauses, spread stops, stale-data stops and prohibition of martingale sizing.
- Desktop service tests will assert that only an eligible target code is accepted; No-Go, expired and stale-data exit states contain no target quote, observation or break-even prices; both otherwise displayed prices share one snapshot and policy version; and all values use legal tick rounding.
- Desktop presentation tests will assert that the primary screen contains no proxy trading control, broker action, order shortcut or guaranteed-profit wording, and that paper-observation status remains visible whenever G8 is unavailable.
- Database integration tests will prove that all 16 configured instrument identities can be listed, bar and quote vintages retain source/run lineage, duplicate ingestion is idempotent, and one target cannot query another target’s data.
- Desktop query tests will prove that SQLite opens in true read-only mode, cannot accept writes, and retains the same file hash after discovery and target-detail queries.
- Trend-research tests will use fixed native-bar fixtures to verify predeclared completed intervals, duration, cumulative return and maximum pullback. They will assert that no interval contains future bars, that missing data yields `WAIT_DATA`, and that a bar-only interval has no executable-profit claim.
- Desktop workbench tests will cover the 16-row capability list, target-only detail selection, historical versus current-day labels, data-quality presentation and at least two independent target detail paths.
- Desktop workbench tests will require exact equality between configured and stored instrument codes, partial-date coverage x/16, the latest useful complete local date by default, explicit `WAIT_DATA` no-run wording, reload success/failure feedback, wrapped critical labels and recoverable scrolling at 1180×760.
- Cross-source evidence tests will preserve source and receive times, reject future-aligned quotes, report rather than interpolate missing rows, and keep G2 and G3 as separate gates.
- Manual-fill tests will bind a user-entered target fill to its originating decision, validate tick/lot/time/fee fields and recompute break-even status without mutating the original decision snapshot or delaying a mandatory exit.
- Desktop clock and session tests will prove that UI input cannot override freshness time and that lunch, opening/closing auctions, post-close, resume and network recovery clear prices.
- Stress tests will rerun the same policy under baseline, 1.5-times and 2-times transaction-cost assumptions.
- Robustness tests will compare neighboring parameter settings and remove the best five trading days to detect dependence on isolated outcomes.
- Simulation acceptance will require at least 20 trading days, no cash or inventory violations, and observed execution quality within the modeled range before any live validation.
- 项目已有行为测试、桌面展示测试、SQLite 数据库集成测试、费用/回本账本测试与保守策略测试。后续变更必须优先扩展这些高层行为契约，而不是仅对内部实现细节断言。

## Out of Scope

- Automatic order generation, broker API connectivity, order submission, cancellation or modification.
- Custody of credentials, account numbers, authentication tokens or other brokerage secrets.
- Guaranteed profit, guaranteed mean reversion or representation of the strategy as risk-free arbitrage.
- Direct trading of Hong Kong-listed securities through a Hong Kong account.
- Margin financing, securities lending, uncovered short selling, derivatives hedging or leverage.
- ETF primary-market creation and redemption execution.
- High-frequency or low-latency trading.
- Historical Level-2, order-book or tick data purchases in the initial free-data phase.
- Exact historical IOPV reconstruction unless constituent, weight, foreign-exchange and corporate-action inputs are independently available.
- Tax, legal or personalized regulated investment advice.
- Portfolio allocation across unrelated asset classes.
- Mobile application, production web dashboard or automated alert delivery in the first research version.
- Broker deep links, clipboard order templates, automatic form filling or any feature that turns an observation price into an order instruction.
- Strategy optimization or live trading before actual broker fees are confirmed.
- Treating the initial 30-day sample as proof of profitability.
- Including after-hours fixed-price ETF trading in the initial strategy.

## Further Notes

### Terminology

- “当日回转交易” means an eligible ETF bought in the secondary market can be sold on the same trading day.
- “检查频率” means how often the research system evaluates a signal.
- “价格网格间距” means the price distance required between trading layers.
- “基础仓位” means inventory intentionally carried overnight to permit both sell-then-buy and buy-then-sell intraday paths.
- “套利” should be reserved for a hedged or structurally convergent trade. The current project should be described as ETF intraday grid or mean-reversion research.

### Stage Gates

1. G0 — Fee acceptance: obtain the actual ETF commission schedule or an anonymized statement and reproduce fees to 0.01 yuan.
2. G1 — Universe acceptance: every candidate has exchange-level T+0 evidence and a current status review.
3. G2 — Data acceptance: at least 30 complete trading days, expected-session completeness of at least 99.5%, no unexplained duplicates or gaps, and successful cross-source checks.
4. G3 — Execution-data acceptance: quote or tick data are available for realistic execution; otherwise only conservative bar-level screening is permitted.
5. G4 — Hypothesis acceptance: greater fair-value deviation has a stable, economically sensible relationship with future returns after costs across multiple subperiods.
6. G5 — Holdout acceptance: final holdout of at least 60 days, positive net result, at least 100 completed round trips covering at least 40 active days, and profit factor of at least 1.2.
7. G6 — Robustness acceptance: positive result after 1.5-times costs, tolerable drawdown at 2-times costs, and broadly valid neighboring parameters.
8. G7 — Simulation acceptance: at least 20 trading days with no cash, inventory, stop or execution-model violations.
9. G8 — Controlled live validation: begin at the minimum trading unit and do not scale before a predeclared number of days and trades.

Failure at a gate stops advancement. It does not authorize additional parameter tuning on the failed validation sample.

G0 does not block raw-data acquisition, data-quality work or descriptive 30-day reporting under clearly labelled provisional costs. It does block any actual-cost conclusion, strategy Go decision, simulated-live progression and controlled live validation.

`G0.5 — break-even ledger` is a pre-backtest rejection gate, not a replacement for G0–G8. A parameter group must fund a legal lot and have a proposed grid distance at least equal to its declared tick-aligned full-cost movement. A `RESEARCH_BLOCKED_COST`, `RESEARCH_BLOCKED_LOT`, `WAIT_QUOTE_DATA`, `WAIT_EXECUTION_COST_EVIDENCE`, `WAIT_FEE_EVIDENCE`, `WAIT_FEE_SCOPE`, `NO_EXECUTION_CLAIM` or `PAPER_EXECUTION_FEASIBILITY_ONLY` row must not be selected by a performance optimizer as a viable configuration.

### Open Items

- The user reported on 2026-07-25 that their CMB A-share account charges a 5-yuan minimum commission on each filled 159567 buy and sell, with no stamp duty or transfer fee. This is versioned as a user-reported lower-bound scenario, not a full broker-statement calibration.
- The user's ETF commission percentage and partial-fill charging method remain pending.
- Whether exchange handling fees are included in the user's quoted commission remains pending.
- The initial free-data path is approved.
- If minute-level history is insufficient, an alternative source will be considered separately.
- Overnight base inventory is approved.
- The 50% inventory / 50% cash split is an initial research baseline only and may be changed by an explicit risk decision before formal validation.
- Issue #26 implements the approved M1 fixed-fixture prototype; it is now available only through the explicit `--demo` option.
- Issue #28 implements M2 current-paper observation: live local target quote/IOPV diagnostics, a versioned 2026 normal-overlap calendar, causal one-minute-to-five-minute vintages, L48 frozen-policy calculation, full-config lineage locking, conservative cost gating, fail-closed expiry, atomic manifests, independent supervised collection heartbeats, target-only decision journaling and background desktop refresh.
- Issue #30 implements M3 multi-ETF research workbench: SQLite-backed local research records, 16-instrument capability discovery, independently persisted multi-symbol data, target detail trend charts and reproducible continuous-uptrend interval research. It does not grant a generic strategy or paper-trading price to every listed instrument.
- Issue #31 已通过 PR #35 合并：实现回本账本、严格 OHLC/quote-aware/paper-execution 证据分层、人工纸面记录本机 SQLite 留存和不可变本地报告 lineage。它没有完成券商账单校准或券商可执行盘口验证；因此所有 M3 OHLC 趋势区间仍是描述性的 `NO_EXECUTABLE_QUOTES`。
- The public web feed remains `UNVERIFIED RESEARCH FEED`. G2/G3 stay blocked until independent calendar/cross-source and broker-executable quote/depth validation are complete. They block controlled-live validation, but do not permanently hide explicitly watermarked paper prices after the current-snapshot, causal L48, eligibility, policy-lineage and conservative-cost gates pass.
- Issue #37 定义桌面可靠性修复与三源证据方案：Tushare Pro 作为首选独立分钟候选，富途 OpenAPI 只读行情作为程序化盘口备选，招商客户端仍由用户手工留证；任一候选都不自动授予 G2/G3。
- Issue #39 跟踪上述设计的实施：数据权限/上游来源探针、16标的 adapter、3日跨源核验、招商首日手工权限探针与20日纸面执行；它是开放的后续工作，不由已关闭的设计 Issue #37 继续承载。
- 用户录入成交结果的桌面流程、持仓状态驱动的强制退出细化、以及可分发的独立签名应用包装仍是后续垂直切片；M3 的目标 ETF 图表和本机启动脚本已实现。

### Primary References

- Shenzhen Stock Exchange rule on cross-border ETF same-day turnaround trading: https://www.szse.cn/disclosure/notice/general/t20150109_501348.html
- Shenzhen Stock Exchange 2026 fee schedule: https://investor.szse.cn/marketServices/deal/payFees/index.html
- Shenzhen Stock Exchange Trading Rules (2026 revision): https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf
- SSE official 2026 Southbound Stock Connect trading-day arrangement: https://www.sse.com.cn/services/hkexsc/disclo/announ/c/c_20251222_10802519.shtml
- HKEX Stock Connect trading calendar for 2026: https://www.hkex.com.hk/-/media/HKEX-Market/Services/Circulars-and-Notices/Participant-and-Members-Circulars/SEHK/2025/ce_SEHK_CTCSC_176_2025.pdf
- PRC Stamp Tax Law: https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html
- ChinaClear Shenzhen market fee schedule: https://www.chinaclear.cn/zdjs/fbzyls/202506/ab6384ba25514554a7eceaee3e521032/files/%E6%B7%B1%E5%9C%B3%E5%B8%82%E5%9C%BA%E8%AF%81%E5%88%B8%E7%99%BB%E8%AE%B0%E7%BB%93%E7%AE%97%E4%B8%9A%E5%8A%A1%E6%94%B6%E8%B4%B9%E5%8F%8A%E4%BB%A3%E6%94%B6%E7%A8%8E%E8%B4%B9%E4%B8%80%E8%A7%88%E8%A1%A8.pdf
- AKShare public-fund data documentation: https://akshare.akfamily.xyz/data/fund/fund_public.html
