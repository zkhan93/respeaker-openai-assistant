"""OpenAI Realtime API consumer - bidirectional voice conversation."""

import asyncio
import logging
import queue
import threading
import time
from typing import Optional

import pyaudio

from ..core.audio_handler import AudioHandler
from ..core.event_bus import EventBus, HotwordEvent, VoiceActivityEvent
from ..services.openai_client import OpenAIRealtimeClient

logger = logging.getLogger(__name__)


class RealtimeConsumer:
    """Consumes hotword events and enables realtime voice conversation with OpenAI."""

    def __init__(
        self,
        event_bus: EventBus,
        audio_handler: AudioHandler,
        openai_api_key: str,
        model: str = "gpt-4o-realtime-preview-2024-12-17",
    ):
        """Initialize Realtime consumer.

        Args:
            event_bus: Event bus to subscribe to
            audio_handler: Audio handler to pull audio from
            openai_api_key: OpenAI API key
            model: Realtime API model name
        """
        self.event_bus = event_bus
        self.audio_handler = audio_handler
        self.openai_api_key = openai_api_key
        self.model = model

        # OpenAI Realtime client
        self.client: Optional[OpenAIRealtimeClient] = None
        self.listen_task: Optional[asyncio.Task] = None  # Track listen task

        # Conversation state
        self.in_conversation = False
        self.streaming_audio = False
        self.audio_stream_thread: Optional[threading.Thread] = None
        self.collected_audio = bytearray()  # Collect audio instead of streaming

        # Audio playback
        self.audio = pyaudio.PyAudio()
        self.playback_stream: Optional[pyaudio.Stream] = None
        self.playback_queue = queue.Queue()
        self.playing = False
        self.playback_thread: Optional[threading.Thread] = None

        # Event loop for async operations
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None

        # Subscribe to events
        self.event_bus.subscribe("hotword_detected", self.on_hotword_detected)
        self.event_bus.subscribe("voice_activity_stopped", self.on_voice_stopped)

        logger.info(f"RealtimeConsumer initialized (model={model})")

    def start(self):
        """Start the consumer (initializes async event loop)."""
        if self.loop_thread:
            logger.warning("Consumer already started")
            return

        # Create event loop in separate thread
        self.loop_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.loop_thread.start()

        # Wait for loop to be ready
        while not self.loop:
            time.sleep(0.01)

        logger.info("RealtimeConsumer started")

    def _run_event_loop(self):
        """Run asyncio event loop in background thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        logger.info("Event loop started")
        self.loop.run_forever()

    def on_hotword_detected(self, event: HotwordEvent):
        """Handle hotword detected event - start conversation.

        Args:
            event: Hotword event with timestamp and details
        """
        if self.in_conversation:
            # Already in conversation - might be interruption or new command
            logger.info(
                f"Hotword detected during conversation - treating as interruption/new command"
            )
            print(f"\n🎤 New hotword '{event.hotword}' - restarting conversation...")

            # Cancel ongoing response and restart
            if self.loop:
                asyncio.run_coroutine_threadsafe(self._restart_conversation(), self.loop)
            return

        print(f"\n🎤 Hotword '{event.hotword}' detected! Starting conversation...")

        logger.info(f"🎤 Hotword detected! Starting realtime conversation...")
        logger.info(f"   Hotword: '{event.hotword}' (score: {event.score:.3f})")
        logger.info(f"   Queue size at detection: {event.audio_queue_size} frames")

        # Start conversation
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._start_conversation(), self.loop)

    def on_voice_stopped(self, event: VoiceActivityEvent):
        """Handle voice activity stopped event - commit audio and get response.

        Args:
            event: Voice activity event with timestamp and duration
        """
        if not self.in_conversation:
            # Not in conversation, ignore
            logger.debug(
                f"Voice activity stopped (duration: {event.duration:.1f}s) but not in conversation"
            )
            return

        print(f"🔇 Voice stopped. Getting AI response...")

        logger.info(f"🔇 Voice stopped (duration: {event.duration:.1f}s)")

        # Stop streaming audio and commit
        self.streaming_audio = False

        if self.loop:
            asyncio.run_coroutine_threadsafe(self._commit_and_respond(), self.loop)

    async def _start_conversation(self):
        """Start conversation - connect to OpenAI and begin streaming."""
        try:
            # Reuse existing connection if available and connected
            if self.client and self.client.connected:
                logger.info("♻️ Reusing existing connection")
                print("♻️ Connection active! Speak your command...")
                # Clear collected audio for fresh start
                self.collected_audio.clear()
            else:
                # Create new connection
                if self.client:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                    self.client = None
                    self.listen_task = None
                
                # Create new client
                self.client = OpenAIRealtimeClient(
                    api_key=self.openai_api_key, model=self.model
                )

                # Setup callbacks
                self.client.on_audio_delta = self._on_audio_received
                self.client.on_response_done = self._on_response_done
                self.client.on_error = self._on_error

                # Connect
                print("⏳ Connecting to OpenAI Realtime API...")
                await self.client.connect()
                print("✓ Connected! Speak your command...")

                # Start listening for responses (only once per connection!)
                self.listen_task = asyncio.create_task(self.client.listen())
                logger.debug("Started listen task")

            # Start conversation
            self.in_conversation = True
            self.streaming_audio = True

            # Start audio streaming thread
            self.audio_stream_thread = threading.Thread(
                target=self._stream_audio_loop, daemon=True
            )
            self.audio_stream_thread.start()

            # Start playback thread if not already running
            if not self.playback_thread or not self.playback_thread.is_alive():
                self.playback_thread = threading.Thread(
                    target=self._playback_loop, daemon=True
                )
                self.playback_thread.start()

        except Exception as e:
            logger.error(f"Error starting conversation: {e}", exc_info=True)
            print(f"\n❌ Error: {e}\n")
            self.in_conversation = False

    async def _restart_conversation(self):
        """Restart conversation (cancel current and start new)."""
        try:
            # Try to cancel ongoing response (gracefully handle if none exists)
            if self.client:
                cancelled = await self.client.cancel_response()
                if cancelled:
                    logger.info("Cancelled ongoing response")
                else:
                    logger.debug("No active response to cancel, proceeding with restart")

            # Stop current audio streaming
            self.streaming_audio = False
            
            # Give threads a moment to stop
            await asyncio.sleep(0.1)

            # Clear playback queue
            while not self.playback_queue.empty():
                try:
                    self.playback_queue.get_nowait()
                except queue.Empty:
                    break

            # Clear audio queue and collected audio to start fresh
            self.audio_handler.clear_audio_queue()
            self.collected_audio.clear()

            # Restart collecting
            self.streaming_audio = True

            # Start new audio streaming thread
            self.audio_stream_thread = threading.Thread(
                target=self._stream_audio_loop, daemon=True
            )
            self.audio_stream_thread.start()

            logger.info("✓ Conversation restarted - ready for new command")
            print("✓ Ready! Speak your new command...")

        except Exception as e:
            logger.error(f"Error restarting conversation: {e}", exc_info=True)
            print(f"❌ Restart error: {e}")

    async def _commit_and_respond(self):
        """Send collected audio and request response."""
        try:
            if not self.client:
                return

            print(f"⏳ Processing {len(self.collected_audio)} bytes of audio...")
            logger.info(f"Sending complete audio: {len(self.collected_audio)} bytes")
            
            # Send complete audio (bypasses server VAD!)
            await self.client.send_complete_audio(bytes(self.collected_audio))

        except Exception as e:
            logger.error(f"Error sending audio: {e}", exc_info=True)
            print(f"\n❌ Error: {e}\n")

    def _stream_audio_loop(self):
        """Collect audio from microphone in background thread."""
        logger.debug("Audio collection thread started")
        
        # Clear collected audio at start
        self.collected_audio.clear()

        while self.streaming_audio and self.in_conversation:
            chunk = self.audio_handler.read_audio_chunk(timeout=0.1)
            if chunk:
                # Collect audio instead of streaming it
                self.collected_audio.extend(chunk)
                logger.debug(f"Collected audio chunk: {len(chunk)} bytes (total: {len(self.collected_audio)})")

        logger.debug(f"Audio collection stopped. Total collected: {len(self.collected_audio)} bytes")

    def _on_audio_received(self, audio_data: bytes):
        """Callback when audio delta received from OpenAI.

        Args:
            audio_data: PCM16 audio data to play
        """
        logger.debug(f"Received audio chunk: {len(audio_data)} bytes")
        
        # Queue audio for playback
        self.playback_queue.put(audio_data)

        # Start playing if not already
        if not self.playing:
            self.playing = True
            print("🔊 AI is responding...")
            if not self.playback_stream:
                self._start_playback()

    def _start_playback(self):
        """Start audio playback stream."""
        try:
            # Get default output device info
            default_output = self.audio.get_default_output_device_info()
            logger.info(f"Using output device: {default_output['name']}")
            
            self.playback_stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,  # Mono
                rate=24000,  # OpenAI outputs 24kHz
                output=True,
                frames_per_buffer=1024,
            )
            logger.info("Playback stream started (24kHz, mono, PCM16)")
            print("🔊 Playback stream ready")
        except Exception as e:
            logger.error(f"Error starting playback: {e}", exc_info=True)
            print(f"❌ Playback error: {e}")

    def _playback_loop(self):
        """Playback audio from queue in background thread."""
        logger.debug("Playback thread started")
        chunks_played = 0

        while True:
            try:
                # Get audio from queue (blocking with timeout)
                audio_data = self.playback_queue.get(timeout=1.0)
                chunks_played += 1
                logger.debug(f"Playing audio chunk {chunks_played}: {len(audio_data)} bytes")

                # Play audio
                if self.playback_stream:
                    self.playback_stream.write(audio_data)

            except queue.Empty:
                # No audio to play, continue waiting
                continue
            except Exception as e:
                logger.error(f"Error in playback loop: {e}")
                time.sleep(0.1)

    def _on_response_done(self):
        """Callback when response is complete."""
        print("\n✓ Response complete\n")
        logger.info("Response complete")

        # End conversation (order matters!)
        self.streaming_audio = False  # Stop streaming first
        self.playing = False
        
        # Wait a moment for threads to finish
        time.sleep(0.2)
        
        # DON'T disconnect - keep connection alive for next conversation!
        # Just clear the audio buffer for fresh start
        if self.loop and self.client:
            asyncio.run_coroutine_threadsafe(self.client.clear_audio_buffer(), self.loop)
        
        # Mark as ready for next conversation
        self.in_conversation = False
        
        logger.info("Ready for next conversation (connection kept alive)")

    def _on_error(self, error_msg: str):
        """Callback when error occurs.

        Args:
            error_msg: Error message
        """
        # Some errors are not critical and don't require ending the conversation
        non_critical_errors = [
            "no active response",
            "Cancellation failed",
        ]
        
        is_critical = not any(err in error_msg for err in non_critical_errors)
        
        if is_critical:
            print(f"\n❌ API Error: {error_msg}\n")
            logger.error(f"Critical API error: {error_msg}")
            
            # End conversation for critical errors
            self.in_conversation = False
            self.streaming_audio = False
        else:
            # Just log non-critical errors
            logger.warning(f"Non-critical API message: {error_msg}")

    def cleanup(self):
        """Cleanup consumer resources."""
        # Stop conversation
        self.in_conversation = False
        self.streaming_audio = False
        self.playing = False

        # Cancel listen task
        if self.listen_task and not self.listen_task.done():
            self.listen_task.cancel()
            logger.debug("Cancelled listen task")
        
        # Disconnect from OpenAI
        if self.loop and self.client:
            try:
                asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop).result(timeout=2.0)
            except Exception as e:
                logger.error(f"Error disconnecting during cleanup: {e}")
            finally:
                self.client = None
                self.listen_task = None

        # Cleanup audio
        if self.playback_stream:
            self.playback_stream.stop_stream()
            self.playback_stream.close()
        self.audio.terminate()

        # Stop event loop
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

        # Unsubscribe from events
        self.event_bus.unsubscribe("hotword_detected", self.on_hotword_detected)
        self.event_bus.unsubscribe("voice_activity_stopped", self.on_voice_stopped)

        logger.info("RealtimeConsumer cleaned up")

