"""PySide6 multi-ETF end-of-day research workbench with target-only detail."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from etf_t0.trend_research import CompletedUptrend
from etf_t0.workbench_service import ResearchWorkbenchService, TargetTrendDetail

SHANGHAI = ZoneInfo("Asia/Shanghai")


class TrendChart(QWidget):
    """Small dependency-free line chart for completed native close data."""

    def __init__(self) -> None:
        super().__init__()
        self._bars: tuple[tuple[str, str, str, str, str], ...] = ()
        self._intervals: tuple[CompletedUptrend, ...] = ()
        self.setMinimumHeight(260)
        self.setObjectName("trendChart")

    def set_detail(
        self,
        bars: tuple[tuple[str, str, str, str, str], ...],
        intervals: tuple[CompletedUptrend, ...],
    ) -> None:
        self._bars = bars
        self._intervals = intervals
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QPen(QColor("#d9e2ec"), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        if len(self._bars) < 2:
            painter.setPen(QPen(QColor("#52606d")))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无本地原生趋势数据")
            return
        closes = [Decimal(item[2]) for item in self._bars]
        lower = min(closes)
        upper = max(closes)
        if upper == lower:
            upper += Decimal("0.001")
            lower -= Decimal("0.001")
        rect = self.rect().adjusted(48, 22, -18, -34)
        points = []
        for index, close in enumerate(closes):
            x = rect.left() + rect.width() * index / (len(closes) - 1)
            ratio = float((close - lower) / (upper - lower))
            y = rect.bottom() - rect.height() * ratio
            points.append(QPointF(x, y))
        painter.setPen(QPen(QColor("#1f6feb"), 2))
        for first, second in zip(points, points[1:]):
            painter.drawLine(first, second)
        index_by_time = {item[0]: index for index, item in enumerate(self._bars)}
        painter.setPen(QPen(QColor("#18864b"), 4))
        for interval in self._intervals:
            start = index_by_time.get(interval.start_at)
            end = index_by_time.get(interval.end_at)
            if start is not None and end is not None:
                painter.drawLine(points[start], points[end])
        painter.setPen(QPen(QColor("#52606d"), 1))
        painter.drawText(8, 26, f"¥{upper:.3f}")
        painter.drawText(8, rect.bottom(), f"¥{lower:.3f}")
        painter.drawText(rect.left(), 16, "蓝：每分钟收盘价｜绿：收盘后上涨区间（非交易信号）")
        painter.drawText(
            rect.left(), self.height() - 10, self._bars[0][0][11:16] + " — " + self._bars[-1][0][11:16]
        )


class WorkbenchWindow(QMainWindow):
    """Show the research universe first, then one selected target’s local trend."""

    def __init__(self, *, service: ResearchWorkbenchService, trade_date: date) -> None:
        super().__init__()
        self._service = service
        self._latest_coverage = service.latest_complete_coverage()
        self._latest_complete_date = self._latest_coverage.trade_date
        self._trade_date = self._latest_complete_date or trade_date
        self._selected_code: str | None = None
        self.setWindowTitle("T+0 ETF 研究工作台 — M3｜本地数据｜不会自动下单")
        self.setMinimumSize(1180, 760)
        self.setCentralWidget(self._build_content())
        self._update_latest_date_controls()
        self._populate_instruments()
        self._apply_style()

    def _build_content(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("T+0 ETF 研究工作台")
        title.setObjectName("titleLabel")
        subtitle = QLabel(
            "16只研究标的｜收盘后原生趋势研究｜不会自动下单｜上涨区间不代表下一笔交易建议"
        )
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        date_controls = QHBoxLayout()
        date_controls.addWidget(QLabel("研究日期（可查看任意已完成交易日）："))
        date_picker = QDateEdit(
            QDate(self._trade_date.year, self._trade_date.month, self._trade_date.day)
        )
        date_picker.setObjectName("tradeDatePicker")
        date_picker.setCalendarPopup(True)
        date_picker.setDisplayFormat("yyyy-MM-dd")
        date_picker.dateChanged.connect(self._selected_date_changed)
        date_controls.addWidget(date_picker)
        latest_button = QPushButton("跳至最新有数据日")
        latest_button.setObjectName("latestDateButton")
        latest_button.clicked.connect(self._go_to_latest_complete_date)
        date_controls.addWidget(latest_button)
        latest_data_label = QLabel()
        latest_data_label.setObjectName("latestDataLabel")
        date_controls.addWidget(latest_data_label, 1)
        layout.addLayout(date_controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("workbenchSplitter")

        universe_panel = QFrame()
        universe_panel.setObjectName("universePanel")
        universe_layout = QVBoxLayout(universe_panel)
        universe_layout.setContentsMargins(14, 14, 14, 14)
        universe_layout.addWidget(QLabel("研究标的（点击一行查看详情）"))
        table = QTableWidget(0, 2)
        table.setObjectName("instrumentTable")
        table.setHorizontalHeaderLabels(["代码", "ETF名称"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(0, 82)
        table.itemSelectionChanged.connect(self._selected_row_changed)
        universe_layout.addWidget(table)
        splitter.addWidget(universe_panel)

        detail = QFrame()
        detail.setObjectName("detailArea")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_title = QLabel("选择一只ETF查看详情")
        detail_title.setObjectName("detailTitle")
        detail_status = QLabel("状态：等待选择")
        detail_status.setObjectName("detailStatus")
        detail_status.setWordWrap(True)
        detail_status.setMinimumHeight(detail_status.fontMetrics().lineSpacing() * 7 + 8)
        chart_caption = QLabel("收盘趋势：—")
        chart_caption.setObjectName("chartCaption")
        chart_caption.setWordWrap(True)
        day_stats = QLabel("今日变动：—")
        day_stats.setObjectName("dayStats")
        day_stats.setWordWrap(True)
        chart = TrendChart()
        trend_summary = QLabel("连续上涨区间：—")
        trend_summary.setObjectName("trendSummary")
        trend_summary.setWordWrap(True)
        interval_list = QTextEdit("区间明细：—")
        interval_list.setObjectName("intervalList")
        interval_list.setReadOnly(True)
        interval_list.setMinimumHeight(170)
        interval_list.setMaximumHeight(220)
        disclaimer = QLabel(
            "研究提示：图形和上涨区间仅由已完成原生分钟K线计算。"
            "没有对应盘口时，不能证明成交或短线利润。"
        )
        disclaimer.setObjectName("disclaimerLabel")
        disclaimer.setWordWrap(True)

        overview_panel, overview_layout = _section_panel("标的概览", "overviewPanel")
        overview_layout.addWidget(detail_title)
        overview_layout.addWidget(detail_status)
        chart_panel, chart_layout = _section_panel("价格与日内走势", "chartPanel")
        chart_layout.addWidget(chart_caption)
        chart_layout.addWidget(day_stats)
        chart_layout.addWidget(chart)
        interval_panel, interval_layout = _section_panel("连续上涨区间（收盘后研究）", "intervalPanel")
        interval_layout.addWidget(trend_summary)
        interval_layout.addWidget(interval_list)
        boundary_panel, boundary_layout = _section_panel("研究边界", "researchBoundaryPanel")
        boundary_layout.addWidget(disclaimer)
        for panel in (overview_panel, chart_panel, interval_panel, boundary_panel):
            detail_layout.addWidget(panel)
        detail_layout.addStretch(1)
        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("detailScrollArea")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_scroll.setWidget(detail)
        splitter.addWidget(detail_scroll)
        splitter.setSizes([430, 750])
        layout.addWidget(splitter, 1)

        reload_button = QPushButton("重新读取本机数据库")
        reload_button.setObjectName("reloadButton")
        reload_button.clicked.connect(self._reload_database)
        reload_status = QLabel("本机数据库：等待读取")
        reload_status.setObjectName("reloadStatusLabel")
        reload_status.setWordWrap(True)
        reload_row = QHBoxLayout()
        reload_row.addWidget(reload_status, 1)
        reload_row.addWidget(reload_button)
        layout.addLayout(reload_row)
        return root

    def _populate_instruments(self) -> None:
        table = self.findChild(QTableWidget, "instrumentTable")
        capabilities = self._service.list_instruments()
        table.setRowCount(len(capabilities))
        for row, item in enumerate(capabilities):
            values = (
                item.code,
                item.trading_name,
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.setColumnWidth(0, 82)

    def _reload_database(self) -> None:
        button = self.findChild(QPushButton, "reloadButton")
        status = self.findChild(QLabel, "reloadStatusLabel")
        button.setEnabled(False)
        button.setText("正在读取…")
        status.setText("正在读取本机数据库…")
        try:
            self._populate_instruments()
            self._latest_coverage = self._service.latest_complete_coverage()
            self._latest_complete_date = self._latest_coverage.trade_date
            self._update_latest_date_controls()
            if self._selected_code is not None:
                self.show_target(self._selected_code)
        except (OSError, sqlite3.Error, ValueError) as error:
            status.setText(f"读取失败：请确认数据库存在且未损坏后重试｜{error}")
        else:
            observed_at = datetime.now(SHANGHAI).strftime("%H:%M:%S")
            status.setText(f"读取成功：{observed_at}｜仅查询，未改写数据库")
        finally:
            button.setEnabled(True)
            button.setText("重新读取本机数据库")

    def _update_latest_date_controls(self) -> None:
        label = self.findChild(QLabel, "latestDataLabel")
        button = self.findChild(QPushButton, "latestDateButton")
        if self._latest_coverage.status == "WAIT_UNIVERSE_DATA":
            label.setText(
                f"本机标的台账不完整：期待 {self._latest_coverage.expected_instrument_count} 只"
            )
            button.setEnabled(False)
            return
        if self._latest_complete_date is None:
            label.setText("本机尚无完整ETF交易日")
            button.setEnabled(False)
            return
        label.setText(
            f"最新完整ETF数据：{self._latest_complete_date}｜"
            f"覆盖 {self._latest_coverage.complete_instrument_count}/"
            f"{self._latest_coverage.expected_instrument_count}"
        )
        button.setEnabled(True)

    def _go_to_latest_complete_date(self) -> None:
        if self._latest_complete_date is None:
            return
        selected = self._latest_complete_date
        self.findChild(QDateEdit, "tradeDatePicker").setDate(
            QDate(selected.year, selected.month, selected.day)
        )

    def _selected_row_changed(self) -> None:
        table = self.findChild(QTableWidget, "instrumentTable")
        selected = table.selectedItems()
        if selected:
            self.show_target(selected[0].text())

    def show_target(self, code: str) -> None:
        self._selected_code = code
        detail = self._service.target_detail(code, trade_date=self._trade_date)
        self._render_detail(detail)

    def _selected_date_changed(self, selected: QDate) -> None:
        self._trade_date = date(selected.year(), selected.month(), selected.day())
        if self._selected_code is not None:
            self.show_target(self._selected_code)

    def _render_detail(self, detail: TargetTrendDetail) -> None:
        self.findChild(QLabel, "detailTitle").setText(
            f"{detail.capability.code}｜{detail.capability.trading_name}"
        )
        status_text = {
            "RESEARCH_READY": "状态：本地收盘研究可用",
            "WAIT_UNIVERSE_DATA": (
                "状态：WAIT-UNIVERSE-DATA｜本机标的台账与版本化配置不一致，"
                "详情研究已阻断"
            ),
            "WAIT_COMPLETE_DAY": "状态：WAIT-COMPLETE-DAY｜本地序列不完整，不能生成收盘区间",
            "WAIT_DATA_QUALITY": (
                "状态：WAIT-DATA-QUALITY｜供应商开盘/最高/最低/"
                "收盘（OHLC）字段异常，不能生成趋势区间"
            ),
            "WAIT_DATA": "状态：WAIT-DATA｜该ETF没有可用本地当日序列",
        }[detail.status]
        self.findChild(QLabel, "detailStatus").setText(
            f"{status_text}\n资格：{detail.capability.t0_evidence_status}｜"
            f"状态：{_format_security_status(detail.capability.security_status)}｜"
            f"复核：{detail.capability.last_review_date}\n"
            f"模式：{detail.capability.paper_policy_status}\n"
            f"门禁：{detail.capability.research_gate_status}\n"
            "费用：仅使用用户待核实的券商费率；本页不生成买卖价格。"
        )
        self.findChild(QLabel, "chartCaption").setText(
            f"{detail.trade_date}｜原生{detail.interval_minutes}分钟"
            f"{'收盘趋势' if detail.status == 'RESEARCH_READY' else '未完成日内趋势'}"
            if detail.interval_minutes is not None
            else f"{detail.trade_date}｜无原生1分钟或5分钟数据"
        )
        self.findChild(TrendChart, "trendChart").set_detail(
            detail.bars, detail.completed_uptrends
        )
        self.findChild(QLabel, "dayStats").setText(_format_day_stats(detail))
        summary = (
            f"连续上涨区间：{len(detail.completed_uptrends)} 个｜"
            "仅收盘价描述，未证明可成交利润"
            if detail.status == "RESEARCH_READY"
            else "连续上涨区间：识别未运行"
        )
        self.findChild(QLabel, "trendSummary").setText(summary)
        self.findChild(QTextEdit, "intervalList").setPlainText(_format_intervals(detail))

    def detail_text_snapshot(self) -> str:
        widgets: Iterable[QWidget] = self.findChildren(QWidget)
        return "\n".join(widget.text() for widget in widgets if isinstance(widget, QLabel))

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f4f6f8; }
            QLabel { color: #1f2933; font-size: 13px; }
            QLabel#titleLabel { font-size: 26px; font-weight: 700; }
            QLabel#subtitleLabel { color: #52606d; }
            QLabel#detailTitle { font-size: 20px; font-weight: 650; }
            QFrame#universePanel, QFrame#detailArea { background: transparent; }
            QFrame#overviewPanel, QFrame#chartPanel, QFrame#intervalPanel,
            QFrame#researchBoundaryPanel { background: white; border: 1px solid #d9e2ec;
                                           border-radius: 10px; padding: 8px; }
            QLabel#sectionTitle { font-size: 15px; font-weight: 700; color: #243b53;
                                  padding-bottom: 3px; }
            QTableWidget { background: white; border: 1px solid #d9e2ec; }
            QTextEdit#intervalList { background: #f8fafc; border: 1px solid #d9e2ec;
                                     border-radius: 6px; padding: 6px; }
            QPushButton { padding: 8px 14px; background: #1f6feb; color: white;
                          border: 0; border-radius: 6px; font-weight: 650; }
            QLabel#disclaimerLabel { background: #fff8e6; color: #7c4a03;
                                     padding: 8px; border-radius: 6px; }
            """
        )


def _section_panel(title: str, object_name: str) -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName(object_name)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(14, 12, 14, 12)
    heading = QLabel(title)
    heading.setObjectName("sectionTitle")
    layout.addWidget(heading)
    return panel, layout


def _format_security_status(status: str) -> str:
    return {
        "listed": "正常上市（listed）",
        "suspended": "停牌（suspended）",
        "delisted": "已终止上市（delisted）",
        "unknown": "待核验（unknown）",
    }.get(status, f"待核验（{status}）")


def _format_intervals(detail: TargetTrendDetail) -> str:
    if not detail.completed_uptrends:
        if detail.status == "WAIT_DATA":
            return "区间识别未运行：该日期没有该ETF的本地原生1分钟或5分钟数据。"
        if detail.status == "WAIT_UNIVERSE_DATA":
            return "区间识别未运行：本机标的台账与版本化配置不一致。"
        if detail.status == "WAIT_COMPLETE_DAY":
            return "区间识别未运行：该日原生序列不完整。"
        if detail.status == "WAIT_DATA_QUALITY":
            return "区间识别未运行：开盘/最高/最低/收盘（OHLC）字段未通过数据质量检查。"
        return "区间明细：未达到当前预设的持续时间、涨幅和回撤条件。"
    rows = ["区间明细（均为收盘后描述性结果）："]
    for interval in detail.completed_uptrends:
        rows.append(
            f"{interval.start_at[11:16]}–{interval.end_at[11:16]}｜"
            f"{interval.duration_bars}根｜涨幅 {interval.rise_bps:.1f}bp｜"
            f"最大回撤 {interval.maximum_pullback_bps:.1f}bp｜"
            "无盘口可执行性证据"
        )
    return "\n".join(rows)


def _format_day_stats(detail: TargetTrendDetail) -> str:
    if not detail.bars:
        return "今日变动：等待本地原生数据。"
    opening = Decimal(detail.bars[0][1])
    latest = Decimal(detail.bars[-1][2])
    if opening <= 0:
        return (
            f"日内统计：数据质量异常｜首根开盘字段为 {opening}，"
            f"不计算涨跌幅｜末根收盘 {latest:.3f}"
        )
    high = max(Decimal(item[3]) for item in detail.bars)
    low = min(Decimal(item[4]) for item in detail.bars)
    change_bps = (latest - opening) / opening * Decimal(10_000)
    direction = "+" if change_bps >= 0 else ""
    return (
        f"今日变动：开盘 {opening:.3f}｜收盘 {latest:.3f}｜"
        f"{direction}{change_bps:.1f}bp｜日内高/低 {high:.3f}/{low:.3f}"
    )
