from datetime import datetime, timedelta, timezone
from math import inf

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


@pytest.mark.parametrize("delta_t, humidity, expected", [
    (0.5, 80.0, Status.SAFE),
    (4.5, 50.0, Status.WARNING),
    (9.0, 25.0, Status.ALERT),
])
def test_assess_picks_the_dominant_status(delta_t, humidity, expected):
    result = assess(delta_t, humidity, cloud=20.0)
    assert result.status is expected
    assert set(result.utilities) == {"SAFE", "WARNING", "ALERT"}


def test_safety_margin_prefers_the_more_severe_status():
    close = assess(2.0, 52.0, cloud=10.0)   # SAFE 0.54 vs WARNING 0.50
    assert close.utilities["SAFE"] > close.utilities["WARNING"]
    assert close.status is Status.WARNING and "safety margin" in close.reason
    clear = assess(2.0, 60.0, cloud=10.0)   # SAFE 0.70 vs WARNING 0.50
    assert clear.status is Status.SAFE


def test_no_observable_when_cloudy_or_inputs_missing():
    assert assess(9.0, 25.0, cloud=75.0).status is Status.NO_OBSERVABLE
    missing = assess(None, 25.0, cloud=10.0)
    assert missing.status is Status.NO_OBSERVABLE and missing.utilities == {} and "delta_t" in missing.reason


def test_humidity_is_optional_and_said_so():
    dry_unknown = assess(9.0, None, cloud=10.0)
    assert dry_unknown.status is Status.ALERT and dry_unknown.utilities["ALERT"] == 1.0
    assert "humidity_low" not in dry_unknown.memberships and "humidity unavailable" in dry_unknown.reason


def test_degraded_views_are_capped_at_warning():
    capped = assess(9.0, 25.0, cloud=10.0, cap="daytime")
    assert capped.status is Status.WARNING and capped.utilities["ALERT"] == 1.0
    assert capped.reason.endswith("capped at WARNING (daytime)")
    assert assess(0.5, 80.0, cloud=10.0, cap="daytime").status is Status.SAFE   # the cap only lowers ALERT


def test_support_only_ever_raises_alert():
    base = assess(9.0, 25.0, cloud=10.0)
    weak = assess(9.0, 25.0, cloud=10.0, support={"viirs_support": 0.1})
    assert weak.utilities["ALERT"] == base.utilities["ALERT"] == 1.0   # a weak support never dilutes ALERT
    lifted = assess(6.8, 35.0, cloud=10.0, support={"viirs_support": 1.0, "power_support": 1.0})
    assert lifted.utilities["ALERT"] == 0.5692 and lifted.status is Status.ALERT
    assert assess(6.8, 35.0, cloud=10.0, support={"unknown": 1.0}).status is Status.WARNING


# ---------- state machine ----------

def test_fsm_records_only_changes_and_ignores_old_observations():
    first, t1 = step(None, "r1", T0, assess(0.5, 80.0, 10.0))
    assert t1.previous is None and t1.current is Status.SAFE
    same, t2 = step(first, "r1", T0 + timedelta(hours=1), assess(0.4, 82.0, 10.0))
    assert t2 is None and same.since == T0
    stale, t3 = step(same, "r1", T0, assess(9.0, 25.0, 10.0))
    assert t3 is None and stale is same


def test_agent_runs_on_entering_alert_and_respects_the_cooldown():
    safe, _ = step(None, "r1", T0, assess(0.5, 80.0, 10.0))
    alert, change = step(safe, "r1", T0 + timedelta(hours=1), assess(9.0, 25.0, 10.0))
    assert change.current is Status.ALERT and should_trigger_agent(safe, alert, T0 + timedelta(hours=1))
    alert = mark_agent_called(alert, T0 + timedelta(hours=1))
    still, _ = step(alert, "r1", T0 + timedelta(hours=2), assess(9.5, 24.0, 10.0))
    assert not should_trigger_agent(alert, still, T0 + timedelta(hours=2))
    warning, _ = step(still, "r1", T0 + timedelta(hours=3), assess(4.5, 50.0, 10.0))
    again, _ = step(warning, "r1", T0 + timedelta(hours=4), assess(9.0, 25.0, 10.0))
    assert not should_trigger_agent(warning, again, T0 + timedelta(hours=4))   # within 6 h
    later, _ = step(warning, "r1", T0 + timedelta(hours=8), assess(9.0, 25.0, 10.0))
    assert should_trigger_agent(warning, later, T0 + timedelta(hours=8))
    short = RuleConfig(cooldown=timedelta(hours=1))
    assert should_trigger_agent(warning, again, T0 + timedelta(hours=4), short)   # the cooldown lives in RuleConfig
