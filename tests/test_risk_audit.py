"""후보 가용성 조사와 비공개 CLI. 합성 항등 자료를 실측 기준 확정으로 취급하지 않는다."""
import json
import os
import stat
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.risk_audit import audit_risk_samples
from schemas.engine import Period
from tests.test_risk_runtime import END, START, case

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit-risk-samples.py"


def audit(frame, **overrides):
    args = dict(period=Period(start=START, end=END), observed_through=END,
                origins=(START, START + timedelta(days=7)), horizons=(10,), candidates=(1, 2, 3))
    args.update(overrides)
    return audit_risk_samples(frame, **args)


def test_candidates_compare_coverage_without_selecting_a_threshold():
    _, frame = case()
    report = audit(frame)
    a, b = report["profiles"]
    assert a["method"] == "A" and a["usable_ratio_cases"] == 0
    assert a["excluded_reasons"]["FORECAST_UNAVAILABLE"] == 2
    assert b["method"] == "B" and b["usable_ratio_cases"] == 2
    assert b["non_overlapping_windows_sum"] < b["usable_ratio_cases"]
    assert b["sku_cases"] == {"min": 2, "median": 2, "max": 2}
    assert [c["sku_count"] for c in b["candidates"]] == [1, 1, 0]
    assert [c["insufficient_count"] for c in b["candidates"]] == [0, 0, 1]
    assert b["candidates"][1]["nominal_step_percent_points"] == 100 / 2
    assert report["selected_min_samples"] is None
    assert "product_id" not in json.dumps(report)


def test_forecast_lengths_and_methods_are_separate_and_future_data_does_not_change_report():
    _, frame = case()
    report = audit(frame, horizons=(10, 11))
    assert len(report["profiles"]) == 4
    future = frame.assign(stock_date=lambda df: df.stock_date + timedelta(days=500), product_id=999)
    import pandas as pd
    assert audit(pd.concat([frame, future]), horizons=(11, 10)) == report


def test_zero_forecast_and_invalid_observations_appear_as_exclusions():
    _, frame = case()
    frame["outbound_qty"] = 0
    b = audit(frame)["profiles"][1]
    assert b["usable_ratio_cases"] == 0 and b["excluded_reasons"]["ZERO_FORECAST"] == 2
    frame.loc[frame.stock_date == START + timedelta(days=1), "is_valid_observation"] = False
    b = audit(frame)["profiles"][1]
    assert b["excluded_reasons"]["UNOBSERVED_ACTUALS"] == 1


def test_audit_denominator_only_includes_products_present_in_tune():
    import pandas as pd

    _, frame = case()
    historical = frame.assign(product_id=303, stock_date=lambda f: f.stock_date - timedelta(days=365))
    assert audit(pd.concat([frame, historical])) == audit(frame)


@pytest.mark.parametrize("changes", [
    {"candidates": ()}, {"candidates": (1, 1)}, {"candidates": (False,)},
    {"horizons": ()}, {"horizons": (0,)}, {"horizons": (10, 10)},
    {"period": Period(start=date(2026, 8, 1), end=date(2026, 9, 30))},
])
def test_bad_audit_inputs_are_not_reported_as_zero_risk(changes):
    _, frame = case()
    with pytest.raises(ValueError):
        audit(frame, **changes)


def run_cli(source, output, **env):
    return subprocess.run([
        sys.executable, str(SCRIPT), "--input", str(source), "--output", str(output),
        "--year", "2026", "--observed-through", "2026-07-31", "--origin-step-days", "7",
        "--horizons", "10", "--candidates", "1", "2", "3",
    ], text=True, capture_output=True, env=dict(os.environ, **env))


def test_cli_writes_new_private_aggregate_report_without_network_or_llm(tmp_path):
    _, frame = case()
    source, output = tmp_path / "synthetic.csv", tmp_path / "aggregate.json"
    frame.to_csv(source, index=False)
    result = run_cli(source, output)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["selected_min_samples"] is None and len(report["input_sha256"]) == 64
    assert "product_id" not in output.read_text() and "synthetic.csv" not in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "profiles" in result.stdout and "4" not in result.stderr
    first = output.read_bytes()
    assert run_cli(source, output).returncode != 0
    assert output.read_bytes() == first


def test_cli_hides_bad_source_and_refuses_output_in_repository(tmp_path):
    source, output = tmp_path / "synthetic.csv", tmp_path / "aggregate.json"
    source.write_text("sensitive-customer-cell\nsecret-data\n")
    result = run_cli(source, output)
    assert result.returncode != 0 and "secret-data" not in result.stderr + result.stdout
    assert not output.exists()
    repo_output = SCRIPT.parent.parent / "forbidden-private-report.json"
    result = run_cli(source, repo_output)
    assert result.returncode != 0 and not repo_output.exists()

