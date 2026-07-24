"""
Main FastAPI application entry point for the AI Agronomist platform.

This module instantiates the FastAPI app, configures CORS middleware for localhost
Streamlit access, registers global error handlers, setups dependency injection for db sessions,
configures lifecycle startup/shutdown hooks (database table creation and engine disposal), and
wires up custom Request ID middleware for end-to-end logging traceability.
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response as FastAPIResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import asyncio as aioredis

from src.api.cache_keys import request_scoped_key_builder

# Try to load formatted logging configurations from our devops setup
try:
    from src.logging.config import setup_logging
    setup_logging()
except (ImportError, ValueError):
    logging.basicConfig(level=logging.INFO)

from src.config import REDIS_URL
from src.db.jobs import fail_stale_preparing_jobs
from src.db.session import AsyncSessionLocal, engine, get_db, init_db
from src.mq.connection import close_mq
from src.api.routes import (
    anomalies,
    auth,
    bootstrap,
    feedback,
    feedback_integrated,
    gas_anomalies,
    health,
    incidents,
    inference,
    inference_integrated,
    jobs,
    learning,
    live,
    models,
    monitoring,
    network_anomalies,
    operational_config,
    readings,
    seasonal_baselines,
    upload,
)

# Initialize module logger
logger = logging.getLogger("api.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Asynchronous lifespan manager handling startup database initialization
    and shutdown resource cleanup.
    """
    logger.info("Starting up FastAPI application...")

    # Pin the CPU torch thread pool once at startup (the sync upload path and
    # any in-process inference run here).
    try:
        from src.models.torch_runtime import configure_torch_threads

        configure_torch_threads()
    except Exception as exc:  # never block startup on a threading tweak
        logger.warning("Could not configure torch threads: %s", exc)

    try:
        # Alembic migrations run before uvicorn starts, so init_db() is a
        # safety-net create_all only. Cap it at 10 s so a slow/unreachable DB
        # never blocks startup and prevents the API from serving requests.
        await asyncio.wait_for(init_db(), timeout=10.0)
        logger.info("Database schema initialized successfully.")
    except asyncio.TimeoutError:
        logger.warning(
            "Database connection timed out during startup (>10 s). "
            "Schema init skipped — Alembic migrations already applied."
        )
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}", exc_info=True)
        raise e

    try:
        async with AsyncSessionLocal() as db:
            orphaned_count = await fail_stale_preparing_jobs(db)
        if orphaned_count:
            logger.warning(
                "Marked %d orphaned 'preparing' upload job(s) as failed "
                "(background task lost in a previous restart).",
                orphaned_count,
            )
    except Exception as e:
        logger.error(f"Orphaned upload-job recovery failed: {e}", exc_info=True)

    redis = aioredis.from_url(REDIS_URL, encoding="utf8", decode_responses=True)
    # request_scoped_key_builder: default fastapi-cache keys hash Depends(db)
    # session identity → permanent misses (duplicate Redis entries, full recompute).
    FastAPICache.init(
        RedisBackend(redis),
        prefix="fastapi-cache",
        key_builder=request_scoped_key_builder,
    )
    logger.info("Redis cache initialized.")

    # Live runtime — resilient: API boots normally even if Redis / scoring
    # are unavailable.  Same try/except pattern as the RabbitMQ init below.
    app.state.live_runtime = None
    try:
        from src.api.services.live_hub_provider import get_live_hub
        from src.live.runtime import LiveRuntime

        hub = get_live_hub()
        runtime = LiveRuntime.build_from_config(hub)
        await runtime.start()
        app.state.live_runtime = runtime
        logger.info("Live runtime started.")
    except Exception as exc:
        logger.warning(
            "Live runtime failed to start — live monitoring unavailable: %s", exc
        )
        app.state.live_runtime = None

    yield

    logger.info("Shutting down FastAPI application...")

    # Stop live runtime before MQ / DB teardown so the final batcher flush
    # can still reach the database.
    live_rt = getattr(app.state, "live_runtime", None)
    if live_rt is not None:
        try:
            await live_rt.stop()
            logger.info("Live runtime stopped.")
        except Exception as exc:
            logger.error("Failed to stop live runtime: %s", exc, exc_info=True)

    # Close the hub's memoized publisher Redis client.
    try:
        from src.api.services.live_hub_provider import get_live_hub
        hub = get_live_hub()
        await hub.aclose()
    except Exception as exc:
        logger.debug("Hub aclose skipped: %s", exc)

    try:
        await redis.close()
        logger.info("Redis connection closed.")
    except Exception as e:
        logger.error(f"Failed to cleanly close Redis connection: {e}", exc_info=True)
    try:
        # Close the (lazily opened) RabbitMQ connection, if any.
        await close_mq()
    except Exception as e:
        logger.error(f"Failed to cleanly close MQ connection: {e}", exc_info=True)
    try:
        # Dispose of SQLAlchemy pool and close active connections
        await engine.dispose()
        logger.info("Database connection engine successfully disposed.")
    except Exception as e:
        logger.error(f"Failed to cleanly dispose of database resources: {e}", exc_info=True)


# Instantiate the FastAPI app
app = FastAPI(
    title="AI Agronomist API",
    description="REST API for multi-tenant greenhouse telemetry data ingestion, inference, and threshold management.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configurations allowing Streamlit dashboard and local React/Vue dev tools
origins = [
    "http://localhost",
    "http://localhost:3000",   # Default React developer port
    "http://localhost:8501",   # Default Streamlit dashboard port
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8501",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id_and_log_middleware(request: Request, call_next: Callable) -> Response:
    """
    Middleware that generates and injects an X-Request-ID for all requests,
    logs incoming and outgoing paths with duration tracking, and injects
    request context parameters into the response headers.
    """
    # Extract existing Request ID from client header, or generate a fresh UUID4
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    # Benchmarking request start
    start_time = time.time()
    logger.info(
        f"Request started: {request.method} {request.url.path} | RequestID: {request_id}"
    )

    try:
        response = await call_next(request)
    except Exception as e:
        # Just in case an unhandled exception manages to escape call_next
        process_time_ms = (time.time() - start_time) * 1000.0
        logger.error(
            f"Exception escaped middleware: {request.method} {request.url.path} | "
            f"Duration: {process_time_ms:.2f}ms | RequestID: {request_id} | Error: {e}",
            exc_info=True
        )
        raise e

    # Benchmarking request completion
    duration_ms = (time.time() - start_time) * 1000.0

    # Inject tracking ID and diagnostic header into client response
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"

    logger.info(
        f"Request completed: {request.method} {request.url.path} | "
        f"Status: {response.status_code} | Duration: {duration_ms:.2f}ms | RequestID: {request_id}"
    )

    return response


# =============================================================================
# Global Exception Handlers
# =============================================================================

from fastapi.encoders import jsonable_encoder

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Catches 422 Unprocessable Entity validation errors (such as Pydantic structural errors).
    Restructures the output with clear validation feedback and correlates with Request ID.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    errors = jsonable_encoder(exc.errors())
    logger.warning(
        f"Validation failure: {request.method} {request.url.path} | "
        f"RequestID: {request_id} | Validation errors: {errors}"
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": errors,
            "message": "The incoming payload validation failed. Please check the structure of your fields.",
            "request_id": request_id,
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    Catches standard Starlette / FastAPI HTTPException.
    Simplifies the error structure and stamps details with Request ID.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    logger.warning(
        f"HTTP operational error: {request.method} {request.url.path} | "
        f"Status: {exc.status_code} | Reason: {exc.detail} | RequestID: {request_id}"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "message": exc.detail,
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def default_unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all global exception handler returning a sanitized 500 error payload to
    the client while fully logging the internal traceback for backend engineering auditing.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(
        f"Unhandled application exception: {request.method} {request.url.path} | "
        f"RequestID: {request_id} | Error: {str(exc)}",
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "message": "An internal server error occurred. Please contact system administrators.",
            "request_id": request_id,
        },
    )


# =============================================================================
# Routes Registration
# =============================================================================

app.include_router(health.router)
app.include_router(bootstrap.router)
app.include_router(upload.router, prefix="/api")
app.include_router(inference.router, prefix="/api")
app.include_router(inference_integrated.router)
app.include_router(models.router)
app.include_router(incidents.router)
app.include_router(learning.router)
app.include_router(feedback.router)
app.include_router(feedback_integrated.router)
app.include_router(monitoring.router)
app.include_router(gas_anomalies.router)
app.include_router(jobs.router)
app.include_router(live.router)
app.include_router(readings.router)
app.include_router(network_anomalies.router)
app.include_router(anomalies.router)
app.include_router(auth.router)
app.include_router(operational_config.router)
app.include_router(seasonal_baselines.router)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> FastAPIResponse:
    """Prometheus scrape endpoint. No auth required."""
    from src.monitoring.prometheus_metrics import generate_prometheus_output
    body, content_type = generate_prometheus_output()
    return FastAPIResponse(content=body, media_type=content_type)
