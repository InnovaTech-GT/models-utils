"""Shared loaders for the mobile-integration (mi1/mi2) tests. Revisions and
seeds are loaded by file path (test_matrix_permissions_seed precedent)."""
import importlib.util
import os

_ROOT = os.path.join(os.path.dirname(__file__), "..", "alembic")


def load(rel_path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mi1():
    return load("versions/mi1_mobile_enum_labels.py", "mi1_mobile_enum_labels")


def mi2():
    return load("versions/mi2_mobile_field_ops.py", "mi2_mobile_field_ops")


def isp_seed():
    return load("seeds/isp_seed.py", "isp_seed_mi_test")
