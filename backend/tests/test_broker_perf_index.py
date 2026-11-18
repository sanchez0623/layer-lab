"""FIX #3 回归测试：BrokerAccount 未结委托索引（per-symbol bucket + open 列表）。

验证引入 ``_open_by_symbol`` / ``_open_orders`` 索引后，撮合与资金口径与旧的
「每次遍历 append-only self.orders 全表」逐字节一致：

1. process_bar 只考虑该标的正确的未结委托，成交结果与朴素全表扫描一致；
2. reserved_cash 每一步都等于「遍历全部 orders 的朴素求和」；
3. snapshot_state 仍列出**每一笔**委托（含终态 FILLED/CANCELLED/REJECTED）；
4. 部分成交后再全部成交：委托只在不再 PENDING/PARTIALLY_FILLED 时才离开未结索引。
"""

from datetime import datetime, timedelta

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig

OPEN_STATES = {"PENDING", "PARTIALLY_FILLED"}


def make_bar(symbol: str, ts: datetime, price: float = 10.0, volume: float = 1_000_000) -> Bar:
    return Bar(
        timestamp=ts, symbol=symbol,
        open=price, high=price * 1.02, low=price * 0.98, close=price,
        volume=volume, amount=price * volume, prev_close=price,
    )


def config() -> StrategyConfig:
    return StrategyConfig(slippage_bps=0, volume_participation=1.0)


def req(symbol: str, side="BUY", quantity=100, purpose="PERMANENT",
        order_type="MARKET", limit_price=None, execution_mode="NEXT_BAR") -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side=side, quantity=quantity, purpose=purpose,
        order_type=order_type, limit_price=limit_price, execution_mode=execution_mode,
    )


def naive_pending(account: BrokerAccount, symbol: str, modes=None) -> list:
    """朴素口径：遍历**全部** orders，按 symbol + 未结状态 + execution_mode 过滤。"""
    allowed = set(modes) if modes is not None else None
    return [
        o for o in account.orders
        if o.symbol == symbol and o.status in OPEN_STATES
        and (allowed is None or o.execution_mode in allowed)
    ]


def naive_reserved(account: BrokerAccount) -> float:
    """朴素口径：遍历**全部** orders 逐项累加买入预留（与旧实现同序同式）。"""
    reserved = 0.0
    for order in account.orders:
        if order.side != "BUY" or order.status not in OPEN_STATES:
            continue
        reference = order.limit_price
        if reference is None and order.symbol in account.positions:
            reference = account.positions[order.symbol].last_price
        if reference:
            reserved += order.remaining * reference * 1.002
    from app.domain import money
    return money(reserved)


def assert_index_matches_naive(account: BrokerAccount) -> None:
    """两份派生索引必须与朴素全表扫描出的未结委托集合完全一致（含顺序）。"""
    naive_all = [o for o in account.orders if o.status in OPEN_STATES]
    assert account._open_orders == naive_all, "未结列表应等于朴素全表扫描结果"
    by_symbol: dict[str, list] = {}
    for o in naive_all:
        by_symbol.setdefault(o.symbol, []).append(o)
    assert account._open_by_symbol == by_symbol, "每标的未结桶应与朴素分组一致"


def test_process_bar_uses_correct_pending_and_matches_naive_scan():
    """多标的混合委托，成交/撤单若干后，process_bar 的成交与朴素扫描一致。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")

    # 三个标的各挂一笔买单（NEXT_BAR，下一根 K 线撮合）。
    buys = {
        "TEST": account.submit(req("TEST", quantity=100)),
        "AAA": account.submit(req("AAA", quantity=200)),
        "BBB": account.submit(req("BBB", quantity=300)),
    }
    for o in buys.values():
        assert o.status == "PENDING"
    assert_index_matches_naive(account)

    # 撤掉 BBB 的买单 → 应立刻离开未结索引。
    assert account.cancel(buys["BBB"].id) is True
    assert buys["BBB"].status == "CANCELLED"
    assert_index_matches_naive(account)
    assert all(o.id != buys["BBB"].id for o in account._open_orders)

    # 只推 AAA 的 bar：只有 AAA 那笔未结委托会成交，TEST 不受影响。
    bar_aaa = make_bar("AAA", ts + timedelta(minutes=5), price=10.0)
    fills = account.process_bar(bar_aaa)
    assert [f.order_id for f in fills] == [buys["AAA"].id]
    assert [f.quantity for f in fills] == [200]
    assert buys["AAA"].status == "FILLED"
    # TEST 那笔仍未结，未受 AAA bar 影响。
    assert buys["TEST"].status == "PENDING"
    assert_index_matches_naive(account)

    # 再推 TEST 的 bar：AAA 已 FILLED 不应再次成交；只有 TEST 成交。
    bar_test = make_bar("TEST", ts + timedelta(minutes=10), price=10.0)
    fills = account.process_bar(bar_test)
    assert [f.order_id for f in fills] == [buys["TEST"].id]
    assert buys["TEST"].status == "FILLED"
    assert_index_matches_naive(account)
    # 全部成交后未结索引为空。
    assert account._open_orders == []
    assert account._open_by_symbol == {}

    # 成交集合与朴素扫描一致：对每个标的再推一根 bar 都不再产生成交。
    for sym in ("TEST", "AAA", "BBB"):
        assert account.process_bar(make_bar(sym, ts + timedelta(minutes=15))) == []


def test_process_bar_execution_mode_filter_still_applied():
    """execution_mode 过滤在索引路径下依旧生效：只撮合指定模式的未结委托。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")

    next_bar = account.submit(req("TEST", quantity=100, execution_mode="NEXT_BAR"))
    next_open = account.submit(req("TEST", quantity=100, execution_mode="NEXT_OPEN"))
    assert_index_matches_naive(account)

    # 只允许撮合 NEXT_BAR：NEXT_OPEN 那笔必须原封不动留在未结索引。
    fills = account.process_bar(
        make_bar("TEST", ts + timedelta(minutes=5)), modes={"NEXT_BAR"},
    )
    assert [f.order_id for f in fills] == [next_bar.id]
    assert next_bar.status == "FILLED"
    assert next_open.status == "PENDING"
    assert account._open_orders == [next_open]
    assert account._open_by_symbol == {"TEST": [next_open]}

    # 再只允许 NEXT_OPEN：这次轮到它成交。
    fills = account.process_bar(
        make_bar("TEST", ts + timedelta(minutes=10)), modes={"NEXT_OPEN"},
    )
    assert [f.order_id for f in fills] == [next_open.id]
    assert next_open.status == "FILLED"
    assert_index_matches_naive(account)


def test_reserved_cash_equals_naive_sum_at_each_step():
    """每一步 reserved_cash 都必须等于「遍历全部 orders 的朴素求和」。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")

    # 限价买单（限价单预留按 limit_price）+ 市价买单（预留按 last_price）混合。
    account.submit(req("TEST", quantity=100, order_type="LIMIT", limit_price=9.5))
    account.submit(req("AAA", quantity=200, order_type="LIMIT", limit_price=20.0))
    account.submit(req("TEST", quantity=100))  # 市价，预留按 last_price
    assert account.reserved_cash == naive_reserved(account)

    # 推一根 bar 更新 last_price：市价买单预留随之变化，仍要与朴素口径一致。
    account.process_bar(make_bar("TEST", ts + timedelta(minutes=5), price=12.0))
    assert account.reserved_cash == naive_reserved(account)
    # available_cash 口径不变：money(max(0, cash - reserved_cash))。
    assert account.available_cash == max(0.0, account.cash - account.reserved_cash)

    # 部分成交（低成交量）→ remaining 减少，预留减少，仍要逐项一致。
    partial_bar = make_bar("TEST", ts + timedelta(minutes=10), price=9.5, volume=100)
    account.process_bar(partial_bar)
    assert account.reserved_cash == naive_reserved(account)

    # 撤掉一笔 → 预留进一步变化，仍要一致。
    open_test = [o for o in account.orders if o.symbol == "TEST" and o.status in OPEN_STATES]
    if open_test:
        account.cancel(open_test[0].id)
    assert account.reserved_cash == naive_reserved(account)
    assert_index_matches_naive(account)


def test_snapshot_state_lists_all_orders_including_terminal():
    """snapshot_state 必须仍全量输出每一笔委托，含 FILLED/CANCELLED/REJECTED。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")

    filled_order = account.submit(req("TEST", quantity=100))
    account.process_bar(make_bar("TEST", ts + timedelta(minutes=5)))
    assert filled_order.status == "FILLED"

    cancelled_order = account.submit(req("AAA", quantity=100))
    account.cancel(cancelled_order.id)
    assert cancelled_order.status == "CANCELLED"

    # 无持仓却卖 → REJECTED（T+1 可卖不足，append-only，仍要进快照）。
    rejected_order = account.submit(req("BBB", side="SELL", quantity=100))
    assert rejected_order.status == "REJECTED"

    pending_order = account.submit(req("BBB", quantity=100))
    assert pending_order.status == "PENDING"

    snap = account.snapshot_state()
    snap_ids = [o["id"] for o in snap["orders"]]
    # 全部 4 笔（含三种终态 + 1 笔未结）都必须在快照里。
    for order in (filled_order, cancelled_order, rejected_order, pending_order):
        assert order.id in snap_ids
    assert len(snap_ids) == len(account.orders) == 4
    # 索引是派生状态，不应出现在快照里（内含 Order 对象，非 JSON 友好）。
    assert "_open_by_symbol" not in snap
    assert "_open_orders" not in snap


def test_partially_filled_order_leaves_index_only_when_no_longer_open():
    """部分成交后再全部成交：只在状态离开 PENDING/PARTIALLY_FILLED 时才出索引。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")

    # 限价 9.9 买单 200 股；低成交量（参与度 0.1 × 1000 股）只够成交 100 股。
    order = account.submit(req("TEST", quantity=200, order_type="LIMIT", limit_price=9.9))
    bar = make_bar("TEST", ts + timedelta(minutes=5), price=10.2, volume=1_000)
    bar.low = 9.8
    account.config.volume_participation = 0.1
    fills = account.process_bar(bar)
    assert len(fills) == 1 and fills[0].quantity == 100
    assert order.status == "PARTIALLY_FILLED"
    assert order.remaining == 100
    # 部分成交后**必须仍留在**未结索引（还有 100 股未成交）。
    assert account._open_orders == [order]
    assert account._open_by_symbol == {"TEST": [order]}
    assert_index_matches_naive(account)

    # 放开参与度，再推一根可成交 bar：补足剩余 100 股 → 全部成交。
    account.config.volume_participation = 1.0
    bar2 = make_bar("TEST", ts + timedelta(minutes=10), price=9.9, volume=1_000_000)
    bar2.low = 9.8
    fills = account.process_bar(bar2)
    assert len(fills) == 1 and fills[0].quantity == 100
    assert order.status == "FILLED"
    assert order.remaining == 0
    # 全部成交后**才**离开未结索引。
    assert account._open_orders == []
    assert account._open_by_symbol == {}
    assert_index_matches_naive(account)


def test_cancel_open_orders_removes_all_from_index():
    """日终撤单（cancel_open_orders）必须把所有未结委托从索引摘除。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")

    account.submit(req("TEST", quantity=100, execution_mode="NEXT_BAR"))
    account.submit(req("AAA", quantity=100, execution_mode="NEXT_BAR"))
    assert len(account._open_orders) == 2

    account.cancel_open_orders("日终取消")
    assert account._open_orders == []
    assert account._open_by_symbol == {}
    assert all(o.status == "CANCELLED" for o in account.orders)
    assert_index_matches_naive(account)


def test_restore_and_from_dict_rebuild_index_consistently():
    """restore_state / from_dict 后索引必须按还原的 orders 正确重建。"""
    ts = datetime(2025, 1, 2, 9, 30)
    account = BrokerAccount(1_000_000, config())
    account.new_day("2025-01-02")
    account.submit(req("TEST", quantity=100))
    account.submit(req("AAA", quantity=100))
    account.process_bar(make_bar("TEST", ts + timedelta(minutes=5)))  # TEST 成交

    state = account.snapshot_state()
    restored = BrokerAccount(1_000_000, config())
    restored.restore_state(state)
    assert_index_matches_naive(restored)

    reloaded = BrokerAccount.from_dict(account.to_dict(include_internal=True))
    assert_index_matches_naive(reloaded)
