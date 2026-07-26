"""PySide6 target-only observation window for the fixed M1 demo fixture."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from datetime import datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer
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
    TargetObservationDecision,
    TargetObservationRequest,
    TargetObservationService,
    create_m1_fixture_service,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ObservationWindow(QMainWindow):
    """Render one target ETF decision without broker or proxy controls."""

    def __init__(self, service: TargetObservationService) -> None:
        super().__init__()
        self._service = service
        self._rendered_snapshot_id: str | None = None
        self._expired_snapshot_ids: set[str] = set()
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setSingleShot(True)
        self._expiry_timer.timeout.connect(self._expire_rendered_prices)
        self.setWindowTitle("T+0 ETF 人工观察台 — M1 固定夹具")
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
        subtitle = QLabel("M1 固定测试夹具｜纸面观察｜不连接券商｜不会自动下单")
        subtitle.setObjectName("subtitleLabel")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("目标 ETF 代码"))
        symbol_input = QLineEdit()
        symbol_input.setObjectName("symbolInput")
        symbol_input.setPlaceholderText("例如 159570")
        symbol_input.setMaxLength(6)
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
        decision = self._service.evaluate(
            TargetObservationRequest(etf_code=symbol_input.text().strip())
        )
        self.render_decision(decision)

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
        status.setText("纸面观察｜等待目标卖一价进入观察区｜实盘准入未通过")
        gates.setText(
            "交易所T+0台账资格（夹具） ✓｜连续竞价 ✓｜固定夹具 ✓｜实盘准入 ✕"
        )
        buy_level.setText(f"≤ {decision.buy_observation_ceiling.price:.3f}")
        exit_level.setText(f"≥ {decision.strategy_exit_level.price:.3f}")
        quote.setText(f"目标买一/卖一：{decision.target_bid:.3f} / {decision.target_ask:.3f}")
        break_even.setText(
            f"费用档案：{decision.fee_profile_status.value}｜"
            f"最低佣金口径盈亏平衡下界：{decision.break_even_reference:.3f}｜"
            f"计划数量：{decision.estimated_quantity:,} 份"
        )
        valid_until.setText(
            "价格有效期："
            + decision.buy_observation_ceiling.valid_until.strftime("%Y-%m-%d %H:%M:%S")
        )
        reasons.setText(
            "说明：M1 固定夹具仅验证界面和门禁；费用为临时下界，"
            "未含滑点、排队和部分成交；不代表当前行情。"
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
        self.findChild(QLabel, "statusLabel").setText(
            "纸面观察价格已过期｜不显示或沿用价格"
        )
        self.findChild(QLabel, "gatesLabel").setText(
            "交易所T+0台账资格（夹具） ✓｜固定夹具已过期 ✕｜实盘准入 ✕"
        )
        self.findChild(QLabel, "buyLevelLabel").setText("—")
        self.findChild(QLabel, "exitLevelLabel").setText("—")
        self.findChild(QLabel, "quoteLabel").setText("目标买一/卖一：—")
        self.findChild(QLabel, "breakEvenLabel").setText("盈亏平衡参考：—")
        self.findChild(QLabel, "validUntilLabel").setText("价格有效期：—")
        self.findChild(QLabel, "reasonsLabel").setText(
            "原因：" + (reason or "价格已过期；该夹具快照不可重放。")
        )

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
    app = QApplication(sys.argv)
    fixture_start = datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    monotonic_start = monotonic()

    def advancing_fixture_clock() -> datetime:
        return fixture_start + timedelta(seconds=monotonic() - monotonic_start)

    service = create_m1_fixture_service(clock=advancing_fixture_clock)
    window = ObservationWindow(service)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
