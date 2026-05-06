"""Tests for the Volcengine TTS voice→resource_id inference.

Regression guard for the "TTS synthesis failed (ConnectionResetError)"
bug where the server dropped the WebSocket because the voice was from
one catalog (BigTTS 2.0) but the code sent the resource_id of another
(Seed-TTS 2.0).
"""

from __future__ import annotations

from syll.providers.voice_volc import (
    _BIGTTS_2_RESOURCE,
    _BUILTIN_VOICE_RESOURCES,
    _SEED_TTS_2_RESOURCE,
    _infer_voice_resource,
)


def test_builtin_catalog_includes_both_families():
    # Sanity: the builtin map has entries from both catalogs.
    resources = set(_BUILTIN_VOICE_RESOURCES.values())
    assert _BIGTTS_2_RESOURCE in resources
    assert _SEED_TTS_2_RESOURCE in resources


def test_seed_tts_default_voice_resolves():
    rid, exact = _infer_voice_resource(
        "zh_female_vv_uranus_bigtts", None, "seed-tts-2.0"
    )
    assert rid == _SEED_TTS_2_RESOURCE
    assert exact is True


def test_bigtts_voice_overrides_configured_default():
    # The whole point of this helper: even if the configured default
    # resource_id is seed-tts-2.0, a BigTTS voice must pick volc.10029.
    rid, exact = _infer_voice_resource(
        "zh_female_shuangkuaisisi_moon_bigtts", None, "seed-tts-2.0"
    )
    assert rid == _BIGTTS_2_RESOURCE
    assert exact is True


def test_user_map_wins_over_builtin():
    rid, exact = _infer_voice_resource(
        "zh_female_vv_uranus_bigtts",
        {"zh_female_vv_uranus_bigtts": "custom-resource"},
        "seed-tts-2.0",
    )
    assert rid == "custom-resource"
    assert exact is True


def test_unknown_moon_bigtts_heuristic():
    # Voices not in the builtin map but ending _moon_bigtts are
    # heuristically assumed to be BigTTS — exact=False so the caller logs
    # a warning.
    rid, exact = _infer_voice_resource(
        "zh_male_totally_new_moon_bigtts", None, "seed-tts-2.0"
    )
    assert rid == _BIGTTS_2_RESOURCE
    assert exact is False


def test_unknown_voice_falls_back_to_configured_default():
    rid, exact = _infer_voice_resource(
        "nonsense-voice", None, "seed-tts-2.0"
    )
    assert rid == "seed-tts-2.0"
    assert exact is False


def test_empty_user_map_behaves_like_none():
    rid_none, _ = _infer_voice_resource("zh_female_vv_uranus_bigtts", None, "x")
    rid_empty, _ = _infer_voice_resource("zh_female_vv_uranus_bigtts", {}, "x")
    assert rid_none == rid_empty == _SEED_TTS_2_RESOURCE
