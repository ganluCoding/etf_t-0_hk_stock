# 159570/513780 前向数据采集

## 目标和冻结点

本采集器从 2026-07-27 起为 `PROXY_RESIDUAL_L48_Z150_H12_MAX1` 积累全新数据。策略参数位于 `config/forward_collection.json`，采集期间不得修改。它是研究数据管道，不是盘中交易信号或自动下单程序。

| 角色 | ETF | 指数 |
| --- | --- | --- |
| 目标 | 159570 港股通创新药ETF汇添富 | 国证港股通创新药指数 |
| 同主题价格代理 | 513780 港股创新药ETF景顺 | 中证港股通创新药指数 |

两只产品不跟踪同一指数，不应称为同指数套利。

## 采集内容

- 默认每 15 秒保存一个成对 capture：最新价、买一、卖一、IOPV、提供方折价率、累计成交量/额、数据日期和更新时间。分页请求不是交易所级同步快照。
- 默认每 60 秒探测五档价量。未到探测周期为 `not_sampled_this_interval`；已探测但缺失为 `partial_or_unavailable`。深度数量保留提供方原始单位，交易时段核验前不换算成股数。
- 默认每 300 秒同步提供方原生 1 分钟窗口。`241` 只表示提供方时间标签覆盖（09:30–11:30 及 13:01–15:00），09:30 语义和 OHLC 价格合法性另行验收。
- 每次一分钟响应以微秒和 UUID 不可覆盖地保存。标准化表保留首次完整 OHLC，以 `first_seen_at`/`last_seen_at` 记录版本边界。
- 保存请求开始、响应接收和提供方更新时间。提供方时间戳年龄只是候选门槛，不证明端到端实时性。

## 有效前向数据条件

行情快照只有同时满足以下条件才标记为 `is_candidate_forward_quote=true`：

1. 本地日期不早于 2026-07-27。
2. 本地时间位于 09:30–11:30 或 13:00–15:00。
3. 提供方数据日期与本地日期相同。
4. 提供方更新时间与响应接收时间的差不超过 120 秒。
5. 最新价、买一、卖一和 IOPV 均为正，且卖一不低于买一。
6. 同一 capture 内 159570 和 513780 都通过，才标记 `is_candidate_forward_pair=true`。

一分钟 bar 的 timestamp 必须属于对应核心时段，并在该时段或紧邻的预声明收尾窗口（11:30–11:37/15:00–15:07）首次合格观测。它还必须已结束至少 60 秒且不超过 420 秒、OHLC 合法，两标的同时点均存在，才成为成对候选 bar。周末和事后补回均不合格。

“候选”不等于“有效实时行情”。东财 `push2delay` 的传输时延、沪深与香港/港股通交易日历错位、跨源核对和券商可执行性未完成前，`valid_forward_sample_available` 固定为 `false`，G2/G3 仍为 BLOCKED。

## 运行方式

非交易时段只做一次链路探测：

```bash
PYTHONPATH=src uv run python -m etf_t0.forward_collection \
  --allow-outside-session
```

交易日 09:30 开始运行，覆盖早盘、午休和下午盘：

```bash
PYTHONPATH=src uv run python -m etf_t0.forward_collection \
  --duration-minutes 337
```

运行期间只有核心时段会写 quote/depth 快照。一分钟同步额外保留 11:30–11:37 和 15:00–15:07 的收尾窗口，只为让 11:30/15:00 bar 在结束 60 秒后被首次合格观测，不会在该窗口采 quote。如果提供方时间戳年龄超过 120 秒，数据仍会保留，但不进入候选前向样本。

## 本机与版本管理

| 内容 | 位置 |
| --- | --- |
| 原始快照和1分钟响应 | `data/raw/forward_capture/` |
| 去重标准化行情 | `data/interim/forward_capture/` |
| 最新质量清单 | `reports/generated/forward_capture/latest_manifest.json` |
| 原始数据 DVC 指针 | `data/raw/forward_capture.dvc` |
| 标准化数据 DVC 指针 | `data/interim/forward_capture.dvc` |

每次完成新交易日后重新运行 `dvc add`，提交新指针与版本化质量摘要。当前没有 DVC remote，数据仅在本机可恢复。
