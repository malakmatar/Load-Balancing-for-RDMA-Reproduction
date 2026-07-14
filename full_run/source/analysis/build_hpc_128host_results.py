#!/usr/bin/env python3

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "analysis"
STATE = ROOT / "hpc_128host"
RUNS = (
    ("920128101", "fecmp", "na", "alltoall_128host_fecmp"),
    ("920128102", "letflow", "na", "alltoall_128host_letflow"),
    ("920128103", "conweave", "default", "alltoall_128host_conweave_default"),
    ("920128104", "conweave", "half", "alltoall_128host_conweave_half"),
    ("920128105", "conweave", "double", "alltoall_128host_conweave_double"),
)
FIELDS = (
    "run_id", "algorithm", "flow_size_bytes", "reply_timeout_setting",
    "reply_timeout_extra_us", "exit_code", "expected_flows", "completed_flows",
    "phase_completion_time_us", "mean_fct_us", "p99_fct_us", "max_fct_us",
    "pfc_events", "cnp_events", "uplink_transmitted_bytes", "uplink_imbalance",
    "runtime_seconds", "peak_process_tree_rss_kb", "reroutes", "ooo_packets",
    "max_active_reorder_queues", "max_reorder_occupancy_packets",
    "voq_flushes_total", "voq_flushes_tail", "voq_flushes_timeout",
    "notify_count", "timely_clear_count", "timely_rtt_reply_count", "validation_status",
)


def main():
    rows = []
    uplinks = []
    manifests = []
    for run_id, algorithm, setting, label in RUNS:
        output = ROOT / "mix/output" / run_id
        summary_path = output / (run_id + "_hpc_128host_summary.json")
        exit_path = STATE / "exitcodes" / (label + ".exitcode")
        log_path = STATE / "logs" / (label + ".log")
        resource_path = STATE / "resources" / (label + ".txt")
        summary = json.loads(summary_path.read_text())
        exit_code = int(exit_path.read_text().strip())
        if exit_code != 0 or summary["validation_status"] != "accepted":
            raise RuntimeError("unaccepted run {}".format(run_id))
        if summary["completed_flows"] != 16256 or summary["flow_size_bytes"] != 4194304:
            raise RuntimeError("flow validation failed for {}".format(run_id))
        row = {field: summary.get(field, "") for field in FIELDS}
        row["exit_code"] = exit_code
        if algorithm != "conweave":
            row["reply_timeout_setting"] = "NA"
            row["reply_timeout_extra_us"] = "NA"
            for field in ("reroutes", "ooo_packets", "max_active_reorder_queues",
                          "max_reorder_occupancy_packets", "voq_flushes_total",
                          "voq_flushes_tail", "voq_flushes_timeout", "notify_count",
                          "timely_clear_count", "timely_rtt_reply_count"):
                row[field] = "NA"
        rows.append(row)
        with (output / (run_id + "_uplink_bytes.csv")).open(newline="") as input_file:
            uplinks.extend(csv.DictReader(input_file))
        manifests.append({
            "run_id": run_id, "algorithm": algorithm, "reply_timeout_setting": setting,
            "trace_path": summary["trace_path"], "expected_flows": 16256,
            "output_path": str(output.relative_to(ROOT)),
            "stdout_log": str(log_path.relative_to(ROOT)),
            "exitcode_path": str(exit_path.relative_to(ROOT)),
            "resource_path": str(resource_path.relative_to(ROOT)),
            "summary_path": str(summary_path.relative_to(ROOT)),
            "validation_status": "accepted",
        })

    with (ANALYSIS / "hpc_128host_results.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    with (ANALYSIS / "hpc_128host_uplink_bytes.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(uplinks[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(uplinks)
    with (STATE / "run_manifest.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(manifests[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifests)

    figures = ANALYSIS / "figures"
    figures.mkdir(exist_ok=True)
    main_rows = rows[:3]
    baseline = main_rows[0]["phase_completion_time_us"]
    normalized = [row["phase_completion_time_us"] / baseline for row in main_rows]
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    bars = ax.bar(["ECMP", "LetFlow", "ConWeave"], normalized,
                  color=["#3B6FB6", "#D9822B", "#2E8B57"], width=0.62)
    ax.axhline(1.0, color="#333333", linewidth=1.0, linestyle="--")
    ax.set_ylabel("Phase completion time / ECMP")
    ax.set_title("128-host synchronized all-to-all, 4 MiB per flow")
    ax.set_ylim(0, max(normalized) * 1.12)
    ax.grid(axis="y", alpha=0.25)
    for bar, value in zip(bars, normalized):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02,
                "{:.4f}".format(value), ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    for suffix in ("png", "pdf"):
        fig.savefig(figures / ("hpc_128host_normalized_completion." + suffix), dpi=180)
    plt.close(fig)

    timeout_rows = [rows[3], rows[2], rows[4]]
    labels = ["Half\n2 us", "Default\n4 us", "Double\n8 us"]
    fig, axes = plt.subplots(1, 3, figsize=(11.0, 4.0))
    metrics = (
        ("phase_completion_time_us", "Completion time (us)"),
        ("reroutes", "Reroutes"),
        ("max_reorder_occupancy_packets", "Max reorder occupancy (packets)"),
    )
    colors = ["#3B6FB6", "#2E8B57", "#D9822B"]
    for ax, (field, ylabel) in zip(axes, metrics):
        values = [row[field] for row in timeout_rows]
        ax.bar(labels, values, color=colors, width=0.62)
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", alpha=0.25)
        ax.ticklabel_format(axis="y", style="plain")
    fig.suptitle("ConWeave reply-timeout sensitivity")
    fig.tight_layout()
    for suffix in ("png", "pdf"):
        fig.savefig(figures / ("hpc_128host_timeout_sensitivity." + suffix), dpi=180)
    plt.close(fig)
    print("wrote {} accepted rows, {} uplink rows, and two figures".format(
        len(rows), len(uplinks)))


if __name__ == "__main__":
    main()
