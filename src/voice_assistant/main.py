"""Main voice assistant service orchestrator."""

import asyncio
import logging
import queue
import signal
from typing import Optional

import pygame

from .config import Config
from .core import AudioHandler, HotwordDetector
from .services import OpenAIRealtimeClient, State, StateMachine

logger = logging.getLogger(__name__)


class VoiceAssistant:
    """Main voice assistant service."""

    def __init__(self, config_path: str = "config/config.yaml"):
        """Initialize voice assistant.

        Args:
            config_path: Path to configuration file
        """
        # Load configuration
        self.config = Config(config_path)

        # Initialize components
        self.audio_handler = AudioHandler(
            device_name=self.config.audio_device,
            sample_rate=self.config.audio_sample_rate,
            channels=self.config.audio_channels,
            vad_aggressiveness=self.config.vad_aggressiveness,
        )

        self.hotword_detector = HotwordDetector(
            threshold=self.config.hotword_threshold,
            sample_rate=self.config.audio_sample_rate,
        )

        self.state_machine = StateMachine()

        # OpenAI client (initialized async)
        self.openai_client: Optional[OpenAIRealtimeClient] = None

        # Audio playback
        pygame.mixer.init(frequency=24000, size=-16, channels=1)
        self.playback_queue = queue.Queue()

        # Control flags
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.tasks: list = []

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("VoiceAssistant initialized")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

        # Cancel all running tasks
        if self.loop and self.tasks:
            for task in self.tasks:
                if not task.done():
                    task.cancel()

        # Schedule cleanup in the event loop
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def run(self):
        """Run the voice assistant service."""
        self.running = True

        # Start audio stream
        self.audio_handler.start_stream()

        # Run async event loop
        try:
            asyncio.run(self._async_main())
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            logger.info("Service stopped")

    async def _async_main(self):
        """Async main event loop."""
        self.loop = asyncio.get_event_loop()

        # Initialize OpenAI client
        self.openai_client = OpenAIRealtimeClient(api_key=self.config.openai_api_key)

        # Setup callbacks
        self.openai_client.on_audio_delta = self._on_audio_delta
        self.openai_client.on_response_done = self._on_response_done
        self.openai_client.on_error = self._on_error

        try:
            # Connect to OpenAI
            await self.openai_client.connect()

            # Start listening task
            listen_task = asyncio.create_task(self.openai_client.listen())

            # Start main loop
            main_task = asyncio.create_task(self._main_loop())

            # Track tasks for cancellation
            self.tasks = [listen_task, main_task]

            # Wait for tasks
            await asyncio.gather(listen_task, main_task, return_exceptions=True)

        except asyncio.CancelledError:
            logger.info("Tasks cancelled, shutting down...")
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        finally:
            await self._cleanup()

    async def _main_loop(self):
        """Main processing loop."""
        logger.info("Starting main loop")

        while self.running:
            current_state = self.state_machine.state

            if current_state == State.IDLE:
                await self._handle_idle_state()

            elif current_state == State.LISTENING:
                await self._handle_listening_state()

            elif current_state == State.PROCESSING:
                await self._handle_processing_state()

            elif current_state == State.INTERRUPTED:
                # Transition back to listening
                self.state_machine.transition(State.LISTENING)

            await asyncio.sleep(0.01)  # Small delay to prevent busy loop

    async def _handle_idle_state(self):
        """Handle IDLE state - listen for hotword."""
        # Read audio chunk
        audio_data = self.audio_handler.read_chunk()
        if not audio_data:
            return

        # Convert to PCM16 mono
        pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)

        # Check for hotword
        if self.hotword_detector.detect(pcm16_data):
            logger.info("Hotword detected! Transitioning to LISTENING")
            self.state_machine.transition(State.LISTENING)

    async def _handle_listening_state(self):
        """Handle LISTENING state - stream audio to OpenAI."""
        # Read audio chunk
        audio_data = self.audio_handler.read_chunk()
        if not audio_data:
            return

        # Convert to PCM16 mono
        pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)

        # Send to OpenAI
        if self.openai_client:
            await self.openai_client.send_audio(pcm16_data)

        # Check for silence/end of speech (simplified for now)
        # In production, rely on OpenAI's server VAD

    async def _handle_processing_state(self):
        """Handle PROCESSING state - play audio and monitor for interruptions."""
        # Read audio chunk
        audio_data = self.audio_handler.read_chunk()
        if not audio_data:
            return

        # Convert to PCM16 mono
        pcm16_data = self.audio_handler.convert_to_pcm16_mono(audio_data)

        # Check for interruption (user speaking)
        if self.audio_handler.is_speech(pcm16_data):
            logger.info("User interruption detected!")
            self.state_machine.transition(State.INTERRUPTED)

            # Cancel OpenAI response
            if self.openai_client:
                await self.openai_client.cancel_response()

            # Clear playback queue
            while not self.playback_queue.empty():
                try:
                    self.playback_queue.get_nowait()
                except queue.Empty:
                    break

            # Stop playback
            pygame.mixer.stop()

    def _on_audio_delta(self, audio_data: bytes):
        """Callback for receiving audio from OpenAI.

        Args:
            audio_data: PCM16 audio data from OpenAI
        """
        # Transition to PROCESSING if not already
        if self.state_machine.state == State.LISTENING:
            self.state_machine.transition(State.PROCESSING)

        # Queue audio for playback
        self.playback_queue.put(audio_data)

        # Play audio (simplified - in production use proper audio streaming)
        try:
            sound = pygame.mixer.Sound(buffer=audio_data)
            sound.play()
        except Exception as e:
            logger.error(f"Error playing audio: {e}")

    def _on_response_done(self):
        """Callback for response completion."""
        logger.info("Response complete, returning to IDLE")
        self.state_machine.transition(State.IDLE)

    def _on_error(self, error: str):
        """Callback for errors.

        Args:
            error: Error message
        """
        logger.error(f"OpenAI client error: {error}")
        self.state_machine.reset()

    async def _cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up resources...")

        # Disconnect from OpenAI
        if self.openai_client:
            await self.openai_client.disconnect()

        # Stop audio
        self.audio_handler.cleanup()
        pygame.mixer.quit()

        logger.info("Cleanup complete")
