import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sofit import generate, model_backends as models
from sofit.footage_selection import ModelFrameJudge, ClaudeFrameJudge


def test_default_transports_and_versioned_cache_identity(monkeypatch):
    monkeypatch.delenv("SOFIT_TITLER_MODEL", raising=False)
    assert models.backend("api").transport is generate._call_api
    assert models.backend("claude-cli").transport is generate._call_claude_cli
    assert ModelFrameJudge is ClaudeFrameJudge
    assert (
        ModelFrameJudge("api").cache_key
        == "claude-frames-v1:api:semantic-v2:" + generate.CLAUDE_MODEL
    )
    assert (
        ModelFrameJudge("claude-cli").cache_key
        == "claude-frames-v1:claude-cli:semantic-v2:configured-cli-default"
    )
    assert (
        ModelFrameJudge("codex-cli").cache_key
        != ModelFrameJudge("claude-cli").cache_key
    )


def test_registered_transport_receives_text_images_and_model(monkeypatch, tmp_path):
    calls = []

    def transport(system, user, model, images=None):
        calls.append((system, user, model, images))
        return '{"ok": true}'

    monkeypatch.setattr(models, "_registered", {})
    models.register_backend("example", transport, cache_key="example-v2")
    image = tmp_path / "frame.jpg"
    for images in (None, [image]):
        assert generate.call_claude_json(
            "system",
            "user",
            lambda x: x,
            model="vision-v1",
            titler="example",
            images=images,
        ) == {"ok": True}
        assert calls[-1] == ("system", "user", "vision-v1", images)
    assert ModelFrameJudge("example").cache_key.startswith("example-v2:")
    with pytest.raises(ValueError, match="unknown model backend"):
        models.backend("typo")


def test_codex_native_images_and_isolated_command(monkeypatch, tmp_path):
    image = tmp_path / "frame.jpg"
    observed = []
    monkeypatch.setattr(models.shutil, "which", lambda name: "/bin/codex")

    def run(command, **kwargs):
        observed.append(command)
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command and "--ephemeral" in command
        assert command[command.index("--image") + 1] == str(image.resolve())
        assert command[command.index("--model") + 1] == "model-v1"
        assert b"Do not use tools" in kwargs["input"]
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"frames": []}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(models.subprocess, "run", run)
    result = models._call_codex_cli("judge", "intent", "model-v1", [image])
    assert json.loads(result) == {"frames": []}
    assert not Path(observed[0][observed[0].index("--cd") + 1]).exists()


def test_codex_quota_is_reported_without_leaking_diagnostics(monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda name: "/bin/codex")

    def run(command, **kwargs):
        kwargs["stderr"].write(b"usage limit reached private diagnostic")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(models.subprocess, "run", run)
    with pytest.raises(generate.GenerationError, match="quota/authentication") as error:
        models._call_codex_cli("judge", "intent", "")
    assert "private diagnostic" not in str(error.value)


def test_source_judgment_adds_attribution_without_replacing_visual_evidence(
    monkeypatch, tmp_path
):
    from sofit.footage import Candidate, VisualIntent
    from sofit.footage_selection import Frame

    candidate = Candidate(
        "youtube",
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://example.org/v.mp4",
        "Manufacturing robot heads",
        30,
        320,
        240,
        creator="Maker",
        description="Publisher-supplied context",
    )

    def call(system, user, validate, **kwargs):
        data = json.loads(user)
        assert data["source_context"]["creator"] == "Maker"
        assert data["source_context"]["source_url"] == candidate.source_url
        assert "Metadata alone cannot establish identity or action" in system
        assert "For manufacturing intents" in system
        assert "actual screen recordings" in system
        assert "explicitly requested" in system
        assert "Never accept an unrelated" in system
        assert kwargs["images"] == [tmp_path / "frame.jpg"]
        return validate(
            {"frames": [{"index": 0, "scores": [0.2], "reason": "unrelated presenter"}]}
        )

    monkeypatch.setattr(generate, "call_claude_json", call)
    assert ModelFrameJudge().score_source(
        [Frame(1, tmp_path / "frame.jpg")],
        [VisualIntent("robot manufacturing", "Maker robots", 3)],
        candidate,
    ) == [[(0.2, "unrelated presenter")]]
