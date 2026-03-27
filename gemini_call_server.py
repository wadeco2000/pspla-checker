"""Gemini AI Call Server — FastAPI WebSocket bridge between Twilio and Gemini Live API.

This server handles:
1. Creating outbound calls via Twilio
2. Bridging audio between Twilio Media Streams and Gemini Live API
3. Broadcasting live transcripts to dashboard WebSocket clients
4. Supporting barge-in (human takes over from AI)

Run: uvicorn gemini_call_server:app --host 0.0.0.0 --port 8001
"""
import os
import re
import json
import uuid
import hmac
import base64
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SERVER_SECRET = os.getenv("GEMINI_CALL_SERVER_SECRET", "")
SELF_URL = os.getenv("GEMINI_CALL_SERVER_SELF_URL", "http://localhost:8001")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_MODEL = "gemini-3.1-flash-live-preview"  # Gemini 3.1 Flash Live

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gemini-call")

app = FastAPI(title="Gemini Call Server")

# CORS — allow dashboard domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Active call state ────────────────────────────────────────────────────────
_active_calls: Dict[str, dict] = {}
_transcript_subscribers: Dict[str, Set[WebSocket]] = {}
_monitor_subscribers: Dict[str, Set[WebSocket]] = {}


def _verify_secret(request: Request):
    """Verify shared secret on API calls."""
    if not SERVER_SECRET:
        return  # No secret configured — skip (dev mode)
    token = request.headers.get("X-Server-Secret", "")
    if not hmac.compare_digest(token, SERVER_SECRET):
        raise HTTPException(status_code=403, detail="Invalid server secret.")


# ═══════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

_error_log = []  # In-memory error log for debugging

def _log_error(call_id, msg):
    entry = f"[{datetime.now(timezone.utc).isoformat()[:19]}] [{call_id}] {msg}"
    _error_log.append(entry)
    if len(_error_log) > 100:
        _error_log.pop(0)
    log.error(f"[{call_id}] {msg}")


@app.get("/health")
async def health():
    return {"ok": True, "active_calls": len(_active_calls)}


@app.get("/debug/errors")
async def debug_errors(request: Request):
    _verify_secret(request)
    return {"errors": _error_log[-50:]}


@app.get("/debug/active-calls")
async def debug_active_calls(request: Request):
    """Show active call state for debugging."""
    _verify_secret(request)
    return {cid: {k: v for k, v in c.items() if k not in ("gemini_session", "transcript", "twilio_ws")}
            for cid, c in _active_calls.items()}


@app.get("/debug/test-gemini")
async def test_gemini(request: Request):
    """Test if Gemini API key works and Live model is available."""
    _verify_secret(request)
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY not set"}
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        # Test basic API
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents='Say hello in one word.'
        )
        # Check Live model
        return {"ok": True, "text_test": response.text, "live_model": GEMINI_MODEL}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/make-call")
async def make_call(request: Request):
    _verify_secret(request)
    data = await request.json()

    to_number = data.get("to_number", "")
    from_number = data.get("from_number", TWILIO_PHONE_NUMBER)
    system_instruction = data.get("system_instruction", "")
    voice_name = data.get("voice_name", "Kore")
    triggered_by = data.get("triggered_by", "unknown")
    settings = data.get("settings", {})

    if not to_number or not re.match(r"^\+64[2-9]\d{7,9}$", to_number):
        raise HTTPException(status_code=400, detail="Invalid NZ phone number.")
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio not configured.")
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Gemini API key not configured.")

    call_id = str(uuid.uuid4())[:12]
    log.info(f"[{call_id}] Making call to {to_number} from {from_number}")

    # Store call state
    _active_calls[call_id] = {
        "to_number": to_number,
        "from_number": from_number,
        "system_instruction": system_instruction,
        "voice_name": voice_name,
        "triggered_by": triggered_by,
        "settings": settings,
        "call_sid": None,
        "status": "initiating",
        "transcript": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "barged_in": False,
    }
    _transcript_subscribers[call_id] = set()

    # Create Twilio call
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        twiml_url = f"{SELF_URL}/twiml/{call_id}"
        status_url = f"{SELF_URL}/api/call-status"

        recording_callback = f"{SELF_URL}/api/recording-status/{call_id}"
        call = client.calls.create(
            from_=from_number,
            to=to_number,
            url=twiml_url,
            record=True,
            recording_status_callback=recording_callback,
            recording_status_callback_event=["completed"],
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

        _active_calls[call_id]["call_sid"] = call.sid
        _active_calls[call_id]["status"] = "ringing"
        log.info(f"[{call_id}] Twilio call SID: {call.sid}")

        return {"ok": True, "call_id": call_id, "call_sid": call.sid}

    except Exception as e:
        log.error(f"[{call_id}] Twilio error: {e}")
        del _active_calls[call_id]
        del _transcript_subscribers[call_id]
        raise HTTPException(status_code=502, detail=f"Twilio error: {e}")


@app.api_route("/twiml/{call_id}", methods=["GET", "POST"])
async def twiml(call_id: str):
    """Return TwiML that tells Twilio to connect a media stream to us."""
    if call_id not in _active_calls:
        raise HTTPException(status_code=404, detail="Unknown call ID.")

    ws_url = SELF_URL.replace("http://", "wss://").replace("https://", "wss://")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}/media-stream/{call_id}" />
    </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.post("/api/recording-status/{call_id}")
async def recording_status(call_id: str, request: Request):
    """Twilio recording status callback — saves recording URL."""
    if not re.match(r"^[a-f0-9\-]{12}$", call_id):
        return Response(content="Invalid call_id", status_code=400)
    form = await request.form()
    recording_url = form.get("RecordingUrl", "")
    recording_sid = form.get("RecordingSid", "")
    recording_duration = form.get("RecordingDuration", "")

    log.info(f"[{call_id}] Recording ready: {recording_sid} ({recording_duration}s) URL: {recording_url}")

    if recording_url and recording_url.startswith("https://api.twilio.com/"):
        # Twilio recording URLs need .mp3 or .wav appended
        mp3_url = f"{recording_url}.mp3"

        # Update in active calls if still there
        if call_id in _active_calls:
            _active_calls[call_id]["recording_url"] = mp3_url

        # Also update directly in Supabase (call may already be saved)
        if SUPABASE_URL and SUPABASE_SERVICE_KEY:
            import requests as _req
            try:
                # Find by call_id
                _req.patch(
                    f"{SUPABASE_URL}/rest/v1/gemini_call_history",
                    params={"call_id": f"eq.{call_id}"},
                    json={"recording_url": mp3_url},
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                    timeout=10
                )
                log.info(f"[{call_id}] Recording URL saved to Supabase")
            except Exception as e:
                log.error(f"[{call_id}] Failed to save recording URL: {e}")

    return Response(content="OK", status_code=200)


@app.post("/api/end-call")
async def end_call(request: Request):
    _verify_secret(request)
    data = await request.json()
    call_sid = data.get("call_sid", "")

    if not call_sid:
        raise HTTPException(status_code=400, detail="Missing call_sid.")

    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.calls(call_sid).update(status="completed")
        log.info(f"Call {call_sid} ended via API.")
        return {"ok": True}
    except Exception as e:
        log.error(f"Error ending call {call_sid}: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/call-status")
async def call_status(request: Request):
    """Twilio status callback webhook."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "")

    log.info(f"[Status] {call_sid}: {status} (duration: {duration})")

    # Find the call by SID
    for cid, call in _active_calls.items():
        if call.get("call_sid") == call_sid:
            call["status"] = status
            if duration:
                call["duration_seconds"] = int(duration)
            if status in ("completed", "failed", "busy", "no-answer", "canceled"):
                # Broadcast end to subscribers
                await _broadcast_transcript(cid, {"type": "status", "status": "ended",
                                                   "timestamp": datetime.now(timezone.utc).isoformat()})
                # Save to Supabase
                await _save_call_history(cid)
            break

    return Response(content="OK", status_code=200)


# ═══════════════════════════════════════════════════════════════════════════════
#  TWILIO MEDIA STREAM ↔ GEMINI LIVE API BRIDGE
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/media-stream/{call_id}")
async def media_stream(websocket: WebSocket, call_id: str):
    """Handle Twilio media stream and bridge to Gemini Live API."""
    if call_id not in _active_calls:
        await websocket.close(code=4004, reason="Unknown call ID")
        return

    await websocket.accept()
    call = _active_calls[call_id]
    call["twilio_ws"] = websocket
    stream_sid = None
    log.info(f"[{call_id}] Twilio WebSocket connected")

    try:
        import audioop_lts as audioop
    except ImportError:
        try:
            import audioop
        except ImportError:
            log.error("audioop not available — cannot convert audio")
            await websocket.close()
            return

    # Connect to Gemini Live API
    try:
        from google import genai
        from google.genai import types

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        _log_error(call_id, f"Connecting to {GEMINI_MODEL}...")

        # Build config from call settings
        _settings = call.get("settings", {})

        # Map sensitivity strings to Gemini enum values
        # Only LOW and HIGH exist — DEFAULT maps to UNSPECIFIED (lets Gemini decide)
        _START_SENS_MAP = {
            "LOW": types.StartSensitivity.START_SENSITIVITY_LOW,
            "DEFAULT": types.StartSensitivity.START_SENSITIVITY_UNSPECIFIED,
            "HIGH": types.StartSensitivity.START_SENSITIVITY_HIGH,
        }
        _END_SENS_MAP = {
            "LOW": types.EndSensitivity.END_SENSITIVITY_LOW,
            "DEFAULT": types.EndSensitivity.END_SENSITIVITY_UNSPECIFIED,
            "HIGH": types.EndSensitivity.END_SENSITIVITY_HIGH,
        }

        # Default to LOW start sensitivity to avoid coughs/hums triggering interruptions
        start_sens = _START_SENS_MAP.get(
            _settings.get("start_sensitivity", "LOW").upper(),
            types.StartSensitivity.START_SENSITIVITY_LOW
        )
        # Default to HIGH end sensitivity — 8kHz telephony line static can be
        # misinterpreted as whispering on LOW/UNSPECIFIED, causing long pauses
        end_sens = _END_SENS_MAP.get(
            _settings.get("end_sensitivity", "HIGH").upper(),
            types.EndSensitivity.END_SENSITIVITY_HIGH
        )
        silence_ms = int(_settings.get("silence_duration_ms", 500))

        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=call.get("system_instruction", "You are a helpful AI assistant.") + "\n\nIMPORTANT: You are on a live phone call. Start speaking immediately — introduce yourself right away without waiting for the other person to speak first.")]
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=call.get("voice_name", "Kore")
                    )
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY",
                automatic_activity_detection=types.AutomaticActivityDetection(
                    startOfSpeechSensitivity=start_sens,
                    endOfSpeechSensitivity=end_sens,
                    silenceDurationMs=silence_ms,
                ),
            ),
        )
        log.info(f"[{call_id}] VAD config: start={start_sens}, end={end_sens}, silence={silence_ms}ms")

        async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=config) as gemini_session:
            call["status"] = "connected"
            await _broadcast_transcript(call_id, {
                "type": "status", "status": "connected",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            log.info(f"[{call_id}] Gemini Live session connected")

            # Send text trigger to make Gemini start speaking immediately
            # (bypasses VAD waiting — Gemini responds to text instantly)
            try:
                await gemini_session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text="The phone call has been answered. Start speaking now — greet the caller.")]
                    ),
                    turn_complete=True
                )
                log.info(f"[{call_id}] Text trigger sent to Gemini")
            except Exception as e:
                _log_error(call_id, f"send_client_content failed (non-fatal): {e}")

            # Transcript accumulation buffers — collect fragments into sentences
            _ai_transcript_buffer = []
            _caller_transcript_buffer = []

            # Rate conversion state
            _ratecv_state_up = None    # 8kHz → 16kHz
            _ratecv_state_down = None  # 24kHz → 8kHz

            # 20ms chunk buffer for consistent input to Gemini
            # 16kHz × 2 bytes × 0.020s = 640 bytes per 20ms chunk
            _CHUNK_20MS = 640
            _input_buffer = bytearray()

            # Performance tracking
            _perf = {"first_audio_in": None, "first_audio_out": None, "turns": 0}

            async def twilio_to_gemini():
                """Read audio from Twilio, convert to 20ms PCM chunks, send to Gemini."""
                nonlocal stream_sid, _ratecv_state_up, _input_buffer
                try:
                    while True:
                        msg = await websocket.receive_text()
                        data = json.loads(msg)

                        if data["event"] == "start":
                            stream_sid = data["start"]["streamSid"]
                            call["stream_sid"] = stream_sid
                            log.info(f"[{call_id}] Stream started: {stream_sid}")

                        elif data["event"] == "media":
                            if call.get("barged_in"):
                                # Still broadcast to monitors even when barged
                                asyncio.create_task(_broadcast_monitor(call_id, "caller", data["media"]["payload"]))
                                continue

                            # Track first audio received
                            if not _perf["first_audio_in"]:
                                _perf["first_audio_in"] = datetime.now(timezone.utc).isoformat()

                            # Broadcast caller audio to monitors (already base64 mulaw)
                            asyncio.create_task(_broadcast_monitor(call_id, "caller", data["media"]["payload"]))

                            # Decode mulaw → PCM 16-bit
                            payload = base64.b64decode(data["media"]["payload"])
                            pcm_8k = audioop.ulaw2lin(payload, 2)
                            # Resample 8kHz → 16kHz
                            pcm_16k, _ratecv_state_up = audioop.ratecv(
                                pcm_8k, 2, 1, 8000, 16000, _ratecv_state_up
                            )

                            # Buffer and send in 20ms chunks (640 bytes at 16kHz 16-bit mono)
                            _input_buffer.extend(pcm_16k)
                            while len(_input_buffer) >= _CHUNK_20MS:
                                chunk = bytes(_input_buffer[:_CHUNK_20MS])
                                del _input_buffer[:_CHUNK_20MS]
                                await gemini_session.send_realtime_input(
                                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                                )

                        elif data["event"] == "stop":
                            # Send remaining buffer
                            if _input_buffer:
                                await gemini_session.send_realtime_input(
                                    audio=types.Blob(data=bytes(_input_buffer), mime_type="audio/pcm;rate=16000")
                                )
                                _input_buffer.clear()
                            log.info(f"[{call_id}] Stream stopped")
                            break

                except WebSocketDisconnect:
                    log.info(f"[{call_id}] Twilio WebSocket disconnected")
                except Exception as e:
                    _log_error(call_id, f"Twilio→Gemini error: {e}")
                    import traceback
                    _log_error(call_id, traceback.format_exc())

            async def gemini_to_twilio():
                """Read audio from Gemini, convert, send to Twilio."""
                nonlocal _ratecv_state_down
                try:
                    # Re-enter receive() in a loop — iterator ends after turn_complete
                    # and must be re-entered to keep listening (per official Google example)
                    while True:
                        async for response in gemini_session.receive():
                            # Handle go_away (server asking us to disconnect)
                            if response.go_away:
                                _log_error(call_id, f"Gemini GoAway: {response.go_away}")
                                return

                            if not response.server_content:
                                continue

                            sc = response.server_content

                            # Handle audio output
                            if sc.model_turn:
                                if not _perf["first_audio_out"]:
                                    _perf["first_audio_out"] = datetime.now(timezone.utc).isoformat()
                                    _log_error(call_id, f"PERF: first audio out at {_perf['first_audio_out']}")
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        if call.get("barged_in"):
                                            continue

                                        pcm_24k = part.inline_data.data
                                        if len(pcm_24k) == 0:
                                            continue
                                        pcm_8k, _ratecv_state_down = audioop.ratecv(
                                            pcm_24k, 2, 1, 24000, 8000, _ratecv_state_down
                                        )
                                        mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)

                                        if stream_sid and len(mulaw_8k) > 0:
                                            payload = base64.b64encode(mulaw_8k).decode("utf-8")
                                            # Broadcast AI audio to monitors
                                            asyncio.create_task(_broadcast_monitor(call_id, "ai", payload))
                                            try:
                                                await websocket.send_text(json.dumps({
                                                    "event": "media",
                                                    "streamSid": stream_sid,
                                                    "media": {"payload": payload}
                                                }))
                                            except Exception:
                                                return  # Twilio disconnected

                            # Handle input transcription (what the caller says) — accumulate
                            if sc.input_transcription and sc.input_transcription.text:
                                _caller_transcript_buffer.append(sc.input_transcription.text)

                            # Handle output transcription (what the AI says) — accumulate
                            if sc.output_transcription and sc.output_transcription.text:
                                _ai_transcript_buffer.append(sc.output_transcription.text)

                            # Handle turn complete — flush transcript buffers, log perf, re-enter receive loop
                            if sc.turn_complete:
                                _perf["turns"] += 1
                                _log_error(call_id, f"PERF: turn {_perf['turns']} complete")
                                # Flush AI buffer
                                if _ai_transcript_buffer:
                                    full_text = " ".join(_ai_transcript_buffer)
                                    ts = datetime.now(timezone.utc).isoformat()
                                    call["transcript"].append({"speaker": "ai", "text": full_text, "timestamp": ts})
                                    await _broadcast_transcript(call_id, {
                                        "type": "transcript", "speaker": "ai", "text": full_text, "timestamp": ts
                                    })
                                    _ai_transcript_buffer.clear()
                                # Flush caller buffer
                                if _caller_transcript_buffer:
                                    full_text = " ".join(_caller_transcript_buffer)
                                    ts = datetime.now(timezone.utc).isoformat()
                                    call["transcript"].append({"speaker": "caller", "text": full_text, "timestamp": ts})
                                    await _broadcast_transcript(call_id, {
                                        "type": "transcript", "speaker": "caller", "text": full_text, "timestamp": ts
                                    })
                                    _caller_transcript_buffer.clear()

                            # Handle interrupted (barge-in by caller)
                            if sc.interrupted:
                                if stream_sid:
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "event": "clear",
                                            "streamSid": stream_sid
                                        }))
                                    except Exception:
                                        return

                        # receive() iterator ended — re-enter
                        _log_error(call_id, "Gemini receive iterator ended, re-entering...")

                except Exception as e:
                    _log_error(call_id, f"Gemini→Twilio error: {e}")
                    import traceback
                    _log_error(call_id, traceback.format_exc())

            # Run both directions concurrently using create_task (per official example)
            _log_error(call_id, "Gemini Live session connected! Starting audio bridge tasks...")
            twilio_task = asyncio.create_task(twilio_to_gemini())
            gemini_task = asyncio.create_task(gemini_to_twilio())

            # Wait for either task to finish (usually twilio_to_gemini ends on disconnect)
            done, pending = await asyncio.wait(
                [twilio_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.exception():
                    _log_error(call_id, f"Task exception: {task.exception()}")
            for task in pending:
                task.cancel()

    except Exception as e:
        _log_error(call_id, f"Gemini connection error: {e}")
        import traceback
        _log_error(call_id, traceback.format_exc())
        await _broadcast_transcript(call_id, {
            "type": "status", "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    finally:
        call["status"] = "ended"
        call.pop("twilio_ws", None)
        call.pop("stream_sid", None)
        log.info(f"[{call_id}] Call ended, saving history")
        # Broadcast ended to transcript subscribers (don't rely on Twilio callback alone)
        await _broadcast_transcript(call_id, {
            "type": "status", "status": "ended",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await _save_call_history(call_id)
        # Clean up monitor subscribers
        _monitor_subscribers.pop(call_id, None)
        try:
            await websocket.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  TRANSCRIPT WEBSOCKET (Dashboard connects here)
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/transcript/{call_id}")
async def transcript_ws(websocket: WebSocket, call_id: str):
    """WebSocket for live transcript broadcast to dashboard."""
    await websocket.accept()

    if call_id not in _transcript_subscribers:
        _transcript_subscribers[call_id] = set()
    _transcript_subscribers[call_id].add(websocket)
    log.info(f"[{call_id}] Transcript subscriber connected ({len(_transcript_subscribers[call_id])} total)")

    try:
        # Send existing transcript lines
        if call_id in _active_calls:
            for line in _active_calls[call_id].get("transcript", []):
                await websocket.send_json({"type": "transcript", **line})

        # Keep alive until disconnect
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _transcript_subscribers.get(call_id, set()).discard(websocket)
        log.info(f"[{call_id}] Transcript subscriber disconnected")


# ═══════════════════════════════════════════════════════════════════════════════
#  MONITOR WEBSOCKET (listen to call audio)
# ═══════════════════════════════════════════════════════════════════════════════

async def _broadcast_monitor(call_id: str, speaker: str, mulaw_b64: str):
    """Send audio chunk to all monitor subscribers (non-blocking)."""
    subscribers = _monitor_subscribers.get(call_id, set())
    if not subscribers:
        return
    msg = json.dumps({"type": "audio", "speaker": speaker, "payload": mulaw_b64})
    dead = set()
    for ws in subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    subscribers -= dead


@app.websocket("/ws/monitor/{call_id}")
async def monitor_ws(websocket: WebSocket, call_id: str):
    """WebSocket for live audio monitoring — browser listens to both sides."""
    if call_id not in _active_calls:
        await websocket.close(code=4004, reason="Unknown call")
        return

    await websocket.accept()

    if call_id not in _monitor_subscribers:
        _monitor_subscribers[call_id] = set()
    _monitor_subscribers[call_id].add(websocket)
    log.info(f"[{call_id}] Monitor subscriber connected ({len(_monitor_subscribers[call_id])} total)")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _monitor_subscribers.get(call_id, set()).discard(websocket)
        log.info(f"[{call_id}] Monitor subscriber disconnected")


# ═══════════════════════════════════════════════════════════════════════════════
#  BARGE-IN WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/barge/{call_id}")
async def barge_ws(websocket: WebSocket, call_id: str):
    """WebSocket for barge-in — human operator takes over from AI."""
    if call_id not in _active_calls:
        await websocket.close(code=4004, reason="Unknown call")
        return

    await websocket.accept()
    call = _active_calls[call_id]
    call["barged_in"] = True
    log.info(f"[{call_id}] Barge-in activated")

    # Clear any AI audio still playing on the caller's end
    twilio_ws = call.get("twilio_ws")
    sid = call.get("stream_sid")
    if twilio_ws and sid:
        try:
            await twilio_ws.send_text(json.dumps({"event": "clear", "streamSid": sid}))
        except Exception:
            pass

    await _broadcast_transcript(call_id, {
        "type": "status", "status": "barged_in",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    try:
        while True:
            # Receive base64 mulaw audio from human operator's browser
            msg = await websocket.receive_text()
            data = json.loads(msg)
            mulaw_b64 = data.get("payload", "")
            if not mulaw_b64:
                continue

            # Forward to Twilio media stream
            twilio_ws = call.get("twilio_ws")
            sid = call.get("stream_sid")
            if twilio_ws and sid:
                try:
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": sid,
                        "media": {"payload": mulaw_b64}
                    }))
                except Exception:
                    _log_error(call_id, "Failed to forward barge audio to Twilio")
                    break
    except WebSocketDisconnect:
        pass
    finally:
        call["barged_in"] = False
        log.info(f"[{call_id}] Barge-in released")
        await _broadcast_transcript(call_id, {
            "type": "status", "status": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _broadcast_transcript(call_id: str, message: dict):
    """Send a message to all transcript WebSocket subscribers."""
    subscribers = _transcript_subscribers.get(call_id, set())
    dead = set()
    for ws in subscribers:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    subscribers -= dead


async def _save_call_history(call_id: str):
    """Save call transcript and status to Supabase."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    call = _active_calls.get(call_id)
    if not call or not call.get("call_sid"):
        return

    # Compute duration from started_at (don't rely on Twilio callback which may arrive after cleanup)
    duration = call.get("duration_seconds")
    if not duration and call.get("started_at"):
        try:
            started = datetime.fromisoformat(call["started_at"])
            duration = int((datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            pass

    import requests as _req
    try:
        _req.patch(
            f"{SUPABASE_URL}/rest/v1/gemini_call_history",
            params={"call_sid": f"eq.{call['call_sid']}"},
            json={
                "status": call.get("status", "unknown"),
                "duration_seconds": duration,
                "transcript": call.get("transcript", []),
                "recording_url": call.get("recording_url"),
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            timeout=10
        )
        log.info(f"[{call_id}] Call history saved to Supabase")
    except Exception as e:
        log.error(f"[{call_id}] Failed to save history: {e}")

    # Clean up
    _active_calls.pop(call_id, None)
    _transcript_subscribers.pop(call_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    log.info(f"Starting Gemini Call Server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
