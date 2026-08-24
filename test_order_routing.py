from broker_alpaca import decide_order_type


def test_missing_quote_fails_closed():
    assert decide_order_type("strong_up", None) == {
        "order_type": "skip",
        "reason": "missing_quote",
    }


def test_invalid_crossed_quote_fails_closed():
    assert decide_order_type(
        "strong_up", {"bid": 101.0, "ask": 100.0, "spread": -1.0}
    ) == {"order_type": "skip", "reason": "invalid_quote"}


def test_strong_buy_with_tight_spread_uses_market():
    route = decide_order_type(
        "strong_up", {"bid": 99.99, "ask": 100.01, "spread": 0.02}
    )
    assert route == {"order_type": "market"}


def test_mild_buy_uses_bid_limit():
    route = decide_order_type(
        "mild_up", {"bid": 99.99, "ask": 100.01, "spread": 0.02}
    )
    assert route == {"order_type": "limit", "limit_price": 99.99}


def test_strong_sell_with_tight_spread_uses_market():
    route = decide_order_type(
        "strong_down",
        {"bid": 99.99, "ask": 100.01, "spread": 0.02},
        side="sell",
    )
    assert route == {"order_type": "market"}


def test_mild_sell_uses_ask_limit():
    route = decide_order_type(
        "mild_down",
        {"bid": 99.99, "ask": 100.01, "spread": 0.02},
        side="sell",
    )
    assert route == {"order_type": "limit", "limit_price": 100.01}
