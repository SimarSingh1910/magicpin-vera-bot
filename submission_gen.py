#!/usr/bin/env python3
"""
Builds the full expanded dataset (dataset/expanded/, including the canonical
30-pair test_pairs.json — same deterministic SEED as generate_dataset.py) and
runs the real composer offline (no HTTP) over each pair to produce
submission.jsonl, per challenge-brief.md §7.2.

Usage: python submission_gen.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from dataset.generate_dataset import (
    SEED, load_seeds, expand_merchants, expand_customers, expand_triggers,
    write_outputs, write_test_pairs,
)
from vera.composer import compose

ROOT = Path(__file__).parent
SEED_DIR = ROOT / "dataset"
OUT_DIR = ROOT / "dataset" / "expanded"


def index_by(items, key):
    return {i[key]: i for i in items}


def build_dataset():
    rnd = random.Random(SEED)
    categories, m_seeds, c_seeds, t_seeds = load_seeds(SEED_DIR)
    merchants = expand_merchants(m_seeds, rnd)
    customers = expand_customers(c_seeds, merchants, rnd)
    triggers = expand_triggers(t_seeds, merchants, customers, rnd)
    write_outputs(OUT_DIR, categories, merchants, customers, triggers)
    write_test_pairs(OUT_DIR, triggers, rnd)
    return categories, merchants, customers, triggers


def main():
    categories, merchants, customers, triggers = build_dataset()
    merch_by_id = index_by(merchants, "merchant_id")
    cust_by_id = index_by(customers, "customer_id")
    trig_by_id = index_by(triggers, "id")

    test_pairs = json.load(open(OUT_DIR / "test_pairs.json"))["pairs"]

    lines = []
    skipped = []
    for pair in test_pairs:
        trig = trig_by_id.get(pair["trigger_id"])
        merch = merch_by_id.get(pair["merchant_id"])
        cust = cust_by_id.get(pair.get("customer_id")) if pair.get("customer_id") else None
        if not trig or not merch:
            skipped.append(pair["test_id"])
            continue
        cat = categories.get(merch.get("category_slug"))
        composed = compose(cat, merch, trig, cust)
        lines.append({
            "test_id": pair["test_id"],
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    out_path = ROOT / "submission.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {out_path}")
    if skipped:
        print(f"Skipped (missing merchant/trigger): {skipped}")


if __name__ == "__main__":
    main()
