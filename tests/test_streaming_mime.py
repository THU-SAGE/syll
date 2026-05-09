"""_encode_media must resolve audio MIME types, not fall back to image/png."""

import base64

from syll.web.streaming import _encode_media


def test_mp3_resolves_to_audio_mpeg(tmp_path):
    mp3 = tmp_path / "voice.mp3"
    mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00fake")

    result = _encode_media([str(mp3)])
    assert len(result) == 1
    assert result[0]["mime"] == "audio/mpeg"
    assert base64.b64decode(result[0]["data"]).startswith(b"ID3")


def test_wav_resolves_to_audio(tmp_path):
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"RIFF____WAVEfake")

    result = _encode_media([str(wav)])
    assert result[0]["mime"].startswith("audio/")


def test_unknown_extension_falls_back_to_octet_stream(tmp_path):
    blob = tmp_path / "voice.syllext"
    blob.write_bytes(b"\x00\x01\x02")

    result = _encode_media([str(blob)])
    # Must NOT mis-label as image/png (the pre-fix default).
    assert result[0]["mime"] == "application/octet-stream"


def test_missing_file_is_skipped(tmp_path):
    result = _encode_media([str(tmp_path / "nope.mp3")])
    assert result == []
