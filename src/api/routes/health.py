"""
Health check endpoints for the AI Agronomist platform.
"""

import logging
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db

# Module logger
logger = logging.getLogger("api.routes.health")

router = APIRouter(tags=["Health Checks"])


@router.get("/health", response_model=dict, status_code=status.HTTP_200_OK)
async def get_health() -> dict:
    """
    Perform a basic health check on the API service to verify it is responsive.

    Returns:
        dict: A JSON response containing the status of the service representation.
    """
    logger.info("Basic health check endpoint triggered.")
    return {"status": "ok"}


@router.get("/health/db", response_model=dict)
async def get_db_health(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Check the database connection health by executing a low-overhead query
    against the sensor readings table as specified in the service contracts.

    Args:
        db (AsyncSession): The database session dependency.

    Returns:
        dict: A JSON response describing the connection status.
    """
    logger.info("Database health check endpoint triggered.")
    try:
        # Perform query on the database to check connectivity
        query = text("SELECT 1 FROM sensor_reading LIMIT 1")
        logger.info("Executing health check query against the database...")
        await db.execute(query)
        
        logger.info("Database connectivity check succeeded.")
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        logger.error(
            f"Database connectivity check failed: {e}",
            exc_info=True
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "database": "disconnected"
            }
        )
