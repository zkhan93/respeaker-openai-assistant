#!/usr/bin/env python3
"""ZMQ OpenAI Realtime consumer -- full voice conversation via ZMQ.

Subscribes to the core service's audio+events, talks to OpenAI Realtime API,
plays response audio locally, and sends LED commands back to the core.

Usage:
    python examples/zmq_realtime.py \
        --endpoint tcp://localhost:5555 \
        --push tcp://localhost:5556 \
        --api-key sk-...

Requires: pyzmq, websockets, pyaudio (pip install pyzmq websockets pyaudio)
"""

import argparse
import asyncio
import json
import logging
import threading
import time

import zmq

logger = logging.getLogger(__name__)


class ZmqRealtimeConsumer:
    """Subscribes to core audio/events, manages OpenAI Realtime conversations."""

    def __init__(self, sub_endpoint: str, push_endpoint: str, api_key: str):
        self.sub_endpoint = sub_endpoint
        self.push_endpoint = push_endpoint
        self.api_key = api_key

        self.ctx = zmq.Context()

        # SUB for audio + events from core
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.connect(sub_endpoint)
        self.sub.subscribe(b"audio")
        self.sub.subscribe(b"event")
        self.sub.subscribe(b"meta")

        # PUSH for LED commands back to core
        self.push = self.ctx.socket(zmq.PUSH)
        self.push.connect(push_endpoint)

        # State
        self.in_conversation = False
        self.collecting_audio = False
        self.collected_audio = bytearray()
        self.sample_rate = 16000
        self.running = True

    def send_led(self, pattern: str, **kwargs):
        """Send LED command to core."""
        msg = {"type": "led", "pattern": pattern, **kwargs}
        try:
            self.push.send_json(msg, zmq.NOBLOCK)
        except zmq.Again:
            pass

    def run(self):
        """Main loop — process ZMQ messages."""
        print(f"Connected to {self.sub_endpoint}")
        print(f"LED commands -> {self.push_endpoint}")
        print("Waiting for hotword...")
        print()

        poller = zmq.Poller()
        poller.register(self.sub, zmq.POLLIN)

        try:
            while self.running:
                socks = dict(poller.poll(timeout=200))
                if self.sub not in socks:
                    continue

                parts = self.sub.recv_multipart()
                topic = parts[0]

                if topic == b"meta":
                    meta = json.loads(parts[1])
                    self.sample_rate = meta["sample_rate"]
                    print(f"Stream: {meta['sample_rate']}Hz, {meta['format']}")

                elif topic == b"event":
                    event = json.loads(parts[1])
                    self._handle_event(event)

                elif topic == b"audio" and self.collecting_audio:
                    self.collected_audio.extend(parts[2])

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.send_led("off")
            self.sub.close()
            self.push.close()
            self.ctx.term()

    def _handle_event(self, event: dict):
        event_type = event["type"]

        if event_type == "hotword_detected":
            hotword = event.get("hotword", "?")
            score = event.get("score", 0)
            print(f"\nHotword '{hotword}' detected (score={score:.3f})")

            self.in_conversation = True
            self.collecting_audio = True
            self.collected_audio.clear()
            self.send_led("think")

        elif event_type == "voice_activity_stopped" and self.in_conversation:
            self.collecting_audio = False
            audio_size = len(self.collected_audio)
            print(f"Voice stopped. Collected {audio_size} bytes.")

            if audio_size > 0:
                # TODO: Send to OpenAI Realtime API, play response, then:
                #   self.send_led("speak")
                #   ... play audio ...
                #   self.send_led("off")
                print("(OpenAI Realtime integration goes here)")
                print("Sending LED off...")
                self.send_led("off")

            self.in_conversation = False


def main():
    parser = argparse.ArgumentParser(description="ZMQ OpenAI Realtime consumer")
    parser.add_argument(
        "--endpoint", default="tcp://localhost:5555", help="ZMQ PUB endpoint to subscribe to"
    )
    parser.add_argument(
        "--push", default="tcp://localhost:5556", help="ZMQ PULL endpoint for LED commands"
    )
    parser.add_argument("--api-key", default="", help="OpenAI API key")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    consumer = ZmqRealtimeConsumer(
        sub_endpoint=args.endpoint,
        push_endpoint=args.push,
        api_key=args.api_key,
    )
    consumer.run()


if __name__ == "__main__":
    main()
