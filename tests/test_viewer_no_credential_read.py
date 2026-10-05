"""vw1_viewer_no_credential_read: VIEWER must not hold device_credentials.read,
neither via the revision nor via the post-upgrade seed filter."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
VW1 = ROOT / "alembic/versions/vw1_viewer_no_credential_read.py"


def _load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vw1_chains_on_cc1_and_revokes_credential_read():
    mod = _load(VW1)
    assert mod.revision == "vw1_viewer_no_credential_read"
    assert len(mod.revision) <= 32
    assert mod.down_revision == "cc1_client_code"
    assert mod.VIEWER_REVOKED == ("device_credentials.read",)


def test_seed_filter_excludes_credential_read():
    seed = _load(ROOT / "alembic/seeds/rbac_seed.py")
    assert "p.name <> 'device_credentials.read'" in seed.VIEWER_PERMISSION_FILTER
    assert "p.name = 'web.access'" in seed.VIEWER_PERMISSION_FILTER
