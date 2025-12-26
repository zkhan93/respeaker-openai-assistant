"""OpenAI Realtime API client for bidirectional audio streaming."""

import asyncio
import base64
import json
import logging
from typing import Callable, Optional

import websockets

logger = logging.getLogger(__name__)


class OpenAIRealtimeClient:
    """WebSocket client for OpenAI Realtime API."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-realtime",
    ):
        """Initialize OpenAI Realtime API client.

        Args:
            api_key: OpenAI API key
            model: Model name for Realtime API (default: gpt-realtime)
        """
        self.api_key = api_key
        self.model = model
        self.ws_url = f"wss://api.openai.com/v1/realtime?model={model}"

        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False

        # Callbacks
        self.on_audio_delta: Optional[Callable[[bytes], None]] = None
        self.on_response_done: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    async def connect(self):
        """Establish WebSocket connection to OpenAI Realtime API."""
        try:
            # Use additional_headers parameter for websockets library (v13.0+)
            headers = {
                "Authorization": f"Bearer {self.api_key}",
            }

            self.websocket = await websockets.connect(
                self.ws_url,
                additional_headers=headers,
            )

            self.connected = True
            logger.info("Connected to OpenAI Realtime API")

            # Configure session
            await self._configure_session()

        except Exception as e:
            logger.error(f"Failed to connect to OpenAI Realtime API: {e}")
            self.connected = False
            if self.on_error:
                self.on_error(str(e))
            raise

    async def disconnect(self):
        """Close WebSocket connection."""
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
            self.connected = False
            logger.info("Disconnected from OpenAI Realtime API")

    async def _configure_session(self):
        """Configure session parameters."""
        config = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "modalities": ["text", "audio"],
                "instructions": "You are a helpful voice assistant. Be concise and conversational.",
                "voice": "alloy",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
            },
        }

        await self.websocket.send(json.dumps(config))
        logger.info("Session configured")

    async def send_audio(self, audio_data: bytes):
        """Send audio data to OpenAI.

        Args:
            audio_data: PCM16 mono audio data
        """
        if not self.connected or not self.websocket:
            logger.warning("Cannot send audio: not connected")
            return

        try:
            # Encode audio as base64
            audio_base64 = base64.b64encode(audio_data).decode("utf-8")

            message = {
                "type": "input_audio_buffer.append",
                "audio": audio_base64,
            }

            await self.websocket.send(json.dumps(message))

        except Exception as e:
            logger.error(f"Error sending audio: {e}")
            if self.on_error:
                self.on_error(str(e))

    async def commit_audio(self):
        """Commit audio buffer and trigger response generation."""
        if not self.connected or not self.websocket:
            return

        try:
            message = {"type": "input_audio_buffer.commit"}
            await self.websocket.send(json.dumps(message))

            # Request response generation
            response_message = {
                "type": "response.create",
                "response": {
                    "modalities": ["text", "audio"],
                },
            }
            await self.websocket.send(json.dumps(response_message))

            logger.debug("Audio committed and response requested")

        except Exception as e:
            logger.error(f"Error committing audio: {e}")
            if self.on_error:
                self.on_error(str(e))

    async def cancel_response(self):
        """Cancel ongoing response (for interruptions)."""
        if not self.connected or not self.websocket:
            return

        try:
            message = {"type": "response.cancel"}
            await self.websocket.send(json.dumps(message))
            logger.info("Response cancelled")
        except Exception as e:
            logger.error(f"Error cancelling response: {e}")

    async def listen(self):
        """Listen for messages from OpenAI and process them."""
        if not self.websocket:
            return

        try:
            async for message in self.websocket:
                await self._handle_message(message)
        except asyncio.CancelledError:
            logger.info("Listen task cancelled")
            self.connected = False
            raise
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self.connected = False
        except Exception as e:
            logger.error(f"Error in listen loop: {e}")
            if self.on_error:
                self.on_error(str(e))

    async def _handle_message(self, message: str):
        """Handle incoming WebSocket message.

        Args:
            message: JSON message from OpenAI
        """
        try:
            data = json.loads(message)
            event_type = data.get("type")

            if event_type == "response.audio.delta":
                # Audio chunk received
                audio_base64 = data.get("delta", "")
                if audio_base64 and self.on_audio_delta:
                    audio_data = base64.b64decode(audio_base64)
                    self.on_audio_delta(audio_data)

            elif event_type == "response.audio.done":
                logger.info("Audio response complete")

            elif event_type == "response.done":
                logger.info("Response complete")
                if self.on_response_done:
                    self.on_response_done()

            elif event_type == "error":
                error_msg = data.get("error", {}).get("message", "Unknown error")
                logger.error(f"API error: {error_msg}")
                if self.on_error:
                    self.on_error(error_msg)

            else:
                logger.debug(f"Received event: {event_type}")

        except Exception as e:
            logger.error(f"Error handling message: {e}")
