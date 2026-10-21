from __future__ import annotations

import csv
import datetime as dt
import json
import random
import statistics
from pathlib import Path

from .experiments import collect, host_snapshot
from .report import summarize

FAMILY_FIELDS = (
    "board",
    "profile",
    "zephyr_commit",
    "zephyr_diff_sha256",
    "subsystem_sha256",
    "harness_firmware_sha256",
    "board_conf_sha256",
    "board_overlay_sha256",
    "work_priority",
    "period_ms",
    "deadline_ms",
    "default_work_rounds",
)
METRICS = (
    "p99_lateness_us",
    "max_observed_lateness_us",
    "p99_response_us",
    "max_observed_response_us",
    "max_observed_execution_us",
    "median_ms",
    "p95_ms",
)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def family(manifest):
    if any(key not in manifest for key in FAMILY_FIELDS):
        raise ValueError("Rebuild with the current build tool before scheduling measurements")
    return {key: manifest[key] for key in FAMILY_FIELDS}


def validate(node, manifest_path):
    if not manifest_path:
        raise ValueError("--build-manifest is required")
    manifest = json.loads(Path(manifest_path).read_text())
    hello = node.hello()
    if manifest.get("build_id") != hello.get("build_id") or not manifest.get("build_id"):
        raise ValueError("Manifest identity does not match the flashed image")
    if hello.get("variant") != "settings" or hello.get("measurement_schema") != 3:
        raise ValueError("Scheduling measurements require the Settings measurement firmware")
    if hello.get("workload_clock") != "cycle64" or hello.get("workload") != "synthetic_triangle":
        raise ValueError("Scheduling measurements require the cycle clock and synthetic workload")
    if (
        hello["period_ticks"] / hello["tick_hz"] != 0.1
        or hello["deadline_ticks"] != hello["period_ticks"]
    ):
        raise ValueError("The fixed procedure requires a 100 ms period and deadline")
    if hello["work_priority"] != 5 or hello.get("fido_priority") not in (4, 7):
        raise ValueError("Expected application priority 5 and FIDO2 priority 4 or 7")
    family(manifest)
    return hello, manifest


def calibrate(node, manifest_path, output):
    hello, manifest = validate(node, manifest_path)
    if hello["fido_priority"] != 7:
        raise ValueError("Calibrate using the application-higher-priority Settings image")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    write_json(
        root / "procedure.json",
        {
            "targets": [0.30, 0.70],
            "tolerance_percentage_points": 1,
            "calibration_duration_s": 3,
            "validation_duration_s": 5,
            "maximum_attempts_per_target": 6,
            "definition": "mean idle job elapsed execution / 100 ms period",
        },
    )
    loads = []
    attempt = 0

    def measure(rounds, duration):
        nonlocal attempt
        attempt += 1
        path = collect(
            node,
            None,
            None,
            None,
            None,
            root / f"attempt-{attempt:02d}",
            mode="idle",
            duration=duration,
            build_manifest=manifest_path,
            work_rounds=rounds,
        )
        row = summarize(path)
        if (
            not row["measurement_complete"]
            or not row["raw_samples_complete"]
            or row["sensor_errors"]
        ):
            raise RuntimeError("Calibration collection is incomplete")
        if row["deadline_misses"] or row["skipped_releases"]:
            raise RuntimeError("Idle calibration missed a deadline; inspect its saved data")
        return row["mean_execution_us"] / 100000, path

    try:
        light_rounds = manifest["default_work_rounds"]
        fraction, path = measure(light_rounds, 5)
        if not 0 < fraction < 0.01:
            raise RuntimeError("Default load is not below 1%; inspect the firmware configuration")
        loads.append(
            {
                "name": "light",
                "target_fraction": None,
                "rounds": light_rounds,
                "calibrated_fraction": fraction,
                "validation_run": path.name,
            }
        )
        for name, target in (("30pct", 0.30), ("70pct", 0.70)):
            rounds = max(1, round(light_rounds * target / loads[0]["calibrated_fraction"]))
            accepted = False
            for _ in range(6):
                if not 1 <= rounds <= 10000000:
                    raise RuntimeError("Target load exceeds the supported mixing-round range")
                fraction, path = measure(rounds, 3)
                if abs(fraction - target) <= 0.01:
                    fraction, path = measure(rounds, 5)
                    if abs(fraction - target) <= 0.01:
                        accepted = True
                        break
                if fraction <= 0:
                    raise RuntimeError("Cannot calibrate a zero execution interval")
                rounds = max(1, round(rounds * target / fraction))
            if not accepted:
                raise RuntimeError(f"{name} did not converge; all attempts are saved")
            loads.append(
                {
                    "name": name,
                    "target_fraction": target,
                    "rounds": rounds,
                    "calibrated_fraction": fraction,
                    "validation_run": path.name,
                }
            )
        metadata = json.loads((path / "metadata.json").read_text())
        result = {
            "schema": 1,
            "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "build": manifest,
            "family": family(manifest),
            "host": metadata["host"],
            "loads": loads,
        }
        write_json(root / "calibration.json", result)
        return result
    finally:
        node.configure(manifest["default_work_rounds"])


def make_plan(loads, priority):
    cells = [(rep, load) for rep in range(1, 4) for load in loads]
    random.Random(523).shuffle(cells)
    runs = []
    for index, (rep, load) in enumerate(cells):
        modes = ("idle", "active") if (index + (priority == 4)) % 2 == 0 else ("active", "idle")
        for mode in modes:
            runs.append(
                {
                    "order": len(runs) + 1,
                    "repetition": rep,
                    "load": load,
                    "condition": mode,
                    "directory": f"{len(runs)+1:02d}-{load['name']}-r{rep}-{mode}",
                }
            )
    return runs


def run(
    node,
    auth,
    binding,
    pin,
    database,
    manifest_path,
    calibration_path,
    output,
    presence_mode="manual",
):
    hello, manifest = validate(node, manifest_path)
    calibration = json.loads(Path(calibration_path).read_text())
    if calibration["family"] != family(manifest):
        raise ValueError("Calibration and firmware source/configuration differ")
    host = host_snapshot()
    for key in ("suite_sha256", "python", "fido2", "cpu_governors"):
        if host[key] != calibration["host"][key]:
            raise ValueError(f"Host {key} differs from calibration")
    loads = calibration["loads"]
    if [load["name"] for load in loads] != ["light", "30pct", "70pct"]:
        raise ValueError("Calibration must contain light, 30pct and 70pct loads")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    plan = {
        "schema": 1,
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "build": manifest,
        "host": host,
        "family": family(manifest),
        "calibration": calibration,
        "fido_priority": hello["fido_priority"],
        "work_priority": hello["work_priority"],
        "duration_s": 60,
        "rate_hz": 0.2,
        "repetitions": 3,
        "order_seed": 523,
        "presence_mode": presence_mode,
        "latency_metric": "event_excluded_ns",
        "percentile_method": "nearest_rank_per_run",
        "runs": make_plan(loads, hello["fido_priority"]),
    }
    write_json(root / "plan.json", plan)
    try:
        for item in plan["runs"]:
            print(f"[{item['order']}/18] {item['directory']}", flush=True)
            path = collect(
                node,
                auth if item["condition"] == "active" else None,
                binding if item["condition"] == "active" else None,
                pin,
                database,
                root / item["directory"],
                mode=item["condition"],
                duration=60,
                rate=0.2,
                build_manifest=manifest_path,
                work_rounds=item["load"]["rounds"],
                presence_mode=presence_mode,
            )
            summary = summarize(path)
            write_json(path / "summary.json", summary)
            if not summary["raw_samples_complete"] or not summary["measurement_complete"]:
                raise RuntimeError(f"Incomplete measurement: {path}")
        write_json(root / "completion.json", {"complete": True})
    except BaseException as error:
        write_json(root / "completion.json", {"complete": False, "error": str(error)})
        raise
    finally:
        node.configure(manifest["default_work_rounds"])
    return root


def report(paths, output):
    plans = [(Path(path), json.loads((Path(path) / "plan.json").read_text())) for path in paths]
    if sorted(plan["fido_priority"] for _, plan in plans) != [4, 7]:
        raise ValueError("Provide one completed scheduling directory for each priority order")
    if (
        plans[0][1]["family"] != plans[1][1]["family"]
        or plans[0][1]["calibration"] != plans[1][1]["calibration"]
    ):
        raise ValueError("Scheduling directories use different sources or calibrations")
    rows = []
    for root, plan in plans:
        if not json.loads((root / "completion.json").read_text())["complete"]:
            raise ValueError(f"Incomplete scheduling procedure: {root}")
        for item in plan["runs"]:
            row = summarize(root / item["directory"])
            if not row["measurement_complete"] or not row["raw_samples_complete"]:
                raise ValueError(f"Incomplete run: {item['directory']}")
            rows.append(
                {
                    "priority_order": (
                        "application_higher" if plan["fido_priority"] == 7 else "fido_higher"
                    ),
                    "load": item["load"]["name"],
                    "repetition": item["repetition"],
                    "order": item["order"],
                    "rounds": item["load"]["rounds"],
                    "calibrated_fraction": item["load"]["calibrated_fraction"],
                    **row,
                }
            )
    cells = []
    for priority in ("application_higher", "fido_higher"):
        for load in ("light", "30pct", "70pct"):
            for mode in ("idle", "active"):
                group = [
                    r
                    for r in rows
                    if (r["priority_order"], r["load"], r["condition"]) == (priority, load, mode)
                ]
                if len(group) != 3:
                    raise ValueError("Each cell must have three repetitions")
                cell = {
                    "priority_order": priority,
                    "load": load,
                    "condition": mode,
                    "repetitions": 3,
                }
                for metric in METRICS:
                    values = [r[metric] for r in group if r[metric] is not None]
                    for suffix, operation in (
                        ("min", min),
                        ("median", statistics.median),
                        ("max", max),
                    ):
                        cell[f"{metric}_{suffix}"] = operation(values) if values else None
                for metric in (
                    "samples",
                    "deadline_misses",
                    "skipped_releases",
                    "successes",
                    "failures",
                    "outside_window_successes",
                ):
                    cell[metric] = sum(r[metric] for r in group)
                cells.append(cell)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    for name, data in (("runs", rows), ("cells", cells)):
        write_json(out / f"{name}.json", data)
        with (out / f"{name}.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)
    return cells
