"""Unit tests for live scope mute gate (Clear all data)."""

from src.live.scope_control import (
    clear_all_suppressed_scopes,
    is_live_scope_suppressed,
    resume_live_scope,
    suppress_live_scope,
)


def setup_function() -> None:
    clear_all_suppressed_scopes()


def teardown_function() -> None:
    clear_all_suppressed_scopes()


def test_suppress_and_resume_round_trip() -> None:
    assert not is_live_scope_suppressed("t1", "gh_A")
    suppress_live_scope("t1", "gh_A")
    assert is_live_scope_suppressed("t1", "gh_A")
    assert not is_live_scope_suppressed("t1", "gh_B")
    resume_live_scope("t1", "gh_A")
    assert not is_live_scope_suppressed("t1", "gh_A")
