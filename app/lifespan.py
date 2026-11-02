"""Optional co-hosted worker lifecycle and health supervision."""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.worker import create_worker, run_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    thread = None
    if get_settings().embedded_worker_enabled:
        settings, sqs = create_worker()

        def worker() -> None:
            try:
                run_loop(stop, settings, sqs)
            except Exception:
                logger.error("embedded worker stopped unexpectedly")

        thread = threading.Thread(target=worker, name="ipgo-worker", daemon=True)
        thread.start()
    app.state.worker_thread = thread
    try:
        yield
    finally:
        stop.set()
        if thread:
            thread.join(timeout=30)
            if thread.is_alive():
                logger.error("embedded worker did not stop before shutdown deadline")
        app.state.worker_thread = None


def register_worker_health(app: FastAPI) -> None:
    @app.middleware("http")
    async def worker_health(request: Request, call_next):
        thread = getattr(request.app.state, "worker_thread", None)
        if request.url.path == "/health" and thread is not None and not thread.is_alive():
            return JSONResponse(status_code=503, content={"status": "worker_unavailable"})
        return await call_next(request)
