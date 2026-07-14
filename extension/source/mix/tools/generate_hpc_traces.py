#!/usr/bin/env python3

import argparse
import json
from collections import defaultdict
from pathlib import Path


FLOW_GROUP = 3
ALLTOALL_SIZE = 1024 * 1024
INCAST_SIZE = 4 * 1024 * 1024
T0_NS = 2_010_000_000
SKEW_NS = 50_000


def parse_topology(path):
    with path.open() as topology_file:
        header = topology_file.readline().split()
        if len(header) != 3:
            raise ValueError("invalid topology header")
        node_count, switch_count, link_count = map(int, header)
        switch_ids = list(map(int, topology_file.readline().split()))
        if len(switch_ids) != switch_count or len(set(switch_ids)) != switch_count:
            raise ValueError("invalid switch ID line")
        host_count = node_count - switch_count
        switch_set = set(switch_ids)
        host_to_racks = defaultdict(set)
        for link_index in range(link_count):
            fields = topology_file.readline().split()
            if len(fields) < 5:
                raise ValueError("missing or invalid link {}".format(link_index))
            left, right = map(int, fields[:2])
            if left < host_count and right in switch_set:
                host_to_racks[left].add(right)
            elif right < host_count and left in switch_set:
                host_to_racks[right].add(left)

    if set(host_to_racks) != set(range(host_count)):
        raise ValueError("every host must have exactly one rack link")
    if any(len(racks) != 1 for racks in host_to_racks.values()):
        raise ValueError("a host maps to multiple racks")
    host_to_rack = {host: next(iter(racks)) for host, racks in host_to_racks.items()}
    rack_to_hosts = defaultdict(list)
    for host, rack in host_to_rack.items():
        rack_to_hosts[rack].append(host)
    for hosts in rack_to_hosts.values():
        hosts.sort()
    if len(rack_to_hosts) != 8 or any(len(hosts) != 16 for hosts in rack_to_hosts.values()):
        raise ValueError("expected eight racks with sixteen hosts each")
    return host_to_rack, dict(sorted(rack_to_hosts.items()))


def format_seconds(timestamp_ns):
    return "{}.{:09d}".format(timestamp_ns // 1_000_000_000, timestamp_ns % 1_000_000_000)


def format_trace_seconds(timestamp_ns):
    # The loader parses a double and truncates to NS-3 time steps. A tenth-ns
    # guard keeps decimal values such as 2.010000000 from landing one ns early.
    return "{}.{:09d}1".format(timestamp_ns // 1_000_000_000,
                               timestamp_ns % 1_000_000_000)


def make_alltoall(participants, skew_ns):
    pairs = [(src, dst) for src in participants for dst in participants if src != dst]
    if len(pairs) != 240 or len(set(pairs)) != 240:
        raise ValueError("all-to-all pair set is not exactly 240 unique directed flows")
    flows = []
    for index, (src, dst) in enumerate(pairs):
        offset = 0 if skew_ns == 0 else (index * skew_ns) // (len(pairs) - 1)
        flows.append((src, dst, FLOW_GROUP, ALLTOALL_SIZE, T0_NS + offset))
    return flows


def make_incast(receiver, senders):
    flows = [(sender, receiver, FLOW_GROUP, INCAST_SIZE, T0_NS) for sender in senders]
    if len(flows) != 15 or len(set((flow[0], flow[1]) for flow in flows)) != 15:
        raise ValueError("incast is not exactly 15 unique sender-receiver flows")
    return flows


def validate_flows(name, flows, expected_count, expected_size, host_to_rack):
    if len(flows) != expected_count:
        raise ValueError("{} has {} flows, expected {}".format(name, len(flows), expected_count))
    pairs = set()
    last_start = None
    for src, dst, pg, size, start_ns in flows:
        if src == dst:
            raise ValueError("{} contains a self-flow".format(name))
        if src not in host_to_rack or dst not in host_to_rack:
            raise ValueError("{} contains a non-host endpoint".format(name))
        if (src, dst) in pairs:
            raise ValueError("{} contains a duplicate pair".format(name))
        if pg != FLOW_GROUP or size != expected_size or not isinstance(start_ns, int):
            raise ValueError("{} contains an invalid flow field".format(name))
        if last_start is not None and start_ns < last_start:
            raise ValueError("{} is not sorted by start time".format(name))
        pairs.add((src, dst))
        last_start = start_ns


def write_trace(path, flows):
    with path.open("w") as trace_file:
        trace_file.write("{}\n".format(len(flows)))
        for src, dst, pg, size, start_ns in flows:
            trace_file.write("{} {} {} {} {}\n".format(
                src, dst, pg, size, format_trace_seconds(start_ns)))


def main():
    parser = argparse.ArgumentParser(description="Generate deterministic ConWeave HPC traces")
    parser.add_argument("--topology", type=Path,
                        default=Path("config/leaf_spine_128_100G_OS2.txt"))
    parser.add_argument("--output-dir", type=Path, default=Path("config/hpc_extension"))
    args = parser.parse_args()

    host_to_rack, rack_to_hosts = parse_topology(args.topology)
    rack_ids = sorted(rack_to_hosts)
    participants = [host for rack in rack_ids for host in rack_to_hosts[rack][:2]]
    if len(participants) != 16 or any(
            sum(host_to_rack[host] == rack for host in participants) != 2 for rack in rack_ids):
        raise ValueError("participant placement is not two hosts per rack")

    receiver = rack_to_hosts[rack_ids[0]][0]
    sender_racks = rack_ids[1:]
    senders = []
    for index, rack in enumerate(sender_racks):
        take = 3 if index == 0 else 2
        senders.extend(rack_to_hosts[rack][:take])
    sender_counts = [sum(host_to_rack[host] == rack for host in senders) for rack in sender_racks]
    if len(senders) != 15 or receiver in senders or max(sender_counts) - min(sender_counts) > 1:
        raise ValueError("incast sender placement is invalid")

    traces = {
        "hpc_alltoall_sync": make_alltoall(participants, 0),
        "hpc_alltoall_skew50us": make_alltoall(participants, SKEW_NS),
        "hpc_incast_sync": make_incast(receiver, senders),
    }
    validate_flows("hpc_alltoall_sync", traces["hpc_alltoall_sync"], 240,
                   ALLTOALL_SIZE, host_to_rack)
    validate_flows("hpc_alltoall_skew50us", traces["hpc_alltoall_skew50us"], 240,
                   ALLTOALL_SIZE, host_to_rack)
    validate_flows("hpc_incast_sync", traces["hpc_incast_sync"], 15,
                   INCAST_SIZE, host_to_rack)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, flows in traces.items():
        write_trace(args.output_dir / (name + ".txt"), flows)

    metadata = {
        "topology": str(args.topology),
        "t0_ns": T0_NS,
        "t0_seconds": format_seconds(T0_NS),
        "priority_group": FLOW_GROUP,
        "host_to_rack": {str(host): rack for host, rack in sorted(host_to_rack.items())},
        "traces": {
            "hpc_alltoall_sync": {
                "participants": participants,
                "participant_racks": {str(host): host_to_rack[host] for host in participants},
                "receiver": None,
                "flow_count": 240,
                "message_size_bytes": ALLTOALL_SIZE,
                "skew_interval_ns": 0,
                "seed": None,
            },
            "hpc_alltoall_skew50us": {
                "participants": participants,
                "participant_racks": {str(host): host_to_rack[host] for host in participants},
                "receiver": None,
                "flow_count": 240,
                "message_size_bytes": ALLTOALL_SIZE,
                "skew_interval_ns": SKEW_NS,
                "seed": None,
            },
            "hpc_incast_sync": {
                "participants": senders + [receiver],
                "participant_racks": {
                    str(host): host_to_rack[host] for host in senders + [receiver]
                },
                "receiver": receiver,
                "senders": senders,
                "flow_count": 15,
                "message_size_bytes": INCAST_SIZE,
                "skew_interval_ns": 0,
                "seed": None,
            },
        },
    }
    metadata_path = args.output_dir / "hpc_workloads_metadata.json"
    with metadata_path.open("w") as metadata_file:
        json.dump(metadata, metadata_file, indent=2, sort_keys=True)
        metadata_file.write("\n")

    for name, flows in traces.items():
        starts = [flow[4] for flow in flows]
        print("{}: flows={} size={} start_ns=[{},{}]".format(
            name, len(flows), flows[0][3], min(starts), max(starts)))
    print("participants={}".format(",".join(map(str, participants))))
    print("incast_receiver={} senders={}".format(receiver, ",".join(map(str, senders))))
    print("metadata={}".format(metadata_path))


if __name__ == "__main__":
    main()
