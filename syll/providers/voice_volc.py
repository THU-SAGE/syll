"""Volcengine (Doubao) voice providers: ASR + TTS over WebSocket v3.

Two distinct binary protocols share the same 4-byte header layout:

* **ASR** (``/api/v3/sauc/bigmodel_nostream``): a simple
  "full-client-request (JSON) + audio-only packets + final negative
  packet" flow, compatible with the public Seed ASR 2.0 docs.
* **TTS** (``/api/v3/tts/bidirection``): an **event-based** flow
  (``StartConnection`` → ``StartSession`` → ``TaskRequest`` →
  ``FinishSession`` → ``FinishConnection``). Each upstream frame
  carries a 4-byte ``int32`` event number right after the header, and
  a length-prefixed ``session_id`` / ``connection_id``.

Header layout (4 bytes, shared):
    byte 0 = (protocol_version << 4) | header_size_in_words   (0x11)
    byte 1 = (message_type     << 4) | message_specific_flags
    byte 2 = (serialization    << 4) | compression
    byte 3 = reserved (0)

Message types:
    0b0001 full client request  (JSON)
    0b0010 audio-only request   (raw audio bytes)
    0b1001 full server response (JSON)
    0b1011 audio-only response  (TTS audio chunks)
    0b1111 server error frame

TTS message_specific_flags bit layout (only ``0b0100`` / "with event"
is used; the server rejects ``0b0000`` on the v3 bidirection path).
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import socket
import struct
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

# Reserved / fake-IP ranges used by transparent TUN proxies (Clash, Surge,
# Shadowrocket, Loon, …). A DNS lookup that returns an IP inside these
# ranges means your local proxy is intercepting the request but does NOT
# necessarily tunnel it — if the proxy has no matching rule for the
# destination, TCP connects succeed but TLS fails silently with
# ConnectionResetError. This has nothing to do with our credentials or
# the TTS protocol.
_FAKE_IP_NETS = [
    ipaddress.ip_network("198.18.0.0/15"),    # RFC2544 benchmarking → Clash/Surge
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT → Shadowrocket / Loon
]


def _looks_like_fake_ip_host(host: str) -> str | None:
    """Return a diagnostic note if the host resolves into a fake-IP
    range used by transparent TUN proxies. Returns ``None`` when DNS
    resolution fails or the IP looks normal.
    """
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net in _FAKE_IP_NETS:
        if addr in net:
            return (
                f"{host} resolved to {ip}, which is inside {net} — a fake-IP "
                "range used by TUN-mode proxies (Clash / Surge / Shadowrocket). "
                "Your local proxy is intercepting DNS but likely has no "
                "forwarding rule for bytedance.com. Add "
                "DOMAIN-SUFFIX,bytedance.com,DIRECT (or route through a working "
                "proxy group) — or temporarily quit the proxy to test."
            )
    return None

try:  # websockets is a common transitive dep; only fail when actually called.
    import websockets
    import websockets.exceptions as _ws_exc
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore
    _ws_exc = None  # type: ignore


# ── Shared header helpers ─────────────────────────────────────────────

_PROTOCOL_VERSION = 0b0001
_HEADER_SIZE = 0b0001  # 4 bytes

_MSG_FULL_CLIENT = 0b0001
_MSG_AUDIO_ONLY = 0b0010
_MSG_FULL_SERVER = 0b1001
_MSG_AUDIO_SERVER = 0b1011
_MSG_SERVER_ERROR = 0b1111

_FLAG_NONE = 0b0000
_FLAG_LAST_NO_SEQ = 0b0010  # last audio packet, no seq (ASR)
_FLAG_WITH_EVENT = 0b0100   # carries an int32 event right after header (TTS)

_SER_JSON = 0b0001
_SER_RAW = 0b0000

_COMP_NONE = 0b0000
_COMP_GZIP = 0b0001


def _pack_header(msg_type: int, flags: int, ser: int, comp: int) -> bytes:
    return bytes(
        [
            (_PROTOCOL_VERSION << 4) | _HEADER_SIZE,
            (msg_type << 4) | flags,
            (ser << 4) | comp,
            0,
        ]
    )


# ── ASR framing (no event; compatible with bigmodel_nostream) ────────


def _asr_full_client(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body = gzip.compress(raw)
    header = _pack_header(_MSG_FULL_CLIENT, _FLAG_NONE, _SER_JSON, _COMP_GZIP)
    return header + struct.pack(">I", len(body)) + body


def _asr_audio_packet(chunk: bytes, last: bool) -> bytes:
    body = gzip.compress(chunk) if chunk else b""
    flags = _FLAG_LAST_NO_SEQ if last else _FLAG_NONE
    header = _pack_header(_MSG_AUDIO_ONLY, flags, _SER_RAW, _COMP_GZIP)
    return header + struct.pack(">I", len(body)) + body


def _asr_parse_frame(frame: bytes) -> tuple[int, int, bytes]:
    """Return (msg_type, flags, payload_bytes) for an ASR response."""
    if len(frame) < 4:
        raise ValueError("short frame")
    header_size_words = frame[0] & 0x0F
    header_len = header_size_words * 4
    msg_type = (frame[1] >> 4) & 0x0F
    flags = frame[1] & 0x0F
    comp = frame[2] & 0x0F
    body = frame[header_len:]

    offset = 0
    if flags & 0b0001:  # sequence number present
        offset += 4
    size = struct.unpack(">I", body[offset:offset + 4])[0]
    offset += 4
    payload = body[offset:offset + size]
    if comp == _COMP_GZIP and payload:
        payload = gzip.decompress(payload)
    return msg_type, flags, payload


# ── TTS event-based framing ──────────────────────────────────────────

# Upstream events
_EV_START_CONNECTION = 1
_EV_FINISH_CONNECTION = 2
_EV_START_SESSION = 100
_EV_FINISH_SESSION = 102
_EV_TASK_REQUEST = 200

# Downstream events
_EV_CONNECTION_STARTED = 50
_EV_CONNECTION_FAILED = 51
_EV_CONNECTION_FINISHED = 52
_EV_SESSION_STARTED = 150
_EV_SESSION_FINISHED = 152
_EV_SESSION_FAILED = 153
_EV_TTS_SENTENCE_START = 350
_EV_TTS_SENTENCE_END = 351
_EV_TTS_RESPONSE = 352


def _tts_full_client_event(
    event: int,
    payload: dict[str, Any],
    session_id: str | None = None,
) -> bytes:
    """Build a Full-client-request frame with event + optional session id.

    Layout:
        header(4) | event(int32) | [sid_len(u32) | sid_bytes] | payload_len(u32) | payload_json
    """
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = _pack_header(
        _MSG_FULL_CLIENT, _FLAG_WITH_EVENT, _SER_JSON, _COMP_NONE
    )
    parts = [header, struct.pack(">i", event)]
    if session_id is not None:
        sid_bytes = session_id.encode("ascii")
        parts.append(struct.pack(">I", len(sid_bytes)))
        parts.append(sid_bytes)
    parts.append(struct.pack(">I", len(raw)))
    parts.append(raw)
    return b"".join(parts)


def _tts_parse_frame(frame: bytes) -> tuple[int, int, int, bytes]:
    """Parse a server frame. Returns (msg_type, flags, event, payload)."""
    if len(frame) < 4:
        raise ValueError("short frame")
    header_size_words = frame[0] & 0x0F
    header_len = header_size_words * 4
    msg_type = (frame[1] >> 4) & 0x0F
    flags = frame[1] & 0x0F
    comp = frame[2] & 0x0F
    rest = frame[header_len:]

    event = 0
    if flags & _FLAG_WITH_EVENT:
        event = struct.unpack(">i", rest[:4])[0]
        rest = rest[4:]

    if msg_type == _MSG_SERVER_ERROR:
        # header | error_code(u32) | payload_len(u32) | payload
        _err_code = struct.unpack(">I", rest[:4])[0]
        rest = rest[4:]
        size = struct.unpack(">I", rest[:4])[0]
        payload = rest[4:4 + size]
        if comp == _COMP_GZIP and payload:
            payload = gzip.decompress(payload)
        return msg_type, flags, event, payload

    # Full-server / audio-only response: skip optional session_id then read payload.
    if msg_type in (_MSG_FULL_SERVER, _MSG_AUDIO_SERVER):
        sid_len = struct.unpack(">I", rest[:4])[0]
        rest = rest[4:]
        rest = rest[sid_len:]  # skip session_id
        size = struct.unpack(">I", rest[:4])[0]
        payload = rest[4:4 + size]
        if comp == _COMP_GZIP and payload:
            payload = gzip.decompress(payload)
        return msg_type, flags, event, payload

    return msg_type, flags, event, rest


# ── ASR Provider ──────────────────────────────────────────────────────


class VolcengineASRProvider:
    """Doubao streaming ASR client (Seed ASR 2.0).

    Defaults to ``bigmodel_nostream`` which is the endpoint this account
    is granted on. See ``docs/voice/asr.md`` for protocol details.
    """

    ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"

    def __init__(
        self,
        appid: str,
        access_token: str,
        resource_id: str = "volc.seedasr.sauc.duration",
        cluster: str = "",
    ):
        self.appid = appid
        self.access_token = access_token
        self.resource_id = resource_id
        self.cluster = cluster

    async def transcribe(self, audio_path: str | Path, sample_rate: int = 16000) -> str:
        if websockets is None:
            logger.error("websockets package not installed — cannot call Volcengine ASR")
            return ""
        if not self.appid or not self.access_token:
            logger.warning("Volcengine ASR credentials missing")
            return ""

        path = Path(audio_path)
        if not path.is_file():
            logger.error(f"ASR audio not found: {path}")
            return ""

        audio_bytes = path.read_bytes()
        fmt = path.suffix.lstrip(".").lower() or "wav"
        fmt_map = {
            "wav": "wav", "pcm": "pcm", "mp3": "mp3",
            "ogg": "ogg_opus", "opus": "ogg_opus",
        }
        volc_fmt = fmt_map.get(fmt, "wav")

        params = {
            "user": {"uid": "syll"},
            "audio": {
                "format": volc_fmt,
                "rate": sample_rate,
                "bits": 16,
                "channel": 1,
                "codec": "raw" if volc_fmt == "pcm" else volc_fmt,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "show_utterances": False,
            },
        }

        headers = {
            "X-Api-App-Key": self.appid,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        try:
            async with websockets.connect(
                self.ENDPOINT,
                additional_headers=headers,
                max_size=10 * 1024 * 1024,
            ) as ws:
                await ws.send(_asr_full_client(params))

                chunk_size = 6400 if volc_fmt == "pcm" else 16 * 1024
                total = len(audio_bytes)
                if total == 0:
                    await ws.send(_asr_audio_packet(b"", last=True))
                else:
                    for i in range(0, total, chunk_size):
                        chunk = audio_bytes[i:i + chunk_size]
                        is_last = (i + chunk_size) >= total
                        await ws.send(_asr_audio_packet(chunk, last=is_last))
                        await asyncio.sleep(0.01)

                final_text = ""
                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        msg_type, flags, payload = _asr_parse_frame(bytes(raw))
                    except Exception as e:
                        logger.warning(f"ASR parse error: {e}")
                        continue
                    if msg_type == _MSG_SERVER_ERROR:
                        logger.error(f"ASR server error: {payload[:500]!r}")
                        return ""
                    if msg_type != _MSG_FULL_SERVER:
                        continue
                    try:
                        data = json.loads(payload.decode("utf-8"))
                    except Exception:
                        continue
                    result = data.get("result") or {}
                    t = result.get("text") or ""
                    if t:
                        final_text = t
                    # nostream path: last packet flag means server is done.
                    if flags & _FLAG_LAST_NO_SEQ:
                        break
                return final_text

        except Exception as e:
            logger.error(f"Volcengine ASR failed: {e}")
            return ""


# ── TTS Provider ──────────────────────────────────────────────────────


# Bytedance publishes voices under two resource families. The same
# ``_bigtts`` suffix appears in both, so the voice name alone is NOT a
# reliable discriminator — the entry below is the source of truth. When
# a voice is missing from this map we fall through to the configured
# default ``resource_id`` and log a warning so the user can add it.
_BIGTTS_2_RESOURCE = "volc.service_type.10029"   # BigTTS 2.0 catalog
_SEED_TTS_2_RESOURCE = "seed-tts-2.0"             # Seed-TTS 2.0 catalog

_BUILTIN_VOICE_RESOURCES: dict[str, str] = {
    # ── Seed-TTS 2.0 (planet / zodiac naming) ──────────────────────
    "zh_female_vv_uranus_bigtts": _SEED_TTS_2_RESOURCE,
    "zh_male_sagittarius_mars_bigtts": _SEED_TTS_2_RESOURCE,
    "zh_female_gemini_uranus_bigtts": _SEED_TTS_2_RESOURCE,
    "zh_male_jupiter_mars_bigtts": _SEED_TTS_2_RESOURCE,
    "zh_female_scorpio_uranus_bigtts": _SEED_TTS_2_RESOURCE,
    "zh_female_libra_uranus_bigtts": _SEED_TTS_2_RESOURCE,

    # ── BigTTS 2.0 (character naming, often ``_moon_bigtts``) ──────
    "zh_female_shuangkuaisisi_moon_bigtts": _BIGTTS_2_RESOURCE,
    "zh_female_wanqudashu_moon_bigtts": _BIGTTS_2_RESOURCE,
    "zh_female_wanwanxiaohe_moon_bigtts": _BIGTTS_2_RESOURCE,
    "zh_male_yuanboxiaoshu_moon_bigtts": _BIGTTS_2_RESOURCE,
    "zh_male_shaonianzixin_moon_bigtts": _BIGTTS_2_RESOURCE,
    "zh_male_xionger_mars_bigtts": _BIGTTS_2_RESOURCE,
    "en_female_sarah_moon_bigtts": _BIGTTS_2_RESOURCE,
    "en_male_adam_moon_bigtts": _BIGTTS_2_RESOURCE,
}


def _infer_voice_resource(
    voice: str,
    user_map: dict[str, str] | None,
    fallback: str,
) -> tuple[str, bool]:
    """Return (resource_id, exact_match).

    Lookup order: user override → built-in catalog → heuristic by suffix
    (``_moon_bigtts`` → BigTTS 2.0) → configured fallback.
    """
    if user_map and voice in user_map:
        return user_map[voice], True
    if voice in _BUILTIN_VOICE_RESOURCES:
        return _BUILTIN_VOICE_RESOURCES[voice], True
    # Heuristic: ``_moon_bigtts`` voices are exclusively BigTTS 2.0 in
    # the published catalog. Still mark as inexact so callers can warn.
    if voice.endswith("_moon_bigtts"):
        return _BIGTTS_2_RESOURCE, False
    return fallback, False


class VolcengineTTSProvider:
    """Doubao bigmodel bidirectional streaming TTS (event-based v3).

    Writes the synthesized audio to
    ``~/.syll/media/tts/<uuid>.<ext>`` and returns the resulting
    path. Caller (``SpeakTool``) wraps the path into
    ``ToolResult.media``.
    """

    ENDPOINT = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

    def __init__(
        self,
        appid: str,
        access_token: str,
        default_speaker: str = "zh_female_vv_uranus_bigtts",
        resource_id: str = "seed-tts-2.0",
        media_dir: Path | None = None,
        voice_resources: dict[str, str] | None = None,
    ):
        self.appid = appid
        self.access_token = access_token
        self.default_speaker = default_speaker
        self.resource_id = resource_id
        self.voice_resources = dict(voice_resources) if voice_resources else {}
        self.media_dir = media_dir or (Path.home() / ".syll" / "media" / "tts")

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        fmt: str = "mp3",
    ) -> Path:
        if websockets is None:
            raise RuntimeError("websockets package not installed — cannot call Volcengine TTS")
        if not self.appid or not self.access_token:
            raise RuntimeError("Volcengine TTS credentials missing")

        self.media_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.media_dir / f"{uuid.uuid4().hex}.{fmt}"

        speaker = voice or self.default_speaker
        # Pick the right resource_id for this specific voice. Bytedance
        # groups voices into catalogs (BigTTS 2.0 vs Seed-TTS 2.0); using
        # the wrong resource_id makes the server drop the WS before any
        # audio comes back — which was the root cause of the silent
        # ConnectionResetError users saw in the wild.
        resource_id, exact = _infer_voice_resource(
            speaker, self.voice_resources, self.resource_id
        )
        if not exact:
            logger.warning(
                f"Volcengine TTS: voice {speaker!r} not in builtin catalog or "
                f"voice_resources map; using resource_id={resource_id!r}. "
                "If the server drops the connection, add an entry under "
                "voice.tts.voiceResources in your config."
            )

        audio_params = {
            "format": fmt,
            "sample_rate": 24000,
        }

        session_id = uuid.uuid4().hex
        headers = {
            "X-Api-App-Key": self.appid,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        audio_chunks: list[bytes] = []
        try:
            try:
                ws_ctx = websockets.connect(
                    self.ENDPOINT,
                    additional_headers=headers,
                    max_size=20 * 1024 * 1024,
                )
            except TypeError:
                # websockets < 13 used ``extra_headers``; fall back transparently
                # so running an older pin here raises our message, not a
                # cryptic kwarg error.
                ws_ctx = websockets.connect(
                    self.ENDPOINT,
                    extra_headers=headers,
                    max_size=20 * 1024 * 1024,
                )
            async with ws_ctx as ws:
                # 1. StartConnection — no session_id.
                await ws.send(
                    _tts_full_client_event(_EV_START_CONNECTION, {})
                )

                # Fast-fail handshake check: Bytedance replies with event 50
                # (ConnectionStarted) within ~1s on success. On auth /
                # resource_id failures it typically drops the TCP socket
                # with no close frame, which would otherwise surface as an
                # empty-string ``ConnectionResetError`` 30+ seconds later.
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError as e:
                    raise RuntimeError(
                        "TTS server did not respond to StartConnection within 5s — "
                        "check network / endpoint reachability."
                    ) from e
                if isinstance(first, (bytes, bytearray)):
                    try:
                        mt, _fl, ev, pl = _tts_parse_frame(bytes(first))
                    except Exception:
                        mt, ev, pl = 0, 0, b""
                    if mt == _MSG_SERVER_ERROR or ev == _EV_CONNECTION_FAILED:
                        raise RuntimeError(
                            f"TTS handshake rejected (event={ev}): {pl[:300]!r}. "
                            "Verify voice.tts.appid / access_token / resource_id. "
                            "BigTTS voices (e.g. *_bigtts) require "
                            "resource_id='volc.service_type.10029'; Seed-TTS 2.0 "
                            "voices require resource_id='seed-tts-2.0'."
                        )

                # 2. StartSession — carries speaker + audio_params; no text yet.
                session_payload = {
                    "user": {"uid": "syll"},
                    "event": _EV_START_SESSION,
                    "namespace": "BidirectionalTTS",
                    "req_params": {
                        "speaker": speaker,
                        "audio_params": audio_params,
                    },
                }
                await ws.send(
                    _tts_full_client_event(
                        _EV_START_SESSION,
                        session_payload,
                        session_id=session_id,
                    )
                )

                # 3. TaskRequest — the actual text to synthesize.
                task_payload = {
                    "user": {"uid": "syll"},
                    "event": _EV_TASK_REQUEST,
                    "namespace": "BidirectionalTTS",
                    "req_params": {
                        "text": text,
                        "speaker": speaker,
                        "audio_params": audio_params,
                    },
                }
                await ws.send(
                    _tts_full_client_event(
                        _EV_TASK_REQUEST,
                        task_payload,
                        session_id=session_id,
                    )
                )

                # 4. FinishSession — tell server we have no more text.
                await ws.send(
                    _tts_full_client_event(
                        _EV_FINISH_SESSION,
                        {},
                        session_id=session_id,
                    )
                )

                # Drain audio frames until session/connection finishes.
                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        msg_type, flags, event, payload = _tts_parse_frame(bytes(raw))
                    except Exception as e:
                        logger.warning(f"TTS parse error: {e}")
                        continue

                    if msg_type == _MSG_SERVER_ERROR:
                        raise RuntimeError(
                            f"TTS server error event={event}: {payload[:500]!r}"
                        )
                    if msg_type == _MSG_AUDIO_SERVER:
                        if payload:
                            audio_chunks.append(payload)
                        continue
                    if msg_type == _MSG_FULL_SERVER:
                        if event in (_EV_CONNECTION_FAILED, _EV_SESSION_FAILED):
                            raise RuntimeError(
                                f"TTS event={event}: {payload[:500]!r}"
                            )
                        if event == _EV_SESSION_FINISHED:
                            # Close our side so the async-for exits promptly.
                            try:
                                await ws.send(
                                    _tts_full_client_event(
                                        _EV_FINISH_CONNECTION, {}
                                    )
                                )
                            except Exception:
                                pass
                        if event == _EV_CONNECTION_FINISHED:
                            break
        except ConnectionResetError as e:
            # Most common causes, in order of what we've actually seen:
            #   1. Local transparent proxy (Clash/Surge) intercepting but
            #      not forwarding — surface a fake-IP diagnostic first.
            #   2. Invalid appid / access_token
            #   3. resource_id that doesn't match the voice catalog
            proxy_hint = _looks_like_fake_ip_host("openspeech.bytedance.com")
            logger.error(
                f"Volcengine TTS: connection reset. Voice={speaker!r}, "
                f"resource_id={resource_id!r}. raw={e!r}. "
                + (f"Network note: {proxy_hint}" if proxy_hint else "")
            )
            if proxy_hint:
                raise RuntimeError(
                    "TTS connection reset before any handshake could complete. "
                    + proxy_hint
                ) from e
            raise RuntimeError(
                "TTS connection reset by server before audio was returned — "
                "usually means invalid appid/access_token OR a resource_id "
                f"that doesn't match the voice. Voice={speaker!r}, "
                f"resource_id={resource_id!r}. Doubao TTS 1.0 voices (e.g. "
                "*_moon_bigtts) need 'volc.service_type.10029'; Doubao TTS 2.0 "
                "voices (planet/zodiac names) need 'seed-tts-2.0'. Add the "
                "mapping under voice.tts.voiceResources in your config."
            ) from e
        except Exception as e:
            if _ws_exc is not None and isinstance(
                e, (_ws_exc.ConnectionClosed, _ws_exc.InvalidHandshake)
            ):
                detail = str(e) or repr(e)
                logger.error(
                    f"Volcengine TTS WS error ({type(e).__name__}): {detail}"
                )
                raise RuntimeError(
                    f"TTS WebSocket {type(e).__name__}: {detail}. "
                    "Check voice.tts.appid / access_token / resource_id, "
                    "and that the speaker id belongs to the selected resource."
                ) from e
            logger.exception(
                f"Volcengine TTS failed ({type(e).__name__}): {e!r}"
            )
            raise

        if not audio_chunks:
            raise RuntimeError("Volcengine TTS returned no audio")
        out_path.write_bytes(b"".join(audio_chunks))
        return out_path
