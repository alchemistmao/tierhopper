from pathlib import Path

import pytest

from tierhopper.spec import SpecError, load_spec, parse_spec

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "hello-gpu"


def test_example_spec_loads():
    spec, root = load_spec(EXAMPLE)
    assert spec.name == "hello-gpu"
    assert spec.gpu.min_vram_gb == 12
    assert root == EXAMPLE


def test_unknown_field_rejected():
    with pytest.raises(SpecError):
        parse_spec({"name": "x", "entrypoint": "python a.py", "gpuu": {}})


def test_shard_needs_exactly_one_source():
    with pytest.raises(SpecError):
        parse_spec({"name": "x", "entrypoint": "a", "shard": {"over": "*.pdf", "count": 2}})
    spec = parse_spec({"name": "x", "entrypoint": "a", "shard": {"count": 4}})
    assert spec.shard.count == 4


def test_spec_hash_ignores_name():
    a = parse_spec({"name": "a", "entrypoint": "python run.py"})
    b = parse_spec({"name": "b", "entrypoint": "python run.py"})
    c = parse_spec({"name": "a", "entrypoint": "python other.py"})
    assert a.spec_hash() == b.spec_hash() != c.spec_hash()


def test_missing_spec(tmp_path):
    with pytest.raises(SpecError):
        load_spec(tmp_path)
