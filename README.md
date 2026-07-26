# 港股 ETF T+0 日内研究

这是一个面向人工交易决策的研究项目，用于验证可当日回转的港股 ETF 的标的资格、数据质量、交易成本、波动特征和策略可行性。

## 项目入口

- [产品需求文档](PRD.md)
- [领域上下文](CONTEXT.md)
- [T+0 标的证据台账](docs/UNIVERSE_LEDGER.md)
- [临时费用模型](docs/FEE_MODEL.md)
- [数据版本规则](docs/DATA_VERSIONING.md)
- [多策略与低频探索报告](reports/t0_etf_multi_strategy_exploration.md)
- [159570/513780 前向采集手册](docs/FORWARD_COLLECTION.md)
- [2026-07-27 159570 人工观察简报](reports/2026-07-27_159570_manual_observation_brief.md)
- [架构决策记录](docs/adr/)
- [GitHub Issues](https://github.com/ganluCoding/etf_t-0_hk_stock/issues)

## 研究边界

- 仅研究和决策支持，不自动下单。
- 初始标的为 159567；其他标的必须在证据台账中确认当日回转资格。
- 当前阶段优先验证 30 个交易日的原生 5 分钟数据和免费 1 分钟数据可得性。
- 所有策略结论必须计入双边费用、价差、滑点、库存浮盈亏和执行约束。

## 本地快速开始

```bash
uv sync --all-groups
uv run pytest
PYTHONPATH=src uv run python -m etf_t0.multi_strategy
# 仅用于非交易时段的 stale 链路探测，不产生有效样本
PYTHONPATH=src uv run python -m etf_t0.forward_collection --allow-outside-session
```

数据文件保留在本机，具体规则见 `docs/DATA_VERSIONING.md`。不要将券商交割单、API 密钥或原始行情直接提交到 Git。
