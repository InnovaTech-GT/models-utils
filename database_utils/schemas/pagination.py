"""
Pagination schemas for consistent paginated responses across the application.
"""
from typing import Generic, TypeVar, List
from pydantic import BaseModel, Field

# Generic type for the data model
T = TypeVar('T')


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response model."""
    items: List[T] = Field(description="List of items for the current page")
    total: int = Field(description="Total number of items across all pages")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Number of items per page")
    total_pages: int = Field(description="Total number of pages")

    model_config = {
        "from_attributes": True
    }
