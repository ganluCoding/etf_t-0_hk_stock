import json
import os
import threading
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QPushButton

from etf_t0.desktop_app import ObservationWindow, create_desktop_service
from etf_t0.observation import (
    DecisionStatus,
    ObservationMode,
    TargetObservationDecision,
    TargetObservationRequest,
    create_m1_fixture_service,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_window_renders_target_only_paper_observation() -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    window = ObservationWindow(service)
    window.show()

    window.findChild(QLineEdit, "symbolInput").setText("159570")
    window.findChild(QPushButton, "evaluateButton").click()
    app.processEvents()

    assert "纸面观察" in window.findChild(QLabel, "statusLabel").text()
    assert window.findChild(QLabel, "buyLevelLabel").text() == "≤ 1.413"
    assert window.findChild(QLabel, "exitLevelLabel").text() == "≥ 1.417"
    assert "159570" in window.findChild(QLabel, "identityLabel").text()
    assert window.findChild(QLabel, "gatesLabel").text() == (
        "交易所T+0台账资格（夹具） ✓｜连续竞价 ✓｜固定夹具 ✓｜实盘准入 ✕"
    )
    assert "PROVISIONAL" in window.findChild(QLabel, "breakEvenLabel").text()
    assert "未含滑点、排队和部分成交" in window.findChild(QLabel, "reasonsLabel").text()
    assert window.findChild(QLabel, "buyLevelLabel").height() >= 40
    assert window.findChild(QLabel, "exitLevelLabel").height() >= 40
    assert window.findChild(QLabel, "quoteLabel").geometry().top() > (
        window.findChild(QFrame, "cardsFrame").geometry().bottom()
    )
    assert "513780" not in window.text_snapshot()
    assert all(button.text() not in {"买入", "卖出"} for button in window.findChildren(QPushButton))


def test_window_clears_prices_when_the_decision_expires() -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 29, 950000, tzinfo=SHANGHAI)
    )
    window = ObservationWindow(service)
    window.findChild(QLineEdit, "symbolInput").setText("159570")
    window.findChild(QPushButton, "evaluateButton").click()

    assert window.findChild(QLabel, "buyLevelLabel").text() == "≤ 1.413"
    QTest.qWait(80)
    app.processEvents()

    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    assert window.findChild(QLabel, "exitLevelLabel").text() == "—"
    assert "已过期" in window.findChild(QLabel, "statusLabel").text()

    window.findChild(QPushButton, "evaluateButton").click()
    app.processEvents()

    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    assert window.findChild(QLabel, "exitLevelLabel").text() == "—"
    assert "已过期" in window.findChild(QLabel, "statusLabel").text()
    assert "不可重放" in window.findChild(QLabel, "reasonsLabel").text()


def test_no_go_result_clears_a_previously_rendered_target_price() -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    window = ObservationWindow(service)
    symbol_input = window.findChild(QLineEdit, "symbolInput")
    evaluate_button = window.findChild(QPushButton, "evaluateButton")

    symbol_input.setText("159570")
    evaluate_button.click()
    symbol_input.setText("159567")
    evaluate_button.click()
    app.processEvents()

    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    assert window.findChild(QLabel, "exitLevelLabel").text() == "—"
    assert "NO-GO" in window.findChild(QLabel, "statusLabel").text()
    assert "尚未注册" in window.findChild(QLabel, "reasonsLabel").text()


def test_wait_decision_without_eligibility_evidence_cannot_show_a_green_gate() -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))
    window = ObservationWindow(service)

    window.render_decision(replace(decision, eligibility_evidence_id=None))
    app.processEvents()

    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    assert "T+0台账资格（夹具） ✓" not in window.findChild(
        QLabel, "gatesLabel"
    ).text()
    assert "资格证据缺失" in window.findChild(QLabel, "reasonsLabel").text()


def test_wait_data_renders_diagnostic_quote_but_never_strategy_prices() -> None:
    app = QApplication.instance() or QApplication([])
    now = datetime(2026, 7, 28, 13, 18, 30, tzinfo=SHANGHAI)
    service = create_m1_fixture_service(clock=lambda: now)
    window = ObservationWindow(service)
    decision = TargetObservationDecision(
        decision_id="m2-wait",
        etf_code="159570",
        etf_name="港股通创新药ETF汇添富",
        mode=ObservationMode.PAPER_OBSERVATION,
        status=DecisionStatus.WAIT_DATA,
        target_bid=Decimal("1.404"),
        target_ask=Decimal("1.405"),
        buy_observation_ceiling=None,
        strategy_exit_level=None,
        break_even_reference=None,
        estimated_quantity=None,
        round_trip_cost_cny=None,
        fee_profile_status=None,
        policy_version=None,
        data_snapshot_id="capture-current",
        eligibility_evidence_id=None,
        eligibility_reviewed_on=None,
        policy_metadata=None,
        generated_at=now,
        reasons=("因果5分钟bar 6/48。", "G2/G3 未通过。"),
        target_iopv=Decimal("1.4036"),
        feed_label="UNVERIFIED RESEARCH FEED",
        data_valid_until=datetime(2026, 7, 28, 13, 19, tzinfo=SHANGHAI),
        signal_bar_count=6,
        signal_bar_required=48,
        data_gate_reasons=("G2/G3 未通过。",),
    )

    window.render_decision(decision)
    app.processEvents()

    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    assert window.findChild(QLabel, "exitLevelLabel").text() == "—"
    assert "WAIT-DATA" in window.findChild(QLabel, "statusLabel").text()
    assert "1.404 / 1.405" in window.findChild(QLabel, "quoteLabel").text()
    assert "IOPV 1.4036" in window.findChild(QLabel, "quoteLabel").text()
    assert "6/48" in window.findChild(QLabel, "gatesLabel").text()
    assert "UNVERIFIED RESEARCH FEED" in window.text_snapshot()
    assert "513780" not in window.text_snapshot()

    window._expire_rendered_prices()

    assert window.findChild(QLabel, "quoteLabel").text() == "目标买一/卖一：—"
    assert "研究行情新鲜度 ✕" in window.findChild(QLabel, "gatesLabel").text()
    assert "固定夹具" not in window.findChild(QLabel, "gatesLabel").text()


def test_background_refresh_keeps_button_state_and_renders_result() -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    window = ObservationWindow(service, background_refresh=True)
    symbol_input = window.findChild(QLineEdit, "symbolInput")
    button = window.findChild(QPushButton, "evaluateButton")
    symbol_input.setText("159570")

    button.click()

    assert button.isEnabled() is False
    assert "正在刷新" in button.text()
    for _ in range(100):
        app.processEvents()
        if button.isEnabled():
            break
        QTest.qWait(5)
    assert button.isEnabled() is True
    assert button.text() == "刷新观察"
    assert "纸面观察" in window.findChild(QLabel, "statusLabel").text()


def test_rendered_decision_writes_target_only_local_audit_record(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    journal = tmp_path / "desktop_decisions"
    window = ObservationWindow(service, journal_directory=journal)
    window.findChild(QLineEdit, "symbolInput").setText("159570")

    window.evaluate_current_symbol()
    app.processEvents()

    records = list(journal.glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["etf_code"] == "159570"
    assert payload["status"] == "wait"
    assert payload["buy_observation_ceiling"] == "1.413"
    assert "513780" not in records[0].read_text(encoding="utf-8")


def test_m2_actionable_paper_state_never_claims_fixed_fixture_gate() -> None:
    app = QApplication.instance() or QApplication([])
    service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))
    window = ObservationWindow(service)

    window.render_decision(
        replace(
            decision,
            feed_label="UNVERIFIED RESEARCH FEED",
            reasons=("已覆盖费用与2个价格档执行缓冲。",),
            signal_bar_count=48,
            signal_bar_required=48,
        )
    )
    app.processEvents()

    assert "因果信号 ✓" in window.findChild(QLabel, "gatesLabel").text()
    assert "成本压力 ✓" in window.findChild(QLabel, "gatesLabel").text()
    assert "固定夹具" not in window.findChild(QLabel, "gatesLabel").text()
    assert "UNVERIFIED RESEARCH FEED" in window.findChild(
        QLabel, "reasonsLabel"
    ).text()


def test_background_result_is_discarded_when_symbol_changes_mid_refresh() -> None:
    app = QApplication.instance() or QApplication([])
    fixture_service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    decision = fixture_service.evaluate(TargetObservationRequest(etf_code="159570"))
    release = threading.Event()

    class BlockingService:
        def evaluate(self, request):
            release.wait(timeout=2)
            return decision

    window = ObservationWindow(BlockingService(), background_refresh=True)
    symbol_input = window.findChild(QLineEdit, "symbolInput")
    button = window.findChild(QPushButton, "evaluateButton")
    symbol_input.setText("159570")
    button.click()
    symbol_input.setText("159567")
    release.set()
    for _ in range(100):
        app.processEvents()
        if button.isEnabled():
            break
        QTest.qWait(5)

    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    assert "旧结果已丢弃" in window.findChild(QLabel, "reasonsLabel").text()
    assert "1.413" not in window.text_snapshot()


def test_background_observation_refreshes_again_when_application_resumes() -> None:
    app = QApplication.instance() or QApplication([])
    fixture_service = create_m1_fixture_service(
        clock=lambda: datetime(2026, 7, 27, 10, 0, 15, tzinfo=SHANGHAI)
    )
    decision = fixture_service.evaluate(TargetObservationRequest(etf_code="159570"))

    class RecordingService:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, _request):
            self.calls += 1
            return decision

    service = RecordingService()
    window = ObservationWindow(service, background_refresh=True)
    window.findChild(QLineEdit, "symbolInput").setText("159570")
    window.render_decision(decision)

    window._application_state_changed(Qt.ApplicationState.ApplicationInactive)
    assert window.findChild(QLabel, "buyLevelLabel").text() == "—"
    window._application_state_changed(Qt.ApplicationState.ApplicationActive)
    for _ in range(100):
        app.processEvents()
        if service.calls:
            break
        QTest.qWait(5)

    assert service.calls == 1


def test_broken_startup_configuration_becomes_visible_no_go(tmp_path: Path) -> None:
    service = create_desktop_service(workspace=tmp_path)

    decision = service.evaluate(TargetObservationRequest(etf_code="159570"))

    assert decision.status is DecisionStatus.NO_GO
    assert decision.target_bid is None
    assert "启动配置加载失败" in decision.reasons[0]
