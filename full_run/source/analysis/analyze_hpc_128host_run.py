#!/usr/bin/env python3

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from analyze_hpc_4mib_run import (FATAL_RE, LB_MODES, log_counter, nearest_rank,
                                  read_config, read_fct, read_trace)


ROOT = Path(__file__).resolve().parent.parent
BASE_RUNS = {"fecmp": "920400101", "letflow": "920400102", "conweave": "920400103"}
EXPECTED_HOSTS = list(range(128))
EXPECTED_FLOWS = 128 * 127
EXPECTED_SIZE = 4 * 1024 * 1024
EXPECTED_T0_NS = 2_010_000_000
STOP_NS = 2_200_000_000


def count_pfc(path):
    pauses = 0
    with path.open() as input_file:
        for line in input_file:
            fields = line.split()
            if len(fields) != 5:
                raise RuntimeError("invalid PFC row")
            pauses += int(fields[4]) == 1
    return pauses


def count_cnp(path):
    total = 0
    with path.open() as input_file:
        for line in input_file:
            fields = line.split()
            if len(fields) != 5:
                raise RuntimeError("invalid CNP row")
            total += int(fields[4])
    return total


def aggregate_voq(path):
    current_time = None
    queues = packets = max_queues = max_packets = 0
    with path.open() as input_file:
        for line in input_file:
            fields = line.split(",")
            if len(fields) != 4:
                raise RuntimeError("invalid VOQ row")
            timestamp, _tor, row_queues, row_packets = map(int, fields)
            if current_time is not None and timestamp != current_time:
                max_queues = max(max_queues, queues)
                max_packets = max(max_packets, packets)
                queues = packets = 0
            current_time = timestamp
            queues += row_queues
            packets += row_packets
    max_queues = max(max_queues, queues)
    max_packets = max(max_packets, packets)
    return max_queues, max_packets


def analyze_uplinks(path, start_ns, finish_ns, detail_path, run_id, algorithm):
    before = {}
    after = {}
    with path.open() as input_file:
        for line in input_file:
            fields = line.split(",")
            if len(fields) != 4:
                raise RuntimeError("invalid uplink row")
            timestamp, tor, interface, tx_bytes = map(int, fields)
            key = (tor, interface)
            if timestamp <= start_ns:
                before[key] = (timestamp, tx_bytes)
            elif timestamp >= finish_ns and key not in after:
                after[key] = (timestamp, tx_bytes)
    if set(before) != set(after) or len(before) != 64:
        raise RuntimeError("uplink samples do not bracket all 64 uplinks")
    rows = []
    by_tor = defaultdict(list)
    for tor, interface in sorted(before):
        first = before[(tor, interface)]
        last = after[(tor, interface)]
        delta = last[1] - first[1]
        if delta < 0:
            raise RuntimeError("uplink counter decreased")
        rows.append({
            "run_id": run_id, "algorithm": algorithm, "tor_id": tor,
            "interface": interface, "sample_start_ns": first[0],
            "sample_finish_ns": last[0], "transmitted_bytes": delta,
        })
        by_tor[tor].append(delta)
    with detail_path.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    imbalance = []
    for deltas in by_tor.values():
        average = sum(deltas) / len(deltas)
        if average > 0:
            imbalance.append((max(deltas) - min(deltas)) / average * 100.0)
    return sum(imbalance) / len(imbalance), sum(row["transmitted_bytes"] for row in rows)


def read_resources(path):
    values = {}
    for line in path.read_text().splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    required = ("runtime_seconds", "peak_process_tree_rss_kb", "command_exit_code")
    if any(key not in values for key in required):
        raise RuntimeError("resource file is incomplete")
    if int(values["command_exit_code"]) != 0:
        raise RuntimeError("measured simulation command failed")
    return float(values["runtime_seconds"]), int(values["peak_process_tree_rss_kb"])


def validate_config(config, algorithm, setting):
    required = {
        "TOPOLOGY_FILE": "config/leaf_spine_128_100G_OS2.txt",
        "FLOW_FILE": "config/hpc_128host/hpc_alltoall_sync_128host_4mib.txt",
        "FLOWGEN_START_TIME": "2.0",
        "FLOWGEN_STOP_TIME": "2.2",
        "CC_MODE": "1", "LB_MODE": LB_MODES[algorithm],
        "ENABLE_PFC": "1", "ENABLE_IRN": "0", "RANDOM_SEED": "1",
    }
    for key, expected in required.items():
        if config.get(key) != expected:
            raise RuntimeError("config mismatch {}={} expected {}".format(
                key, config.get(key), expected))
    extra = {"na": "4", "default": "4", "half": "2", "double": "8"}[setting]
    if config.get("CONWEAVE_REPLY_TIMEOUT_EXTRA") != extra:
        raise RuntimeError("ConWeave extra reply timeout mismatch")
    baseline = read_config(ROOT / "mix/output" / BASE_RUNS[algorithm] / "config.txt")
    ignored = {key for key in set(config) | set(baseline) if key.endswith("_FILE")}
    ignored.update(("FLOWGEN_STOP_TIME", "QLEN_MON_END", "CONWEAVE_REPLY_TIMEOUT_EXTRA"))
    for key in sorted((set(config) & set(baseline)) - ignored):
        if config[key] != baseline[key]:
            raise RuntimeError("parameter {} differs from accepted 4 MiB reference".format(key))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--algorithm", choices=tuple(LB_MODES), required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--reply-timeout-setting", choices=("na", "default", "half", "double"),
                        required=True)
    parser.add_argument("--resource-file", type=Path, required=True)
    args = parser.parse_args()

    output = ROOT / "mix/output" / args.run_id
    paths = {
        "config": output / "config.txt",
        "log": output / "config.log",
        "fct": output / (args.run_id + "_out_fct.txt"),
        "pfc": output / (args.run_id + "_out_pfc.txt"),
        "cnp": output / (args.run_id + "_out_cnp.txt"),
        "uplink": output / (args.run_id + "_out_uplink.txt"),
        "voq": output / (args.run_id + "_out_voq.txt"),
    }
    required = ["config", "log", "fct", "pfc", "cnp", "uplink"]
    if args.algorithm == "conweave":
        required.append("voq")
    for name in required:
        if not paths[name].is_file():
            raise RuntimeError("missing {} output".format(name))
    for name in ("config", "log", "fct", "uplink"):
        if paths[name].stat().st_size == 0:
            raise RuntimeError("required {} output is empty".format(name))

    trace_path = args.trace if args.trace.is_absolute() else ROOT / args.trace
    trace = read_trace(trace_path)
    expected_pairs = {(src, dst) for src in EXPECTED_HOSTS for dst in EXPECTED_HOSTS if src != dst}
    if len(trace) != EXPECTED_FLOWS or {(row[0], row[1]) for row in trace} != expected_pairs:
        raise RuntimeError("trace is not the complete 128-host directed all-to-all")
    if any(row[2] != 3 or row[3] != EXPECTED_SIZE or row[4] != EXPECTED_T0_NS for row in trace):
        raise RuntimeError("trace PG, size, or synchronized time mismatch")

    config = read_config(paths["config"])
    validate_config(config, args.algorithm, args.reply_timeout_setting)
    log_text = paths["log"].read_text(errors="replace")
    if FATAL_RE.search(log_text):
        raise RuntimeError("fatal signature in simulator log")
    marker = r"finished so far:\s*{0}/\s*total:\s*{0}".format(EXPECTED_FLOWS)
    if not re.search(marker, log_text):
        raise RuntimeError("missing exact completion marker")

    records = read_fct(paths["fct"])
    if len(records) != EXPECTED_FLOWS:
        raise RuntimeError("completed {} of {} flows".format(len(records), EXPECTED_FLOWS))
    expected = sorted((row[0], row[1], row[3], row[4]) for row in trace)
    actual = sorted((row["src"], row["dst"], row["size"], row["start_ns"]) for row in records)
    if actual != expected:
        raise RuntimeError("raw FCT flow set differs from trace")
    fcts = [row["fct_ns"] for row in records]
    finishes = [row["finish_ns"] for row in records]
    if any(fct <= 0 for fct in fcts) or max(finishes) > STOP_NS:
        raise RuntimeError("FCT values invalid or simulation duration insufficient")
    completion_us = (max(finishes) - EXPECTED_T0_NS) / 1000.0
    max_fct_us = max(fcts) / 1000.0
    if completion_us != max_fct_us:
        raise RuntimeError("synchronized completion and max FCT disagree")

    detail_path = output / (args.run_id + "_uplink_bytes.csv")
    imbalance, uplink_bytes = analyze_uplinks(
        paths["uplink"], EXPECTED_T0_NS, max(finishes), detail_path, args.run_id, args.algorithm)
    resource_path = args.resource_file if args.resource_file.is_absolute() else ROOT / args.resource_file
    runtime_seconds, peak_rss_kb = read_resources(resource_path)

    conweave = args.algorithm == "conweave"
    counters = {}
    if conweave:
        labels = {
            "reroutes": "Number of Rerouting",
            "ooo_packets": "Number of OoO enqueued pkts",
            "voq_flushes_total": "Number of VOQ Flush Total",
            "voq_flushes_tail": "Number of VOQ Flush by TAIL",
            "notify_count": "Number of NOTIFY Sent",
            "timely_clear_count": "Number of Timely CLEAR (TAIL's Reply)",
            "timely_rtt_reply_count": "Number of Timely RTT_REPLY (INIT's Reply)",
        }
        counters = {key: log_counter(log_text, label) for key, label in labels.items()}
        counters["voq_flushes_timeout"] = counters["voq_flushes_total"] - counters["voq_flushes_tail"]
        if counters["voq_flushes_timeout"] < 0:
            raise RuntimeError("VOQ flush counters inconsistent")
        max_queues, max_packets = aggregate_voq(paths["voq"])
    else:
        max_queues = max_packets = None

    summary = {
        "run_id": args.run_id, "algorithm": args.algorithm,
        "flow_size_bytes": EXPECTED_SIZE, "expected_flows": EXPECTED_FLOWS,
        "completed_flows": len(records), "phase_completion_time_us": completion_us,
        "mean_fct_us": sum(fcts) / len(fcts) / 1000.0,
        "p99_fct_us": nearest_rank(fcts, 99) / 1000.0, "max_fct_us": max_fct_us,
        "pfc_events": count_pfc(paths["pfc"]), "cnp_events": count_cnp(paths["cnp"]),
        "uplink_imbalance": imbalance, "uplink_transmitted_bytes": uplink_bytes,
        "uplink_detail_path": str(detail_path.relative_to(ROOT)),
        "runtime_seconds": runtime_seconds, "peak_process_tree_rss_kb": peak_rss_kb,
        "reply_timeout_setting": args.reply_timeout_setting,
        "reply_timeout_extra_us": ({"default": 4, "half": 2, "double": 8}.get(
            args.reply_timeout_setting) if conweave else None),
        "reroutes": counters.get("reroutes"), "ooo_packets": counters.get("ooo_packets"),
        "max_active_reorder_queues": max_queues,
        "max_reorder_occupancy_packets": max_packets,
        "voq_flushes_total": counters.get("voq_flushes_total"),
        "voq_flushes_tail": counters.get("voq_flushes_tail"),
        "voq_flushes_timeout": counters.get("voq_flushes_timeout"),
        "notify_count": counters.get("notify_count"),
        "timely_clear_count": counters.get("timely_clear_count"),
        "timely_rtt_reply_count": counters.get("timely_rtt_reply_count"),
        "trace_path": str(trace_path.relative_to(ROOT)),
        "topology": "leaf_spine_128_100G_OS2", "pfc": 1, "irn": 0,
        "random_seed": 1, "simulation_duration_s": 0.2,
        "log_fatal_error": False, "validation_status": "accepted",
    }
    for key in ("phase_completion_time_us", "mean_fct_us", "p99_fct_us", "max_fct_us",
                "uplink_imbalance", "runtime_seconds"):
        if not math.isfinite(summary[key]):
            raise RuntimeError("non-finite metric {}".format(key))
    if peak_rss_kb <= 0:
        raise RuntimeError("invalid peak RSS measurement")
    summary_path = output / (args.run_id + "_hpc_128host_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("accepted run={} algorithm={} flows={} completion_us={:.3f} runtime_s={:.3f} peak_rss_kb={} reroutes={}".format(
        args.run_id, args.algorithm, EXPECTED_FLOWS, completion_us, runtime_seconds,
        peak_rss_kb, summary["reroutes"]))


if __name__ == "__main__":
    main()
