"""SafetyManager: ported from february11's safety-manager.test.ts."""

import pytest

from video_stream.safety import SafetyBlocked, SafetyManager


def make_manager(**kwargs):
    t = {"now": 0.0}
    manager = SafetyManager(clock=lambda: t["now"], **kwargs)
    return manager, t


def test_rate_limits_actions_inside_window():
    manager, t = make_manager(max_actions=2, window=60.0)

    assert manager.guard_action("a") == (True, None)
    assert manager.guard_action("b")[0] is True

    ok, reason = manager.guard_action("c")
    assert ok is False
    assert "rate limited" in reason

    status = manager.status()
    assert status["actions_in_window"] == 2
    assert status["remaining"] == 0
    assert "rate limited" in status["last_blocked"]

    # The window slides: past it, the budget frees up again.
    t["now"] = 61.0
    assert manager.guard_action("d")[0] is True


def test_kill_switch_blocks_unless_bypassed():
    manager, _ = make_manager(max_actions=5, window=60.0)

    manager.set_kill_switch(True, "operator-triggered")
    ok, reason = manager.guard_action("chaos:demo")
    assert ok is False
    assert "kill switch" in reason

    with pytest.raises(SafetyBlocked) as exc_info:
        manager.assert_action("chaos:demo")
    assert exc_info.value.status_code == 423

    # The panic path must always work.
    manager.assert_action("safety:fallback", bypass_kill=True)

    manager.set_kill_switch(False)
    assert manager.guard_action("chaos:demo")[0] is True


def test_rate_limit_raises_429():
    manager, _ = make_manager(max_actions=1, window=60.0)
    manager.assert_action("director:switch")
    with pytest.raises(SafetyBlocked) as exc_info:
        manager.assert_action("director:switch")
    assert exc_info.value.status_code == 429


def test_bypass_rate_does_not_consume_budget():
    manager, _ = make_manager(max_actions=1, window=60.0)
    manager.assert_action("safety:fallback", bypass_rate=True)
    assert manager.status()["actions_in_window"] == 0
    manager.assert_action("director:switch")  # budget still available


def test_check_action_is_a_pure_probe():
    manager, _ = make_manager(max_actions=1, window=60.0)
    assert manager.check_action("director:switch")[0] is True
    assert manager.status()["actions_in_window"] == 0  # consumed nothing

    manager.guard_action("director:switch")
    ok, reason = manager.check_action("director:switch")
    assert ok is False
    assert "rate limited" in reason

    manager, _ = make_manager(max_actions=5, window=60.0)
    manager.set_kill_switch(True)
    ok, reason = manager.check_action("director:switch")
    assert ok is False
    assert "kill switch" in reason


def test_on_change_receives_current_and_updated_snapshots():
    events = []
    manager = SafetyManager(max_actions=10, window=60.0, on_change=events.append)

    manager.guard_action("test")
    manager.set_kill_switch(True)
    manager.set_kill_switch(False)

    assert len(events) >= 3
    assert events[0]["actions_in_window"] == 1
    assert events[1]["kill_switch"] is True
    assert events[-1]["kill_switch"] is False


def test_broken_listener_never_blocks_the_action():
    def boom(_status):
        raise RuntimeError("listener crashed")

    manager = SafetyManager(max_actions=5, window=60.0, on_change=boom)
    assert manager.guard_action("director:switch")[0] is True
