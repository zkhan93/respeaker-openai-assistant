"""ZeroMQ PUB/SUB audio and event broadcaster with LED command ingestion."""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

import zmq

from .audio_bus import AudioBusReader
from .audio_handler import AudioHandler
from .event_bus import EventBus, HotwordEvent, VoiceActivityEvent

logger = logging.getLogger(__name__)


class AudioBroadcaster:
    """Bridges the in-process AudioBus and EventBus to ZeroMQ.

    Outgoing (PUB socket):
      - b"audio" : [header_json, pcm16_bytes]
      - b"event" : [event_json]
      - b"meta"  : [meta_json]

    Incoming (PULL socket):
      - LED commands: {"type": "led", "pattern": "think"|"speak"|"off"|...}
    """

    def __init__(
        self,
        audio_handler: AudioHandler,
        event_bus: EventBus,
        led_consumer=None,
        pub_endpoint: str = "tcp://*:5555",
        pull_endpoint: str = "tcp://*:5556",
        meta_interval: float = 30.0,
    ):
        self._audio_handler = audio_handler
        self._event_bus = event_bus
        self._led_consumer = led_consumer
        self._pub_endpoint = pub_endpoint
        self._pull_endpoint = pull_endpoint
        self._meta_interval = meta_interval

        # ZMQ (created in start())
        self._ctx: Optional[zmq.Context] = None
        self._pub: Optional[zmq.Socket] = None
        self._pull: Optional[zmq.Socket] = None

        # Audio reader
        self._reader: Optional[AudioBusReader] = None

        # Threading
        self._running = False
        self._send_lock = threading.Lock()
        self._audio_thread: Optional[threading.Thread] = None
        self._meta_thread: Optional[threading.Thread] = None
        self._cmd_thread: Optional[threading.Thread] = None

        # Sequence counter
        self._seq = 0

        logger.info(
            f"AudioBroadcaster initialized: pub={pub_endpoint}, "
            f"pull={pull_endpoint}, meta_interval={meta_interval}s"
        )

    def start(self) -> None:
        """Bind sockets, subscribe to events, start background threads."""
        if self._running:
            logger.warning("AudioBroadcaster already running")
            return

        self._ctx = zmq.Context()

        # PUB socket for outgoing audio + events
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, 1000)
        self._pub.setsockopt(zmq.LINGER, 0)
        self._pub.bind(self._pub_endpoint)
        logger.info(f"PUB socket bound to {self._pub_endpoint}")

        # PULL socket for incoming commands
        self._pull = self._ctx.socket(zmq.PULL)
        self._pull.setsockopt(zmq.LINGER, 0)
        self._pull.bind(self._pull_endpoint)
        logger.info(f"PULL socket bound to {self._pull_endpoint}")

        # Create audio reader
        self._reader = self._audio_handler.create_reader()

        # Subscribe to EventBus
        self._event_bus.subscribe("hotword_detected", self._on_hotword)
        self._event_bus.subscribe("voice_activity_started", self._on_voice_started)
        self._event_bus.subscribe("voice_activity_stopped", self._on_voice_stopped)

        self._running = True

        # Start threads
        self._audio_thread = threading.Thread(
            target=self._audio_loop, daemon=True, name="zmq-audio"
        )
        self._meta_thread = threading.Thread(target=self._meta_loop, daemon=True, name="zmq-meta")
        self._cmd_thread = threading.Thread(
            target=self._command_loop, daemon=True, name="zmq-commands"
        )
        self._audio_thread.start()
        self._meta_thread.start()
        self._cmd_thread.start()

        # Send initial meta
        self._publish_meta()

        logger.info("AudioBroadcaster started")

    # --- Outgoing: audio frames ---

    def _audio_loop(self) -> None:
        while self._running:
            frame = self._reader.read(timeout=0.2)
            if frame is not None:
                self._seq += 1
                header = json.dumps(
                    {
                        "seq": self._seq,
                        "ts": datetime.now().isoformat(),
                        "size": len(frame),
                    }
                ).encode()
                with self._send_lock:
                    try:
                        self._pub.send_multipart([b"audio", header, frame], zmq.NOBLOCK)
                    except zmq.Again:
                        pass

    # --- Outgoing: events ---

    def _publish_event(self, event_type: str, event) -> None:
        payload = {"type": event_type, "ts": event.timestamp.isoformat()}

        if isinstance(event, HotwordEvent):
            payload["hotword"] = event.hotword
            payload["score"] = event.score
        elif isinstance(event, VoiceActivityEvent):
            payload["activity_type"] = event.activity_type
            payload["duration"] = event.duration

        data = json.dumps(payload).encode()
        with self._send_lock:
            try:
                self._pub.send_multipart([b"event", data], zmq.NOBLOCK)
            except zmq.Again:
                pass

    def _on_hotword(self, event: HotwordEvent) -> None:
        self._publish_event("hotword_detected", event)

    def _on_voice_started(self, event: VoiceActivityEvent) -> None:
        self._publish_event("voice_activity_started", event)

    def _on_voice_stopped(self, event: VoiceActivityEvent) -> None:
        self._publish_event("voice_activity_stopped", event)

    # --- Outgoing: meta ---

    def _publish_meta(self) -> None:
        meta = json.dumps(
            {
                "sample_rate": self._audio_handler.sample_rate,
                "channels": self._audio_handler.channels,
                "format": "pcm16",
                "chunk_size": self._audio_handler.chunk_size,
                "chunk_ms": int(
                    self._audio_handler.chunk_size / self._audio_handler.sample_rate * 1000
                ),
                "ts": datetime.now().isoformat(),
            }
        ).encode()
        with self._send_lock:
            try:
                self._pub.send_multipart([b"meta", meta], zmq.NOBLOCK)
            except zmq.Again:
                pass

    def _meta_loop(self) -> None:
        while self._running:
            time.sleep(self._meta_interval)
            if self._running:
                self._publish_meta()

    # --- Incoming: commands ---

    def _command_loop(self) -> None:
        """Read commands from PULL socket and route them."""
        poller = zmq.Poller()
        poller.register(self._pull, zmq.POLLIN)

        while self._running:
            try:
                socks = dict(poller.poll(timeout=200))
                if self._pull in socks:
                    raw = self._pull.recv(zmq.NOBLOCK)
                    msg = json.loads(raw)
                    self._handle_command(msg)
            except zmq.Again:
                pass
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid command JSON: {e}")
            except Exception as e:
                logger.error(f"Error in command loop: {e}", exc_info=True)

    def _handle_command(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "led" and self._led_consumer:
            pattern = msg.get("pattern", "off")
            kwargs = {k: v for k, v in msg.items() if k not in ("type", "pattern")}
            self._led_consumer.set_pattern(pattern, **kwargs)
            logger.debug(f"LED command: {pattern}")
        else:
            logger.debug(f"Unknown command type: {msg_type}")

    # --- Lifecycle ---

    def cleanup(self) -> None:
        self._running = False

        for thread in (self._audio_thread, self._meta_thread, self._cmd_thread):
            if thread is not None:
                thread.join(timeout=2.0)

        self._audio_thread = None
        self._meta_thread = None
        self._cmd_thread = None

        self._event_bus.unsubscribe("hotword_detected", self._on_hotword)
        self._event_bus.unsubscribe("voice_activity_started", self._on_voice_started)
        self._event_bus.unsubscribe("voice_activity_stopped", self._on_voice_stopped)

        if self._pub:
            self._pub.close()
            self._pub = None
        if self._pull:
            self._pull.close()
            self._pull = None
        if self._ctx:
            self._ctx.term()
            self._ctx = None

        logger.info("AudioBroadcaster cleaned up")
