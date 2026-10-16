from datetime import datetime, timedelta, timezone
from math import inf, nan

import pytest

from asapify_engine import power
from asapify_engine.canonical import Observation, Quality, anomaly, build_baseline
from asapify_engine.fsm import mark_agent_called, should_trigger_agent, step
from asapify_engine.models import Status
from asapify_engine.rules import RuleConfig, assess, trapezoid

UTC = timezone.utc
T0 = datetime(2026, 10, 10, 5, tzinfo=UTC)


def skin(value, at, source="nasa_power", parameter="skin_temperature"):
    return Observation(source=source, parameter=parameter, value=value, unit="K",
                       measurement_type="earth_skin_temperature", observed_at=at, ingested_at=at,
                       spatial_reference="EPSG:4326 point -2.4,113.9", spatial_resolution="0.5deg x 0.625deg",
                       quality=Quality.ACCEPTED if value is not None else Quality.REJECTED)


# ---------- canonicalisation ----------

def test_power_canonicalises_units_and_rejects_fill_values():
    raw = {"geometry": {"coordinates": [113.9, -2.4, 25.7]},
           "header": {"fill_value": -999.0, "sources": ["GEOSIT", "POWER"]},
           "properties": {"parameter": {"TS": {"2026101005": 36.85, "2026101006": -999.0},
                                        "RH2M": {"2026101005": 35.0, "2026101006": -999.0}}}}
    obs = {(o.parameter, o.observed_at.hour): o for o in power.canonicalize(raw, T0)}
    assert obs[("skin_temperature", 5)].value == 310.0 and obs[("skin_temperature", 5)].unit == "K"
    assert obs[("relative_humidity", 5)].usable and obs[("relative_humidity", 5)].unit == "%"
    assert obs[("skin_temperature", 6)].quality is Quality.REJECTED
    assert "missing_value" in obs[("relative_humidity", 6)].quality_flags
    assert all("reanalysis_estimate" in o.quality_flags for o in obs.values())


def test_baseline_uses_same_hour_and_calendar_window_of_earlier_years_only():
    history = [skin(305.0, datetime(year, 10, day, 5, tzinfo=UTC)) for year in range(2021, 2026) for day in range(1, 21)]
    history += [skin(330.0, datetime(2024, 10, 10, 6, tzinfo=UTC)),   # other hour
                skin(330.0, datetime(2024, 12, 20, 5, tzinfo=UTC)),   # outside the window
                skin(330.0, datetime(2026, 10, 9, 5, tzinfo=UTC))]    # same year as the target
    baseline = build_baseline(skin(310.0, T0), history)
    assert baseline.mean == 305.0 and baseline.samples == 100
    assert "2021-2026" not in baseline.description and "UTC hour 05" in baseline.description
    assert anomaly(skin(310.0, T0), baseline) == 5.0


def test_anomaly_refuses_incompatible_measurements_and_thin_baselines():
    history = [skin(305.0, datetime(2025, 10, d, 5, tzinfo=UTC)) for d in range(1, 6)]
    thin = build_baseline(skin(310.0, T0), history)
    assert anomaly(skin(310.0, T0), thin) is None  # 5 samples < 30
    with pytest.raises(ValueError):
        anomaly(skin(310.0, T0, parameter="air_temperature"), thin)


# ---------- rules engine ----------

def test_trapezoid_shapes():
    assert trapezoid(0.0, (-inf, -inf, 1.0, 3.0)) == 1.0
    assert trapezoid(2.0, (-inf, -inf, 1.0, 3.0)) == 0.5
    assert trapezoid(9.0, (6.0, 8.0, inf, inf)) == 1.0
    assert trapezoid(5.0, (6.0, 8.0, inf, inf)) == 0.0


def statuses(**kw):
    """Where the status changes along ΔT, in 0.01 K steps."""
    out, prev = [], None
    for i in range(0, 1201):
        status = assess(i / 100, 10.0, **kw).status
        if status is not prev:
            out.append((i / 100, status))
            prev = status
    return out


@pytest.mark.parametrize("delta_t, expected", [(0.5, Status.SAFE), (4.5, Status.WARNING), (9.0, Status.ALERT)])
def test_assess_picks_the_dominant_status(delta_t, expected):
    result = assess(delta_t, cloud=20.0)
    assert result.status is expected
    assert set(result.utilities) == {"SAFE", "WARNING", "ALERT"}


def test_status_boundaries_follow_the_curves_and_the_safety_margin():
    assert statuses() == [(0.0, Status.SAFE), (1.95, Status.WARNING), (6.95, Status.ALERT)]
    close = assess(6.96, 10.0)   # WARNING 0.52 vs ALERT 0.48: within the margin
    assert close.utilities["WARNING"] > close.utilities["ALERT"]
    assert close.status is Status.ALERT and "safety margin" in close.reason


def test_drier_air_lowers_both_thresholds_and_wetter_air_raises_them():
    assert statuses(dry_z=2.5) == [(0.0, Status.SAFE), (1.11, Status.WARNING), (6.11, Status.ALERT)]
    assert statuses(dry_z=-2.5) == [(0.0, Status.SAFE), (2.8, Status.WARNING), (7.8, Status.ALERT)]
    assert assess(5.0, 10.0, dry_z=50).status is Status.WARNING   # the shift stops at 1 K: dry air cannot make ALERT


def test_the_sun_raises_the_thresholds_by_day_only():
    assert statuses(sun_elevation_deg=90) == [(0.0, Status.SAFE), (2.95, Status.WARNING), (7.95, Status.ALERT)]
    assert statuses(sun_elevation_deg=-20) == statuses()
    assert assess(7.5, 10.0, sun_elevation_deg=30).adjust["sun_shift_k"] == 0.5


def test_reason_and_adjust_explain_the_shift():
    result = assess(6.5, 10.0, dry_z=2.5, sun_elevation_deg=30)
    assert result.adjust == {"delta_t_k": 6.5, "dry_z": 2.5, "rh_shift_k": 0.848, "sun_shift_k": 0.5,
                             "delta_t_eff_k": 6.848}
    assert "ΔT 6.50 K counted as 6.85 K (air 2.5σ drier than normal +0.85 K, sun -0.50 K)" in result.reason
    plain = assess(6.5, 10.0)
    assert plain.adjust["rh_shift_k"] == 0.0 and "no humidity anomaly" in plain.reason
    assert set(plain.memberships) == {"delta_t_low", "delta_t_medium", "delta_t_high"}


def test_no_observable_when_cloudy_or_inputs_missing():
    assert assess(9.0, cloud=75.0).status is Status.NO_OBSERVABLE
    missing = assess(None, cloud=10.0)
    assert missing.status is Status.NO_OBSERVABLE and missing.utilities == {} and "delta_t" in missing.reason


def test_support_only_ever_raises_alert():
    assert assess(6.9, 10.0).status is Status.WARNING
    lifted = assess(6.9, 10.0, support={"viirs_support": 1.0, "power_support": 1.0})
    assert lifted.utilities["ALERT"] == 0.5769 and lifted.status is Status.ALERT
    weak = assess(9.0, 10.0, support={"viirs_support": 0.1})
    assert weak.utilities["ALERT"] == 1.0   # a weak support never dilutes ALERT
    assert assess(6.9, 10.0, support={"unknown": 1.0}).status is Status.WARNING


@pytest.mark.parametrize("delta_t", [0.0, 3.0, 5.0, 5.99])
def test_support_cannot_create_alert_without_a_high_membership(delta_t):
    result = assess(delta_t, 10.0, support={"viirs_support": 1.0, "power_support": 1.0})
    assert result.utilities["ALERT"] == 0.0 and result.status is not Status.ALERT


@pytest.mark.parametrize("delta_t, cloud", [(nan, 10.0), (inf, 10.0), (-inf, 10.0), (9.0, nan), (9.0, -1.0),
                                            (9.0, 101.0)])
def test_invalid_thermal_or_cloud_is_not_observable(delta_t, cloud):
    result = assess(delta_t, cloud=cloud)
    assert result.status is Status.NO_OBSERVABLE and result.utilities == {}


@pytest.mark.parametrize("bad", [nan, inf, -inf])
def test_invalid_dryness_or_sun_moves_nothing(bad):
    assert assess(6.5, 10.0, dry_z=bad).adjust == assess(6.5, 10.0).adjust
    assert assess(6.5, 10.0, sun_elevation_deg=bad).adjust == assess(6.5, 10.0).adjust


def test_support_is_finite_and_at_most_one():
    full = assess(6.9, 10.0, support={"viirs_support": 1.0})
    assert assess(6.9, 10.0, support={"viirs_support": 5.0}).utilities == full.utilities
    assert assess(6.9, 10.0, support={"viirs_support": nan}).utilities == assess(6.9, 10.0).utilities
    assert "viirs_support" not in assess(6.9, 10.0, support={"viirs_support": nan}).memberships


# ---------- state machine ----------

def test_fsm_records_only_changes_and_ignores_old_observations():
    first, t1 = step(None, "r1", T0, assess(0.5, 10.0))
    assert t1.previous is None and t1.current is Status.SAFE
    same, t2 = step(first, "r1", T0 + timedelta(hours=1), assess(0.4, 10.0))
    assert t2 is None and same.since == T0
    stale, t3 = step(same, "r1", T0, assess(9.0, 10.0))
    assert t3 is None and stale is same


def test_agent_runs_on_entering_alert_and_respects_the_cooldown():
    safe, _ = step(None, "r1", T0, assess(0.5, 10.0))
    alert, change = step(safe, "r1", T0 + timedelta(hours=1), assess(9.0, 10.0))
    assert change.current is Status.ALERT and should_trigger_agent(safe, alert, T0 + timedelta(hours=1))
    alert = mark_agent_called(alert, T0 + timedelta(hours=1))
    still, _ = step(alert, "r1", T0 + timedelta(hours=2), assess(9.5, 10.0))
    assert not should_trigger_agent(alert, still, T0 + timedelta(hours=2))
    warning, _ = step(still, "r1", T0 + timedelta(hours=3), assess(4.5, 10.0))
    again, _ = step(warning, "r1", T0 + timedelta(hours=4), assess(9.0, 10.0))
    assert not should_trigger_agent(warning, again, T0 + timedelta(hours=4))   # within 6 h
    later, _ = step(warning, "r1", T0 + timedelta(hours=8), assess(9.0, 10.0))
    assert should_trigger_agent(warning, later, T0 + timedelta(hours=8))
    short = RuleConfig(cooldown=timedelta(hours=1))
    assert should_trigger_agent(warning, again, T0 + timedelta(hours=4), short)   # the cooldown lives in RuleConfig
