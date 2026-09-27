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
            # Argument TYPES and keyword NAMES only, never values: every
            # handler decorated with this receives its request body, and the
            # Capa 3 bodies carry plaintext secrets (DeviceCredentialCreate.
            # secret, RotationStartRequest.secret). Dumping them wrote a
            # tenant's CWMP fleet password into loguru on any 500.
            logger.error(
                f"{func.__name__} args={[type(a).__name__ for a in args]} "
                f"kwargs={sorted(kwargs)}"
            )
            logger.error(f"Unexpected error in {func.__name__}: {str(e)}")
            # Convert generic exceptions to HTTPException
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Internal server error: {str(e)}"
            )
    return wrapper