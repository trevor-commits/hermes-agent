"""Audio discovery respects the activated toolchain before local fallback prefixes."""

import pytest

from tools import transcription_audio as audio


@pytest.mark.parametrize("binary", ["ffmpeg", "ffprobe", "whisper"])
def test_audio_binary_prefers_selected_path_over_local_prefix(tmp_path, monkeypatch, binary):
    local = tmp_path / "local"
    local.mkdir()
    fallback = local / binary
    fallback.touch(mode=0o755)
    selected = str(tmp_path / "managed" / binary)
    monkeypatch.setattr(audio, "COMMON_LOCAL_BIN_DIRS", (str(local),))
    monkeypatch.setattr(audio.shutil, "which", lambda name: selected if name == binary else None)

    assert audio._find_binary(binary) == selected


def test_audio_binary_uses_local_prefix_when_path_has_none(tmp_path, monkeypatch):
    fallback = tmp_path / "ffmpeg"
    fallback.touch(mode=0o755)
    monkeypatch.setattr(audio, "COMMON_LOCAL_BIN_DIRS", (str(tmp_path),))
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)

    assert audio._find_ffmpeg_binary() == str(fallback)


def test_audio_binary_reports_missing_when_no_candidate_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "COMMON_LOCAL_BIN_DIRS", (str(tmp_path),))
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)

    assert audio._find_ffmpeg_binary() is None


def test_discord_explicit_ffmpeg_override_precedes_shared_discovery(tmp_path, monkeypatch):
    from plugins.platforms.discord import ffmpeg_utils

    explicit = str(tmp_path / "chosen ffmpeg")
    monkeypatch.setenv("FFMPEG_PATH", explicit)
    monkeypatch.setattr(ffmpeg_utils, "_shared_find_ffmpeg", lambda: pytest.fail("override ignored"))

    assert ffmpeg_utils.resolve_ffmpeg_executable() == explicit
