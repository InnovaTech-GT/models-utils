"""handle_exceptions never sends exception text to the client (security audit
#90 root cause); the cause is still logged server-side."""
import pytest
from fastapi import HTTPException
from loguru import logger

from database_utils.utils.error_handling import handle_exceptions


@handle_exceptions
async def _boom():
    raise RuntimeError("password=hunter2 at db.internal:5432")


async def test_500_detail_hides_exception_text():
    with pytest.raises(HTTPException) as ei:
        await _boom()
    assert ei.value.status_code == 500
    assert ei.value.detail == "Internal server error"
    assert "hunter2" not in str(ei.value.detail)


async def test_exception_text_still_logged():
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
    try:
        with pytest.raises(HTTPException):
            await _boom()
    finally:
        logger.remove(sink)
    assert any("hunter2" in line and "_boom" in line for line in lines)


async def test_http_exception_passes_through():
    @handle_exceptions
    async def missing():
        raise HTTPException(404, "Client not found")

    with pytest.raises(HTTPException) as ei:
        await missing()
    assert (ei.value.status_code, ei.value.detail) == (404, "Client not found")
