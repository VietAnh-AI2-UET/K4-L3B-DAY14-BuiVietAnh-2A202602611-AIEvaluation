"""Check rate limits without sending API requests or waiting in real time."""

from datetime import datetime

import pytest

from gemini_rate_limit import GeminiRateLimiter


class FakeClock:
    def __init__(self, timestamp: float = 1_791_000_000.0):
        self.now = timestamp
        self.waits: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def limiter_factory(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_RPM", raising=False)
    monkeypatch.delenv("GEMINI_RPD", raising=False)
    clock = FakeClock()

    def create(model="gemini-3.5-flash-lite"):
        return GeminiRateLimiter(
            model,
            state_path=tmp_path / "usage.sqlite3",
            clock=clock.time,
            sleep=clock.sleep,
        )

    return create, clock


def test_twenty_requests_stay_below_minute_limit(limiter_factory):
    create, clock = limiter_factory
    limiter = create()
    sent_at = []
    for _ in range(20):
        limiter.wait_for_slot()
        sent_at.append(clock.now)

    assert sent_at[-1] - sent_at[0] == pytest.approx(95)
    assert min(b - a for a, b in zip(sent_at, sent_at[1:])) >= 5
    assert all(sum(start <= sent < start + 60 for sent in sent_at) <= 15 for start in sent_at)
    assert limiter.ensure_capacity(480) == 480


def test_slow_responses_need_no_extra_wait(limiter_factory):
    create, clock = limiter_factory
    limiter = create()
    limiter.wait_for_slot()
    clock.now += 7
    limiter.wait_for_slot()

    assert clock.waits == []


def test_restarting_preserves_count_and_request_spacing(limiter_factory):
    create, clock = limiter_factory
    create().wait_for_slot()
    restarted = create()

    assert restarted.ensure_capacity(499) == 499
    restarted.wait_for_slot()

    assert clock.waits == [5]
    assert restarted.ensure_capacity(498) == 498


def test_does_not_send_request_501(limiter_factory):
    create, _ = limiter_factory
    limiter = create()
    for _ in range(500):
        limiter.wait_for_slot()

    with pytest.raises(RuntimeError, match=r"exhausted \(500/500\)"):
        limiter.wait_for_slot()
    assert limiter.ensure_capacity(0) == 0


@pytest.mark.parametrize(
    "before_midnight",
    ["2026-10-02T06:59:59+00:00", "2026-12-02T07:59:59+00:00"],
)
def test_daily_budget_resets_at_pacific_midnight(limiter_factory, before_midnight):
    create, clock = limiter_factory
    clock.now = datetime.fromisoformat(before_midnight).timestamp()
    limiter = create()
    limiter.wait_for_slot()
    assert limiter.ensure_capacity(499) == 499

    clock.now += 2
    assert limiter.ensure_capacity(500) == 500
    limiter.wait_for_slot()

    assert clock.waits == [3]
    assert limiter.ensure_capacity(499) == 499


def test_counts_are_separate_for_each_model(limiter_factory):
    create, clock = limiter_factory
    create("first-model").wait_for_slot()
    other_model = create("second-model")

    assert other_model.ensure_capacity(500) == 500
    other_model.wait_for_slot()
    assert clock.waits == []


def test_rejects_whole_batch_when_remaining_budget_is_insufficient(limiter_factory):
    create, _ = limiter_factory
    limiter = create()
    limiter.wait_for_slot()

    with pytest.raises(RuntimeError, match="this run needs 500"):
        limiter.ensure_capacity(500)
    assert limiter.ensure_capacity(499) == 499


def test_lower_configured_limits_are_respected(monkeypatch, limiter_factory):
    create, clock = limiter_factory
    monkeypatch.setenv("GEMINI_RPM", "6")
    monkeypatch.setenv("GEMINI_RPD", "2")
    limiter = create()
    limiter.wait_for_slot()
    limiter.wait_for_slot()

    assert clock.waits == [12.5]
    with pytest.raises(RuntimeError, match="exhausted"):
        limiter.wait_for_slot()


@pytest.mark.parametrize("name", ["GEMINI_RPM", "GEMINI_RPD"])
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_rejects_invalid_limits(monkeypatch, limiter_factory, name, value):
    create, _ = limiter_factory
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        create()
