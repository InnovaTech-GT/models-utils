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


def test_queries_the_given_serial_and_its_upper_case_under_the_tenant_tag():
    client = _Client([{"_id": "dev-1"}])
    assert find_device_id(client, "co", " gpon00c7af66") == "dev-1"
    assert client.query["$and"][0] == {"_tags": tenant_tag("co")}
    assert _serials(client.query) == {" gpon00c7af66", "GPON00C7AF66"}


def test_an_upper_case_serial_is_queried_once_and_a_miss_is_none():
    client = _Client([])
    assert find_device_id(client, "co", "GPON1") is None
    assert len(client.query["$and"][1]["$or"]) == 3  # one per serial path
