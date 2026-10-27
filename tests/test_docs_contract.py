import pytest
from ops.sync_docs import canonical, sync, START, END


def test_shared_claims_cannot_drift_or_depend_on_document_order():
    expected = canonical()
    assert "MET is withheld" in expected
    for position in ["intro", "evaluation", "appendix"]:
        original = f"Unrelated {position}\n{START}\nstale copy\n{END}\nUnrelated ending"
        updated = sync(original, expected)
        assert updated.startswith(f"Unrelated {position}\n") and updated.endswith("\nUnrelated ending")
        assert sync(updated, expected) == updated and "stale copy" not in updated


@pytest.mark.parametrize("text", ["no markers", START + END + START + END, END + START])
def test_document_updates_require_exact_explicit_block_boundaries(text):
    with pytest.raises(ValueError): sync(text, canonical())


def test_inventory_generator_counts_parameters_and_preserves_explanations():
    from collections import Counter
    from ops.check_test_inventory import update
    text = "1 collected tests across 1 files: 1 are selected by default, and the network integration test is deselected.\n- `test_a.py` (1) — important purpose\n- `test_net.py` (1, network-gated) — external check"
    result = update(text, Counter({"test_a.py": 3}), Counter({"test_net.py": 1}))
    assert "4 collected tests across 2 files: 3 are selected" in result
    assert "`test_a.py` (3) — important purpose" in result
    with pytest.raises(ValueError): update(text, Counter({"test_a.py": 3, "test_missing.py": 1}), Counter({"test_net.py": 1}))


def test_mdx_uses_valid_comment_syntax_without_changing_shared_claims():
    from ops.sync_docs import MDX_START, MDX_END
    old = f"Intro\n{MDX_START}\nstale\n{MDX_END}\nEnding"
    updated = sync(old, canonical())
    assert "<!--" not in updated and "MET is withheld" in updated
    assert sync(updated, canonical()) == updated
    with pytest.raises(ValueError): sync(old + START + END, canonical())
