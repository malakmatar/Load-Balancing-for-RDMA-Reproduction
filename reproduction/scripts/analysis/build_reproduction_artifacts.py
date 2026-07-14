#!/usr/bin/env python3

import csv
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = ROOT / "mix" / "output"
HISTORY = ROOT / "mix" / ".history"
WINDOW_START = 2_005_000_000
WINDOW_END = 2_150_000_000
FATAL_RE = re.compile(
    r"assert|assertion failed|segmentation fault|fatal error|aborted|core dumped|traceback",
    re.IGNORECASE,
)
LB_MODES = {
    "0": "fecmp",
    "2": "drill",
    "3": "conga",
    "6": "letflow",
    "9": "conweave",
}


def read_history():
    runs = []
    with HISTORY.open(newline="") as history_file:
        for row in csv.reader(history_file):
            if len(row) < 18 or row[0] == "date":
                continue
            runs.append(
                {
                    "run_id": row[1],
                    "algorithm": LB_MODES.get(row[3], "unknown-{}".format(row[3])),
                    "network_load": row[16],
                    "topology": row[13],
                    "pfc": row[9],
                    "irn": row[10],
                    "simulation_duration_s": row[17],
                }
            )
    return runs


def read_config(path):
    config = {}
    with path.open() as config_file:
        for line in config_file:
            fields = line.split()
            if len(fields) >= 2:
                config[fields[0]] = fields[1]
    return config


def line_count(path):
    if not path.is_file():
        return 0
    count = 0
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            count += chunk.count(b"\n")
    return count


def percentile(sorted_values, percentile_value):
    if not sorted_values:
        return math.nan
    index = int(math.ceil(percentile_value / 100.0 * len(sorted_values))) - 1
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


def slowdown_metrics(path):
    values = []
    with path.open() as fct_file:
        for line in fct_file:
            fields = line.split()
            if len(fields) < 8:
                continue
            start = int(fields[5])
            duration = int(fields[6])
            ideal = int(fields[7])
            if start > WINDOW_START and start + duration < WINDOW_END:
                values.append(max(1.0, duration / ideal))
    values.sort()
    return {
        "selected_flows": len(values),
        "average_slowdown": sum(values) / len(values) if values else math.nan,
        "p99_slowdown": percentile(values, 99),
    }


def main():
    runs = read_history()
    manifest_rows = []
    metric_rows = []

    for run in runs:
        run_id = run["run_id"]
        output = OUTPUT_ROOT / run_id
        config_path = output / "config.txt"
        fct_path = output / "{}_out_fct.txt".format(run_id)
        summary_path = output / "{}_out_fct_summary.txt".format(run_id)
        log_path = output / "config.log"
        config = read_config(config_path)
        log_text = log_path.read_text(errors="replace") if log_path.is_file() else ""
        config_match = all(
            (
                config.get("LOAD") == run["network_load"],
                config.get("ENABLE_PFC") == run["pfc"],
                config.get("ENABLE_IRN") == run["irn"],
                config.get("LB_MODE")
                == next((mode for mode, name in LB_MODES.items() if name == run["algorithm"]), None),
            )
        )
        manifest_rows.append(
            {
                **run,
                "output_path": str(output.relative_to(ROOT)),
                "fct_exists": fct_path.is_file(),
                "fct_line_count": line_count(fct_path),
                "fct_summary_exists": summary_path.is_file(),
                "fct_summary_line_count": line_count(summary_path),
                "config_exists": config_path.is_file(),
                "history_config_match": config_match,
                "log_exists": log_path.is_file(),
                "log_fatal_error": bool(FATAL_RE.search(log_text)),
            }
        )
        metric_rows.append({**run, **slowdown_metrics(fct_path)})

    manifest_fields = list(manifest_rows[0].keys())
    with (ROOT / "reproduction_manifest.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=manifest_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest_rows)

    metric_fields = list(metric_rows[0].keys())
    with (ROOT / "reproduction_metrics.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=metric_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(metric_rows)

    by_mode = {(row["pfc"], row["irn"], row["algorithm"]): row for row in metric_rows}
    comparison_rows = []
    for pfc, irn, flow_control in (("1", "0", "Lossless"), ("0", "1", "IRN")):
        conweave = by_mode[(pfc, irn, "conweave")]
        for baseline in ("fecmp", "letflow", "conga"):
            other = by_mode[(pfc, irn, baseline)]
            comparison_rows.append(
                {
                    "flow_control": flow_control,
                    "baseline": baseline,
                    "conweave_average": conweave["average_slowdown"],
                    "baseline_average": other["average_slowdown"],
                    "conweave_average_lower": conweave["average_slowdown"] < other["average_slowdown"],
                    "conweave_p99": conweave["p99_slowdown"],
                    "baseline_p99": other["p99_slowdown"],
                    "conweave_p99_lower": conweave["p99_slowdown"] < other["p99_slowdown"],
                }
            )
    fields = list(comparison_rows[0].keys())
    with (ROOT / "qualitative_comparison.csv").open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(comparison_rows)


if __name__ == "__main__":
    main()
