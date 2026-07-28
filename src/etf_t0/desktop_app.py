"""PySide6 target-only observation window for the fixed M1 demo fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from etf_t0.observation import (
    DecisionStatus,
    ObservationMode,
    TargetObservationDecision,
    TargetObservationRequest,
    TargetObservationService,
    create_m1_fixture_service,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class StartupFailureObservationService:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def evaluate(self, request: TargetObservationRequest) -> TargetObservationDecision:
        now = datetime.now(SHANGHAI)
        return TargetObservationDecision(
            decision_id=f"startup-failure-{now.strftime('%Y%m%dT%H%M%S.%f%z')}",
            etf_code=request.etf_code,
            etf_name=None,
            mode=ObservationMode.PAPER_OBSERVATION,
            status=DecisionStatus.NO_GO,
            target_bid=None,
            target_ask=None,
            buy_observation_ceiling=None,
            strategy_exit_level=None,
            break_even_reference=None,
            estimated_quantity=None,
            round_trip_cost_cny=None,
            fee_profile_status=None,
            policy_version=None,
            data_snapshot_id=None,
            eligibility_evidence_id=None,
            eligibility_reviewed_on=None,
            policy_metadata=None,
            generated_at=now,
            reasons=(f"启动配置加载失败：{self._reason}",),
            feed_label="UNVERIFIED RESEARCH FEED",
        )


def create_desktop_service(*, workspace: Path):
    from etf_t0.live_observation import create_current_research_service

    try:
        return create_current_research_service(workspace=workspace)
    except Exception as exc:  # noqa: BLE001 - GUI startup must remain visible
        return StartupFailureObservationService(str(exc))


class RefreshSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class RefreshWorker(QRunnable):
    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback
        self.signals = RefreshSignals()

    def run(self) -> None:
        try:
            self.signals.completed.emit(self._callback())
        except Exception as exc:  # noqa: BLE001 - last-resort GUI boundary
            self.signals.failed.emit(str(exc))


class ObservationWindow(QMainWindow):
    """Render one target ETF decision without broker or proxy controls."""

    def __init__(
        self,
        service: TargetObservationService,
        *,
        background_refresh: bool = False,
        journal_directory: Path | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._background_refresh = background_refresh
        self._journal_directory = journal_directory
        self._rendered_feed_label: str | None = None
        self._refresh_worker: RefreshWorker | None = None
        self._refresh_generation = 0
        self._rendered_snapshot_id: str | None = None
        self._expired_snapshot_ids: set[str] = set()
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setSingleShot(True)
        self._expiry_timer.timeout.connect(self._expire_rendered_prices)
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(15_000)
        self._auto_refresh_timer.timeout.connect(self.evaluate_current_symbol)
        application = QApplication.instance()
        if application is not None:
            application.applicationStateChanged.connect(
                self._application_state_changed
            )
        self.setWindowTitle(
            "T+0 ETF 人工观察台 — M2 当前纸面观察"
            if background_refresh
            else "T+0 ETF 人工观察台 — M1 固定夹具"
        )
        self.setMinimumSize(760, 700)
        self.setCentralWidget(self._build_content())
        self._apply_style()

    def _build_content(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("T+0 ETF 人工观察台")
        title.setObjectName("titleLabel")
        subtitle = QLabel(
            "M2｜UNVERIFIED RESEARCH FEED｜实盘准入 ✕｜不会自动下单"
            if self._background_refresh
            else "M1 固定测试夹具｜纸面观察｜不连接券商｜不会自动下单"
        )
        subtitle.setObjectName("subtitleLabel")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("目标 ETF 代码"))
        symbol_input = QLineEdit()
        symbol_input.setObjectName("symbolInput")
        symbol_input.setPlaceholderText("例如 159570")
        symbol_input.setMaxLength(6)
        symbol_input.textChanged.connect(self._input_changed)
        symbol_input.returnPressed.connect(self.evaluate_current_symbol)
        input_row.addWidget(symbol_input, 1)
        evaluate_button = QPushButton("开始观察")
        evaluate_button.setObjectName("evaluateButton")
        evaluate_button.clicked.connect(self.evaluate_current_symbol)
        input_row.addWidget(evaluate_button)
        layout.addLayout(input_row)

        identity = QLabel("请输入目标 ETF 代码")
        identity.setObjectName("identityLabel")
        status = QLabel("等待输入")
        status.setObjectName("statusLabel")
        gates = QLabel("T+0资格 —｜连续竞价 —｜固定夹具 —｜实盘准入 ✕")
        gates.setObjectName("gatesLabel")
        layout.addWidget(identity)
        layout.addWidget(status)
        layout.addWidget(gates)

        cards = QFrame()
        cards.setObjectName("cardsFrame")
        cards.setMinimumHeight(140)
        card_layout = QGridLayout(cards)
        card_layout.addWidget(QLabel("观察买入价上限"), 0, 0)
        card_layout.addWidget(QLabel("策略卖出观察线"), 0, 1)
        buy_level = QLabel("—")
        buy_level.setObjectName("buyLevelLabel")
        buy_level.setMinimumHeight(56)
        exit_level = QLabel("—")
        exit_level.setObjectName("exitLevelLabel")
        exit_level.setMinimumHeight(56)
        card_layout.addWidget(buy_level, 1, 0)
        card_layout.addWidget(exit_level, 1, 1)
        layout.addWidget(cards)

        for object_name, initial in (
            ("quoteLabel", "目标买一/卖一：—"),
            ("breakEvenLabel", "盈亏平衡参考：—"),
            ("validUntilLabel", "价格有效期：—"),
            ("reasonsLabel", "说明：这是固定夹具，不是实时行情。"),
        ):
            label = QLabel(initial)
            label.setObjectName(object_name)
            label.setWordWrap(True)
            layout.addWidget(label)

        disclaimer = QLabel("研究观察，不构成实盘准入、收益承诺或下单指令。")
        disclaimer.setObjectName("disclaimerLabel")
        disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(disclaimer)
        return root

    def evaluate_current_symbol(self) -> None:
        symbol_input = self.findChild(QLineEdit, "symbolInput")
        request = TargetObservationRequest(etf_code=symbol_input.text().strip())
        if not self._background_refresh:
            self.render_decision(self._service.evaluate(request))
            return
        if self._refresh_worker is not None:
            return
        button = self.findChild(QPushButton, "evaluateButton")
        button.setEnabled(False)
        button.setText("正在刷新…")
        self._refresh_generation += 1
        generation = self._refresh_generation
        requested_code = request.etf_code
        worker = RefreshWorker(lambda: self._service.evaluate(request))
        worker.signals.completed.connect(
            lambda decision: self._finish_refresh(
                decision, generation, requested_code
            )
        )
        worker.signals.failed.connect(
            lambda error: self._refresh_failed(error, generation, requested_code)
        )
        self._refresh_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _input_changed(self) -> None:
        self._refresh_generation += 1
        self._auto_refresh_timer.stop()
        self._render_fail_closed(
            reason="ETF代码已变化，请重新刷新。",
            gates_text="输入已变化 ✕｜实盘准入 ✕",
        )

    def _finish_refresh(
        self,
        decision: TargetObservationDecision,
        generation: int,
        requested_code: str,
    ) -> None:
        self._refresh_worker = None
        button = self.findChild(QPushButton, "evaluateButton")
        button.setEnabled(True)
        button.setText("刷新观察")
        current_code = self.findChild(QLineEdit, "symbolInput").text().strip()
        if generation != self._refresh_generation or current_code != requested_code:
            self._render_fail_closed(
                reason="ETF代码在刷新期间已变化，旧结果已丢弃。",
                gates_text="输入已变化 ✕｜实盘准入 ✕",
            )
            return
        self.render_decision(decision)
        symbol = self.findChild(QLineEdit, "symbolInput").text().strip()
        if symbol == "159570":
            self._auto_refresh_timer.start()
        else:
            self._auto_refresh_timer.stop()

    def _refresh_failed(
        self, error: str, generation: int, requested_code: str
    ) -> None:
        self._refresh_worker = None
        button = self.findChild(QPushButton, "evaluateButton")
        button.setEnabled(True)
        button.setText("重试刷新")
        self._auto_refresh_timer.stop()
        current_code = self.findChild(QLineEdit, "symbolInput").text().strip()
        if generation != self._refresh_generation or current_code != requested_code:
            self._render_fail_closed(
                reason="ETF代码在刷新期间已变化，旧错误已丢弃。",
                gates_text="输入已变化 ✕｜实盘准入 ✕",
            )
            return
        self._render_fail_closed(
            reason=f"桌面刷新任务失败：{error}",
            gates_text="刷新任务 ✕｜实盘准入 ✕",
        )

    def render_decision(self, decision: TargetObservationDecision) -> None:
        identity = self.findChild(QLabel, "identityLabel")
        status = self.findChild(QLabel, "statusLabel")
        gates = self.findChild(QLabel, "gatesLabel")
        buy_level = self.findChild(QLabel, "buyLevelLabel")
        exit_level = self.findChild(QLabel, "exitLevelLabel")
        quote = self.findChild(QLabel, "quoteLabel")
        break_even = self.findChild(QLabel, "breakEvenLabel")
        valid_until = self.findChild(QLabel, "validUntilLabel")
        reasons = self.findChild(QLabel, "reasonsLabel")

        if not self._write_decision_audit(decision):
            self._render_fail_closed(
                reason="本地决策审计记录写入失败，已 fail-closed。",
                gates_text="本地审计 ✕｜实盘准入 ✕",
            )
            return
        self._rendered_feed_label = decision.feed_label

        identity.setText(
            f"{decision.etf_code}  {decision.etf_name}"
            if decision.etf_name
            else decision.etf_code or "未输入代码"
        )
        if decision.status is DecisionStatus.NO_GO:
            self._render_fail_closed(
                reason="；".join(decision.reasons),
                gates_text="目标资格/策略/时段未全部通过｜实盘准入 ✕",
            )
            return

        if decision.status is DecisionStatus.WAIT_DATA:
            if (
                decision.data_snapshot_id is None
                or decision.data_valid_until is None
                or decision.target_bid is None
                or decision.target_ask is None
            ):
                self._render_fail_closed(
                    reason="WAIT-DATA 快照字段不完整，已 fail-closed。",
                    gates_text="数据快照 ✕｜实盘准入 ✕",
                )
                return
            status.setText("WAIT-DATA｜仅显示诊断行情｜策略价格禁用")
            gates.setText(
                f"因果5分钟bar "
                f"{decision.signal_bar_count or 0}/{decision.signal_bar_required or 48}"
                "｜跨源/深度门禁 ✕｜实盘准入 ✕"
            )
            buy_level.setText("—")
            exit_level.setText("—")
            iopv = (
                f"｜IOPV {decision.target_iopv:.4f}"
                if decision.target_iopv is not None
                else "｜IOPV —"
            )
            quote.setText(
                f"目标买一/卖一：{decision.target_bid:.3f} / "
                f"{decision.target_ask:.3f}{iopv}"
            )
            break_even.setText("盈亏平衡参考：—｜费用与执行门禁未全部通过")
            valid_until.setText(
                "诊断行情有效期："
                + decision.data_valid_until.strftime("%Y-%m-%d %H:%M:%S")
            )
            reasons.setText(
                f"数据标签：{decision.feed_label or 'UNVERIFIED RESEARCH FEED'}｜"
                "原因："
                + "；".join(decision.reasons)
            )
            self.findChild(QLabel, "subtitleLabel").setText(
                "M2｜UNVERIFIED RESEARCH FEED｜纸面观察｜实盘准入 ✕"
            )
            self._rendered_snapshot_id = decision.data_snapshot_id
            remaining_ms = max(
                0,
                int((decision.data_valid_until - decision.generated_at).total_seconds() * 1000),
            )
            self._expiry_timer.start(remaining_ms)
            return

        self._render_actionable_decision(decision)

    def _write_decision_audit(self, decision: TargetObservationDecision) -> bool:
        if self._journal_directory is None:
            return True

        def decimal_text(value) -> str | None:
            return str(value) if value is not None else None

        payload = {
            "decision_id": decision.decision_id,
            "etf_code": decision.etf_code,
            "etf_name": decision.etf_name,
            "mode": decision.mode.value,
            "status": decision.status.value,
            "target_bid": decimal_text(decision.target_bid),
            "target_ask": decimal_text(decision.target_ask),
            "target_iopv": decimal_text(decision.target_iopv),
            "buy_observation_ceiling": decimal_text(
                decision.buy_observation_ceiling.price
                if decision.buy_observation_ceiling
                else None
            ),
            "strategy_exit_level": decimal_text(
                decision.strategy_exit_level.price
                if decision.strategy_exit_level
                else None
            ),
            "break_even_reference": decimal_text(decision.break_even_reference),
            "estimated_quantity": decision.estimated_quantity,
            "round_trip_cost_cny": decimal_text(decision.round_trip_cost_cny),
            "fee_profile_status": (
                decision.fee_profile_status.value if decision.fee_profile_status else None
            ),
            "policy_version": decision.policy_version,
            "data_snapshot_id": decision.data_snapshot_id,
            "eligibility_evidence_id": decision.eligibility_evidence_id,
            "eligibility_reviewed_on": (
                decision.eligibility_reviewed_on.isoformat()
                if decision.eligibility_reviewed_on
                else None
            ),
            "generated_at": decision.generated_at.isoformat(),
            "data_valid_until": (
                decision.data_valid_until.isoformat() if decision.data_valid_until else None
            ),
            "feed_label": decision.feed_label,
            "signal_bar_count": decision.signal_bar_count,
            "signal_bar_required": decision.signal_bar_required,
            "data_gate_reasons": list(decision.data_gate_reasons),
            "config_sha256": decision.config_sha256,
            "policy_metadata": (
                {
                    "hypothesis_id": decision.policy_metadata.hypothesis_id,
                    "family": decision.policy_metadata.family,
                    "anchor_formula": decision.policy_metadata.anchor_formula,
                    "signal_timing": decision.policy_metadata.signal_timing,
                    "training_window": decision.policy_metadata.training_window,
                    "parameter_search_log": decision.policy_metadata.parameter_search_log,
                    "frozen_at": decision.policy_metadata.frozen_at.isoformat(),
                    "forward_sample_start_date": (
                        decision.policy_metadata.forward_sample_start_date.isoformat()
                    ),
                    "research_source_git_commit": (
                        decision.policy_metadata.research_source_git_commit
                    ),
                    "strategy_source_blob": decision.policy_metadata.strategy_source_blob,
                    "data_hash": decision.policy_metadata.data_hash,
                    "validation_status": decision.policy_metadata.validation_status,
                }
                if decision.policy_metadata
                else None
            ),
            "reasons": list(decision.reasons),
        }
        digest = hashlib.sha256(decision.decision_id.encode("utf-8")).hexdigest()[:20]
        target = self._journal_directory / f"{digest}-{uuid.uuid4().hex}.json"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._journal_directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(target)
            return True
        except OSError:
            return False
        finally:
            if temporary.exists():
                temporary.unlink()

    def _render_actionable_decision(self, decision: TargetObservationDecision) -> None:
        if decision.data_snapshot_id is None:
            self._render_fail_closed(
                reason="数据快照证据缺失，已 fail-closed。",
                gates_text="数据快照 ✕｜实盘准入 ✕",
            )
            return
        if decision.data_snapshot_id in self._expired_snapshot_ids:
            self._rendered_snapshot_id = decision.data_snapshot_id
            self._expire_rendered_prices()
            return
        if decision.eligibility_evidence_id is None:
            self._render_fail_closed(
                reason="资格证据缺失，已 fail-closed。",
                gates_text="交易所T+0台账资格（夹具） ✕｜实盘准入 ✕",
            )
            return

        assert decision.buy_observation_ceiling is not None
        assert decision.strategy_exit_level is not None
        assert decision.target_bid is not None
        assert decision.target_ask is not None
        assert decision.break_even_reference is not None
        status = self.findChild(QLabel, "statusLabel")
        gates = self.findChild(QLabel, "gatesLabel")
        buy_level = self.findChild(QLabel, "buyLevelLabel")
        exit_level = self.findChild(QLabel, "exitLevelLabel")
        quote = self.findChild(QLabel, "quoteLabel")
        break_even = self.findChild(QLabel, "breakEvenLabel")
        valid_until = self.findChild(QLabel, "validUntilLabel")
        reasons = self.findChild(QLabel, "reasonsLabel")
        is_research_feed = decision.feed_label is not None
        status.setText("纸面观察｜等待目标卖一价进入观察区｜实盘准入未通过")
        gates.setText(
            (
                "交易所T+0台账 ✓｜因果信号 ✓｜成本压力 ✓｜"
                "UNVERIFIED RESEARCH FEED｜实盘准入 ✕"
            )
            if is_research_feed
            else "交易所T+0台账资格（夹具） ✓｜连续竞价 ✓｜固定夹具 ✓｜实盘准入 ✕"
        )
        buy_level.setText(f"≤ {decision.buy_observation_ceiling.price:.3f}")
        exit_level.setText(f"≥ {decision.strategy_exit_level.price:.3f}")
        quote.setText(f"目标买一/卖一：{decision.target_bid:.3f} / {decision.target_ask:.3f}")
        break_even_label = (
            "压力成本盈亏平衡线"
            if is_research_feed
            else "最低佣金口径盈亏平衡下界"
        )
        break_even.setText(
            f"费用档案：{decision.fee_profile_status.value}｜"
            f"{break_even_label}："
            f"{decision.break_even_reference:.3f}｜"
            f"计划数量：{decision.estimated_quantity:,} 份"
        )
        valid_until.setText(
            "价格有效期："
            + decision.buy_observation_ceiling.valid_until.strftime("%Y-%m-%d %H:%M:%S")
        )
        reasons.setText(
            (
                f"数据标签：{decision.feed_label}｜说明："
                + "；".join(decision.reasons)
            )
            if is_research_feed
            else (
                "说明：M1 固定夹具仅验证界面和门禁；费用为临时下界，"
                "未含滑点、排队和部分成交；不代表当前行情。"
            )
        )
        if is_research_feed:
            self.findChild(QLabel, "subtitleLabel").setText(
                "M2｜UNVERIFIED RESEARCH FEED｜纸面观察｜实盘准入 ✕"
            )
        self._rendered_snapshot_id = decision.data_snapshot_id
        remaining_ms = max(
            0,
            int(
                (
                    decision.buy_observation_ceiling.valid_until - decision.generated_at
                ).total_seconds()
                * 1000
            ),
        )
        self._expiry_timer.start(remaining_ms)

    def _render_fail_closed(self, *, reason: str, gates_text: str) -> None:
        self._expiry_timer.stop()
        self._rendered_snapshot_id = None
        self._rendered_feed_label = None
        self.findChild(QLabel, "statusLabel").setText("NO-GO｜不显示或沿用价格")
        self.findChild(QLabel, "gatesLabel").setText(gates_text)
        self.findChild(QLabel, "buyLevelLabel").setText("—")
        self.findChild(QLabel, "exitLevelLabel").setText("—")
        self.findChild(QLabel, "quoteLabel").setText("目标买一/卖一：—")
        self.findChild(QLabel, "breakEvenLabel").setText("盈亏平衡参考：—")
        self.findChild(QLabel, "validUntilLabel").setText("价格有效期：—")
        self.findChild(QLabel, "reasonsLabel").setText("原因：" + reason)

    def _expire_rendered_prices(self, reason: str | None = None) -> None:
        if self._rendered_snapshot_id is not None:
            self._expired_snapshot_ids.add(self._rendered_snapshot_id)
        self._rendered_snapshot_id = None
        is_research_feed = self._rendered_feed_label is not None
        self.findChild(QLabel, "statusLabel").setText(
            "诊断行情已过期｜策略价格禁用"
            if is_research_feed
            else "纸面观察价格已过期｜不显示或沿用价格"
        )
        self.findChild(QLabel, "gatesLabel").setText(
            "研究行情新鲜度 ✕｜策略价格禁用｜实盘准入 ✕"
            if is_research_feed
            else "交易所T+0台账资格（夹具） ✓｜固定夹具已过期 ✕｜实盘准入 ✕"
        )
        self.findChild(QLabel, "buyLevelLabel").setText("—")
        self.findChild(QLabel, "exitLevelLabel").setText("—")
        self.findChild(QLabel, "quoteLabel").setText("目标买一/卖一：—")
        self.findChild(QLabel, "breakEvenLabel").setText("盈亏平衡参考：—")
        self.findChild(QLabel, "validUntilLabel").setText("价格有效期：—")
        self.findChild(QLabel, "reasonsLabel").setText(
            "原因："
            + (
                reason
                or (
                    "诊断行情超过30秒，等待下一次刷新。"
                    if is_research_feed
                    else "价格已过期；该夹具快照不可重放。"
                )
            )
        )

    def _application_state_changed(self, state: Qt.ApplicationState) -> None:
        if state is Qt.ApplicationState.ApplicationActive:
            symbol = self.findChild(QLineEdit, "symbolInput").text().strip()
            if self._background_refresh and symbol == "159570":
                self.evaluate_current_symbol()
            return
        if (
            self._rendered_snapshot_id is not None
        ):
            self._auto_refresh_timer.stop()
            self._expire_rendered_prices("应用进入后台或系统休眠，原行情已清除。")

    def text_snapshot(self) -> str:
        widgets: Iterable[QWidget] = self.findChildren(QWidget)
        return "\n".join(
            widget.text()
            for widget in widgets
            if isinstance(widget, (QLabel, QLineEdit, QPushButton))
        )

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f4f6f8; }
            QLabel { color: #1f2933; font-size: 14px; }
            QLabel#titleLabel { font-size: 26px; font-weight: 700; }
            QLabel#subtitleLabel { color: #52606d; }
            QLabel#identityLabel { font-size: 20px; font-weight: 650; }
            QLabel#statusLabel { color: #8a4b08; font-weight: 650; }
            QLineEdit { padding: 10px; border: 1px solid #bcccdc; border-radius: 6px; }
            QPushButton { padding: 10px 18px; background: #1f6feb; color: white;
                          border: 0; border-radius: 6px; font-weight: 650; }
            QFrame#cardsFrame { background: white; border: 1px solid #d9e2ec;
                                border-radius: 10px; padding: 14px; }
            QLabel#buyLevelLabel, QLabel#exitLevelLabel {
                font-size: 30px; font-weight: 750; color: #102a43; padding: 12px 0;
            }
            QLabel#disclaimerLabel { background: #fff8e6; color: #7c4a03;
                                     padding: 10px; border-radius: 6px; }
            """
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--single-observation",
        action="store_true",
        help="open the M2 single-target current-paper observation screen",
    )
    parser.add_argument("--workspace", type=Path)
    args, qt_args = parser.parse_known_args()
    app = QApplication([sys.argv[0], *qt_args])
    workspace = args.workspace or Path(__file__).resolve().parents[2]
    if not args.demo and not args.single_observation:
        from etf_t0.research_workbench import bootstrap_workspace_database
        from etf_t0.workbench_app import WorkbenchWindow
        from etf_t0.workbench_service import (
            ResearchWorkbenchService,
            load_trend_detection_parameters,
        )

        store = bootstrap_workspace_database(workspace=workspace)
        workbench = WorkbenchWindow(
            service=ResearchWorkbenchService(
                store=store,
                parameters=load_trend_detection_parameters(
                    workspace / "config/trend_detection.json"
                ),
                clock=lambda: datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            ),
            trade_date=datetime.now(SHANGHAI).date(),
        )
        workbench.show()
        raise SystemExit(app.exec())
    journal_directory = None
    if args.demo:
        fixture_start = datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
        monotonic_start = monotonic()

        def advancing_fixture_clock() -> datetime:
            return fixture_start + timedelta(seconds=monotonic() - monotonic_start)

        service = create_m1_fixture_service(clock=advancing_fixture_clock)
    else:
        service = create_desktop_service(workspace=workspace)
        journal_directory = workspace / "reports" / "generated" / "desktop_decisions"
    window = ObservationWindow(
        service,
        background_refresh=not args.demo,
        journal_directory=journal_directory,
    )
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
