"""验证 Bar.date 的缓存行为（性能热点 #5）：计算一次后 O(1) 复用。

date 只依赖构造时给定的 timestamp（timestamp 构造后从不被改写），
因此 ISO 日期串在对象生命周期内恒定，可以安全缓存。
"""

from datetime import datetime

from app.domain import Bar


def _make_bar(timestamp: datetime) -> Bar:
    return Bar(
        timestamp=timestamp,
        symbol="600519",
        open=10.0,
        high=11.0,
        low=9.5,
        close=10.5,
        volume=1000.0,
        amount=10500.0,
    )


def test_bar_date_returns_correct_iso_date():
    bar = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    assert bar.date == "2024-03-15"


def test_bar_date_is_stable_across_reads():
    bar = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    first = bar.date
    second = bar.date
    assert first == second == "2024-03-15"
    assert first is second  # 缓存后返回同一字符串对象


def test_bar_date_cache_populated_after_first_access():
    bar = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    assert bar._date_cache is None  # 未访问前不计算
    _ = bar.date
    assert bar._date_cache == "2024-03-15"  # 首次访问后缓存落地
    _ = bar.date
    assert bar._date_cache == "2024-03-15"  # 再次访问不重算


def test_bar_date_computed_once_via_cache_field():
    """证明只算一次：datetime.date 是不可变的 C 类型无法 monkeypatch，
    故改为 spy 到 isoformat 不可行，这里用 _date_cache 字段证明：
    首次访问前为 None，首次访问后落地，后续访问不再变化（无重算触发点）。
    """
    bar = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    assert bar._date_cache is None
    assert bar.date == "2024-03-15"
    assert bar._date_cache is not None
    cached_obj = bar._date_cache
    for _ in range(3):
        assert bar.date == "2024-03-15"
        assert bar._date_cache is cached_obj  # 同一缓存对象，未重算
    assert bar.date is bar.date


def test_bar_to_dict_contains_correct_date():
    bar = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    data = bar.to_dict()
    assert data["date"] == "2024-03-15"
    assert "_date_cache" not in data  # 内部缓存字段不泄漏到序列化结果


def test_bar_with_time_component_yields_date_only():
    for ts in (
        datetime(2024, 1, 2, 9, 31, 0),
        datetime(2024, 1, 2, 15, 0, 0),
        datetime(2024, 1, 2, 0, 0, 0),
    ):
        assert _make_bar(ts).date == "2024-01-02"


def test_bar_equality_unaffected_by_cache_field():
    a = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    b = _make_bar(datetime(2024, 3, 15, 15, 0, 0))
    assert a == b
    _ = a.date  # 让 a 的缓存落地
    assert a == b  # 缓存字段 compare=False，不影响相等性
