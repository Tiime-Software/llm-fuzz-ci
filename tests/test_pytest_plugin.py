from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_fuzz_ci.pytest_plugin import target_from_marker


def test_budget_marker_infers_target_from_pytest_item(tmp_path):
    def test_checkout_security():
        """Checkout totals must not trust user-controlled discount values."""

    item = SimpleNamespace(
        nodeid="tests/test_checkout.py::test_checkout_security",
        path=tmp_path / "tests" / "test_checkout.py",
        obj=test_checkout_security,
    )
    marker = SimpleNamespace(args=(), kwargs={"budget_usd": 0.25})

    target = target_from_marker(item, marker)

    assert target.id == "tests/test_checkout.py::test_checkout_security"
    assert target.target == "pytest::tests/test_checkout.py::test_checkout_security"
    assert target.budget_usd == 0.25
    assert target.description == "Checkout totals must not trust user-controlled discount values."
    assert target.source_file == str(Path(tmp_path / "tests" / "test_checkout.py"))


def test_marker_rejects_positional_arguments(tmp_path):
    item = SimpleNamespace(
        nodeid="tests/test_app.py::test_policy",
        path=tmp_path / "tests" / "test_app.py",
        obj=lambda: None,
    )
    marker = SimpleNamespace(args=(0.5,), kwargs={})

    with pytest.raises(pytest.UsageError, match="takes no positional arguments"):
        target_from_marker(item, marker)


def test_marker_works_without_a_budget(tmp_path):
    item = SimpleNamespace(
        nodeid="tests/test_app.py::test_policy",
        path=tmp_path / "tests" / "test_app.py",
        obj=lambda: None,
    )
    marker = SimpleNamespace(args=(), kwargs={})

    target = target_from_marker(item, marker)

    assert target.budget_usd is None



def test_marker_rejects_extra_keyword_arguments(tmp_path):
    item = SimpleNamespace(
        nodeid="tests/test_app.py::test_policy",
        path=tmp_path / "tests" / "test_app.py",
        obj=lambda: None,
    )
    marker = SimpleNamespace(args=(), kwargs={"budget_usd": 0.5, "retries": 3})

    with pytest.raises(pytest.UsageError, match="params"):
        target_from_marker(item, marker)


def test_marker_rejects_invalid_budget(tmp_path):
    item = SimpleNamespace(
        nodeid="tests/test_app.py::test_policy",
        path=tmp_path / "tests" / "test_app.py",
        obj=lambda: None,
    )
    marker = SimpleNamespace(args=(), kwargs={"budget_usd": 0})

    with pytest.raises(pytest.UsageError, match="greater than 0"):
        target_from_marker(item, marker)


def test_marker_accepts_a_params_subset(tmp_path):
    item = SimpleNamespace(
        nodeid="tests/test_app.py::test_transfer",
        path=tmp_path / "tests" / "test_app.py",
        obj=lambda: None,
    )
    marker = SimpleNamespace(args=(), kwargs={"params": ["amount"]})

    target = target_from_marker(item, marker)

    assert target.params == ["amount"]


def test_marker_rejects_params_that_is_not_a_list(tmp_path):
    item = SimpleNamespace(
        nodeid="tests/test_app.py::test_transfer",
        path=tmp_path / "tests" / "test_app.py",
        obj=lambda: None,
    )
    marker = SimpleNamespace(args=(), kwargs={"params": "amount"})

    with pytest.raises(pytest.UsageError, match="must be a list of names"):
        target_from_marker(item, marker)
