"""#2618: opt-in "offload the TTS model to RAM after generation".

Users sharing the GPU with a local LLM asked for VRAM back once a generation
is done. Contract pinned here:

* default OFF — a pool drain moves nothing and arms nothing;
* when on, a drained GPU pool moves the resident model to CPU after a grace
  period, and the next generation (get_model()'s heal or the cached
  OmniVoiceBackend path) moves it back to the device it came from;
* CUDA and MPS both move (MPS is unified memory, which the ASR-offload heal
  deliberately ignores — the RAM-offload record must override that);
* a CPU-resident model is a no-op;
* never while another GPU job is running/queued or a background generation
  job (dub, batch, audiobook) is active;
* a failed restore leaves the model wholly on CPU (usable), not split.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("OMNIVOICE_MODEL", "test")
os.environ.setdefault("OMNIVOICE_DISABLE_FILE_LOG", "1")

import pytest


class _FakeDev:
    def __init__(self, spec: str):
        self.type, _, idx = spec.partition(":")
        self.index = int(idx) if idx else None


class _FakeParam:
    def __init__(self, device: str):
        self.device = _FakeDev(device)


class _FakeTTS:
    """Records ``.to()`` moves and owns one parameter, like the TTS runtime."""

    def __init__(self, device: str = "cuda:0", fail_on: str | None = None):
        self._param = _FakeParam(device)
        self.moves: list[str] = []
        self.fail_on = fail_on

    def parameters(self):
        yield self._param

    def to(self, device):
        self.moves.append(device)
        if self.fail_on is not None and device == self.fail_on:
            raise RuntimeError("CUDA out of memory")
        self._param = _FakeParam(device)
        return self

    @property
    def where(self) -> str:
        d = self._param.device
        return d.type if d.index is None else f"{d.type}:{d.index}"


@pytest.fixture
def mm(monkeypatch):
    import services.model_manager as _mm

    monkeypatch.setattr(_mm, "_ram_offload", None)
    monkeypatch.setattr(_mm, "free_vram", lambda: None)
    monkeypatch.setattr(_mm, "_generation_jobs_active", lambda: False)
    monkeypatch.delenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION", raising=False)
    yield _mm
    with _mm._offload_timer_lock:
        if _mm._offload_timer is not None:
            _mm._offload_timer.cancel()


@pytest.fixture
def host(mm, monkeypatch):
    def _install(device: str, *, dedicated: bool = True, **kw) -> _FakeTTS:
        fake = _FakeTTS(device, **kw)
        monkeypatch.setattr(mm, "model", fake, raising=False)
        monkeypatch.setattr(mm, "_has_dedicated_vram", lambda: dedicated)
        monkeypatch.setattr(mm, "get_best_device", lambda: device.split(":")[0])
        return fake

    return _install


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION", "1")


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ── Default off ───────────────────────────────────────────────────────────


def test_default_is_off_and_a_drain_moves_nothing(mm, host, monkeypatch):
    monkeypatch.setenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION_GRACE_S", "0")
    fake = host("cuda:0")
    assert mm.offload_after_generation_enabled() is False
    mm._get_gpu_pool().submit(lambda: None).result(timeout=10)
    time.sleep(0.2)
    assert fake.moves == []
    assert mm._ram_offload is None


def test_saved_pref_enables_it_and_env_wins(mm, monkeypatch):
    from core import prefs

    prefs.set_(mm.OFFLOAD_AFTER_GENERATION_PREF, True)
    assert mm.offload_after_generation_enabled() is True
    monkeypatch.setenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION", "0")
    assert mm.offload_after_generation_enabled() is False


# ── Offload and restore per device ────────────────────────────────────────


@pytest.mark.parametrize("device,dedicated", [("cuda:0", True), ("mps", False), ("xpu:0", True)])
def test_offload_then_restore_round_trips_to_the_same_device(mm, host, device, dedicated):
    fake = host(device, dedicated=dedicated)
    assert mm.offload_tts_to_ram() is True
    assert fake.where == "cpu"
    assert mm._stranded_tts_target() == device
    assert mm.ensure_tts_on_device() is True
    assert fake.where == device
    assert mm._ram_offload is None
    assert mm._stranded_tts_target() is None  # hot path is quiet again


def test_cpu_host_is_a_noop(mm, host, enabled):
    fake = host("cpu", dedicated=False)
    assert mm.offload_tts_to_ram() is False
    mm._offload_when_idle()
    assert fake.moves == []
    assert mm._ram_offload is None


def test_no_model_loaded_is_a_noop(mm, monkeypatch, enabled):
    monkeypatch.setattr(mm, "model", None, raising=False)
    assert mm.offload_tts_to_ram() is False
    mm._note_gpu_pool_idle()
    assert mm._offload_timer is None or not mm._offload_timer.is_alive()


# ── Never under load ──────────────────────────────────────────────────────


def test_offload_job_skips_when_another_job_is_running(mm, host, enabled, monkeypatch):
    fake = host("cuda:0")
    monkeypatch.setattr(mm, "gpu_pool_stats", lambda *a, **k: {"queued": 0, "running": 2, "workers": 2})
    assert mm._offload_job() is False
    monkeypatch.setattr(mm, "gpu_pool_stats", lambda *a, **k: {"queued": 1, "running": 1, "workers": 1})
    assert mm._offload_job() is False
    assert fake.moves == []


def test_idle_callback_skips_while_a_generation_job_is_active(mm, host, enabled, monkeypatch):
    fake = host("cuda:0")
    submitted = []

    class _Pool:
        def submit(self, fn):
            submitted.append(fn)
            raise AssertionError("must not submit")

    monkeypatch.setattr(mm, "gpu_pool_stats", lambda *a, **k: {"queued": 0, "running": 0, "workers": 1})
    monkeypatch.setattr(mm, "_get_gpu_pool", lambda: _Pool())
    monkeypatch.setattr(mm, "_generation_jobs_active", lambda: True)
    mm._offload_when_idle()
    assert submitted == [] and fake.moves == []
    monkeypatch.setattr(mm, "_generation_jobs_active", lambda: False)
    mm._offload_when_idle()  # idle and no job: it would submit (swallowed here)
    assert len(submitted) == 1


def test_generation_jobs_active_reads_job_store_and_batch(monkeypatch):
    import sys

    import services.model_manager as _mm
    from core import job_store

    monkeypatch.setattr(job_store, "list_jobs", lambda **k: [{"id": "dub-1"}])
    assert _mm._generation_jobs_active() is True
    monkeypatch.setattr(job_store, "list_jobs", lambda **k: [])

    class _Batch:
        @staticmethod
        def list_batch_jobs(status=None, limit=50):
            return [{"id": "b"}] if status == "active" else []

    monkeypatch.setitem(sys.modules, "api.routers.batch", _Batch)
    assert _mm._generation_jobs_active() is True
    monkeypatch.setitem(sys.modules, "api.routers.batch", None)
    assert _mm._generation_jobs_active() is False


def test_back_to_back_drains_rearm_a_single_timer(mm, host, enabled, monkeypatch):
    host("cuda:0")
    monkeypatch.setenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION_GRACE_S", "30")
    mm._note_gpu_pool_idle()
    first = mm._offload_timer
    mm._note_gpu_pool_idle()
    second = mm._offload_timer
    assert first is not second
    assert first.finished.is_set()  # cancelled
    assert second.is_alive()


# ── End to end through the real pool ──────────────────────────────────────


def test_pool_drain_offloads_and_the_next_generate_restores(mm, host, enabled, monkeypatch):
    monkeypatch.setenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION_GRACE_S", "0")
    fake = host("cuda:0")
    mm._get_gpu_pool().submit(lambda: "generated").result(timeout=10)
    assert _wait_for(lambda: fake.where == "cpu"), fake.moves
    assert mm._ram_offload == (id(fake), "cuda:0")

    # The cached adapter path (/v1/audio/speech, WS TTS) skips get_model():
    # its _ensure_loaded must restore before generating.
    from services.tts_backend import OmniVoiceBackend

    OmniVoiceBackend(model=fake)._ensure_loaded()
    assert fake.where == "cuda:0"
    assert mm._ram_offload is None


def test_get_model_warm_path_restores(mm, host, monkeypatch):
    import asyncio

    fake = host("cuda:0")
    monkeypatch.setattr(mm, "make_room_before_generate", lambda: None)
    assert mm.offload_tts_to_ram() is True
    asyncio.run(mm.get_model())
    assert fake.where == "cuda:0"


# ── Failure handling ──────────────────────────────────────────────────────


def test_failed_restore_leaves_the_model_whole_on_cpu_and_retries(mm, host):
    fake = host("cuda:0")
    assert mm.offload_tts_to_ram() is True
    fake.fail_on = "cuda:0"
    assert mm.ensure_tts_on_device() is False
    assert fake.where == "cpu"
    assert mm._ram_offload is not None  # the next generation retries
    fake.fail_on = None
    assert mm.ensure_tts_on_device() is True
    assert fake.where == "cuda:0"


def test_unload_clears_the_offload_record(mm, host, monkeypatch):
    host("cuda:0")
    monkeypatch.setattr(mm, "release_tts_side_caches", lambda: None)
    assert mm.offload_tts_to_ram() is True
    assert mm.unload_shared_model() is True
    assert mm._ram_offload is None


# ── Settings endpoint ─────────────────────────────────────────────────────


def test_settings_endpoint_round_trips(monkeypatch):
    from fastapi.testclient import TestClient

    from main import app

    monkeypatch.delenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION", raising=False)
    c = TestClient(app, client=("127.0.0.1", 50000))
    url = "/api/settings/perf/offload-after-generation"
    state = c.get(url).json()
    assert state["enabled"] is False and state["env_pinned"] is False
    assert isinstance(state["device"], str) and state["device"]
    assert c.put(url, json={"enabled": True}).json()["enabled"] is True
    assert c.get(url).json()["enabled"] is True
    monkeypatch.setenv("OMNIVOICE_OFFLOAD_AFTER_GENERATION", "0")
    pinned = c.get(url).json()
    assert pinned["enabled"] is False and pinned["env_pinned"] is True
    assert c.put(url, json={"enabled": "nope"}).status_code == 422
