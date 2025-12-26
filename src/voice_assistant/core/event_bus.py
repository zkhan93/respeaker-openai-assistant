"""Event bus for voice assistant - publish/subscribe pattern."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class HotwordEvent:
    """Event emitted when hotword is detected."""

    timestamp: datetime
    hotword: str
    score: float
    audio_queue_size: int  # How many frames are in queue at detection time


@dataclass
class VoiceActivityEvent:
    """Event emitted when voice activity starts or stops."""

    timestamp: datetime
    activity_type: str  # 'started' or 'stopped'
    duration: float = 0.0  # Duration in seconds (only for 'stopped')


class EventBus:
    """Simple event bus for pub-sub communication between components."""

    def __init__(self):
        """Initialize event bus."""
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        logger.info("EventBus initialized")

    def subscribe(self, event_type: str, callback: Callable[[Any], None]):
        """Subscribe to an event type.

        Args:
            event_type: Type of event to subscribe to (e.g., 'hotword_detected')
            callback: Function to call when event is published
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []

            self._subscribers[event_type].append(callback)
            logger.info(
                f"Subscribed to '{event_type}' "
                f"(total subscribers: {len(self._subscribers[event_type])})"
            )

    def unsubscribe(self, event_type: str, callback: Callable[[Any], None]):
        """Unsubscribe from an event type.

        Args:
            event_type: Type of event to unsubscribe from
            callback: Callback function to remove
        """
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                    logger.info(f"Unsubscribed from '{event_type}'")
                except ValueError:
                    pass

    def publish(self, event_type: str, event_data: Any):
        """Publish an event to all subscribers.

        Args:
            event_type: Type of event to publish
            event_data: Event data to pass to subscribers
        """
        with self._lock:
            subscribers = self._subscribers.get(event_type, []).copy()

        if not subscribers:
            logger.debug(f"No subscribers for event '{event_type}'")
            return

        logger.info(f"Publishing '{event_type}' to {len(subscribers)} subscriber(s)")

        # Call subscribers in separate threads to avoid blocking
        for callback in subscribers:
            threading.Thread(
                target=self._safe_callback, args=(callback, event_data, event_type), daemon=True
            ).start()

    def _safe_callback(self, callback: Callable, event_data: Any, event_type: str):
        """Call subscriber callback with error handling.

        Args:
            callback: Subscriber callback function
            event_data: Event data
            event_type: Event type name (for logging)
        """
        try:
            callback(event_data)
        except Exception as e:
            logger.error(f"Error in subscriber callback for '{event_type}': {e}", exc_info=True)

    def get_subscriber_count(self, event_type: str) -> int:
        """Get number of subscribers for an event type.

        Args:
            event_type: Event type to check

        Returns:
            Number of subscribers
        """
        with self._lock:
            return len(self._subscribers.get(event_type, []))
