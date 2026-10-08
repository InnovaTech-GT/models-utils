"""ACS bootstrap values for playbooks: the `acs.*` namespace (doc 42 §9.7).

An OLT step can push the CPE's TR-069 management config over OMCI (doc 42c,
doc 42b `Set ONU ACS`). The four values it needs already exist, and no
playbook author may type them:

    acs.url              provisioning_settings.acs_base_url, else env
                         GENIEACS_CWMP_PUBLIC_URL (what Settings -> Red shows)
    acs.inform_password  decrypt of provisioning_settings.cwmp_credential_id
    acs.cr_username      the CPE's acs_device_registration CR credentials,
    acs.cr_password      minted when absent (the same minting as backend-erp's
                         /acs/internal/bootstrap, shared here)

`create_run` checks them (and ensures the CPE's registration) before any child
exists; backend-erp's worker loads them again at claim and merges them into
the job's variables IN MEMORY only, never into job.variables, frames or logs.

ponytail: only the CPE's own registration is ensured, and only when a
playbook reads `acs.*`; bulk pre-registration stays POST /acs/registrations/bulk.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from database_utils.models.isp import AcsDeviceRegistration, DeviceCredential
from database_utils.schemas.acs_registration import _normalize_serial
from database_utils.utils import crypto
from database_utils.utils.provisioning_resolution import ResolutionError
from database_utils.utils.transport import company_provisioning_settings

ENV_CWMP_PUBLIC_URL = "GENIEACS_CWMP_PUBLIC_URL"
ACS_KEYS = ("url", "inform_password", "cr_username", "cr_password")
ACS_TOKEN = re.compile(r"\{\{\s*acs\.")
# The capture charset (doc 42 §4.3): a space or a quote would split the
# OLT command line.
ACS_VALUE_SAFE = re.compile(r"[A-Za-z0-9_.:/+-]{1,128}")

ACS_NOT_CONFIGURED = "ACS_NOT_CONFIGURED"
ACS_VALUE_UNSAFE = "ACS_VALUE_UNSAFE"
ACS_SERIAL_CLAIMED = "ACS_SERIAL_CLAIMED"


def reads_acs(definitions: Iterable[Any]) -> bool:
    """True when any of these (definition / step list) dicts holds an {{acs.*}} token."""
    return any(ACS_TOKEN.search(json.dumps(d, default=str)) for d in definitions)


def acs_url(settings) -> str | None:
    """The CWMP URL a CPE must inform: the tenant row's acs_base_url when set,
    else the platform env (Backend and ProvisionWorker both carry it)."""
    url = (getattr(settings, "acs_base_url", None) or os.getenv(ENV_CWMP_PUBLIC_URL) or "").strip()
    return url or None


def mint_cr_credentials(registration: AcsDeviceRegistration) -> str:
    """Issue per-device CWMP connection-request credentials on a registration
    that has none (canon C13/C16: never blank/blank) and return the plaintext
    password. The row must already have its id and company_id (they are the
    AAD). Raises crypto.CredentialCryptoError with the row untouched. Never
    call it on a row whose ciphertext exists but is unreadable: unreadable is
    not gone, re-minting would desync the device."""
    password = _secrets.token_urlsafe(20)
    ciphertext, dek_wrapped, kek_id = crypto.encrypt_secret(
        password, registration.company_id, registration.id)
    registration.cwmp_cr_username = f"cr-{registration.serial_number.lower()}"
    registration.cwmp_cr_secret_ciphertext = ciphertext
    registration.cwmp_cr_dek_wrapped = dek_wrapped
    registration.cwmp_cr_kek_id = kek_id
    return password


def cr_password(registration: AcsDeviceRegistration) -> str | None:
    """The registration's CR password, or None when none was minted yet.
    Raises crypto.CredentialCryptoError when it exists but cannot be read."""
    if not registration.cwmp_cr_secret_ciphertext:
        return None
    return crypto.decrypt_secret(
        registration.cwmp_cr_secret_ciphertext, registration.cwmp_cr_dek_wrapped,
        registration.cwmp_cr_kek_id, registration.company_id, registration.id)


def _refuse(code: str, detail: str):
    raise ResolutionError(code, detail)


def _inform_password(db: Session, company_id, settings) -> str | None:
    cred_id = getattr(settings, "cwmp_credential_id", None)
    cred = db.get(DeviceCredential, cred_id) if cred_id else None
    if cred is None or cred.company_id != company_id:
        return None
    try:
        return crypto.decrypt_secret(cred.secret_ciphertext, cred.dek_wrapped,
                                     cred.kek_id, company_id, cred.id)
    except crypto.CredentialCryptoError:
        return None


def _registration(db: Session, company_id, serial: str, *, item_id=None,
                  ensure: bool = False) -> AcsDeviceRegistration | None:
    """This tenant's registration of the serial (any OUI), created when
    `ensure`. A row owned by another tenant, or quarantined (company NULL,
    awaiting superadmin assignment), refuses: the same rule as
    POST /acs/registrations' 409."""
    rows = (db.query(AcsDeviceRegistration)
            .filter(AcsDeviceRegistration.serial_number == serial)
            .order_by(AcsDeviceRegistration.created_at, AcsDeviceRegistration.id)
            .all())
    if any(r.company_id != company_id for r in rows):
        _refuse(ACS_SERIAL_CLAIMED,
                f"The CPE serial {serial} is registered in the ACS outside this company "
                "(another tenant or quarantine)")
    if rows:
        reg = rows[0]
    elif ensure:
        reg = AcsDeviceRegistration(id=uuid.uuid4(), company_id=company_id, serial_number=serial)
        db.add(reg)
    else:
        return None
    if ensure and reg.inventory_item_id is None and item_id is not None:
        reg.inventory_item_id = item_id
    return reg


def acs_values(db: Session, company_id, serial: str | None, *, item_id=None,
               ensure: bool = False) -> dict[str, str]:
    """{url, inform_password, cr_username, cr_password} for the CPE `serial`
    of `company_id`. Callers must never log or persist the result.

    ensure=True (create_run, non-dry) creates the tenant's registration of the
    serial and mints its CR credentials when absent, and stamps the inventory
    item. ensure=False (preflight, dry run, the worker) writes nothing; a
    not-yet-minted CR pair is then omitted from the result (create_run will
    mint it; the worker treats it as ACS_NOT_CONFIGURED).

    Raises ResolutionError ACS_NOT_CONFIGURED (no URL, no / unreadable inform
    credential, no CPE serial, unreadable CR secret), ACS_SERIAL_CLAIMED, or
    ACS_VALUE_UNSAFE (a value outside [A-Za-z0-9_.:/+-]{1,128})."""
    settings = company_provisioning_settings(db, company_id)
    url = acs_url(settings)
    if url is None:
        _refuse(ACS_NOT_CONFIGURED, "No ACS URL: set GENIEACS_CWMP_PUBLIC_URL (or the tenant's ACS URL)")
    password = _inform_password(db, company_id, settings)
    if password is None:
        _refuse(ACS_NOT_CONFIGURED,
                "No readable TR-069 inform password: generate it in Configuración → Red")
    values = {"url": url, "inform_password": password}
    serial = _normalize_serial(serial or "")
    if not serial:
        _refuse(ACS_NOT_CONFIGURED, "The service's CPE has no serial number to register in the ACS")
    reg = _registration(db, company_id, serial, item_id=item_id, ensure=ensure)
    if reg is not None:
        try:
            cr = cr_password(reg)
        except crypto.CredentialCryptoError:
            _refuse(ACS_NOT_CONFIGURED, "The CPE's connection-request secret cannot be decrypted")
        if cr is None and ensure:
            if not ACS_VALUE_SAFE.fullmatch(f"cr-{serial.lower()}"):
                _refuse(ACS_VALUE_UNSAFE, "acs.cr_username has characters an OLT command cannot carry")
            try:
                cr = mint_cr_credentials(reg)
            except crypto.CredentialCryptoError:
                _refuse(ACS_NOT_CONFIGURED, "No encryption key to mint the CPE's connection-request secret")
        if cr is not None:
            values |= {"cr_username": reg.cwmp_cr_username, "cr_password": cr}
    for key, value in values.items():
        if not ACS_VALUE_SAFE.fullmatch(value or ""):
            _refuse(ACS_VALUE_UNSAFE, f"acs.{key} has characters an OLT command cannot carry")
    return values
