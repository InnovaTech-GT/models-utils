"""genieacs_nbi.find_device_id: tenant-scoped and case-tolerant (doc 77 D11)."""
import json

from database_utils.utils.genieacs_nbi import find_device_id, tenant_tag


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Client:
    def __init__(self, body):
        self.body, self.query = body, None

    def get(self, path, params):
        self.query = json.loads(params["query"])
        return _Resp(self.body)


def _serials(query):
    return {next(iter(c.values())) for c in query["$and"][1]["$or"]}


def test_queries_the_stripped_serial_upper_and_lower_under_the_tenant_tag():
    client = _Client([{"_id": "dev-1"}])
    assert find_device_id(client, "co", " GPON00c7af66 ") == "dev-1"
    assert client.query["$and"][0] == {"_tags": tenant_tag("co")}
    assert _serials(client.query) == {"GPON00c7af66", "GPON00C7AF66", "gpon00c7af66"}


def test_an_upper_case_inventory_serial_also_asks_for_the_lower_case_inform():
    client = _Client([])
    assert find_device_id(client, "co", "GPON00C7AF66") is None
    assert _serials(client.query) == {"GPON00C7AF66", "gpon00c7af66"}
    assert len(client.query["$and"][1]["$or"]) == 6  # 2 forms x 3 serial paths
