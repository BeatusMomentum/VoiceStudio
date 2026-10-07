"""The shutdown-state reset (#1269) must cover ``tests/`` too, not only
``backend/tests/``. CI runs ``tests/`` in one process, and any test there that
exits the app lifespan (the dub tests do) leaves ``model_manager`` in shutdown
mode; before the reset moved to the root conftest.py, the next test in the
directory inherited it (the post-generation offload tests failed that way).

Order-dependent on purpose: the first test dirties exactly what the lifespan
dirties, the second asserts it arrived clean.
"""
import services.model_manager as mm


def test_dirty_the_shutdown_state():
    mm.begin_shutdown()
    mm._reset_gpu_pool()
    assert mm.is_shutting_down()


def test_next_test_starts_clean():
    assert not mm.is_shutting_down(), (
        "the shutdown flag leaked into the next tests/ test — the autouse reset "
        "in the repository-root conftest.py is not running for tests/ (#1269)"
    )
