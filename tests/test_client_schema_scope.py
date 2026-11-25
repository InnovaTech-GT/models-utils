"""Security audit #6 (schema side): a client update can never move the row to
another company. The field is gone; an old caller that still sends it is
ignored (Pydantic extra="ignore"), not rejected."""
import uuid

from database_utils.schemas.client import ClientUpdate


def test_client_update_drops_company_id():
    m = ClientUpdate.model_validate({"name": "x", "company_id": str(uuid.uuid4())})
    assert "company_id" not in ClientUpdate.model_fields
    assert m.model_dump(exclude_unset=True) == {"name": "x"}
