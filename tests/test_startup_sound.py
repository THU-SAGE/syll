from __future__ import annotations

from types import SimpleNamespace

from syll.config.schema import Config


def test_startup_sound_config_defaults_to_packaged_sound():
    config = Config()

    assert config.startup.sound.enabled is True
    assert config.startup.sound.path == ""


def test_resolve_startup_sound_path_uses_user_path_when_configured(tmp_path):
    from syll.cli.startup_sound import resolve_startup_sound_path

    custom_sound = tmp_path / "custom.wav"

    assert resolve_startup_sound_path(str(custom_sound)) == custom_sound


def test_play_startup_sound_dispatches_afplay_on_macos(monkeypatch, tmp_path):
    from syll.cli import startup_sound

    sound_file = tmp_path / "startup.wav"
    sound_file.write_bytes(b"RIFF....WAVE")
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(
        startup_sound,
        "resolve_startup_sound_path",
        lambda configured_path: sound_file,
    )
    monkeypatch.setattr(
        startup_sound.shutil,
        "which",
        lambda command: "/usr/bin/afplay" if command == "afplay" else None,
    )
    monkeypatch.setattr(
        startup_sound.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    startup_sound.play_startup_sound(
        SimpleNamespace(enabled=True, path=""),
        platform="darwin",
    )

    assert calls == [
        (
            ["/usr/bin/afplay", str(sound_file)],
            {
                "stdout": startup_sound.subprocess.DEVNULL,
                "stderr": startup_sound.subprocess.DEVNULL,
                "start_new_session": True,
            },
        )
    ]


def test_play_startup_sound_skips_when_disabled(monkeypatch):
    from syll.cli import startup_sound

    monkeypatch.setattr(
        startup_sound.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not play")),
    )

    startup_sound.play_startup_sound(SimpleNamespace(enabled=False, path=""))
