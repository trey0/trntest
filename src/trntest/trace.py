"""Toggle for this project's lightweight `print()`-based operation tracing (cache hits/misses,
external tool invocations, atomic-publish writes -- see the call sites in `cache.py`, `isis_wac.py`,
`product_io.py`, `subprocess_utils.py`).

Off by default, so interactive work (notebooks, one-off `docker compose run` commands) stays quiet.
`tasks.py`'s `_capture_generator_log` turns it on for the duration of one generator task's `generate()`
call, so that per-entry log file gets full detail without flooding interactive stdout.
"""

import contextlib
from collections.abc import Iterator


class _State:
    enabled = False


_state = _State()


def enabled() -> bool:
    return _state.enabled


@contextlib.contextmanager
def enable() -> Iterator[None]:
    """Turns tracing on for the duration of the `with` block, restoring the prior state after."""
    previous = _state.enabled
    _state.enabled = True
    try:
        yield
    finally:
        _state.enabled = previous
