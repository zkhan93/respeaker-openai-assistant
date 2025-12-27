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
        self.has_active_response = False  # Track if response is in progress
        self.response_id: Optional[str] = None  # Track current response ID

        # Callbacks
        self.on_audio_delta: Optional[Callable[[bytes], None]] = None
        self.on_response_done: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    async def connect(self, max_retries: int = 3, retry_delay: float = 1.0):
        """Establish WebSocket connection to OpenAI Realtime API with retry logic.

        Args:
            max_retries: Maximum number of connection attempts
            retry_delay: Delay between retries in seconds
        """
        last_error = None

        for attempt in range(max_retries):
            try:
                # Use additional_headers parameter for websockets library (v13.0+)
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                }

                self.websocket = await websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=20,  # Send ping every 20s to keep alive
                    ping_timeout=10,  # Wait 10s for pong
                )

                self.connected = True
                self.has_active_response = False
                self.response_id = None
                logger.info("Connected to OpenAI Realtime API")

                # Configure session
                await self._configure_session()
                return  # Success!

            except Exception as e:
                last_error = e
                self.connected = False

                if attempt < max_retries - 1:
                    logger.warning(
                        f"Connection attempt {attempt + 1}/{max_retries} failed: {e}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"Failed to connect after {max_retries} attempts: {e}")
                    if self.on_error:
                        self.on_error(str(e))

        raise last_error or Exception("Failed to connect")

    async def disconnect(self):
        """Close WebSocket connection."""
        if self.websocket:
            self.connected = False  # Set before closing to prevent race conditions
            try:
                await self.websocket.close()
            except Exception as e:
                logger.debug(f"Error during websocket close: {e}")
            finally:
                self.websocket = None
                logger.info("Disconnected from OpenAI Realtime API")

    async def _configure_session(self):
        """Configure session parameters.

        Using absolute minimal configuration. OpenAI has removed most parameters
        from the API. They now auto-detect format and handle VAD internally.
        """
        config = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": "You are a helpful voice assistant. Be concise and conversational.",
            },
        }

        await self.websocket.send(json.dumps(config))
        logger.info("Session configured")

    async def send_audio(self, audio_data: bytes):
        """Send audio data to OpenAI.

        Args:
            audio_data: PCM16 mono audio data (16-bit signed integer, little-endian)
        """
        if not self.connected or not self.websocket:
            logger.warning("Cannot send audio: not connected")
            return

        # Validate audio format
        if len(audio_data) == 0:
            logger.warning("Empty audio data, skipping")
            return

        if len(audio_data) % 2 != 0:
            logger.warning(f"Audio data has odd number of bytes: {len(audio_data)}, truncating")
            audio_data = audio_data[:-1]

        try:
            # Encode audio as base64
            audio_base64 = base64.b64encode(audio_data).decode("utf-8")

            message = {
                "type": "input_audio_buffer.append",
                "audio": audio_base64,
            }

            await self.websocket.send(json.dumps(message))
            logger.debug(f"📤 Sent audio chunk: {len(audio_data)} bytes")

        except Exception as e:
            error_str = str(e)
            # Ignore normal closure errors (code 1000)
            if "1000" in error_str and "OK" in error_str:
                logger.debug(f"WebSocket closed normally while sending: {e}")
                self.connected = False
            else:
                logger.error(f"Error sending audio: {e}")
                if self.on_error:
                    self.on_error(error_str)

    async def send_complete_audio(self, audio_data: bytes):
        """Send complete audio as a conversation item (no streaming).

        This bypasses the input_audio_buffer and server-side VAD entirely.
        We collect all audio client-side, then send it in one message.

        Args:
            audio_data: Complete PCM16 mono audio data for the entire utterance
        """
        if not self.connected or not self.websocket:
            logger.warning("Cannot send audio: not connected")
            return

        if len(audio_data) == 0:
            logger.warning("Empty audio data, skipping")
            return

        try:
            # Encode complete audio as base64
            audio_base64 = base64.b64encode(audio_data).decode("utf-8")

            # Create conversation item with audio
            message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "audio": audio_base64,
                        }
                    ],
                },
            }

            await self.websocket.send(json.dumps(message))
            logger.info(f"Sent complete audio: {len(audio_data)} bytes")

            # Request response
            response_message = {
                "type": "response.create",
            }
            await self.websocket.send(json.dumps(response_message))
            logger.info("Response requested")

        except Exception as e:
            logger.error(f"Error sending complete audio: {e}")
            if self.on_error:
                self.on_error(str(e))

    async def commit_audio(self):
        """Commit audio buffer and trigger response generation.

        NOTE: This uses streaming mode with server-side VAD which may cause issues.
        Consider using send_complete_audio() instead.
        """
        if not self.connected or not self.websocket:
            return

        try:
            message = {"type": "input_audio_buffer.commit"}
            await self.websocket.send(json.dumps(message))

            # Request response generation (modalities set at session level)
            response_message = {
                "type": "response.create",
            }
            await self.websocket.send(json.dumps(response_message))

            logger.info("Audio committed and response requested")

        except Exception as e:
            logger.error(f"Error committing audio: {e}")
            if self.on_error:
                self.on_error(str(e))

    async def clear_audio_buffer(self):
        """Clear the input audio buffer without committing.

        Use this between conversations to start with a fresh buffer.
        """
        if not self.connected or not self.websocket:
            logger.debug("Cannot clear buffer: not connected")
            return

        try:
            message = {"type": "input_audio_buffer.clear"}
            await self.websocket.send(json.dumps(message))
            logger.debug("Audio buffer cleared")
        except Exception as e:
            logger.error(f"Error clearing audio buffer: {e}")

    async def cancel_response(self):
        """Cancel ongoing response (for interruptions).

        Returns:
            bool: True if cancellation was attempted, False if no active response
        """
        if not self.connected or not self.websocket:
            logger.debug("Cannot cancel: not connected")
            return False

        if not self.has_active_response:
            logger.debug("No active response to cancel")
            return False

        try:
            message = {"type": "response.cancel"}
            if self.response_id:
                message["response_id"] = self.response_id

            await self.websocket.send(json.dumps(message))
            logger.info(f"Response cancellation requested (id: {self.response_id})")
            self.has_active_response = False
            self.response_id = None
            return True

        except Exception as e:
            logger.error(f"Error cancelling response: {e}")
            return False

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

            if event_type == "response.output_audio.delta":
                # Audio chunk received (NEW format: response.output_audio.delta)
                audio_base64 = data.get("delta", "")
                if audio_base64 and self.on_audio_delta:
                    audio_data = base64.b64decode(audio_base64)
                    logger.info(f"📥 Received audio delta: {len(audio_data)} bytes")
                    self.on_audio_delta(audio_data)

            elif event_type == "response.output_audio.done":
                logger.info("🎵 Audio response complete")

            elif event_type == "response.done":
                logger.info("✅ Response complete")
                self.has_active_response = False
                self.response_id = None
                if self.on_response_done:
                    self.on_response_done()

            elif event_type == "error":
                error_msg = data.get("error", {}).get("message", "Unknown error")
                logger.error(f"API error: {error_msg}")
                if self.on_error:
                    self.on_error(error_msg)

            elif event_type == "response.output_audio_transcript.delta":
                # Transcript of what the AI is saying (NEW format)
                transcript = data.get("delta", "")
                if transcript:
                    logger.info(f"💬 AI: {transcript}")

            elif event_type == "response.output_audio_transcript.done":
                logger.debug("Transcript complete")

            elif event_type == "response.text.delta":
                # Text response (if any)
                text = data.get("delta", "")
                if text:
                    logger.info(f"AI text: {text}")

            elif event_type == "input_audio_buffer.speech_started":
                logger.info("Speech detected in input")

            elif event_type == "input_audio_buffer.speech_stopped":
                logger.info("Speech stopped in input")

            elif event_type == "input_audio_buffer.committed":
                logger.debug("Audio buffer committed")

            elif event_type == "input_audio_buffer.cleared":
                logger.debug("Audio buffer cleared")

            elif event_type == "conversation.item.created":
                logger.debug("Conversation item created")

            elif event_type == "conversation.item.added":
                logger.debug("Conversation item added")

            elif event_type == "conversation.item.done":
                logger.debug("Conversation item done")

            elif event_type == "response.output_item.added":
                logger.debug("Response output item added")

            elif event_type == "response.output_item.done":
                logger.debug("Response output item done")

            elif event_type == "response.content_part.added":
                logger.debug("Response content part added")

            elif event_type == "response.content_part.done":
                logger.debug("Response content part done")

            elif event_type == "response.created":
                self.has_active_response = True
                self.response_id = data.get("response", {}).get("id")
                logger.info(f"🤖 AI response started (id: {self.response_id})")

            elif event_type == "response.cancelled":
                logger.info("Response cancelled by server")
                self.has_active_response = False
                self.response_id = None

            elif event_type == "session.created":
                logger.debug("Session created")

            elif event_type == "session.updated":
                logger.debug("Session updated")

            elif event_type == "rate_limits.updated":
                logger.debug("Rate limits updated")

            else:
                # Log unknown event types
                logger.info(f"⚠️ Unknown event: {event_type}")

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
