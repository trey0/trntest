from trntest import trace


def test_disabled_by_default():
    assert trace.enabled() is False


def test_enable_turns_on_for_the_block_then_restores_previous_state():
    assert trace.enabled() is False
    with trace.enable():
        assert trace.enabled() is True
    assert trace.enabled() is False


def test_enable_nested_restores_outer_state_not_disabled():
    with trace.enable():
        with trace.enable():
            assert trace.enabled() is True
        assert trace.enabled() is True
    assert trace.enabled() is False
