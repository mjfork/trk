"""timing.assert_fast against a fake clock: a loaded host passes, a 10x slowdown fails, a spike is retried."""

import pytest

from timing import assert_fast


class FakeClock:
    """A clock each fake command advances by its own duration, so no test sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def command(self, *durations):
        """A command taking each of `durations` in turn, then the last one forever."""
        queue = list(durations)

        def run():
            self.now += queue.pop(0) if len(queue) > 1 else queue[0]

        return run


@pytest.fixture
def clock():
    return FakeClock()


def test_a_quiet_host_passes_under_the_budget(clock):
    assert_fast(clock.command(0.110), clock.command(0.110), 0.150, clock=clock)


def test_a_quiet_host_over_the_budget_fails_even_close_to_the_baseline(clock):
    with pytest.raises(AssertionError, match="over 150 ms"):
        assert_fast(clock.command(0.160), clock.command(0.110), 0.150, clock=clock)


def test_a_loaded_host_passes_when_the_command_keeps_pace_with_the_baseline(clock):
    assert_fast(clock.command(0.400), clock.command(0.350), 0.150, clock=clock)


@pytest.mark.parametrize("base", [0.110, 0.350])
def test_a_10x_slowdown_fails_on_a_quiet_or_loaded_host(clock, base):
    with pytest.raises(AssertionError, match="twice"):
        assert_fast(clock.command(10 * base), clock.command(base), 0.150, clock=clock)


def test_a_load_spike_on_the_first_reading_is_measured_again(clock):
    first, second = [0.500] * 7, [0.110] * 7
    assert_fast(clock.command(*first, *second), clock.command(0.110), 0.150, clock=clock)
