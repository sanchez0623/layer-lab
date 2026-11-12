"""轮动 v2：清仓后重新运行五层选股取第一名（不再是固定池顺序）。"""

import pytest

from app import rotation
from app.broker import BrokerAccount
from app.domain import Position


def candidate(code: str, name: str, board: str, chg20d: float, deep_checked: bool = True) -> dict:
    return {
        "code": code, "name": name, "board": board, "chg20d": chg20d,
        "close": 100.0, "prev_close": 101.0, "as_of": "2026-11-12",
        "week52_high": 200.0, "drawdown_52w": 0.5, "day_change": 0.01,
        "amount": 1.2e9, "avg_amount_20d": 8e8, "turnover": 0.02,
        "sessions": 250, "score": chg20d, "checks": [], "vetoed_by": None,
        "deep_checked": deep_checked,
    }


DEFAULT_POOL = [
    candidate("sh.688200", "华峰测控", "科创板", -0.3153),
    candidate("sz.300855", "图南股份", "创业板", -0.2812),
]


def make_run(
    *, run_id: str = "run_new", first_code: str | None = "sh.688200",
    gate_open: bool = True, eligible: list[dict] | None = None,
    config: dict | None = None, warnings: list[str] | None = None,
    as_of: str = "2026-11-12",
) -> dict:
    candidates = DEFAULT_POOL if eligible is None else eligible
    first = next((item for item in candidates if item["code"] == first_code), None) if first_code else None
    return {
        "id": run_id, "created_at": "2026-11-12T10:00:00", "as_of": as_of,
        "config": {"chg20_max": -0.15} if config is None else config,
        "gate": {"boards": {}, "open": gate_open},
        "funnel": {}, "pool": candidates, "eligible": candidates,
        "first": first, "warnings": list(warnings or []),
        "data_info": {}, "elapsed_seconds": 0.1,
    }


def build_account(
    *, current_symbol: str = "sh.688200", pool: list[dict] | None = None,
    pending: bool = True,
) -> BrokerAccount:
    """模拟「已带入的轮动账户，本轮刚清仓」的状态。"""
    account = BrokerAccount(initial_cash=4_000_000.0)
    rotation.set_from_run(
        account, make_run(first_code=current_symbol, eligible=pool),
        enabled=True, current_symbol=current_symbol,
    )
    account.rotation_pending_advance = pending
    account.rotation_last_attempt_date = None
    account.rotation_waiting = False
    account.rotation_note = ""
    return account


def test_summary_and_serialization_roundtrip():
    account = build_account()
    account.rotation_last_attempt_date = "2026-11-12"
    summary = rotation.rotation_summary(account)
    assert summary["mode"] == "rescreen"
    assert summary["enabled"] is True
    assert summary["index"] == 0
    assert summary["current_symbol"] == "sh.688200"
    assert summary["waiting"] is False
    assert summary["run_id"] == "run_new"
    assert summary["last_run_id"] == "run_new"
    assert summary["last_attempt_date"] == "2026-11-12"
    assert summary["last_screen"]["first"]["code"] == "sh.688200"
    assert [item["symbol"] for item in summary["pool"]] == ["sh.688200", "sz.300855"]

    payload = account.to_dict()
    assert payload["rotation"] == summary

    restored = BrokerAccount.from_dict(account.to_dict(include_internal=True))
    assert restored.rotation_enabled is True
    assert restored.rotation_config == {"chg20_max": -0.15}
    assert restored.rotation_pool == summary["pool"]
    assert restored.rotation_index == 0
    assert restored.rotation_last_attempt_date == "2026-11-12"
    assert restored.rotation_last_run_id == "run_new"
    assert restored.rotation_last_screen == summary["last_screen"]
    assert restored.rotation_pending_advance is True


def test_old_account_dict_without_v2_fields_still_loads():
    account = BrokerAccount(initial_cash=100_000.0, name="旧账户")
    legacy = account.to_dict(include_internal=True)
    for key in (
        "rotation", "rotation_config", "rotation_last_attempt_date",
        "rotation_last_run_id", "rotation_last_screen", "rotation_pending_advance",
    ):
        legacy.pop(key, None)
    restored = BrokerAccount.from_dict(legacy)
    assert restored.rotation_enabled is False
    assert restored.rotation_pool == []
    assert restored.rotation_index == -1
    assert restored.rotation_config == {}
    assert restored.rotation_last_attempt_date is None
    assert restored.rotation_last_run_id is None
    assert restored.rotation_last_screen == {}
    assert restored.rotation_pending_advance is False


def test_set_from_run_records_config_snapshot_pool_and_index():
    account = BrokerAccount(initial_cash=4_000_000.0)
    run = make_run(config={"chg20_max": -0.20, "boards": ["创业板", "科创板"]},
                   warnings=["w1", "w2", "w3", "w4"])
    rotation.set_from_run(account, run, enabled=True)
    assert account.rotation_config == {"chg20_max": -0.20, "boards": ["创业板", "科创板"]}
    assert account.rotation_run_id == "run_new"
    assert account.rotation_last_run_id == "run_new"
    assert account.rotation_last_attempt_date is None
    assert account.rotation_pool == [
        {"symbol": "sh.688200", "name": "华峰测控", "chg20d": -0.3153, "board": "科创板", "deep_checked": True},
        {"symbol": "sz.300855", "name": "图南股份", "chg20d": -0.2812, "board": "创业板", "deep_checked": True},
    ]
    assert account.rotation_index == 0
    assert account.rotation_note == "换票→sh.688200"
    assert account.rotation_pending_advance is False
    assert account.rotation_waiting is False
    screen = account.rotation_last_screen
    assert screen["run_id"] == "run_new"
    assert screen["as_of"] == "2026-11-12"
    assert screen["first"] == {"code": "sh.688200", "name": "华峰测控", "chg20d": -0.3153}
    assert screen["eligible_count"] == 2
    assert screen["gate_open"] is True
    assert screen["warnings"] == ["w1", "w2", "w3"]  # 只保留前 3 条


def test_set_from_run_points_at_requested_candidate():
    account = BrokerAccount(initial_cash=4_000_000.0)
    rotation.set_from_run(
        account, make_run(first_code="sh.688200"), enabled=True,
        note="换票→sz.300855", current_symbol="sz.300855",
    )
    assert account.rotation_index == 1
    assert rotation.rotation_summary(account)["current_symbol"] == "sz.300855"
    assert account.rotation_note == "换票→sz.300855"


def test_set_from_run_without_first_waits_with_reason():
    account = BrokerAccount(initial_cash=4_000_000.0)
    rotation.set_from_run(account, make_run(first_code=None, gate_open=False), enabled=True)
    assert account.rotation_waiting is True
    assert account.rotation_note == rotation.NOTE_L0_CLOSED
    assert account.rotation_index == -1
    assert account.rotation_last_screen["first"] is None
    assert account.rotation_last_screen["gate_open"] is False

    other = BrokerAccount(initial_cash=4_000_000.0)
    rotation.set_from_run(other, make_run(first_code=None, gate_open=True), enabled=True)
    assert other.rotation_waiting is True
    assert other.rotation_note == rotation.NOTE_NO_CANDIDATE


def test_set_from_run_disabled_keeps_snapshot_and_config():
    account = BrokerAccount(initial_cash=4_000_000.0)
    rotation.set_from_run(account, make_run(), enabled=False)
    assert account.rotation_enabled is False
    assert account.rotation_config == {"chg20_max": -0.15}
    assert account.rotation_last_run_id == "run_new"
    assert account.rotation_last_screen["first"]["code"] == "sh.688200"
    assert account.rotation_note == ""


def test_should_rescreen_conditions():
    account = build_account()
    assert rotation.should_rescreen(account, today="2026-11-12", current_symbol="sh.688200")

    account.rotation_pending_advance = False
    assert not rotation.should_rescreen(account, today="2026-11-12", current_symbol="sh.688200")
    account.rotation_pending_advance = True

    account.rotation_enabled = False
    assert not rotation.should_rescreen(account, today="2026-11-12", current_symbol="sh.688200")
    account.rotation_enabled = True

    account.positions["sh.688200"] = Position(symbol="sh.688200", permanent_qty=100)
    assert not rotation.should_rescreen(account, today="2026-11-12", current_symbol="sh.688200")
    account.positions.clear()

    assert not rotation.should_rescreen(account, today="2026-11-12", current_symbol="sz.300855")

    account.rotation_last_attempt_date = "2026-11-12"
    assert not rotation.should_rescreen(account, today="2026-11-12", current_symbol="sh.688200")
    assert rotation.should_rescreen(account, today="2026-11-13", current_symbol="sh.688200")


def test_guard_failure_does_not_call_runner():
    calls: list[int] = []

    def runner() -> dict:
        calls.append(1)
        return make_run()

    account = build_account(pending=False)
    assert rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    ) is None
    account.rotation_pending_advance = True
    assert rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sz.300855",
    ) is None
    account.positions["sh.688200"] = Position(symbol="sh.688200", permanent_qty=100)
    assert rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    ) is None
    assert calls == []


def test_rescreen_switches_to_new_first_and_resets_strategy():
    account = build_account()
    account.strategy_state = {"armed": True, "liquidating": False}
    account.sequence_index = 5
    account.last_daily_signal_date = "2026-11-11"
    account.armed_state = "ARMED"

    calls: list[int] = []

    def runner() -> dict:
        calls.append(1)
        return make_run(run_id="run_switch", first_code="sz.300855")

    event = rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    )

    assert calls == [1]
    assert event is not None and event["advanced"] is True
    assert event["symbol"] == "sz.300855"
    assert event["previous_symbol"] == "sh.688200"
    assert event["run_id"] == "run_switch"
    assert event["dataset_symbol_hint"] == "sz.300855"
    assert account.rotation_index == 1
    assert rotation.rotation_summary(account)["current_symbol"] == "sz.300855"
    assert account.rotation_note == rotation.NOTE_SWITCH.format(symbol="sz.300855")
    assert account.rotation_pending_advance is False
    assert account.rotation_waiting is False
    assert account.rotation_last_attempt_date == "2026-11-12"
    assert account.rotation_last_run_id == "run_switch"
    assert account.rotation_last_screen["first"]["code"] == "sz.300855"
    assert account.strategy_state == {}
    assert account.sequence_index == 0
    assert account.last_daily_signal_date is None
    assert account.armed_state == "OFF"


def test_rescreen_same_first_keeps_symbol_and_clears_pending():
    account = build_account()
    account.sequence_index = 3
    account.strategy_state = {"armed": True}

    def runner() -> dict:
        return make_run(run_id="run_same", first_code="sh.688200")

    event = rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    )
    assert event is not None
    assert event["advanced"] is False
    assert event["same_symbol"] is True
    assert event["symbol"] == "sh.688200"
    assert account.rotation_index == 0
    assert account.rotation_note == rotation.NOTE_SAME_SYMBOL.format(symbol="sh.688200")
    assert account.rotation_pending_advance is False
    assert account.rotation_waiting is False
    assert account.sequence_index == 3  # 同一标的不换票，不重置策略状态
    assert account.strategy_state == {"armed": True}
    assert account.rotation_last_run_id == "run_same"


def test_rescreen_l0_closed_waits_and_keeps_pending():
    account = build_account()

    def runner() -> dict:
        return make_run(run_id="run_l0", first_code=None, gate_open=False)

    event = rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    )
    assert event is not None and event["advanced"] is False
    assert event["waiting"] is True
    assert account.rotation_waiting is True
    assert account.rotation_note == rotation.NOTE_L0_CLOSED
    assert account.rotation_pending_advance is True  # 保留，次日再试
    assert account.rotation_last_attempt_date == "2026-11-12"
    assert account.rotation_last_run_id == "run_l0"
    assert account.rotation_last_screen["gate_open"] is False


def test_rescreen_no_candidate_note():
    account = build_account()

    def runner() -> dict:
        return make_run(run_id="run_empty", first_code=None, gate_open=True)

    rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    )
    assert account.rotation_waiting is True
    assert account.rotation_note == rotation.NOTE_NO_CANDIDATE


def test_rescreen_at_most_once_per_trading_day():
    account = build_account()
    calls: list[int] = []

    def runner() -> dict:
        calls.append(1)
        return make_run(run_id="run_l0", first_code=None, gate_open=False)

    first = rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    )
    assert first is not None and first["rescreened"] is True
    second = rotation.rescreen_and_advance(
        account, runner, today="2026-11-12", current_symbol="sh.688200",
    )
    assert second is None
    assert calls == [1]

    # 次日重试：pending 在 L0 关门时保留，所以下一次 tick 会再筛。
    third = rotation.rescreen_and_advance(
        account, runner, today="2026-11-13", current_symbol="sh.688200",
    )
    assert third is not None and third["rescreened"] is True
    assert calls == [1, 1]
    assert account.rotation_last_attempt_date == "2026-11-13"


def test_rescreen_runner_failure_keeps_account_usable():
    account = build_account()

    def broken_runner() -> dict:
        raise RuntimeError("行情源不可用")

    event = rotation.rescreen_and_advance(
        account, broken_runner, today="2026-11-12", current_symbol="sh.688200",
    )
    assert event is not None and event["rescreened"] is False
    assert event["error"] == "行情源不可用"
    assert account.rotation_waiting is True
    assert "重新选股失败" in account.rotation_note
    assert account.rotation_pending_advance is True
    assert account.rotation_last_attempt_date == "2026-11-12"
    assert account.rotation_index == 0  # 未换票，账户仍指向原标的
    # 同一天不重复打行情。
    assert rotation.rescreen_and_advance(
        account, broken_runner, today="2026-11-12", current_symbol="sh.688200",
    ) is None


def test_set_pool_manual_pool_still_supported():
    account = BrokerAccount(initial_cash=4_000_000.0)
    rotation.set_pool(account, DEFAULT_POOL, run_id="manual_run", enabled=True)
    assert [item["symbol"] for item in account.rotation_pool] == ["sh.688200", "sz.300855"]
    assert account.rotation_index == -1
    assert account.rotation_run_id == "manual_run"
    assert account.rotation_pending_advance is False
    assert account.rotation_last_screen == {}  # 手工建池没有选股快照

    account.positions["sz.300855"] = Position(symbol="sz.300855", permanent_qty=100)
    rotation.set_pool(account, DEFAULT_POOL, enabled=True)
    assert account.rotation_index == 1
    assert account.rotation_note == "换票→sz.300855"


def test_set_pool_rejects_entries_without_symbol():
    account = BrokerAccount(initial_cash=4_000_000.0)
    with pytest.raises(ValueError):
        rotation.set_pool(account, [{"name": "缺代码"}])
