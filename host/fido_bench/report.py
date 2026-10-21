from __future__ import annotations
import csv
import json
import math
import statistics
from pathlib import Path


def percentile(values, percent):
    """Return the nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


def summarize(path):
    path = Path(path)
    meta = json.loads((path / "metadata.json").read_text())
    if meta.get("data_kind") != "hardware":
        raise ValueError("Expected hardware measurement data")
    rows = list(csv.DictReader((path / "latency.csv").open()))
    measured = [r for r in rows if r.get("warmup") != "1"]
    good = [r for r in measured if r["success"] == "1"]
    overlap_known = all(r.get("workload_overlap") in ("0", "1") for r in good)
    included = [r for r in good if r.get("workload_overlap") != "0"]
    metric = meta.get("latency_metric", "wait_excluded_ns")
    times = [int(r[metric]) / 1e6 for r in included]
    starts = [int(r["start_offset_ns"]) for r in measured if r.get("start_offset_ns")]
    rate = (
        (len(starts) - 1) * 1e9 / (starts[-1] - starts[0])
        if len(starts) > 1 and starts[-1] > starts[0]
        else None
    )
    stats = json.loads((path / "workload.json").read_text())
    hz = stats.get("clock_hz", meta["firmware"]["tick_hz"])
    modern = stats.get("measurement_schema", 1) >= 2
    suffix = "units" if modern else "ticks"
    hist = stats["histogram"]
    if sum(hist) != stats["samples"]:
        raise ValueError("Histogram does not cover all executed releases")
    collection = path / "collection.json"
    complete = (
        json.loads(collection.read_text())["completed"]
        if collection.exists()
        else (
            len(rows) == meta["trials"] + meta["warmup"] and len(good) == meta["trials"]
            if meta["condition"] == "latency"
            else bool(stats.get("complete", False))
        )
    )
    raw_path = path / "workload-samples.csv"
    raw = list(csv.DictReader(raw_path.open())) if raw_path.exists() else []
    raw_complete = len(raw) == stats["samples"] and not stats.get("dropped_samples", 0)
    quantiles = {}
    for name in ("lateness", "response", "execution"):
        values = [int(row[f"{name}_units"]) * 1e6 / hz for row in raw]
        quantiles[f"p99_{name}_us"] = percentile(values, 99) if raw_complete else None
    return {
        "run": path.name,
        "variant": meta["firmware"]["variant"],
        "control_transport": meta["firmware"].get("control_transport", "unrecorded"),
        "spiram_enabled": meta["firmware"].get("spiram_enabled", "unrecorded"),
        "condition": meta["condition"],
        "workload": meta["firmware"]["workload"],
        "successes": len(good),
        "failures": len(measured) - len(good),
        "latency_samples": len(included),
        "concurrent_successes": len(included) if overlap_known else None,
        "outside_window_successes": len(good) - len(included) if overlap_known else None,
        "observed_request_start_rate_hz": rate,
        "latency_metric": metric,
        "median_ms": statistics.median(times) if times else None,
        "p95_ms": percentile(times, 95),
        "p99_ms": percentile(times, 99),
        **quantiles,
        "raw_samples_complete": raw_complete,
        "dropped_samples": stats.get("dropped_samples"),
        "mean_execution_us": (
            stats["sum_execution_units"] * 1e6 / hz / stats["samples"]
            if stats.get("samples") and "sum_execution_units" in stats
            else None
        ),
        "samples": stats["samples"],
        "deadline_misses": stats["deadline_misses"],
        "skipped_releases": stats["skipped_releases"],
        "sensor_errors": stats["sensor_errors"],
        "max_observed_lateness_us": stats[f"max_lateness_{suffix}"] * 1e6 / hz,
        "max_observed_response_us": stats[f"max_response_{suffix}"] * 1e6 / hz,
        "max_observed_execution_us": (stats["max_execution_units"] * 1e6 / hz if modern else None),
        "timing_quantum_us": 1e6 / hz,
        "histogram_bin_us": stats.get("histogram_bin_units", 1) * 1e6 / hz,
        "histogram_overflow_count": hist[-1],
        "workload_window_complete": bool(stats.get("complete", False)),
        "measurement_complete": complete and not (path / "error.json").exists(),
    }


def report(paths, output):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    summaries = [summarize(p) for p in paths]
    with (out / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (out / "summary.json").write_text(json.dumps(summaries, indent=2))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(3.5, 2.2), layout="constrained")
    for i, path in enumerate(paths):
        meta = json.loads((Path(path) / "metadata.json").read_text())
        stats = json.loads((Path(path) / "workload.json").read_text())
        if not stats["samples"]:
            continue
        raw_path = Path(path) / "workload-samples.csv"
        if raw_path.exists() and not stats.get("dropped_samples", 0):
            with raw_path.open() as f:
                raw = list(csv.DictReader(f))
            if len(raw) != stats["samples"]:
                raise ValueError("Raw sample count differs from workload total")
            xs = [0] + sorted(int(row["lateness_units"]) * 1e6 / stats["clock_hz"] for row in raw)
            ys = [0] + [(j + 1) / len(raw) for j in range(len(raw))]
        else:
            hist = stats["histogram"][:-1]
            bin_us = (
                stats.get("histogram_bin_units", 1)
                * 1e6
                / stats.get("clock_hz", meta["firmware"]["tick_hz"])
            )
            xs = [0] + [(j + 1) * bin_us for j in range(len(hist))]
            cumulative, ys = 0, [0]
            for n in hist:
                cumulative += n
                ys.append(cumulative / stats["samples"])
        style = ["-", "--", "-.", ":"][i % 4]
        ax.step(
            xs,
            ys,
            where="post",
            label=f"{meta['firmware']['variant']} {meta['condition']}",
            linestyle=style,
            color=str(0.1 + 0.12 * (i % 5)),
            linewidth=1,
        )
    ax.set(
        xlabel="Start lateness (microseconds)",
        ylabel="Cumulative fraction",
        ylim=(0, 1.01),
    )
    ax.legend(fontsize=5)
    ax.grid(alpha=0.15)
    fig.savefig(out / "coexistence.pdf")
    fig.savefig(out / "coexistence.png", dpi=300)
    plt.close(fig)
    return summaries
