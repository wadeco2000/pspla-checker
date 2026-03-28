"""AI Call Server — FastAPI WebSocket bridge between Twilio and AI voice providers.

Supports multiple AI providers (Gemini Live, OpenAI Realtime) via swappable provider classes.

This server handles:
1. Creating outbound calls via Twilio
2. Bridging audio between Twilio Media Streams and AI voice providers
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
from pathlib import Path
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")
SERVER_SECRET = os.getenv("GEMINI_CALL_SERVER_SECRET", "")
SELF_URL = os.getenv("GEMINI_CALL_SERVER_SELF_URL", "http://localhost:8001")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SERVER_VERSION = datetime.fromtimestamp(Path(__file__).stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# ── RAG search for multi-turn (runs on call server mid-conversation) ─────────
def _rag_search_sync(kb_id, query_text, top_k=3):
    """Search RAG chunks. Runs synchronously in async context via thread."""
    if not OPENAI_API_KEY or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.embeddings.create(model="text-embedding-3-small", input=[query_text[:4000]])
        query_embedding = resp.data[0].embedding

        import requests as _req
        r = _req.post(f"{SUPABASE_URL}/rest/v1/rpc/match_rag_chunks",
            json={"query_embedding": query_embedding, "match_kb_id": kb_id,
                  "match_threshold": 0.3, "match_count": top_k},
            headers={"apikey": SUPABASE_SERVICE_KEY,
                     "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                     "Content-Type": "application/json"},
            timeout=10)
        if not r.ok or not r.json():
            return ""
        parts = [chunk["content"] for chunk in r.json()]
        return "\n\n---\n\n".join(parts)
    except Exception as e:
        log.error(f"RAG search error: {e}")
        return ""
OPENAI_REALTIME_MODEL = "gpt-realtime"
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# ── Cost tracking (USD rates, converted to NZD at display time) ──────────────
USD_TO_NZD = float(os.getenv("USD_TO_NZD", "1.73"))  # Update via env var as rate changes

# Per-provider cost rates (USD per unit)
# To add a new provider: add an entry here with per-token or per-minute rates
COST_RATES = {
    "twilio": {
        "voice_nz_mobile_per_min": 0.084,
        "media_stream_per_min": 0.004,
        "recording_per_min": 0.0025,
    },
    "gemini": {
        # Gemini Live doesn't report tokens — estimate from duration
        # $3.00/1M input tokens, $12.00/1M output tokens
        # ~1 min audio ≈ ~167 tokens in + ~167 tokens out (rough estimate)
        "audio_per_min": 0.023,  # combined in+out estimate
    },
    "openai": {
        # OpenAI reports actual tokens — we calculate from those
        "audio_input_per_1m_tokens": 32.00,
        "audio_output_per_1m_tokens": 64.00,
        "transcription_per_min": 0.006,  # gpt-4o-transcribe
    },
    "elevenlabs": {
        # ElevenLabs charges per minute of conversation
        "per_min": 0.10,  # $0.10 USD/min (Creator/Pro plan)
    },
}

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


# ═══════════════════════════════════════════════════════════════════════════════
#  AI PROVIDER ABSTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

class GeminiProvider:
    """Gemini 3.1 Flash Live voice provider."""
    name = "gemini"

    def __init__(self, call: dict, call_id: str):
        self._call = call
        self._call_id = call_id
        self._session = None
        self._ctx = None  # context manager
        self._client = None
        # Audio conversion state (Gemini needs PCM 16kHz in, outputs PCM 24kHz)
        self._ratecv_state_up = None
        self._ratecv_state_down = None
        self._input_buffer = bytearray()
        self._CHUNK_20MS = 640  # 16kHz × 2 bytes × 0.020s

    async def connect(self):
        from google import genai
        from google.genai import types
        self._types = types

        self._client = genai.Client(api_key=GEMINI_API_KEY)
        settings = self._call.get("settings", {})

        _START_SENS = {
            "LOW": types.StartSensitivity.START_SENSITIVITY_LOW,
            "DEFAULT": types.StartSensitivity.START_SENSITIVITY_UNSPECIFIED,
            "HIGH": types.StartSensitivity.START_SENSITIVITY_HIGH,
        }
        _END_SENS = {
            "LOW": types.EndSensitivity.END_SENSITIVITY_LOW,
            "DEFAULT": types.EndSensitivity.END_SENSITIVITY_UNSPECIFIED,
            "HIGH": types.EndSensitivity.END_SENSITIVITY_HIGH,
        }
        start_sens = _START_SENS.get(settings.get("start_sensitivity", "LOW").upper(),
                                     types.StartSensitivity.START_SENSITIVITY_LOW)
        end_sens = _END_SENS.get(settings.get("end_sensitivity", "HIGH").upper(),
                                 types.EndSensitivity.END_SENSITIVITY_HIGH)
        silence_ms = int(settings.get("silence_duration_ms", 500))

        # Add language/accent hint to system instruction
        lang = settings.get("language", "en")
        _accent_hints = {
            "en-NZ": "The caller has a New Zealand accent. Interpret words accordingly (e.g. 'fush and chups' = fish and chips, 'six' may sound like 'sux', 'pen' like 'pin').",
            "en-AU": "The caller has an Australian accent. Interpret words accordingly (e.g. 'today' may sound like 'to-die', rising intonation on statements is normal).",
            "en-GB": "The caller has a British accent.",
            "en-US": "The caller has an American accent.",
        }
        if lang in _accent_hints:
            lang_hint = "\n\n" + _accent_hints[lang]
        elif lang != "en":
            lang_hint = f"\n\nThe caller is speaking {lang}. Listen and respond in this language."
        else:
            lang_hint = ""

        strict_hint = ""
        if settings.get("strict_mode"):
            strict_hint = "\n\nSTRICT MODE: You must ONLY use information from the reference documents and knowledge base provided above. If the caller asks something not covered in your reference materials, say 'I don't have that information in my reference materials.' Do NOT use general knowledge to answer questions."

        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=self._call.get("system_instruction", "You are a helpful AI assistant.")
                       + "\n\nIMPORTANT: You are on a live phone call. Start speaking immediately — introduce yourself right away without waiting for the other person to speak first."
                       + "\n\nCRITICAL TRANSCRIPTION RULES: This is a phone call over 8kHz telephony audio. ALL transcription MUST be in English/Latin script only — NEVER output Chinese, Japanese, Korean, Arabic, or other non-Latin characters in transcriptions. If audio is unclear, transcribe your best guess in English or mark as [inaudible]. Prefer common English names and words over unusual interpretations (e.g. 'John' not 'jump', 'Tuesday' not random syllables)."
                       + lang_hint
                       + strict_hint)]
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._call.get("voice_name", "Kore")
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
        log.info(f"[{self._call_id}] Gemini VAD: start={start_sens}, end={end_sens}, silence={silence_ms}ms")

        self._ctx = self._client.aio.live.connect(model=GEMINI_MODEL, config=config)
        self._session = await self._ctx.__aenter__()

    async def send_audio(self, mulaw_bytes: bytes):
        """Send mulaw 8kHz audio. Converts to PCM 16kHz with 20ms chunking."""
        try:
            import audioop_lts as audioop
        except ImportError:
            import audioop
        types = self._types

        pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
        pcm_16k, self._ratecv_state_up = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, self._ratecv_state_up)

        self._input_buffer.extend(pcm_16k)
        while len(self._input_buffer) >= self._CHUNK_20MS:
            chunk = bytes(self._input_buffer[:self._CHUNK_20MS])
            del self._input_buffer[:self._CHUNK_20MS]
            await self._session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
            )

    async def flush_audio(self):
        """Send remaining buffered audio."""
        if self._input_buffer:
            types = self._types
            await self._session.send_realtime_input(
                audio=types.Blob(data=bytes(self._input_buffer), mime_type="audio/pcm;rate=16000")
            )
            self._input_buffer.clear()

    async def send_text(self, text: str):
        """Send text trigger to prompt AI to speak."""
        await self._session.send_realtime_input(text=text)

    async def send_context(self, context: str):
        """Inject reference context mid-conversation."""
        await self._session.send_realtime_input(
            text=f"[REFERENCE INFO - use this to answer the caller's question]: {context}"
        )

    async def receive_loop(self, on_audio, on_ai_transcript, on_caller_transcript, on_turn_complete, on_interrupted):
        """Main receive loop. Calls callbacks with processed data."""
        try:
            import audioop_lts as audioop
        except ImportError:
            import audioop

        while True:
            async for response in self._session.receive():
                if response.go_away:
                    _log_error(self._call_id, f"Gemini GoAway: {response.go_away}")
                    return
                if not response.server_content:
                    continue
                sc = response.server_content

                # Audio output → convert PCM 24kHz to mulaw 8kHz
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            pcm_24k = part.inline_data.data
                            if len(pcm_24k) == 0:
                                continue
                            pcm_8k, self._ratecv_state_down = audioop.ratecv(
                                pcm_24k, 2, 1, 24000, 8000, self._ratecv_state_down
                            )
                            mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
                            if len(mulaw_8k) > 0:
                                await on_audio(mulaw_8k)

                if sc.input_transcription and sc.input_transcription.text:
                    await on_caller_transcript(sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    await on_ai_transcript(sc.output_transcription.text)
                if sc.turn_complete:
                    await on_turn_complete()
                if sc.interrupted:
                    await on_interrupted()

            _log_error(self._call_id, "Gemini receive iterator ended, re-entering...")

    async def close(self):
        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass


class OpenAIProvider:
    """OpenAI Realtime voice provider. Uses g711_ulaw for zero-conversion audio."""
    name = "openai"

    def __init__(self, call: dict, call_id: str):
        self._call = call
        self._call_id = call_id
        self._ws = None
        self._ai_transcript_buf = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.audio_input_tokens = 0
        self.audio_output_tokens = 0

    async def connect(self):
        import websockets
        url = f"{OPENAI_REALTIME_URL}?model={OPENAI_REALTIME_MODEL}"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        }

        self._ws = await websockets.connect(url, additional_headers=headers)

        # Wait for session.created
        msg = json.loads(await self._ws.recv())
        if msg.get("type") == "error":
            raise RuntimeError(f"OpenAI error: {msg.get('error', {}).get('message', msg)}")
        if msg.get("type") != "session.created":
            raise RuntimeError(f"Expected session.created, got {msg.get('type')}: {json.dumps(msg)[:200]}")
        log.info(f"[{self._call_id}] OpenAI session created")

        # Configure session
        settings = self._call.get("settings", {})
        eagerness_map = {"LOW": "low", "DEFAULT": "medium", "HIGH": "high"}
        eagerness = eagerness_map.get(settings.get("end_sensitivity", "HIGH").upper(), "medium")
        language = settings.get("language", "en")

        lang_name = {"en": "English", "en-NZ": "English", "en-AU": "English", "en-GB": "English",
                     "en-US": "English", "mi": "Te Reo Māori", "es": "Spanish", "fr": "French",
                     "de": "German", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi"
                     }.get(language, "English")

        strict_hint = ""
        if settings.get("strict_mode"):
            strict_hint = "\n\nSTRICT MODE: You must ONLY use information from the reference documents and knowledge base provided above. If the caller asks something not covered in your reference materials, say 'I don't have that information in my reference materials.' Do NOT use general knowledge to answer questions."

        session_config = {
            "type": "session.update",
            "session": {
                "instructions": self._call.get("system_instruction", "You are a helpful AI assistant.")
                    + f"\n\nIMPORTANT: You are on a live phone call. You MUST speak in {lang_name} only. Start speaking immediately — introduce yourself right away without waiting for the other person to speak first."
                    + "\n\nALL transcription MUST be in English/Latin script only — NEVER output non-Latin characters."
                    + strict_hint,
                "modalities": ["audio", "text"],
                "voice": self._call.get("voice_name", "coral"),
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "input_audio_transcription": {
                    "model": "gpt-4o-transcribe",
                    "language": language,
                },
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": eagerness,
                    "create_response": True,
                    "interrupt_response": True,
                },
            }
        }
        await self._ws.send(json.dumps(session_config))

        # Wait for session.updated confirmation
        msg = json.loads(await self._ws.recv())
        if msg.get("type") == "error":
            raise RuntimeError(f"OpenAI session.update error: {msg.get('error', {}).get('message', msg)}")
        log.info(f"[{self._call_id}] OpenAI session configured (eagerness={eagerness}, lang={language}, voice={self._call.get('voice_name', 'coral')})")

    async def send_audio(self, mulaw_bytes: bytes):
        """Send mulaw 8kHz audio directly — OpenAI accepts g711_ulaw natively."""
        b64 = base64.b64encode(mulaw_bytes).decode("utf-8")
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": b64
        }))

    async def flush_audio(self):
        """No buffering needed for OpenAI — audio sent directly."""
        pass

    async def send_text(self, text: str):
        """Send text trigger to prompt AI to speak."""
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}]
            }
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_context(self, context: str):
        """Inject reference context mid-conversation as a system message."""
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": f"[REFERENCE INFO for answering the caller]: {context}"}]
            }
        }))

    async def receive_loop(self, on_audio, on_ai_transcript, on_caller_transcript, on_turn_complete, on_interrupted):
        """Main receive loop. Parses OpenAI Realtime events."""
        _first_audio = False
        async for raw in self._ws:
            event = json.loads(raw)
            t = event.get("type", "")

            if t == "response.audio.delta":
                # Audio output — already g711_ulaw, decode base64
                if not _first_audio:
                    _first_audio = True
                    _log_error(self._call_id, "PERF: first OpenAI audio delta received")
                mulaw_bytes = base64.b64decode(event["delta"])
                if len(mulaw_bytes) > 0:
                    await on_audio(mulaw_bytes)

            elif t == "response.audio_transcript.delta":
                self._ai_transcript_buf.append(event.get("delta", ""))

            elif t == "response.audio_transcript.done":
                transcript = event.get("transcript", "")
                if transcript:
                    await on_ai_transcript(transcript)
                self._ai_transcript_buf.clear()

            elif t == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    await on_caller_transcript(transcript)

            elif t == "response.done":
                # Extract token usage
                usage = event.get("response", {}).get("usage", {})
                if usage:
                    self.total_input_tokens += usage.get("input_tokens", 0)
                    self.total_output_tokens += usage.get("output_tokens", 0)
                    details_in = usage.get("input_token_details", {})
                    details_out = usage.get("output_token_details", {})
                    self.audio_input_tokens += details_in.get("audio_tokens", 0)
                    self.audio_output_tokens += details_out.get("audio_tokens", 0)
                await on_turn_complete()

            elif t == "input_audio_buffer.speech_started":
                await on_interrupted()

            elif t == "error":
                _log_error(self._call_id, f"OpenAI error: {event.get('error', {})}")

    async def close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass


class ElevenLabsProvider:
    """ElevenLabs Conversational AI voice provider.
    Input: mulaw 8kHz → PCM 16kHz (conversion needed).
    Output: ulaw 8kHz → Twilio (zero conversion)."""
    name = "elevenlabs"

    def __init__(self, call: dict, call_id: str):
        self._call = call
        self._call_id = call_id
        self._ws = None
        self._initiated = False
        self._ratecv_state_up = None  # 8kHz → 16kHz for input

    async def connect(self):
        import websockets
        import httpx

        # Agent ID from call settings (dropdown) or fallback to env var
        agent_id = self._call.get("settings", {}).get("elevenlabs_agent_id", "") or ELEVENLABS_AGENT_ID
        if not agent_id:
            raise RuntimeError("No ElevenLabs agent selected. Select an agent in Settings or set ELEVENLABS_AGENT_ID.")

        # Get signed URL for private agent
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.elevenlabs.io/v1/convai/conversation/get_signed_url?agent_id={agent_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                timeout=10
            )
            if resp.status_code != 200:
                raise RuntimeError(f"ElevenLabs signed URL error: {resp.status_code} {resp.text[:200]}")
            signed_url = resp.json()["signed_url"]

        self._ws = await websockets.connect(signed_url)
        log.info(f"[{self._call_id}] ElevenLabs WebSocket connected (init deferred until media stream ready)")

    async def send_audio(self, mulaw_bytes: bytes):
        """Send mulaw 8kHz audio — convert to PCM 16kHz for ElevenLabs input."""
        try:
            import audioop_lts as audioop
        except ImportError:
            import audioop
        pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
        pcm_16k, self._ratecv_state_up = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, self._ratecv_state_up)
        b64 = base64.b64encode(pcm_16k).decode("utf-8")
        await self._ws.send(json.dumps({"user_audio_chunk": b64}))

    async def flush_audio(self):
        pass

    async def send_text(self, text: str):
        """Initialize the conversation — sends the init message to start the agent talking.
        Does NOT recv() here — receive_loop handles all incoming messages including metadata."""
        if not self._initiated:
            self._initiated = True
            system_instruction = self._call.get("system_instruction", "")
            settings = self._call.get("settings", {})
            has_agent = bool(settings.get("elevenlabs_agent_id"))

            config = {"type": "conversation_initiation_client_data"}
            prompt_source = settings.get("elevenlabs_prompt_source", "agent")
            language = settings.get("language", "en")

            # Override first_message to empty — wait for caller to speak first
            overrides = {"agent": {"first_message": ""}}

            # Use knowledge base prompt if selected, otherwise agent's own prompt
            if prompt_source == "knowledgebase" and system_instruction:
                strict_suffix = ""
                if settings.get("strict_mode"):
                    strict_suffix = "\n\nSTRICT MODE: You must ONLY use information from the reference documents and knowledge base provided above. If the caller asks something not covered in your reference materials, say 'I don't have that information in my reference materials.' Do NOT use general knowledge to answer questions."
                overrides["agent"]["prompt"] = {"prompt": system_instruction + strict_suffix}
                overrides["agent"]["language"] = language

            if not has_agent:
                voice_id = self._call.get("voice_name", "")
                if voice_id and len(voice_id) > 15:
                    overrides["tts"] = {"voice_id": voice_id}

            config["conversation_config_override"] = overrides

            if system_instruction:
                config["dynamic_variables"] = {"context": system_instruction}

            _log_error(self._call_id, f"ElevenLabs init: agent={has_agent}, prompt_source={prompt_source}")
            await self._ws.send(json.dumps(config))

    async def send_context(self, context: str):
        """Inject reference context mid-conversation via contextual_update."""
        if self._ws:
            try:
                await self._ws.send(json.dumps({
                    "type": "contextual_update",
                    "text": f"[REFERENCE INFO for answering the caller]: {context}"
                }))
            except Exception as e:
                log.error(f"[{self._call_id}] ElevenLabs send_context failed: {e}")

    async def receive_loop(self, on_audio, on_ai_transcript, on_caller_transcript, on_turn_complete, on_interrupted):
        """Main receive loop. Parses ElevenLabs Conversational AI events."""
        _first_audio = False
        async for raw in self._ws:
            event = json.loads(raw)
            t = event.get("type", "")

            if t == "audio":
                audio_b64 = event.get("audio_event", {}).get("audio_base_64", "")
                if audio_b64:
                    if not _first_audio:
                        _first_audio = True
                        _log_error(self._call_id, "PERF: first ElevenLabs audio received")
                    # Already ulaw 8kHz — pass straight through to Twilio
                    mulaw_bytes = base64.b64decode(audio_b64)
                    if len(mulaw_bytes) > 0:
                        await on_audio(mulaw_bytes)

            elif t == "user_transcript":
                transcript = event.get("user_transcription_event", {}).get("user_transcript", "")
                if transcript:
                    await on_caller_transcript(transcript)

            elif t == "agent_response":
                transcript = event.get("agent_response_event", {}).get("agent_response", "")
                if transcript:
                    await on_ai_transcript(transcript)
                    await on_turn_complete()

            elif t == "interruption":
                await on_interrupted()

            elif t == "ping":
                event_id = event.get("ping_event", {}).get("event_id", 0)
                await self._ws.send(json.dumps({"type": "pong", "event_id": event_id}))

            elif t == "conversation_initiation_metadata":
                meta = event.get("conversation_initiation_metadata_event", {})
                _log_error(self._call_id, f"ElevenLabs conversation started: conv={meta.get('conversation_id','?')}, format={meta.get('agent_output_audio_format','?')}")

            elif t == "error":
                _log_error(self._call_id, f"ElevenLabs error: {event}")

            elif t == "conversation_ended":
                _log_error(self._call_id, f"ElevenLabs conversation ended by server: {event}")
                break
        # Log close code/reason
        close_code = getattr(self._ws, 'close_code', None)
        close_reason = getattr(self._ws, 'close_reason', None) or ''
        _log_error(self._call_id, f"ElevenLabs receive_loop ended — close_code={close_code} reason={close_reason}")

    async def close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass


def _create_provider(call: dict, call_id: str):
    """Factory: create the right AI provider based on call settings."""
    provider_name = call.get("settings", {}).get("ai_provider", "gemini")
    if provider_name == "openai":
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not configured")
        return OpenAIProvider(call, call_id)
    elif provider_name == "elevenlabs":
        if not ELEVENLABS_API_KEY:
            raise RuntimeError("ELEVENLABS_API_KEY not configured")
        return ElevenLabsProvider(call, call_id)
    else:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not configured")
        return GeminiProvider(call, call_id)


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
    return {"ok": True, "active_calls": len(_active_calls), "version": SERVER_VERSION}


@app.get("/debug/errors")
async def debug_errors(request: Request):
    _verify_secret(request)
    return {"errors": _error_log[-50:]}


@app.get("/debug/active-calls")
async def debug_active_calls(request: Request):
    """Show active call state for debugging."""
    _verify_secret(request)
    safe_exclude = {"ai_provider", "transcript", "twilio_ws", "ai_ready", "ai_cancel"}
    result = {}
    for cid, c in _active_calls.items():
        entry = {}
        for k, v in c.items():
            if k in safe_exclude:
                continue
            if isinstance(v, asyncio.Event):
                entry[k] = v.is_set()
            else:
                try:
                    json.dumps(v)
                    entry[k] = v
                except (TypeError, ValueError):
                    entry[k] = str(v)
        # Add provider name
        if c.get("ai_provider"):
            entry["ai_provider_name"] = c["ai_provider"].name
        result[cid] = entry
    return result


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

    # Validate AI provider is configured
    ai_provider = settings.get("ai_provider", "gemini")
    if ai_provider == "openai" and not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured.")
    if ai_provider != "openai" and not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured.")

    call_id = str(uuid.uuid4())[:12]
    log.info(f"[{call_id}] Making call to {to_number} from {from_number} (provider: {ai_provider})")

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
        "ai_provider": None,
        "ai_ready": asyncio.Event(),
        "ai_error": None,
        "ai_cancel": asyncio.Event(),
    }
    _transcript_subscribers[call_id] = set()

    # Pre-connect to AI provider while the phone is ringing
    asyncio.create_task(_preconnect_ai(call_id))

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
                # Release pre-connected AI session if still waiting
                if "ai_cancel" in call:
                    call["ai_cancel"].set()
                # Broadcast end to subscribers
                await _broadcast_transcript(cid, {"type": "status", "status": "ended",
                                                   "timestamp": datetime.now(timezone.utc).isoformat()})
                # Save to Supabase
                await _save_call_history(cid)
            break

    return Response(content="OK", status_code=200)


# ═══════════════════════════════════════════════════════════════════════════════
#  AI PROVIDER PRE-CONNECT (starts during ring phase)
# ═══════════════════════════════════════════════════════════════════════════════

async def _preconnect_ai(call_id: str):
    """Connect to AI provider while the phone is still ringing.

    Creates the provider, connects it, stores it in _active_calls for media_stream.
    Keeps the connection alive until ai_cancel is set.
    """
    call = _active_calls.get(call_id)
    if not call:
        return

    provider = None
    try:
        provider = _create_provider(call, call_id)
        _log_error(call_id, f"Pre-connecting to {provider.name} (during ring)...")
        await provider.connect()
        _log_error(call_id, f"{provider.name} pre-connected! Waiting for Twilio media stream...")

        call["ai_provider"] = provider
        call["ai_ready"].set()

        # Keep alive until the call ends
        await call["ai_cancel"].wait()
        log.info(f"[{call_id}] AI pre-connect released")

    except Exception as e:
        _log_error(call_id, f"AI pre-connect error: {e}")
        import traceback
        _log_error(call_id, traceback.format_exc())
        call["ai_error"] = str(e)
        call["ai_ready"].set()
    finally:
        if provider:
            await provider.close()


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

    # Wait for pre-connected AI provider (should already be ready)
    try:
        _log_error(call_id, "Waiting for pre-connected AI provider...")
        await asyncio.wait_for(call["ai_ready"].wait(), timeout=15)
    except asyncio.TimeoutError:
        _log_error(call_id, "AI pre-connect timed out (15s)")
        await _broadcast_transcript(call_id, {
            "type": "status", "status": "error", "error": "AI connection timed out",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        call["ai_cancel"].set()
        await websocket.close()
        return

    if call.get("ai_error"):
        _log_error(call_id, f"AI pre-connect failed: {call['ai_error']}")
        await _broadcast_transcript(call_id, {
            "type": "status", "status": "error", "error": call["ai_error"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        call["ai_cancel"].set()
        await websocket.close()
        return

    provider = call["ai_provider"]
    log.info(f"[{call_id}] Using pre-connected {provider.name} provider")

    try:
        call["status"] = "connected"
        await _broadcast_transcript(call_id, {
            "type": "status", "status": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        # Send text trigger to make AI start speaking immediately
        try:
            await provider.send_text("The phone call has been answered. Start speaking now — greet the caller.")
            log.info(f"[{call_id}] Text trigger sent to {provider.name}")
        except Exception as e:
            _log_error(call_id, f"Text trigger failed (non-fatal): {e}")

        # Transcript accumulation buffers
        _ai_transcript_buffer = []
        _caller_transcript_buffer = []
        _perf = {"first_audio_in": None, "first_audio_out": None, "turns": 0}

        # ── Callbacks for provider.receive_loop() ──
        async def on_audio(mulaw_bytes: bytes):
            """AI audio output → send to Twilio + monitors."""
            if call.get("barged_in"):
                return
            if not _perf["first_audio_out"]:
                _perf["first_audio_out"] = datetime.now(timezone.utc).isoformat()
                _log_error(call_id, f"PERF: first audio out at {_perf['first_audio_out']}")
            if stream_sid and len(mulaw_bytes) > 0:
                payload_b64 = base64.b64encode(mulaw_bytes).decode("utf-8")
                asyncio.create_task(_broadcast_monitor(call_id, "ai", payload_b64))
                try:
                    await websocket.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload_b64}
                    }))
                except Exception:
                    pass

        _hangup_scheduled = {"task": None}

        async def on_ai_transcript(text: str):
            _ai_transcript_buffer.append(text)

        # RAG: check if multi-turn search is enabled for this call
        _rag_kb_id = call.get("settings", {}).get("rag_kb_id")
        _last_rag_query = {"text": ""}  # avoid duplicate searches

        async def on_caller_transcript(text: str):
            """Caller transcript — flush immediately, trigger RAG search if enabled."""
            # Cancel auto-hangup if caller speaks again
            if _hangup_scheduled.get("task"):
                _hangup_scheduled["task"].cancel()
                _hangup_scheduled["task"] = None
                log.info(f"[{call_id}] Auto-hangup cancelled — caller spoke again")
            ts = datetime.now(timezone.utc).isoformat()
            call["transcript"].append({"speaker": "caller", "text": text, "timestamp": ts})
            await _broadcast_transcript(call_id, {
                "type": "transcript", "speaker": "caller", "text": text, "timestamp": ts
            })

            # Multi-turn RAG: search documents based on what the caller just said
            # Fire-and-forget so receive_loop keeps processing pings/audio
            if _rag_kb_id and len(text.strip()) > 10 and text.strip() != _last_rag_query["text"]:
                _last_rag_query["text"] = text.strip()
                async def _do_rag_search(query: str):
                    try:
                        loop = asyncio.get_event_loop()
                        rag_context = await loop.run_in_executor(
                            None, _rag_search_sync, _rag_kb_id, query, 3
                        )
                        if rag_context:
                            await provider.send_context(rag_context)
                            _log_error(call_id, f"RAG: injected {len(rag_context)} chars for '{query[:50]}'")
                        else:
                            _log_error(call_id, f"RAG: no results for '{query[:50]}'")
                    except Exception as e:
                        _log_error(call_id, f"RAG search error: {e}")
                asyncio.create_task(_do_rag_search(text.strip()))

        async def _auto_hangup_after_delay():
            """Wait 5 seconds after AI goodbye, then disconnect."""
            await asyncio.sleep(5)
            log.info(f"[{call_id}] Auto-hangup: conversation ended naturally")
            # End the call via Twilio
            try:
                from twilio.rest import Client
                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                client.calls(call.get("call_sid")).update(status="completed")
            except Exception as e:
                _log_error(call_id, f"Auto-hangup failed: {e}")

        async def on_turn_complete():
            _perf["turns"] += 1
            _log_error(call_id, f"PERF: turn {_perf['turns']} complete")
            # Only AI transcript is buffered (arrives as fragments) — flush it now
            if _ai_transcript_buffer:
                full_text = " ".join(_ai_transcript_buffer)
                ts = datetime.now(timezone.utc).isoformat()
                call["transcript"].append({"speaker": "ai", "text": full_text, "timestamp": ts})
                await _broadcast_transcript(call_id, {
                    "type": "transcript", "speaker": "ai", "text": full_text, "timestamp": ts
                })
                _ai_transcript_buffer.clear()

                # Check if AI said goodbye — schedule auto-hangup
                # Check current turn AND last 2 AI transcripts (in case transcription is fragmented)
                lower = full_text.lower()
                _GOODBYE_PHRASES = ["goodbye", "good bye", "bye bye", "bye!", "bye.",
                    "have a great day", "have a good day", "have a nice day", "have a wonderful day",
                    "take care", "thanks for your time", "thank you for your time",
                    "talk to you soon", "speak to you soon", "cheers!", "cheers."]
                # Also check recent AI transcript entries in case goodbye was split across turns
                recent_ai = " ".join(t["text"] for t in call["transcript"][-3:] if t.get("speaker") == "ai").lower()
                _log_error(call_id, f"Goodbye check: current='{lower[:80]}' recent='{recent_ai[:80]}'")
                if any(phrase in lower for phrase in _GOODBYE_PHRASES) or any(phrase in recent_ai for phrase in _GOODBYE_PHRASES):
                    if not _hangup_scheduled["task"]:
                        log.info(f"[{call_id}] AI said goodbye, scheduling auto-hangup in 5s")
                        _hangup_scheduled["task"] = asyncio.create_task(_auto_hangup_after_delay())


        async def on_interrupted():
            if stream_sid:
                try:
                    await websocket.send_text(json.dumps({
                        "event": "clear", "streamSid": stream_sid
                    }))
                except Exception:
                    pass

        # ── Two concurrent tasks: Twilio→AI and AI→Twilio ──
        async def twilio_to_ai():
            nonlocal stream_sid
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
                            asyncio.create_task(_broadcast_monitor(call_id, "caller", data["media"]["payload"]))
                            continue

                        if not _perf["first_audio_in"]:
                            _perf["first_audio_in"] = datetime.now(timezone.utc).isoformat()

                        asyncio.create_task(_broadcast_monitor(call_id, "caller", data["media"]["payload"]))

                        # Send raw mulaw bytes — provider handles conversion
                        mulaw_bytes = base64.b64decode(data["media"]["payload"])
                        await provider.send_audio(mulaw_bytes)

                    elif data["event"] == "stop":
                        await provider.flush_audio()
                        log.info(f"[{call_id}] Stream stopped")
                        break

            except WebSocketDisconnect:
                _log_error(call_id, "Twilio WebSocket disconnected")
            except Exception as e:
                _log_error(call_id, f"Twilio→AI error: {e}")
                import traceback
                _log_error(call_id, traceback.format_exc())

        async def ai_to_twilio():
            try:
                await provider.receive_loop(on_audio, on_ai_transcript, on_caller_transcript, on_turn_complete, on_interrupted)
            except Exception as e:
                _log_error(call_id, f"AI→Twilio error: {e}")
                import traceback
                _log_error(call_id, traceback.format_exc())

        _log_error(call_id, f"{provider.name} connected! Starting audio bridge tasks...")
        twilio_task = asyncio.create_task(twilio_to_ai())
        ai_task = asyncio.create_task(ai_to_twilio())

        done, pending = await asyncio.wait(
            [twilio_task, ai_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            which = "Twilio" if task is twilio_task else "AI"
            _log_error(call_id, f"Call ended: {which} side disconnected first")
            if task.exception():
                _log_error(call_id, f"Task exception: {task.exception()}")
        for task in pending:
            task.cancel()

    except Exception as e:
        _log_error(call_id, f"Audio bridge error: {e}")
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
        # Release the pre-connected AI session
        call["ai_cancel"].set()
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


def _calculate_cost_nzd(call: dict, duration_seconds: int) -> dict:
    """Calculate call cost in NZD. Returns breakdown dict.

    Each provider calculates its AI cost differently:
    - gemini: estimated from duration (no token reporting)
    - openai: actual tokens from response.done events
    Future providers: add a new elif branch with their cost logic.
    """
    mins = (duration_seconds or 0) / 60.0
    provider_name = call.get("settings", {}).get("ai_provider", "gemini")
    provider = call.get("ai_provider")  # the provider instance

    # Twilio costs (same for all providers)
    twilio = COST_RATES["twilio"]
    twilio_voice = mins * twilio["voice_nz_mobile_per_min"]
    twilio_stream = mins * twilio["media_stream_per_min"]
    twilio_recording = mins * twilio["recording_per_min"]
    twilio_total = twilio_voice + twilio_stream + twilio_recording

    # AI provider cost
    ai_cost = 0.0
    token_info = {}

    if provider_name == "openai" and provider and hasattr(provider, "audio_input_tokens"):
        rates = COST_RATES["openai"]
        ai_in = provider.audio_input_tokens * rates["audio_input_per_1m_tokens"] / 1_000_000
        ai_out = provider.audio_output_tokens * rates["audio_output_per_1m_tokens"] / 1_000_000
        transcription = mins * rates["transcription_per_min"]
        ai_cost = ai_in + ai_out + transcription
        token_info = {
            "audio_input_tokens": provider.audio_input_tokens,
            "audio_output_tokens": provider.audio_output_tokens,
            "total_input_tokens": provider.total_input_tokens,
            "total_output_tokens": provider.total_output_tokens,
        }
    elif provider_name == "elevenlabs":
        ai_cost = mins * COST_RATES["elevenlabs"]["per_min"]
    elif provider_name == "gemini":
        ai_cost = mins * COST_RATES["gemini"]["audio_per_min"]
    # Future providers: add elif branches here

    total_usd = twilio_total + ai_cost
    total_nzd = total_usd * USD_TO_NZD

    return {
        "total_nzd": round(total_nzd, 4),
        "total_usd": round(total_usd, 4),
        "twilio_usd": round(twilio_total, 4),
        "ai_usd": round(ai_cost, 4),
        "provider": provider_name,
        "duration_mins": round(mins, 2),
        "usd_to_nzd": USD_TO_NZD,
        **token_info,
    }


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

    # Calculate cost
    cost = _calculate_cost_nzd(call, duration or 0)
    log.info(f"[{call_id}] Cost: ${cost['total_nzd']:.4f} NZD (Twilio: ${cost['twilio_usd']:.4f} USD, AI: ${cost['ai_usd']:.4f} USD)")

    # Build notes with provider + cost JSON
    notes_data = {
        "ai_provider": call.get("settings", {}).get("ai_provider", "gemini"),
        "cost": cost,
    }

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
                "notes": json.dumps(notes_data),
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
