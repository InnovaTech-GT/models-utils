# utils/exception_handlers.py

from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException, RequestValidationError
from sqlalchemy.exc import IntegrityError, OperationalError, DataError
import traceback

from database_utils.utils.logging_utils import (
    log_with_context,
    get_app_logger
)

# Get a logger for exception handlers
exception_logger = get_app_logger('exception_handlers')

async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handle HTTPException with custom format and comprehensive logging.
    """
    # Log the exception with full context
    log_with_context(
        exception_logger,
        'warning' if exc.status_code < 500 else 'error',
        f"HTTP Exception: {exc.status_code} - {exc.detail}",
        status_code=exc.status_code,
        detail=exc.detail,
        path=request.url.path,
        method=request.method,
        client_ip=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.status_code,
                "message": exc.detail,
                "path": request.url.path
            }
        }
    )

async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Handle validation errors with more useful error messages and comprehensive logging.
    """
    errors = []
    for error in exc.errors():
        # Extract location and field from error
        loc = error.get("loc", [])
        field = loc[-1] if loc else "unknown"

        # Extract error message
        msg = error.get("msg", "Unknown validation error")

        # Add to errors list
        errors.append({
            "field": field,
            "message": msg
        })

    # Enhanced logging with context
    log_with_context(
        exception_logger,
        'warning',
        f"Request validation error: {len(errors)} field(s) invalid",
        path=request.url.path,
        method=request.method,
        validation_errors=errors,
        client_ip=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": status.HTTP_422_UNPROCESSABLE_ENTITY,
                "message": "Request validation error",
                "details": errors,
                "path": request.url.path
            }
        }
    )

async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """
    Handle database integrity errors (foreign key violations, unique constraints) with enhanced logging.
    """
    error_msg = str(exc.orig) if hasattr(exc, 'orig') else str(exc)

    # Try to extract more user-friendly error messages
    if 'UNIQUE constraint failed' in error_msg or 'duplicate key value' in error_msg:
        message = "A record with this information already exists"
        error_type = "unique_constraint"
    elif 'FOREIGN KEY constraint failed' in error_msg or 'foreign key constraint' in error_msg.lower():
        message = "Cannot perform this operation because it would violate data relationships"
        error_type = "foreign_key_constraint"
    else:
        message = "Database constraint violation"
        error_type = "integrity_error"

    # Enhanced logging with full context
    log_with_context(
        exception_logger,
        'warning',
        f"Database integrity error: {error_type}",
        path=request.url.path,
        method=request.method,
        error_type=error_type,
        error_message=error_msg,
        user_friendly_message=message,
        client_ip=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "code": status.HTTP_409_CONFLICT,
                "message": message,
                "path": request.url.path
            }
        }
    )

async def operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """
    Handle database operational errors (connection timeouts, deadlocks) with enhanced logging.
    """
    error_msg = str(exc.orig) if hasattr(exc, 'orig') else str(exc)

    # Enhanced logging with full context and stack trace
    log_with_context(
        exception_logger,
        'error',
        f"Database operational error: {error_msg}",
        path=request.url.path,
        method=request.method,
        error_type='operational_error',
        error_message=error_msg,
        client_ip=request.client.host if request.client else None,
        stack_trace=traceback.format_exc(),
    )

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": {
                "code": status.HTTP_503_SERVICE_UNAVAILABLE,
                "message": "Database service temporarily unavailable. Please try again.",
                "path": request.url.path
            }
        }
    )

async def data_error_handler(request: Request, exc: DataError) -> JSONResponse:
    """
    Handle database data errors (type mismatches, invalid values) with enhanced logging.
    """
    error_msg = str(exc.orig) if hasattr(exc, 'orig') else str(exc)

    # Enhanced logging with context
    log_with_context(
        exception_logger,
        'warning',
        f"Database data error: {error_msg}",
        path=request.url.path,
        method=request.method,
        error_type='data_error',
        error_message=error_msg,
        client_ip=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "code": status.HTTP_400_BAD_REQUEST,
                "message": "Invalid data provided for database operation",
                "path": request.url.path
            }
        }
    )

async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle any unhandled exceptions with comprehensive logging including stack traces.
    """
    error_msg = str(exc)
    error_type = type(exc).__name__

    # Enhanced logging with full stack trace and all context
    log_with_context(
        exception_logger,
        'error',
        f"Unhandled exception: {error_type} - {error_msg}",
        path=request.url.path,
        method=request.method,
        error_type=error_type,
        error_message=error_msg,
        client_ip=request.client.host if request.client else None,
        stack_trace=traceback.format_exc(),
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "message": "Internal server error",
                "path": request.url.path
            }
        }
    )
