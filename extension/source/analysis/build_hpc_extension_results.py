#!/usr/bin/env python3

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "analysis"
MANIFEST = ROOT / "hpc_extension" / "hpc_extension_run_manifest.csv"
ALGORITHMS = ("fecmp", "letflow", "conweave")
SCENARIOS = (
    ("alltoall_sync", "Synchronized\nall-to-all"),
    ("alltoall_skew50us", "50 us skew\nall-to-all"),
    ("incast_sync", "Synchronized\nincast"),
)
COLORS = {"fecmp": "#3B6FB6", "letflow": "#D9822B", "conweave": "#2E8B57"}


def main():
    rows = []
    with MANIFEST.open(newline="") as manifest_file:
        for manifest_row in csv.DictReader(manifest_file):
            run_id = manifest_row["run_id"]
            summary_path = ROOT / manifest_row["output_path"] / (run_id + "_hpc_summary.json")
            summary = json.load(summary_path.open())
            exit_status = int((ROOT / manifest_row["exitcode_path"]).read_text().strip())
            rows.append(
                {
                    "run_id": run_id,
                    "workload": summary["workload"],
                    "synchronization": summary["synchronization"],
                    "algorithm": summary["algorithm"],
                    "participant_count": summary["participant_count"],
                    "flow_count": summary["flow_count"],
                    "message_size_bytes": summary["message_size_bytes"],
                    "start_skew_us": summary["start_skew_us"],
                    "completion_time_us": summary["completion_time_us"],
                    "mean_fct_us": summary["mean_fct_us"],
                    "max_fct_us": summary["max_fct_us"],
                    "p99_fct_us": summary["p99_fct_us"],
                    "completed_flows": summary["completed_flows"],
                    "pfc_events": summary["pfc_events"],
                    "max_queue_bytes": summary["max_queue_bytes"],
                    "reorder_buffer_bytes": summary["reorder_buffer_bytes"],
                    "reorder_buffer_packets": summary["reorder_buffer_packets"],
                    "reorder_queue_count": summary["reorder_queue_count"],
                    "mean_uplink_imbalance_percent": summary["mean_uplink_imbalance_percent"],
                    "exit_status": exit_status,
                    "validation_passed": summary["validation_passed"],
                }
            )

    expected = {(scenario, algorithm) for scenario, _label in SCENARIOS for algorithm in ALGORITHMS}
    actual = {(row["workload"], row["algorithm"]) for row in rows}
    if actual != expected or len(rows) != 9:
        raise RuntimeError("official result matrix is incomplete or duplicated")
    for row in rows:
        expected_count = 15 if row["workload"] == "incast_sync" else 240
        if row["exit_status"] != 0 or not row["validation_passed"]:
            raise RuntimeError("unvalidated run in result input")
        if row["flow_count"] != expected_count or row["completed_flows"] != expected_count:
            raise RuntimeError("incomplete flow count in result input")

    result_fields = list(rows[0].keys())
    with (ANALYSIS / "hpc_extension_results.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=result_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    by_key = {(row["workload"], row["algorithm"]): row for row in rows}
    normalized_rows = []
    for scenario, label in SCENARIOS:
        baseline = by_key[(scenario, "fecmp")]["completion_time_us"]
        for algorithm in ALGORITHMS:
            row = by_key[(scenario, algorithm)]
            normalized_rows.append(
                {
                    "workload": scenario,
                    "scenario_label": label.replace("\n", " "),
                    "algorithm": algorithm,
                    "completion_time_us": row["completion_time_us"],
                    "ecmp_completion_time_us": baseline,
                    "normalized_completion_time": row["completion_time_us"] / baseline,
                }
            )
    normalized_fields = list(normalized_rows[0].keys())
    with (ANALYSIS / "hpc_extension_normalized.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=normalized_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(normalized_rows)

    figure_dir = ANALYSIS / "figures"
    figure_dir.mkdir(exist_ok=True)
    x = list(range(len(SCENARIOS)))
    width = 0.24

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    for offset, algorithm in enumerate(ALGORITHMS):
        values = [by_key[(scenario, algorithm)]["completion_time_us"] /
                  by_key[(scenario, "fecmp")]["completion_time_us"]
                  for scenario, _label in SCENARIOS]
        positions = [value + (offset - 1) * width for value in x]
        ax.bar(positions, values, width, label=algorithm, color=COLORS[algorithm])
    ax.axhline(1.0, color="#333333", linewidth=1.0, linestyle="--")
    ax.set_ylabel("Completion time / ECMP")
    ax.set_xticks(x)
    ax.set_xticklabels([label for _scenario, label in SCENARIOS])
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(frameon=False, ncol=3, loc="upper left")
    fig.tight_layout()
    for suffix in ("png", "pdf"):
        fig.savefig(figure_dir / ("hpc_extension_completion_time." + suffix), dpi=180)
    plt.close(fig)

    fig, (mean_ax, tail_ax) = plt.subplots(1, 2, figsize=(10.0, 4.0))
    for offset, algorithm in enumerate(ALGORITHMS):
        positions = [value + (offset - 1) * width for value in x]
        means = [by_key[(scenario, algorithm)]["mean_fct_us"] for scenario, _label in SCENARIOS]
        tails = []
        for scenario, _label in SCENARIOS:
            row = by_key[(scenario, algorithm)]
            tails.append(row["max_fct_us"] if scenario == "incast_sync" else row["p99_fct_us"])
        mean_ax.bar(positions, means, width, label=algorithm, color=COLORS[algorithm])
        tail_ax.bar(positions, tails, width, label=algorithm, color=COLORS[algorithm])
    for ax, ylabel in ((mean_ax, "Mean FCT (us)"),
                       (tail_ax, "p99 FCT; incast max FCT (us)")):
        ax.set_xticks(x)
        ax.set_xticklabels([label for _scenario, label in SCENARIOS])
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", alpha=0.3)
    mean_ax.legend(frameon=False, ncol=3, loc="upper left")
    fig.tight_layout()
    for suffix in ("png", "pdf"):
        fig.savefig(figure_dir / ("hpc_extension_fct." + suffix), dpi=180)
    plt.close(fig)

    print("wrote {} validated result rows and {} normalized rows".format(
        len(rows), len(normalized_rows)))


if __name__ == "__main__":
    main()
