# utils/error_handling.py

from fastapi import HTTPException, status
from typing import Callable, TypeVar
from functools import wraps
from loguru import logger

T = TypeVar('T')

def handle_exceptions(func: Callable[..., T]) -> Callable[..., T]:
    """
    Decorator to handle exceptions in a standardized way
    """
    @wraps(func)
    async def wrapper(*args, **kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            # Re-raise FastAPI HTTP exceptions as they're already formatted correctly
            raise
        except Exception as e:
            logger.error(f"{args = } :: {kwargs = }")
            logger.error(f"Unexpected error in {func.__name__}: {str(e)}")
            # Convert generic exceptions to HTTPException
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Internal server error: {str(e)}"
            )
    return wrapper