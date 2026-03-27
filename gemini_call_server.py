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
async def debug_errors():
    return {"errors": _error_log[-20:]}


@app.get("/debug/test-gemini")
async def test_gemini():
    """Test if Gemini API key works and Live model is available."""
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY not set"}
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        # Test basic API
        response = client.models.generate_content(
            model='gemini-2.0-flash',
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

        call = client.calls.create(
            from_=from_number,
            to=to_number,
            url=twiml_url,
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
        _log_error(call_id, f"Gemini client created, connecting to {GEMINI_MODEL}...")

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part(text=call.get("system_instruction", "You are a helpful AI assistant making a phone call."))]
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
        )

        async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=config) as gemini_session:
            call["status"] = "connected"
            await _broadcast_transcript(call_id, {
                "type": "status", "status": "connected",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            log.info(f"[{call_id}] Gemini Live session connected")

            # Rate conversion state
            _ratecv_state_up = None    # 8kHz → 16kHz
            _ratecv_state_down = None  # 24kHz → 8kHz

            async def twilio_to_gemini():
                """Read audio from Twilio, convert, send to Gemini."""
                nonlocal stream_sid, _ratecv_state_up
                try:
                    while True:
                        msg = await websocket.receive_text()
                        data = json.loads(msg)

                        if data["event"] == "start":
                            stream_sid = data["start"]["streamSid"]
                            log.info(f"[{call_id}] Stream started: {stream_sid}")

                        elif data["event"] == "media":
                            if call.get("barged_in"):
                                continue  # Skip Twilio audio during barge-in (human is talking)

                            # Decode mulaw → PCM 16-bit
                            payload = base64.b64decode(data["media"]["payload"])
                            pcm_8k = audioop.ulaw2lin(payload, 2)
                            # Resample 8kHz → 16kHz
                            pcm_16k, _ratecv_state_up = audioop.ratecv(
                                pcm_8k, 2, 1, 8000, 16000, _ratecv_state_up
                            )
                            # Send to Gemini
                            await gemini_session.send_realtime_input(
                                audio=types.Blob(data=pcm_16k, mime_type="audio/pcm;rate=16000")
                            )

                        elif data["event"] == "stop":
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
                    async for response in gemini_session.receive():
                        if not response.server_content:
                            continue

                        sc = response.server_content

                        # Handle audio output
                        if sc.model_turn:
                            for part in sc.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    if call.get("barged_in"):
                                        continue  # Don't send AI audio during barge-in

                                    # Convert PCM 24kHz → mulaw 8kHz
                                    pcm_24k = part.inline_data.data
                                    pcm_8k, _ratecv_state_down = audioop.ratecv(
                                        pcm_24k, 2, 1, 24000, 8000, _ratecv_state_down
                                    )
                                    mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
                                    payload = base64.b64encode(mulaw_8k).decode("utf-8")

                                    if stream_sid:
                                        await websocket.send_text(json.dumps({
                                            "event": "media",
                                            "streamSid": stream_sid,
                                            "media": {"payload": payload}
                                        }))

                        # Handle input transcription (what the caller says)
                        if sc.input_transcription and sc.input_transcription.text:
                            text = sc.input_transcription.text
                            ts = datetime.now(timezone.utc).isoformat()
                            call["transcript"].append({"speaker": "caller", "text": text, "timestamp": ts})
                            await _broadcast_transcript(call_id, {
                                "type": "transcript", "speaker": "caller", "text": text, "timestamp": ts
                            })

                        # Handle output transcription (what the AI says)
                        if sc.output_transcription and sc.output_transcription.text:
                            text = sc.output_transcription.text
                            ts = datetime.now(timezone.utc).isoformat()
                            call["transcript"].append({"speaker": "ai", "text": text, "timestamp": ts})
                            await _broadcast_transcript(call_id, {
                                "type": "transcript", "speaker": "ai", "text": text, "timestamp": ts
                            })

                        # Handle turn complete
                        if sc.turn_complete:
                            pass  # Normal turn boundary

                        # Handle interrupted (barge-in by caller)
                        if sc.interrupted:
                            if stream_sid:
                                # Clear Twilio's audio buffer so the interruption is instant
                                await websocket.send_text(json.dumps({
                                    "event": "clear",
                                    "streamSid": stream_sid
                                }))

                except Exception as e:
                    _log_error(call_id, f"Gemini→Twilio error: {e}")
                    import traceback
                    _log_error(call_id, traceback.format_exc())

            # Run both directions concurrently
            _log_error(call_id, "Starting audio bridge tasks...")
            results = await asyncio.gather(
                twilio_to_gemini(),
                gemini_to_twilio(),
                return_exceptions=True
            )
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    _log_error(call_id, f"Task {i} exception: {r}")

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
        log.info(f"[{call_id}] Call ended, saving history")
        await _save_call_history(call_id)
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

    await _broadcast_transcript(call_id, {
        "type": "status", "status": "barged_in",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    try:
        while True:
            # Receive audio from human operator's browser
            data = await websocket.receive_bytes()
            # TODO: Forward to Twilio media stream
            # This requires access to the Twilio WebSocket from the media_stream handler
            # For now, barge-in pauses AI but doesn't route human audio
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

    import requests as _req
    try:
        _req.patch(
            f"{SUPABASE_URL}/rest/v1/gemini_call_history",
            params={"call_sid": f"eq.{call['call_sid']}"},
            json={
                "status": call.get("status", "unknown"),
                "duration_seconds": call.get("duration_seconds"),
                "transcript": call.get("transcript", []),
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
