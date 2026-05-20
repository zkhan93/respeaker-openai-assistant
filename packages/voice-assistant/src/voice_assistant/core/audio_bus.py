"""Shared ring buffer for multi-consumer audio distribution."""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class AudioBus:
    """Shared circular buffer for audio frames.

    One writer publishes frames; N independent readers consume at their own pace.
    Readers that fall behind lose old frames (overwritten) and skip forward.
    """

    def __init__(self, capacity: int = 500):
        self._capacity = capacity
        self._buffer: list[Optional[bytes]] = [None] * capacity
        self._write_pos: int = 0  # Monotonically increasing
        self._condition = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def write_pos(self) -> int:
        return self._write_pos

    def publish(self, frame: bytes) -> None:
        """Write a frame into the ring buffer and wake all waiting readers."""
        with self._condition:
            self._buffer[self._write_pos % self._capacity] = frame
            self._write_pos += 1
            self._condition.notify_all()

    def create_reader(self) -> "AudioBusReader":
        """Create a new independent reader starting from the current write position."""
        with self._condition:
            return AudioBusReader(self, self._write_pos)


class AudioBusReader:
    """Independent read cursor into an AudioBus.

    Each reader tracks its own position and can read, skip ahead,
    or block waiting for new data independently of other readers.
    """

    def __init__(self, bus: AudioBus, read_pos: int):
        self._bus = bus
        self._read_pos = read_pos

    @property
    def position(self) -> int:
        return self._read_pos

    def read(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read the next frame, blocking if caught up.

        If the reader has fallen too far behind (data overwritten),
        it skips forward to the oldest available frame.

        Returns:
            Audio frame bytes, or None on timeout.
        """
        with self._bus._condition:
            # If we've fallen behind the buffer, skip to oldest available
            oldest_available = self._bus._write_pos - self._bus._capacity
            if self._read_pos < oldest_available:
                skipped = oldest_available - self._read_pos
                logger.debug(f"AudioBusReader fell behind, skipping {skipped} frames")
                self._read_pos = oldest_available

            # If caught up, wait for new data
            if self._read_pos >= self._bus._write_pos:
                self._bus._condition.wait(timeout=timeout)
                if self._read_pos >= self._bus._write_pos:
                    return None  # Timed out

            frame = self._bus._buffer[self._read_pos % self._bus._capacity]
            self._read_pos += 1
            return frame

    def skip_to_latest(self) -> None:
        """Jump cursor to the most recent published frame.

        Intended for one-shot use after a long pause (e.g. resuming from
        sleep, attaching a fresh consumer mid-stream). Do NOT call this on
        every iteration of a tight read loop: when the consumer is faster
        than the producer it rewinds the cursor to ``write_pos - 1`` each
        iteration, causing ``read()`` to return the same frame repeatedly
        until a new one is published. For streaming models that depend on
        temporally consecutive frames (e.g. openWakeWord), that breaks
        their internal rolling state. ``read()`` already blocks on the
        bus's condition variable until a new frame arrives and auto-skips
        if the reader falls further than ``capacity`` behind, so plain
        sequential ``read()`` calls are the correct steady-state pattern.
        """
        with self._bus._condition:
            if self._bus._write_pos > 0:
                self._read_pos = self._bus._write_pos - 1
            else:
                self._read_pos = 0

    def available(self) -> int:
        """Number of unread frames available."""
        with self._bus._condition:
            behind = self._bus._write_pos - self._read_pos
            return max(0, min(behind, self._bus._capacity))
