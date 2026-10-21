#!/usr/bin/env python3
"""Compare memory usage across matched build manifests."""
import argparse
import csv
import json
from pathlib import Path


def compare(manifests):
    base = [m for m in manifests if m["variant"] == "baseline"]
    if len(base) != 1:
        raise ValueError("Provide exactly one baseline manifest")
    base = base[0]
    same = (
        "board",
        "zephyr_commit",
        "zephyr_diff_sha256",
        "subsystem_sha256",
        "harness_firmware_sha256",
        "board_conf_sha256",
        "board_overlay_sha256",
        "workload",
    )
    if len({m["variant"] for m in manifests}) != len(manifests):
        raise ValueError("Duplicate variants")
    for m in manifests:
        if m.get("profile", "generic") != base.get("profile", "generic"):
            raise ValueError("Incomparable builds: profile differs")
        for key in same:
            if key not in m or m[key] != base[key]:
                raise ValueError(f"Incomparable builds: {key} differs for {m['variant']}")
    return [
        {
            "variant": m["variant"],
            "rom_bytes": m["rom_bytes"],
            "static_ram_bytes": m["static_ram_bytes"],
            "delta_rom_bytes": m["rom_bytes"] - base["rom_bytes"],
            "delta_static_ram_bytes": m["static_ram_bytes"] - base["static_ram_bytes"],
            "psa_provider": m["psa_provider"],
        }
        for m in manifests
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifests", type=Path, nargs="+")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    rows = compare([json.loads(f.read_text()) for f in a.manifests])
    with a.out.open("x", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
