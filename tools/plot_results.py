#!/usr/bin/env python3
"""Plot scheduling and approval latency from a fido-bench evaluation."""

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, PercentFormatter


SERIES = (
    ("application_higher", "Application higher priority", "C0", "o", "-"),
    ("fido_higher", "FIDO2 higher priority", "C1", "s", "--"),
)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def is_true(value):
    return str(value).lower() in {"1", "true"}


def number(row, key):
    value = float(row[key])
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid {key}: {row[key]!r}")
    return value


def configure_style():
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["DejaVu Serif"],
            "font.size": 9,
            "axes.labelsize": 9,
            "axes.titlesize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.linewidth": 0.6,
            "lines.linewidth": 1.3,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.dpi": 300,
        }
    )


def save_figure(fig, output, name):
    for suffix in ("pdf", "png"):
        path = output / f"{name}.{suffix}"
        fig.savefig(path, facecolor="white")
        print(path)
    plt.close(fig)


def load_scheduling(root):
    calibration = read_json(root / "calibration" / "calibration.json")
    loads = sorted(calibration["loads"], key=lambda item: item["calibrated_fraction"])
    rows = read_csv(root / "scheduling-report" / "runs.csv")
    groups = {}

    for priority, _, _, _, _ in SERIES:
        for load in loads:
            group = [
                row
                for row in rows
                if row["priority_order"] == priority
                and row["load"] == load["name"]
                and row["condition"] == "active"
            ]
            if len(group) != 3 or {row["repetition"] for row in group} != {
                "1",
                "2",
                "3",
            }:
                raise ValueError(
                    f"Expected three repetitions: {priority}/{load['name']}"
                )
            for row in group:
                for field in (
                    "measurement_complete",
                    "raw_samples_complete",
                    "workload_window_complete",
                ):
                    if not is_true(row[field]):
                        raise ValueError(f"Incomplete {field}: {row['run']}")
                if int(row["dropped_samples"]) != 0:
                    raise ValueError(f"Dropped workload samples: {row['run']}")
                if row["latency_metric"] != "event_excluded_ns":
                    raise ValueError(f"Unexpected latency metric: {row['run']}")
                if not math.isclose(
                    float(row["calibrated_fraction"]), load["calibrated_fraction"]
                ):
                    raise ValueError(f"Calibration mismatch: {row['run']}")
                number(row, "p99_response_us")
                number(row, "median_ms")
            groups[priority, load["name"]] = group

    return loads, groups, float(calibration["build"]["deadline_ms"])


def plot_scheduling(data, output):
    loads, groups, deadline_ms = data
    x = [100 * load["calibrated_fraction"] for load in loads]
    labels = [f"{value:.2f}" if value < 1 else f"{value:.1f}" for value in x]
    fig, axes = plt.subplots(1, 2, figsize=(7.16, 2.75))
    fig.subplots_adjust(left=0.085, right=0.985, bottom=0.21, top=0.79, wspace=0.36)

    panels = (
        (
            "p99_response_us",
            0.001,
            "p99 response time (ms)",
            "(a) Application response",
        ),
        ("median_ms", 1.0, "Median approval latency (ms)", "(b) Approval latency"),
    )
    for ax, (metric, scale, ylabel, title) in zip(axes, panels):
        for priority, label, color, marker, linestyle in SERIES:
            values = [
                [number(row, metric) * scale for row in groups[priority, load["name"]]]
                for load in loads
            ]
            centers = [median(items) for items in values]
            lower = [center - min(items) for center, items in zip(centers, values)]
            upper = [max(items) - center for center, items in zip(centers, values)]
            ax.errorbar(
                x,
                centers,
                yerr=[lower, upper],
                label=label,
                color=color,
                marker=marker,
                linestyle=linestyle,
                markersize=4,
                markerfacecolor="white",
                capsize=3,
                elinewidth=0.9,
                clip_on=False,
            )
        ax.set_xticks(x, labels)
        ax.set_xlabel("Calibrated application load (%)")
        ax.set_ylabel(ylabel)
        ax.set_title(title, pad=7)
        ax.set_ylim(bottom=0)
        ax.set_xlim(min(x) - 4, max(x) + 4)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
        ax.grid(axis="y", color="0.88", linewidth=0.5)
        ax.set_axisbelow(True)

    axes[0].axhline(deadline_ms, color="0.4", linestyle=":", linewidth=1)
    axes[0].annotate(
        f"{deadline_ms:g} ms deadline",
        xy=(0.97, deadline_ms),
        xycoords=("axes fraction", "data"),
        xytext=(0, 4),
        textcoords="offset points",
        ha="right",
        color="0.35",
        fontsize=7.5,
    )
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.99),
        ncol=2,
        frameon=False,
        columnspacing=2.2,
        handlelength=2.5,
    )
    save_figure(fig, output, "scheduling_load")


def load_latency(root, backend):
    path = root / backend / "latency"
    meta = read_json(path / "metadata.json")
    if meta["latency_metric"] != "event_excluded_ns":
        raise ValueError(f"Unexpected latency metric: {path}")
    if not read_json(path / "collection.json")["completed"]:
        raise ValueError(f"Incomplete latency run: {path}")
    rows = read_csv(path / "latency.csv")
    measured = [row for row in rows if not is_true(row["warmup"])]
    if len(measured) != meta["trials"]:
        raise ValueError(f"Unexpected trial count: {path}")
    successful = [row for row in measured if is_true(row["success"])]
    if any(row["workload_overlap"] not in {"0", "1"} for row in successful):
        raise ValueError(f"Unknown workload overlap: {path}")
    included = [row for row in successful if is_true(row["workload_overlap"])]
    values = sorted(number(row, "event_excluded_ns") / 1e6 for row in included)
    if not values:
        raise ValueError(f"No measured latency samples: {path}")
    print(
        f"{backend}: {len(values)} included, "
        f"{len(measured) - len(successful)} failed, "
        f"{len(successful) - len(included)} outside the workload window"
    )
    return values


def plot_latency(data, output):
    fig, ax = plt.subplots(figsize=(3.5, 2.55))
    fig.subplots_adjust(left=0.19, right=0.98, bottom=0.21, top=0.84)
    all_values = [value for values in data.values() for value in values]
    span = max(all_values) - min(all_values)
    pad = max(span * 0.06, 1.0)
    left, right = min(all_values) - pad, max(all_values) + pad

    for (label, values), color, linestyle in zip(
        data.items(), ("C0", "C1"), ("-", "--")
    ):
        x = [left, *values, right]
        y = [0, *[(i + 1) / len(values) for i in range(len(values))], 1]
        ax.step(x, y, where="post", color=color, linestyle=linestyle, label=label)
    ax.set_xlim(left, right)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Approval latency (ms)")
    ax.set_ylabel("Cumulative proportion")
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.grid(color="0.9", linewidth=0.5)
    ax.set_axisbelow(True)
    fig.legend(
        *ax.get_legend_handles_labels(),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.99),
        ncol=2,
        frameon=False,
        columnspacing=1.8,
        handlelength=2.5,
    )
    save_figure(fig, output, "approval_latency_ecdf")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="Evaluation directory")
    parser.add_argument("--out", type=Path, default=Path("figures"))
    args = parser.parse_args()

    scheduling = load_scheduling(args.results)
    latency = {
        "Settings": load_latency(args.results, "settings"),
        "PSA PS": load_latency(args.results, "psa"),
    }
    configure_style()
    args.out.mkdir(parents=True, exist_ok=True)
    plot_scheduling(scheduling, args.out)
    plot_latency(latency, args.out)


if __name__ == "__main__":
    main()
