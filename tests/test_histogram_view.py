"""The display window must preserve sample accounting and summary values."""
from dcf import _central_histogram, monte_carlo
from tests.test_dcf import _std_company, _base_assumptions


def test_central_histogram_rebins_samples_and_accounts_for_tails():
    values = list(range(100)) + [200000]
    view = _central_histogram(values, 10, 90)
    assert sum(count for _, count in view.histogram) == 81
    assert view.below_count == 10
    assert view.above_count == 10
    assert sum(count > 0 for _, count in view.histogram) > 30
    assert view.below_count + view.above_count + sum(c for _, c in view.histogram) == len(values)


def test_equal_percentile_bounds_keep_tied_samples():
    view = _central_histogram([1, 2, 2, 2, 3], 2, 2)
    assert view.histogram == [(2, 3)]
    assert view.below_count == view.above_count == 1


def test_simulation_summary_and_full_histogram_cover_every_sample():
    result = monte_carlo(_std_company(), _base_assumptions(), iterations=500, seed=42)
    view = result.central_histogram
    assert view is not None
    assert (view.lower_bound, view.upper_bound) == (result.p10, result.p90)
    assert sum(c for _, c in result.histogram) == result.iterations_completed
    assert sum(c for _, c in view.histogram) + view.below_count + view.above_count == result.iterations_completed
    assert view.lower_bound <= result.median <= view.upper_bound
    assert result.model_dump()['central_histogram']['below_count'] == view.below_count


def test_empty_simulation_has_no_display_window():
    result = monte_carlo(_std_company(), _base_assumptions(), iterations=0, seed=42)
    assert result.central_histogram is None
    assert result.histogram == []
