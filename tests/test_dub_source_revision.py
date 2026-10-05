"""Source subtitles replaced while long dub work runs must win over its result."""
import ast
import asyncio
import io
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import UploadFile

ROUTERS = Path(__file__).resolve().parents[1] / "backend" / "api" / "routers"

# `_sync_job_segments` publishes a render's own segments; it is not a source
# replacement and must not invalidate a concurrent render of another language.
_ALLOWED_WRITERS = {("dub_generate.py", "_sync_job_segments")}


def _segment_writes(tree: ast.AST):
    """(function, line) for every `<name>["segments"] = ...` in a module."""
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            )
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "segments"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "job"
                ):
                    yield func.name, node.lineno


def test_source_subtitles_are_only_replaced_through_the_revision_helper():
    offenders = []
    for path in sorted(ROUTERS.glob("dub_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func, line in _segment_writes(tree):
            if (path.name, func) not in _ALLOWED_WRITERS:
                offenders.append(f"{path.name}:{line} in {func}")
    assert not offenders, (
        "Replace job subtitles with services.dub_pipeline.replace_source_segments "
        f"so running renders and transcriptions see the change: {offenders}"
    )


def _make_wav(path: Path, seconds: float = 1.0, sr: int = 16000) -> None:
    n = int(seconds * sr)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack(f"<{n}h", *([0] * n)))


_SRT = b"1\n00:00:00,000 --> 00:00:00,900\nImported correction\n"


def test_srt_import_during_transcription_survives_its_commit(tmp_path, monkeypatch):
    from api.routers import dub_core as dc
    from services import speaker_clone as sc

    job_id = "t_import_during_asr"
    audio = tmp_path / "a.wav"
    _make_wav(audio)
    dc._dub_jobs[job_id] = {"audio_path": str(audio), "vocals_path": None, "scene_cuts": []}

    fake_model = MagicMock()
    fake_model._asr_pipe = MagicMock()

    async def _ok_model():
        return fake_model

    class _FakeASR:
        id = "fake"

        def ensure_loaded(self):
            pass

        def transcribe(self, *a, **k):
            return {"chunks": [{"text": "asr words", "timestamp": (0.0, 0.5)}],
                    "segments": [], "language": "en"}

        def unload(self):
            pass

    monkeypatch.setattr(dc, "get_model", _ok_model)
    monkeypatch.setattr("services.asr_backend.get_active_asr_backend", lambda *a, **k: _FakeASR())
    monkeypatch.setattr(dc, "offload_tts_for_asr", lambda *a, **k: None)
    monkeypatch.setattr(
        sc, "extract_segment_refs",
        lambda *a, **k: {"0": {"ref_audio": "asr_ref.wav", "ref_text": "asr words"}},
    )

    imported = {}

    def _refine_while_user_imports(refs, _backend):
        # The user imports corrected subtitles while references are refined.
        upload = UploadFile(file=io.BytesIO(_SRT), filename="fixed.srt")
        imported.update(asyncio.run(dc.dub_import_srt(job_id, upload)))
        return refs

    monkeypatch.setattr(sc, "refine_ref_texts", _refine_while_user_imports)

    async def _collect():
        resp = await dc.dub_transcribe_stream(job_id)
        parts = []
        async for chunk in resp.body_iterator:
            parts.append(chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk))
        return "".join(parts)

    try:
        body = asyncio.run(_collect())
        job = dc._dub_jobs[job_id]
    finally:
        dc._dub_jobs.pop(job_id, None)

    assert imported["segments"][0]["text"] == "Imported correction"
    texts = [segment["text"] for segment in job["segments"]]
    assert texts == ["Imported correction"]
    # Nothing from the discarded transcript is attached to the imported cues.
    assert "asr_ref.wav" not in str(job.get("segment_clones"))
    assert not job.get("transcription_complete")
    final = body[body.rfind("event: final"):]
    assert "Imported correction" in final and "asr words" not in final, final
    assert body.rfind("event: done") > body.rfind("event: final")
