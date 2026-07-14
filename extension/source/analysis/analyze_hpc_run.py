#!/usr/bin/env python3

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FATAL_RE = re.compile(
    r"assert|assertion failed|segmentation fault|fatal error|aborted|core dumped|traceback|killed",
    re.IGNORECASE,
)
LB_MODES = {"fecmp": "0", "letflow": "6", "conweave": "9"}


def read_config(path):
    config = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2:
            config[fields[0]] = fields[1]
    return config


def seconds_to_ns(value):
    whole, fraction = value.split(".")
    return int(whole) * 1_000_000_000 + int((fraction + "000000000")[:9])


def read_trace(path):
    lines = path.read_text().splitlines()
    declared = int(lines[0])
    flows = []
    for line in lines[1:]:
        src, dst, pg, size, start = line.split()
        flows.append((int(src), int(dst), int(pg), int(size), seconds_to_ns(start)))
    if declared != len(flows):
        raise ValueError("trace declares {} flows but contains {}".format(declared, len(flows)))
    return flows


def read_fct(path):
    records = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 8:
            raise ValueError("invalid raw FCT row: {}".format(line))
        values = list(map(int, fields))
        records.append(
            {
                "src": values[0],
                "dst": values[1],
                "size": values[4],
                "start_ns": values[5],
                "fct_ns": values[6],
                "finish_ns": values[5] + values[6],
                "standalone_fct_ns": values[7],
            }
        )
    return records


def nearest_rank(values, percentile):
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100.0 * len(ordered)) - 1)
    return ordered[index]


def pfc_counts(path):
    if not path.is_file():
        return 0, 0
    total = 0
    pauses = 0
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) == 5:
            total += 1
            pauses += int(fields[4]) == 1
    return total, pauses


def voq_metrics(path):
    if not path.is_file():
        return None, None
    max_queues = 0
    max_packets = 0
    for line in path.read_text().splitlines():
        fields = line.split(",")
        if len(fields) == 4:
            max_queues = max(max_queues, int(fields[2]))
            max_packets = max(max_packets, int(fields[3]))
    return max_queues, max_packets


def uplink_imbalance(path, start_ns, finish_ns):
    if not path.is_file():
        return None
    samples = defaultdict(list)
    for line in path.read_text().splitlines():
        fields = line.split(",")
        if len(fields) != 4:
            continue
        timestamp, tor, interface, tx_bytes = map(int, fields)
        if start_ns <= timestamp <= finish_ns:
            samples[(tor, interface)].append((timestamp, tx_bytes))
    by_tor = defaultdict(list)
    for (tor, _interface), values in samples.items():
        values.sort()
        by_tor[tor].append(values[-1][1] - values[0][1])
    imbalances = []
    for deltas in by_tor.values():
        average = sum(deltas) / len(deltas)
        if average > 0:
            imbalances.append((max(deltas) - min(deltas)) / average * 100.0)
    return sum(imbalances) / len(imbalances) if imbalances else None


def main():
    parser = argparse.ArgumentParser(description="Validate and summarize one HPC extension run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workload", required=True,
                        choices=("alltoall_sync", "alltoall_skew50us", "incast_sync"))
    parser.add_argument("--algorithm", required=True, choices=tuple(LB_MODES))
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()

    output = ROOT / "mix" / "output" / args.run_id
    config_path = output / "config.txt"
    log_path = output / "config.log"
    fct_path = output / "{}_out_fct.txt".format(args.run_id)
    if not config_path.is_file() or not log_path.is_file() or not fct_path.is_file():
        raise RuntimeError("run {} is missing config, log, or raw FCT output".format(args.run_id))

    trace_path = args.trace if args.trace.is_absolute() else ROOT / args.trace
    trace_flows = read_trace(trace_path)
    records = read_fct(fct_path)
    config = read_config(config_path)
    log_text = log_path.read_text(errors="replace")

    expected_flow_file = str(trace_path.relative_to(ROOT))
    if config.get("FLOW_FILE") != expected_flow_file:
        raise RuntimeError("FLOW_FILE mismatch: {} != {}".format(
            config.get("FLOW_FILE"), expected_flow_file))
    if config.get("LB_MODE") != LB_MODES[args.algorithm]:
        raise RuntimeError("LB_MODE mismatch")
    if config.get("ENABLE_PFC") != "1" or config.get("ENABLE_IRN") != "0":
        raise RuntimeError("extension must use PFC=1 and IRN=0")
    if FATAL_RE.search(log_text):
        raise RuntimeError("fatal signature found in config.log")
    if len(records) != len(trace_flows):
        raise RuntimeError("completed {} flows, expected {}".format(len(records), len(trace_flows)))

    expected = sorted((src, dst, size, start_ns) for src, dst, _pg, size, start_ns in trace_flows)
    actual = sorted((row["src"], row["dst"], row["size"], row["start_ns"]) for row in records)
    if actual != expected:
        raise RuntimeError("raw FCT endpoints, sizes, or start times differ from the trace")
    completion_pattern = re.compile(
        r"finished so far:\s*{0}/\s*total:\s*{0}".format(len(trace_flows)))
    if not completion_pattern.search(log_text):
        raise RuntimeError("config.log lacks the exact completed-flow marker")

    starts = [row["start_ns"] for row in records]
    finishes = [row["finish_ns"] for row in records]
    fcts = [row["fct_ns"] for row in records]
    t0_ns = min(flow[4] for flow in trace_flows)
    if args.workload == "incast_sync":
        completion_time_ns = max(finishes) - t0_ns
        p99_fct_ns = None
        synchronization = "exact"
    else:
        completion_time_ns = max(finishes) - min(starts)
        p99_fct_ns = nearest_rank(fcts, 99)
        synchronization = "50us_skew" if args.workload == "alltoall_skew50us" else "exact"

    pfc_total, pfc_pauses = pfc_counts(output / "{}_out_pfc.txt".format(args.run_id))
    max_reorder_queues, max_reorder_packets = voq_metrics(
        output / "{}_out_voq.txt".format(args.run_id))
    summary = {
        "run_id": args.run_id,
        "workload": args.workload,
        "synchronization": synchronization,
        "algorithm": args.algorithm,
        "trace_path": expected_flow_file,
        "participant_count": len(set([row["src"] for row in records] +
                                     [row["dst"] for row in records])),
        "flow_count": len(trace_flows),
        "message_size_bytes": trace_flows[0][3],
        "start_skew_us": (max(starts) - min(starts)) / 1000.0,
        "t0_ns": t0_ns,
        "completion_time_us": completion_time_ns / 1000.0,
        "mean_fct_us": sum(fcts) / len(fcts) / 1000.0,
        "max_fct_us": max(fcts) / 1000.0,
        "p99_fct_us": None if p99_fct_ns is None else p99_fct_ns / 1000.0,
        "completed_flows": len(records),
        "pfc_events": pfc_pauses,
        "pfc_records_total": pfc_total,
        "max_queue_bytes": None,
        "reorder_buffer_bytes": None,
        "reorder_buffer_packets": max_reorder_packets,
        "reorder_queue_count": max_reorder_queues,
        "mean_uplink_imbalance_percent": uplink_imbalance(
            output / "{}_out_uplink.txt".format(args.run_id), min(starts), max(finishes)),
        "log_fatal_error": False,
        "validation_passed": True,
    }
    summary_path = output / "{}_hpc_summary.json".format(args.run_id)
    with summary_path.open("w") as summary_file:
        json.dump(summary, summary_file, indent=2, sort_keys=True)
        summary_file.write("\n")
    print("validated run={} workload={} algorithm={} flows={} completion_time_us={:.3f}".format(
        args.run_id, args.workload, args.algorithm, len(records), summary["completion_time_us"]))
    print("summary={}".format(summary_path))


if __name__ == "__main__":
    main()
