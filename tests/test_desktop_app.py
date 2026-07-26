import os
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QPushButton

from etf_t0.desktop_app import ObservationWindow
from etf_t0.observation import (
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
