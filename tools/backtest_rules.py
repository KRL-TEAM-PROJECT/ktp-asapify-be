"""Compare the stored statuses with what the current rules would give on the same slots. Read-only.

Run from the repository root in Cloud Shell (ADC, a role that can read Firestore):

    python -m tools.backtest_rules --project hack2skill-510921 --days 3

Every stored slot record keeps both satellites' views, the cloud, the sun's elevation and the support, so the slot is
fused and rated again with asapify_engine as it is now and set beside the status stored then. Old slot records have
no dryness of their own: `--dryness current` uses each region's present NASA POWER dryness for them (an approximation
that also ignores the 96-hour freshness rule), `--dryness none` leaves their curves unshifted; a record that has its
own dry_z (16 October 2026 on) always uses it, so re-rating those with unchanged rules changes nothing. Old records'
power_support was computed with the earlier ΔT-based formula and is taken as stored. Cost: one read per slot (65
regions x 96 slots a day).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

from asapify_engine.contextual import RegionThermal
from asapify_engine.fusion import fuse
from asapify_engine.ingest import iso, parse
from asapify_engine.rules import assess

ORDER = ("SAFE", "WARNING", "ALERT", "NO_OBSERVABLE")


def view(summary: dict | None) -> RegionThermal | None:
    if not summary:
        return None
    pixels = tuple((p["lat"], p["lon"], p["anomaly_k"]) for p in summary.get("hot_pixels") or [])
    fields = {k: summary.get(k) for k in RegionThermal.__dataclass_fields__ if k != "hot_pixels"}
    return RegionThermal(**fields, hot_pixels=pixels)


def rerate(record: dict, centre: tuple[float, float], dry_z: float | None) -> str:
    slot = parse(record["observed_at"])
    views = {name: view(v) for name, v in (record.get("views") or {}).items()}
    fused = fuse(views, night=bool(record.get("night")), slot=slot, centre=centre)
    return assess(fused.delta_t, fused.cloud_pct, dry_z=dry_z, sun_elevation_deg=record.get("sun_elevation_deg"),
                  support=record.get("support") or {}).status.value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--database", default="asapify")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--dryness", choices=("current", "none"), default="current")
    args = parser.parse_args()

    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    db = firestore.Client(project=args.project, database=args.database)
    since = iso(datetime.now(timezone.utc) - timedelta(days=args.days))
    pairs, changed = Counter(), Counter()
    for region in db.collection("engine_regions").stream():
        doc = region.to_dict()
        dry = ((doc.get("power") or {}).get("dry_z")) if args.dryness == "current" else None
        slots = region.reference.collection("slots").where(filter=FieldFilter("observed_at", ">=", since)).stream()
        for snap in slots:
            record = snap.to_dict()
            inputs = record.get("inputs") or {}
            own = inputs["dry_z"] if "dry_z" in inputs else dry
            old, new = record.get("status"), rerate(record, (doc.get("lat"), doc.get("lon")), own)
            pairs[(old, new)] += 1
            if old != new:
                changed[(region.id, old, new)] += 1

    total = sum(pairs.values())
    print(f"{total} slots since {since}, dryness: {args.dryness}\n")
    print("stored \\ now".ljust(16) + "".join(s.ljust(15) for s in ORDER))
    for old in ORDER:
        print(old.ljust(16) + "".join(str(pairs[(old, new)]).ljust(15) for new in ORDER))
    print(f"\nunchanged: {sum(n for (o, n2), n in pairs.items() if o == n2)} of {total}")
    for (rid, old, new), n in sorted(changed.items(), key=lambda kv: -kv[1])[:30]:
        print(f"  {rid}: {old} -> {new} in {n} slots")


if __name__ == "__main__":
    main()
