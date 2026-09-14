from pydantic import ConfigDict
from typing import Optional
from enum import Enum
from uuid import UUID

from .user import UserOut

class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class NotificationOut(UserOut):
    id: UUID
    status: NotificationStatus
    user_id: Optional[UUID]

    model_config = ConfigDict(from_attributes=True)

