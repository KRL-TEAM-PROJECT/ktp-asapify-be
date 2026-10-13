"""Publish an already-computed pipeline bundle; this script does NOT detect fires.

No synthetic data is generated. Use --apply only after reviewing the source bundle.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asapify_api.core import as_time, date_key, night_of, night_slots, validate_grid


def validate_bundle(bundle: dict) -> None:
    index, grid, meta, night = (bundle[k] for k in ("index", "grid", "meta", "night"))
    validate_grid(index, grid)
    assert isinstance(meta["replay"], bool), "replay must be explicit"
    assert date_key(meta["archive_first"]) <= night_of(as_time(grid["slot"]))
    assert night["night"] == night_of(as_time(grid["slot"]))
    assert night["pixels_version"] == index["version"]
    assert night["slots"] == night_slots(night["night"])
    assert len(night["status"]) == len(night["n_sat"]) == 54
    for slot, states, sats in zip(night["slots"], night["status"], night["n_sat"]):
        if states is None:
            assert sats is None
        else:
            assert slot <= grid["slot"] and len(states) == len(index["rows"])
            assert set(states) <= set("SNWA") and sats in (0, 1, 2)
    assert night["status"][night["slots"].index(grid["slot"])] == grid["status"]
    for cluster in bundle.get("clusters", []):
        assert cluster["id"] and "/" not in cluster["id"]
        assert not cluster.get("decision"), "pipeline must never write an operator decision"
        assert cluster["state"] in ("active", "closed")
        assert cluster["n_sat"] == 2
        for key in ("province", "rep_pixel", "centroid", "pixels", "trigger_slot", "utility_score",
                    "neighbour_support", "attributes", "series", "neighbours", "verification", "alert_nights"):
            assert key in cluster, f"missing {key}"
    ids = {c["id"] for c in bundle.get("clusters", [])}
    assert set(grid["clusters"]) <= ids, "every ALERT cluster needs a detail record"


def publish(bundle: dict, project: str, bucket: str, database: str):
    from google.cloud import firestore, storage
    db, gcs = firestore.Client(project=project, database=database), storage.Client(project=project)
    target = gcs.bucket(bucket)
    def put(kind, value):
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        name = f"api/{kind}/{digest}.json"
        target.blob(name).upload_from_string(payload, content_type="application/json")
        return name

    grid, night = bundle["grid"], bundle["night"]
    stamp = grid["slot"]
    objects = {"pixels_object": put("pixels", bundle["index"]), "grid_object": put("grid", grid)}
    night_object = put("nights", night)
    # Immutable objects are written first. Publish the manifest LAST, after every detail is present.
    for source in bundle.get("clusters", []):
        ref = db.collection("clusters").document(source["id"])
        @firestore.transactional
        def update(transaction):
            current = ref.get(transaction=transaction)
            previous = current.to_dict() if current.exists else {}
            value = copy.deepcopy(source)
            value["replay"] = bundle["meta"]["replay"]  # Server-derived; never operator-selected.
            decision = previous.get("decision")
            value["decision"] = decision if decision and decision["at"] <= stamp else None
            transaction.set(ref.collection("history").document(stamp), {"timestamp": stamp, "cluster": value})
            if previous.get("observation_at", "") <= stamp:
                value["decision"] = decision  # Never erase a later operator decision.
                value["observation_at"] = stamp
                transaction.set(ref, value)
        update(db.transaction())

    night_ref = db.collection("api_nights").document(night["night"])
    @firestore.transactional
    def update_night(transaction):
        current = night_ref.get(transaction=transaction)
        existing = current.to_dict() if current.exists else {}
        if existing.get("as_of", "") <= stamp:
            transaction.set(night_ref, {"night": night["night"], "month": night["night"][:7],
                "has_data": any(s is not None for s in night["status"]),
                "alert_clusters": len(night["clusters"]), "object": night_object, "as_of": stamp})
    update_night(db.transaction())
    db.collection("api_snapshots").document(stamp).set({
        "timestamp": stamp, "n_sat": grid["n_sat"], **bundle["meta"], **objects})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("bundle", type=Path)
    p.add_argument("--project", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--database", default="asapify")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    bundle = json.loads(args.bundle.read_text())
    validate_bundle(bundle)
    print(f"Validated snapshot {bundle['grid']['slot']}; replay={bundle['meta']['replay']}")
    if args.apply:
        publish(bundle, args.project, args.bucket, args.database)
        print("Snapshot published; no Telegram message sent.")
    else:
        print("Validation only. Use --apply to publish to the specified project.")


if __name__ == "__main__":
    main()
