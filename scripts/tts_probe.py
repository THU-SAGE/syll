#!/usr/bin/env python3
"""Minimal TTS probe that talks to Bytedance directly and dumps every frame.

Run with::

    python scripts/tts_probe.py \
        --voice zh_female_shuangkuaisisi_moon_bigtts \
        --resource volc.service_type.10029

It bypasses the AgentLoop, config, and tool layers — just the raw
WebSocket protocol — so we can see exactly which header or payload the
server rejects.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Make syll importable when running from the repo root.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import websockets  # noqa: E402

from syll.providers.voice_volc import (  # noqa: E402
    _EV_FINISH_CONNECTION,
    _EV_FINISH_SESSION,
    _EV_START_CONNECTION,
    _EV_START_SESSION,
    _EV_TASK_REQUEST,
    _MSG_AUDIO_SERVER,
    _MSG_FULL_SERVER,
    _MSG_SERVER_ERROR,
    _tts_full_client_event,
    _tts_parse_frame,
)

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"


def load_creds(args) -> tuple[str, str]:
    appid = args.appid or os.getenv("VOLC_APPID") or os.getenv("VOLCENGINE_APPID")
    access_token = (
        args.access_token
        or os.getenv("VOLC_ACCESS_TOKEN")
        or os.getenv("VOLCENGINE_ACCESS_TOKEN")
    )
    if not appid or not access_token:
        raise SystemExit(
            "Missing credentials. Pass --appid/--access-token or export "
            "VOLC_APPID and VOLC_ACCESS_TOKEN."
        )
    return appid, access_token


def dump_frame(label: str, frame: bytes) -> None:
    print(f"\n── {label}  ({len(frame)} bytes) ──")
    print(frame[:64].hex(" "))
    try:
        mt, fl, ev, pl = _tts_parse_frame(frame)
    except Exception as e:
        print(f"  [could not parse: {e}]")
        return
    mt_name = {
        _MSG_FULL_SERVER: "FullServer",
        _MSG_AUDIO_SERVER: "AudioServer",
        _MSG_SERVER_ERROR: "ServerError",
    }.get(mt, f"0b{mt:04b}")
    print(f"  msg_type={mt_name}  flags=0b{fl:04b}  event={ev}  payload_len={len(pl)}")
    if pl:
        head = pl[:400]
        try:
            txt = head.decode("utf-8", errors="replace")
            print(f"  payload: {txt}")
        except Exception:
            print(f"  payload (hex): {head.hex(' ')}")


async def probe(args) -> int:
    appid, access_token = load_creds(args)
    session_id = uuid.uuid4().hex
    headers = {
        "X-Api-App-Key": appid,
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": args.resource,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    print(f"Connecting to {ENDPOINT}")
    print(f"  appid          = {appid}")
    print(f"  access_token   = {access_token[:6]}…{access_token[-4:]}")
    print(f"  X-Api-Resource = {args.resource}")
    print(f"  speaker        = {args.voice}")
    print(f"  session_id     = {session_id}")
    print(f"  websockets ver = {websockets.__version__}")

    try:
        async with websockets.connect(
            ENDPOINT,
            additional_headers=headers,
            max_size=20 * 1024 * 1024,
        ) as ws:
            # Inspect handshake response headers — auth failures usually
            # show up here with X-Tt-Logid.
            try:
                resp_headers = dict(ws.response.headers)  # websockets 13+
            except Exception:
                resp_headers = dict(getattr(ws, "response_headers", {}) or {})
            if resp_headers:
                print("\n── Server response headers ──")
                for k, v in resp_headers.items():
                    if k.lower().startswith(("x-", "tt-", "log")):
                        print(f"  {k}: {v}")

            await ws.send(
                _tts_full_client_event(_EV_START_CONNECTION, {})
            )
            print("\n→ sent StartConnection")

            first = await asyncio.wait_for(ws.recv(), timeout=6.0)
            dump_frame("recv after StartConnection", bytes(first))
            try:
                mt, _fl, ev, _pl = _tts_parse_frame(bytes(first))
            except Exception:
                mt, ev = 0, 0
            if mt == _MSG_SERVER_ERROR or ev not in (50,):
                print("\n[STOP] server did not return ConnectionStarted (event 50).")
                return 1

            await ws.send(
                _tts_full_client_event(
                    _EV_START_SESSION,
                    {
                        "user": {"uid": "probe"},
                        "event": _EV_START_SESSION,
                        "namespace": "BidirectionalTTS",
                        "req_params": {
                            "speaker": args.voice,
                            "audio_params": {"format": "mp3", "sample_rate": 24000},
                        },
                    },
                    session_id=session_id,
                )
            )
            print("\n→ sent StartSession")

            frame = await asyncio.wait_for(ws.recv(), timeout=6.0)
            dump_frame("recv after StartSession", bytes(frame))

            await ws.send(
                _tts_full_client_event(
                    _EV_TASK_REQUEST,
                    {
                        "user": {"uid": "probe"},
                        "event": _EV_TASK_REQUEST,
                        "namespace": "BidirectionalTTS",
                        "req_params": {
                            "text": args.text,
                            "speaker": args.voice,
                            "audio_params": {"format": "mp3", "sample_rate": 24000},
                        },
                    },
                    session_id=session_id,
                )
            )
            print("\n→ sent TaskRequest")

            await ws.send(
                _tts_full_client_event(
                    _EV_FINISH_SESSION, {}, session_id=session_id
                )
            )
            print("→ sent FinishSession")

            audio = bytearray()
            try:
                while True:
                    frame = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    raw = bytes(frame)
                    try:
                        mt, fl, ev, pl = _tts_parse_frame(raw)
                    except Exception as e:
                        print(f"\n[parse error] {e}; frame={raw[:32].hex(' ')}")
                        break
                    if mt == _MSG_AUDIO_SERVER:
                        audio.extend(pl)
                        print(f"  ← audio chunk event={ev} len={len(pl)} (total {len(audio)})")
                        continue
                    dump_frame(f"recv event={ev}", raw)
                    if mt == _MSG_SERVER_ERROR:
                        print("\n[STOP] server error frame.")
                        break
                    if ev == 52:  # ConnectionFinished
                        break
                    if ev == 152:  # SessionFinished
                        # politely close
                        try:
                            await ws.send(
                                _tts_full_client_event(_EV_FINISH_CONNECTION, {})
                            )
                        except Exception:
                            pass
            except asyncio.TimeoutError:
                print("\n[STOP] timeout waiting for frame.")
            if audio:
                out = Path(REPO, "scripts", "tts_probe_out.mp3")
                out.write_bytes(bytes(audio))
                print(f"\n✓ wrote {len(audio)} bytes → {out}")
                return 0
            print("\n✗ no audio bytes received.")
            return 2
    except Exception as e:
        print(f"\n[EXCEPTION] {type(e).__name__}: {e!r}")
        return 3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--appid")
    ap.add_argument("--access-token")
    ap.add_argument("--voice", default="zh_female_shuangkuaisisi_moon_bigtts")
    ap.add_argument("--resource", default="volc.service_type.10029")
    ap.add_argument("--text", default="你好，这是一条语音合成测试。")
    args = ap.parse_args()
    rc = asyncio.run(probe(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
