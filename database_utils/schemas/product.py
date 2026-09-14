from pydantic import BaseModel, ConfigDict
from uuid import UUID


class ProductBase(BaseModel):
    name: str
    price: float
    description: str
    stock: int


class ProductOut(ProductBase):
    id: UUID
    company_id: UUID

    model_config = ConfigDict(from_attributes=True)
