# schemas/company.py
from pydantic import BaseModel, EmailStr, field_validator, model_validator, ConfigDict, Field
from typing import Literal, Optional
from datetime import datetime
from uuid import UUID
from database_utils.utils.timezone_utils import now_gt, make_aware_gt


class BankAccount(BaseModel):
    """mi2: the account collectors deposit cash into (cobros "Efectivo en
    mano" bank card)."""
    model_config = ConfigDict(extra="forbid")

    holder: str = Field(..., min_length=1, max_length=120)
    bank: str = Field(..., min_length=1, max_length=80)
    account: str = Field(..., min_length=1, max_length=40)
    # Display strings, shown as typed ("Monetaria", "GTQ").
    type: str = Field(..., min_length=1, max_length=40)
    currency: str = Field("GTQ", min_length=1, max_length=8)


# Mon=0 .. Sun=6 (datetime.weekday()); the default when working_days is absent.
DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4, 5]


class MobileSettings(BaseModel):
    """mi2: company.mobile_settings (JSON). Strict — unknown keys are
    rejected so a typo cannot silently persist."""
    model_config = ConfigDict(extra="forbid")

    bank_account: Optional[BankAccount] = None
    collector_daily_goal: Optional[int] = Field(None, ge=0)
    technician_daily_goal: Optional[int] = Field(None, ge=0)
    # cp1: days field staff work (Mon=0). None = DEFAULT_WORKING_DAYS.
    working_days: Optional[list[int]] = None

    @field_validator("working_days")
    @classmethod
    def _check_working_days(cls, v):
        if v is None:
            return v
        if not v or len(set(v)) != len(v) or any(d < 0 or d > 6 for d in v):
            raise ValueError("working_days must be a non-empty list of unique ints 0..6")
        return sorted(v)


class PortalContact(BaseModel):
    """cp1: contact block the captive portal shows."""
    model_config = ConfigDict(extra="forbid")

    whatsapp: Optional[str] = Field(None, max_length=40)
    phone: Optional[str] = Field(None, max_length=40)
    hours: Optional[str] = Field(None, max_length=120)
    message: Optional[str] = Field(None, max_length=500)


class DunningSettings(BaseModel):
    """cp1 (doc 52 §2.1): company.dunning_settings (JSON). Strict like
    MobileSettings. NULL column = everything off."""
    model_config = ConfigDict(extra="forbid")

    auto_suspension_mode: Literal["OFF", "REPORT", "ON"] = "OFF"
    min_overdue_orders: int = Field(2, ge=1)
    captive_portal_enabled: bool = False
    portal_contact: PortalContact = Field(default_factory=PortalContact)
    # Calendar days from the suspension date (D7).
    promise_window_days: int = Field(3, ge=0)
    # 0 = promises off.
    promise_capacity_pct: int = Field(20, ge=0, le=100)
    promise_reconnect_enabled: bool = False

    @model_validator(mode="after")
    def _reconnect_needs_cron(self):
        # Without ON nobody re-suspends after a broken promise (doc 52 §4.4).
        if self.promise_reconnect_enabled and self.auto_suspension_mode != "ON":
            raise ValueError("promise_reconnect_enabled requires auto_suspension_mode 'ON'")
        return self


def merge_settings(stored: Optional[dict], patch: Optional[BaseModel], model: type[BaseModel]) -> dict:
    """Deep-merge a partial settings patch over the stored JSON and re-validate.

    The JSON columns (`mobile_settings`, `dunning_settings`) are written by
    several pages that each send a subset of keys; replacing the column would
    reset the others to their defaults (e.g. auto_suspension_mode -> OFF).
    Only keys the client set (`exclude_unset`) are applied, so model-level
    rules (reconnect needs mode ON) are checked on the merged result.
    """
    def deep(base: dict, over: dict) -> dict:
        out = dict(base)
        for k, v in over.items():
            out[k] = deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    over = patch.model_dump(exclude_unset=True) if patch is not None else {}
    return model.model_validate(deep(stored or {}, over)).model_dump(mode="json")


class CompanyBase(BaseModel):
    name: str
    email: Optional[EmailStr]
    phone: Optional[str] = None
    active: Optional[bool] = True
    start_date: Optional[datetime] = None
    tier_id: Optional[UUID]
    tax_id: Optional[str]
    address: Optional[str] = ''

    @field_validator('start_date', mode='before')
    @classmethod
    def set_default_start_date(cls, v):
        """Set default start_date to current Guatemala time if not provided."""
        if v is None:
            return now_gt()
        # If provided, ensure it's timezone-aware
        if isinstance(v, datetime):
            return make_aware_gt(v)
        return v


class CompanyCreate(CompanyBase):
    pass


class CompanyUpdate(BaseModel):
    """Used for PATCH - all fields optional"""
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    active: Optional[bool] = None
    start_date: Optional[datetime] = None
    tier_id: Optional[UUID] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    mobile_settings: Optional[MobileSettings] = None
    dunning_settings: Optional[DunningSettings] = None


class CompanyOut(CompanyBase):
    id: UUID
    mobile_settings: Optional[MobileSettings] = None
    dunning_settings: Optional[DunningSettings] = None

    model_config = ConfigDict(from_attributes=True)
