# 港股 ETF T+0 日内研究

这是一个面向人工交易决策的研究项目，用于验证可当日回转的港股 ETF 的标的资格、数据质量、交易成本、波动特征和策略可行性。

## 项目入口

- [产品需求文档](PRD.md)
- [项目活动与验证记录](docs/PROJECT_ACTIVITY.md)
- [领域上下文](CONTEXT.md)
- [T+0 标的证据台账](docs/UNIVERSE_LEDGER.md)
- [临时费用模型](docs/FEE_MODEL.md)
- [回本账本（回测前成本门禁）](docs/BREAK_EVEN_LEDGER.md)
- [数据版本规则](docs/DATA_VERSIONING.md)
- [独立行情与招商盘口留证方案](docs/EXECUTION_EVIDENCE_PLAN.md)
- [多策略与低频探索报告](reports/t0_etf_multi_strategy_exploration.md)
- [159570/513780 前向采集手册](docs/FORWARD_COLLECTION.md)
- [2026-07-27 159570 人工观察简报](reports/2026-07-27_159570_manual_observation_brief.md)
- [目标 ETF 单代码桌面观察应用设计](docs/DESKTOP_APP_DESIGN.md)
- [桌面观察应用 M1 使用说明](docs/DESKTOP_M1.md)
- [多ETF桌面研究工作台 M3 与 M2 观察应用使用与运维](docs/DESKTOP_M2.md)
- [多ETF研究工作台 M3 设计](docs/WORKBENCH_M3.md)
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
# 生成本机回本账本；5分钟收盘价仅用于 OHLC 保守成本筛选，不是下单价格
PYTHONPATH=src uv run python -m etf_t0.break_even_report
# 仅用于非交易时段的 stale 链路探测，不产生有效样本
PYTHONPATH=src uv run python -m etf_t0.forward_collection --allow-outside-session
# 导入既有本机原生数据到SQLite，或在收盘后采集16只ETF的一分钟窗口
PYTHONPATH=src uv run python -m etf_t0.research_workbench bootstrap
PYTHONPATH=src uv run python -m etf_t0.research_workbench collect-one-minute
# M3多ETF研究工作台；以只读方式使用已建的本机SQLite，不连接券商
PYTHONPATH=src uv run python -m etf_t0.desktop_app
# M2当前纸面观察（当前仅159570有冻结策略）
PYTHONPATH=src uv run python -m etf_t0.desktop_app --single-observation
# 只有显式指定才启动M1固定夹具
PYTHONPATH=src uv run python -m etf_t0.desktop_app --demo
# 有界、单实例的前向采集器（示例：135分钟）
PYTHONPATH=src .venv/bin/python -m etf_t0.collector_service --duration-minutes 135
# 生成 Finder 可双击的本机启动文件
PYTHONPATH=src uv run python -m etf_t0.macos_bundle
```

数据文件保留在本机，具体规则见 `docs/DATA_VERSIONING.md`。不要将券商交割单、API 密钥或原始行情直接提交到 Git。
