# 数据版本规则

## 目录

| 目录 | 内容 | Git 规则 |
| --- | --- | --- |
| `data/raw/` | 原始行情、原始 IOPV、原始盘口快照 | 不提交数据文件；使用 DVC 追踪版本。 |
| `data/interim/` | 清洗但未建模的数据 | 不提交数据文件；使用 DVC 追踪版本。 |
| `data/processed/` | 可复现的分析数据集 | 不提交数据文件；使用 DVC 追踪版本。 |
| `data/external/` | 允许使用的第三方静态输入 | 默认不提交；小型公开许可样本可例外。 |
| `reports/generated/` | 可再生成的大型报告与图表 | 不提交。 |

## 本机工作流

1. 将数据保存到对应目录，并记录来源、时区、单位、时间范围和抓取时间。
2. 完成质量检查后，以 DVC 添加完整的数据集，而不是单个临时文件。
3. 将生成的 `.dvc` 元数据文件、数据字典和质量报告提交到 Git。
4. 在每份研究报告中记录所用数据集的 Git commit 与 DVC 指针。
5. 不执行 DVC garbage collection，除非确认不再需要旧数据版本。

## M3 SQLite 研究数据库

`data/processed/research_workbench.sqlite3` 是本机查询数据库，不进入 Git。它保存标准化的ETF身份、采集运行、原始文件路径与SHA-256、原生bar vintage、质量摘要和收盘后趋势区间；原始JSON仍位于 `data/raw/`，标准化CSV仍位于 `data/interim/`。

数据库可以由本机文件重建：

```bash
PYTHONPATH=src uv run python -m etf_t0.research_workbench bootstrap
```

`bootstrap` 是唯一的 schema 建立/迁移与重建路径。默认桌面进程以 SQLite `mode=ro`
打开数据库，列表、目标详情和“重新读取”都不允许建表、迁移或改写。首次启动前若数据库不存在，必须先显式运行上述命令。

独立行情和券商留证的目录、脱敏与版本边界见
[`EXECUTION_EVIDENCE_PLAN.md`](EXECUTION_EVIDENCE_PLAN.md)。

工作日15:07的本地定时任务会运行 `collect-one-minute`，逐只保存16只ETF的供应商原生一分钟窗口。趋势区间只使用已完成bar收盘价，并保存检测参数版本、输入截止时间和计算时间；它不包含任何券商账号或自动下单记录。

## 禁止提交

- API Key、Token、Cookie、账号信息。
- 未脱敏券商交割单和账户截图。
- 受供应商授权限制而不能再分发的数据。
- 运行缓存、临时下载、浏览器导出和中间调试文件。

## 本机存储限制

当前没有 DVC 远端备份。数据版本仅在本机可恢复；需要跨设备同步或灾备前，必须先新增私有 DVC remote 并补充 ADR。
