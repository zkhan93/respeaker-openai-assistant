"""Entrypoint: `python -m alt_alexa_music_mcp` or `alt-alexa-music-mcp`."""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import load_settings
from .logging_setup import configure_logging
from .server import MusicService, build_server

logger = logging.getLogger(__name__)


async def amain() -> None:
    settings = load_settings()
    configure_logging(settings.logging.level)
    logger.info(
        "starting alt-alexa-music-mcp on %s:%d (transport=%s)",
        settings.server.host,
        settings.server.port,
        settings.server.transport,
    )

    service = await MusicService.create(settings)
    mcp = build_server(service)

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows / non-main-thread fallback; SIGINT still raises KeyboardInterrupt.
            pass

    transport = settings.server.transport
    if transport == "http":
        transport = "streamable-http"

    server_task = asyncio.create_task(
        mcp.run_async(
            transport=transport,
            host=settings.server.host,
            port=settings.server.port,
        ),
        name="fastmcp-server",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    try:
        done, _ = await asyncio.wait({server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        # Propagate any server-side exception.
        for task in done:
            if task is server_task and task.exception():
                raise task.exception()  # type: ignore[misc]
    finally:
        if not server_task.done():
            server_task.cancel()
            try:
                await server_task
            except (asyncio.CancelledError, Exception):
                pass
        if not stop_task.done():
            stop_task.cancel()
        await service.shutdown()
        logger.info("alt-alexa-music-mcp stopped")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
