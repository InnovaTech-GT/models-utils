"""
Logging middleware for FastAPI applications.

This middleware automatically:
- Generates and tracks request IDs
- Extracts user context from JWT tokens
- Logs incoming requests and responses
- Measures request duration
- Handles errors with full context
"""

import time
import uuid
import logging
from typing import Callable, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from database_utils.utils.logging_utils import (
    set_request_context,
    clear_request_context,
    log_with_context,
    get_app_logger
)
from database_utils.utils.jwt_utils import decode_token
from database_utils.utils.telemetry_utils import set_request_span_attributes


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add comprehensive logging to all FastAPI requests.
    """

    def __init__(
        self,
        app: ASGIApp,
        logger: Optional[logging.Logger] = None,
        log_request_body: bool = False,
        log_response_body: bool = False,
    ):
        """
        Initialize the logging middleware.

        Args:
            app: FastAPI application
            logger: Logger to use (creates default if None)
            log_request_body: Whether to log request bodies (can expose sensitive data)
            log_response_body: Whether to log response bodies (can be verbose)
        """
        super().__init__(app)
        self.logger = logger or get_app_logger('middleware.logging')
        self.log_request_body = log_request_body
        self.log_response_body = log_response_body

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process each request with logging.

        Args:
            request: Incoming request
            call_next: Next middleware or route handler

        Returns:
            Response from the application
        """
        # Generate request ID
        request_id = str(uuid.uuid4())

        # Extract user context from JWT token if present
        user_id = None
        company_id = None
        user_roles = []
        user_permissions = set()

        auth_header = request.headers.get("Authorization")
        if auth_header:
            try:
                # Extract token from "Bearer <token>"
                token = auth_header.split()[1] if " " in auth_header else None
                if token:
                    payload = decode_token(token)
                    user_id = payload.get("id")
                    company_id = payload.get("company_id")
                    user_roles = payload.get("roles", [])

                    # Extract permissions from roles if available
                    # Note: This requires the user object to get permissions
                    # In practice, permissions might need to be included in the JWT
                    # or fetched from the database
            except Exception as e:
                self.logger.debug(f"Failed to decode token: {str(e)}")

        # Set context for this request
        set_request_context(
            request_id=request_id,
            user_id=user_id,
            company_id=company_id,
            user_roles=user_roles,
            user_permissions=user_permissions
        )

        # Promote identity context to the active OTEL span for Honeycomb filtering.
        # OTEL ASGI middleware runs outermost, so the HTTP span is already active here.
        set_request_span_attributes()

        # Get client information
        client_host = request.client.host if request.client else "unknown"
        client_port = request.client.port if request.client else 0

        # Log incoming request
        log_data = {
            'event': 'request_started',
            'method': request.method,
            'path': request.url.path,
            'query_params': dict(request.query_params),
            'client_ip': client_host,
            'client_port': client_port,
            'user_agent': request.headers.get('user-agent'),
            'referer': request.headers.get('referer'),
        }

        # Log request body if enabled (be careful with sensitive data!)
        if self.log_request_body and request.method in ['POST', 'PUT', 'PATCH']:
            try:
                # We need to read the body carefully to avoid consuming it
                body = await request.body()
                # Store it back so the endpoint can read it
                request._body = body
                # Convert to string if it's JSON-like
                try:
                    import json
                    log_data['request_body'] = json.loads(body.decode('utf-8'))
                except:
                    log_data['request_body'] = '<binary or non-JSON data>'
            except Exception as e:
                log_data['request_body_error'] = str(e)

        log_with_context(self.logger, 'info', "Incoming request", **log_data)

        # Process request and measure time
        start_time = time.time()
        response = None

        try:
            response = await call_next(request)
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000

            # Log error with full context
            log_with_context(
                self.logger,
                'error',
                f"Request failed: {str(e)}",
                event='request_failed',
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
                error_type=type(e).__name__,
                error_message=str(e),
            )

            # Re-raise to let error handlers deal with it
            raise
        finally:
            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            if response:
                # Log response
                response_log_data = {
                    'event': 'request_completed',
                    'method': request.method,
                    'path': request.url.path,
                    'status_code': response.status_code,
                    'duration_ms': round(duration_ms, 2),
                }

                # Determine log level based on status code and duration
                if response.status_code >= 500:
                    log_level = 'error'
                elif response.status_code >= 400:
                    log_level = 'warning'
                elif duration_ms > 1000:  # Slow request
                    log_level = 'warning'
                else:
                    log_level = 'info'

                log_with_context(
                    self.logger,
                    log_level,
                    f"Request completed: {request.method} {request.url.path} - {response.status_code} ({duration_ms:.2f}ms)",
                    **response_log_data
                )

            # Clear context after request is done
            clear_request_context()

        # Add request ID to response headers for tracing
        if response:
            response.headers["X-Request-ID"] = request_id

        return response
