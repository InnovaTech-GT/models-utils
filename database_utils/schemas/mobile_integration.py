"""uplink-mobile integration (rs1/tc1/uf1): minimal schemas for the new
models. Kept deliberately thin — request/response shaping for the
collection-routes/task-closeout/files endpoints is backend-erp's job in the
next phase, not this repo's."""
from pydantic import BaseModel, ConfigDict
from typing import Optional, Any
from uuid import UUID
from datetime import datetime, date

from database_utils.models.crm import (
    CollectionRouteStatus,
    RouteStopStatus,
    CollectionVisitOutcome,
    CollectionVisitCode,
    CashSessionStatus,
    UploadedFileOwnerType,
    UploadedFileKind,
)


class UploadedFileOut(BaseModel):
    id: UUID
    company_id: UUID
    owner_type: UploadedFileOwnerType
    owner_id: UUID
    kind: UploadedFileKind
    storage_key: str
    content_type: str
    size_bytes: int
    uploaded_by: Optional[UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CollectionRouteOut(BaseModel):
    id: UUID
    company_id: UUID
    collector_id: UUID
    route_date: date
    status: CollectionRouteStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RouteStopOut(BaseModel):
    id: UUID
    route_id: UUID
    client_id: UUID
    sequence: int
    status: RouteStopStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CollectionVisitCreate(BaseModel):
    outcome: CollectionVisitOutcome
    visit_code: Optional[CollectionVisitCode] = None
    promise_date: Optional[date] = None
    note: Optional[str] = None
    signature_file_id: Optional[UUID] = None
    idempotency_key: Optional[str] = None  # forwarded to PaymentService for PAID/PARTIAL


class CollectionVisitOut(BaseModel):
    id: UUID
    route_stop_id: UUID
    outcome: CollectionVisitOutcome
    visit_code: Optional[CollectionVisitCode] = None
    promise_date: Optional[date] = None
    note: Optional[str] = None
    payment_id: Optional[UUID] = None
    signature_file_id: Optional[UUID] = None
    created_by: Optional[UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CashSessionOut(BaseModel):
    id: UUID
    company_id: UUID
    collector_id: UUID
    route_id: Optional[UUID] = None
    opened_at: datetime
    closed_at: Optional[datetime] = None
    counted_cash_cents: Optional[int] = None
    deposit_slip_photo_id: Optional[UUID] = None
    status: CashSessionStatus

    model_config = ConfigDict(from_attributes=True)


class CashSessionClose(BaseModel):
    counted_cash_cents: int
    deposit_slip_photo_id: Optional[UUID] = None


class TaskCloseoutCreate(BaseModel):
    checklist_state: Optional[dict[str, bool]] = None
    serial_number: Optional[str] = None
    device_match: Optional[Any] = None
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None
    gps_accuracy_m: Optional[float] = None
    gps_captured_at: Optional[datetime] = None
    signature_file_id: Optional[UUID] = None
    signer_name: Optional[str] = None
    signer_id_number: Optional[str] = None


class TaskCloseoutOut(BaseModel):
    id: UUID
    task_id: UUID
    technician_id: UUID
    checklist_state: Optional[dict[str, bool]] = None
    serial_number: Optional[str] = None
    device_match: Optional[Any] = None
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None
    gps_accuracy_m: Optional[float] = None
    gps_captured_at: Optional[datetime] = None
    signature_file_id: Optional[UUID] = None
    signer_name: Optional[str] = None
    signer_id_number: Optional[str] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
