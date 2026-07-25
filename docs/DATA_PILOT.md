# 159567 免费 5 分钟数据试点

运行：

```bash
uv run python src/etf_t0/data_pilot.py --symbol 159567
uv run dvc add \
  data/raw/159567_5m_latest data/interim/159567_5m_latest \
  data/raw/159567_1m_probe data/interim/159567_1m_probe
uv run dvc data status data/raw/159567_5m_latest.dvc
```

采集器直接请求东方财富公开端点：5 分钟 K 线（`fqt=0`）和原生 1 分钟探测端点。字段定义与 AKShare ETF 接口兼容，但本试点不把直接端点访问描述为 AKShare 调用。它保留原始 JSON、标准化 CSV、抓取时间、字段单位、时区和质量摘要。远端可能拒绝普通 Python TLS 客户端；程序以有限重试和浏览器兼容传输层访问公开端点，但不使用 Cookie、登录态或密钥。

验收目标是最近 30 个完整交易日，每个标准内地核心交易时段有 48 根 bar。若实际返回较短保留期、存在缺口或接口失败，报告必须如实标为不通过；不得用日线、其他频率、插值或重复数据凑足 30 日。

原始行情仅留本机并由 DVC 指针版本化。`reports/generated/` 下的运行报告不进入 Git；在确认试点结果后，将一份不含原始行情的质量结论写入版本化报告。1 分钟端点的可得窗口由提供方决定；本次只把实际返回的窗口作为可复现探测，不将其扩大解释为 30 日数据。

项目没有定义 `dvc.yaml` 管道，因此应以 `dvc data status <pointer>.dvc` 校验数据指针；无目标的 `dvc status` 只检查管道，可能显示“没有追踪的数据或管道”，不能据此判定指针失效。
