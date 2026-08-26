"""Structural budgets for API JSON that will later be rendered into a prompt."""

import pytest

from steward.input_bounds import EDIT_MAX_NODES, validate_approval_edit


def test_edit_value_count_has_an_exact_boundary() -> None:
    # Root + ten lists + 989 scalars = 1,000 nodes; no container exceeds 100 items.
    exact = {str(i): [0] * (99 if i < 9 else 98) for i in range(10)}
    assert 1 + len(exact) + sum(map(len, exact.values())) == EDIT_MAX_NODES
    validate_approval_edit(exact)

    with pytest.raises(ValueError, match="value limit"):
        validate_approval_edit({str(i): [0] * 99 for i in range(10)})
