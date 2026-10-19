#!/usr/bin/env python3
"""Bounded recording-capacity probes; never operate on existing data files."""

import argparse
import concurrent.futures
import datetime
import json
from pathlib import Path
import shlex
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["storage", "network", "ssh", "iperf", "snapshot", "model"])
    parser.add_argument("--host", default="ncoai@202.112.51.208")
    parser.add_argument("--tools", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--output", default="capacity-results")
    parser.add_argument("--iperf", help="Local iperf3 binary for native TCP probes")
    args = parser.parse_args()
    if not args.tools.startswith("/tmp/nco-capacity-"):
        parser.error("tools must be a dedicated /tmp/nco-capacity-* directory")
    if not args.data.startswith("/data/2027nco/.capacity-test-"):
        parser.error("data must be a dedicated .capacity-test-* directory")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "Compression=no", args.host]

    def remote(command, timeout=90):
        return subprocess.run(ssh + [command], capture_output=True, text=True,
                              timeout=timeout)

    def save(name, value):
        value["observed_at"] = datetime.datetime.now().astimezone().isoformat()
        (output / (name + ".json")).write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n")

    if args.suite == "storage":
        fio = args.tools + "/tools/usr/bin/fio"
        library = args.tools + "/tools/usr/lib/x86_64-linux-gnu"
        common = [fio, "--directory=" + args.data, "--direct=1",
                  "--group_reporting=1", "--output-format=json",
                  "--eta=never", "--end_fsync=1", "--fallocate=none"]
        workloads = [
            ("sequential-write", ["--name=sequential", "--filename=sequential.bin",
                                   "--rw=write", "--bs=1M", "--size=8G",
                                   "--ioengine=libaio", "--iodepth=16",
                                   "--runtime=35"]),
            ("sequential-read", ["--name=sequential", "--filename=sequential.bin",
                                  "--rw=read", "--bs=1M", "--size=8G",
                                  "--ioengine=libaio", "--iodepth=16",
                                  "--runtime=35", "--allow_file_create=0"]),
            ("parallel-32-write", ["--name=parallel", "--filename_format=parallel.$jobnum",
                                    "--rw=write", "--bs=256k", "--size=128M",
                                    "--numjobs=32", "--thread=1",
                                    "--ioengine=psync", "--runtime=35"]),
            ("recording-200-at-2mbps", ["--name=recording200",
                                         "--filename_format=recording200.$jobnum",
                                         "--rw=write", "--bs=64k", "--size=8M",
                                         "--numjobs=200", "--thread=1",
                                         "--ioengine=psync", "--rate=250000",
                                         "--time_based=1", "--runtime=20"]),
            ("recording-500-at-2mbps", ["--name=recording500",
                                         "--filename_format=recording500.$jobnum",
                                         "--rw=write", "--bs=64k", "--size=8M",
                                         "--numjobs=500", "--thread=1",
                                         "--ioengine=psync", "--rate=250000",
                                         "--time_based=1", "--runtime=20"]),
        ]
        for name, options in workloads:
            print("Starting " + name, flush=True)
            command = "env " + shlex.quote("LD_LIBRARY_PATH=" + library) + " "
            command += shlex.join(common + options)
            result = remote(command)
            try:
                parsed = json.loads(result.stdout)
            except json.JSONDecodeError:
                parsed = {"stdout": result.stdout}
            save(name, {"command": command, "returncode": result.returncode,
                        "stderr": result.stderr, "fio": parsed})
            for job in parsed.get("jobs", []):
                summary = {"test": name, "error": job.get("error")}
                for direction in ["read", "write"]:
                    stats = job[direction]
                    if stats.get("io_bytes", 0):
                        summary[direction + "_MBps"] = stats["bw_bytes"] / 1e6
                        summary[direction + "_iops"] = stats["iops"]
                        summary[direction + "_p99_ms"] = stats["clat_ns"].get(
                            "percentile", {}).get("99.000000", 0) / 1e6
                print(json.dumps(summary), flush=True)
            if result.returncode or any(j.get("error") for j in parsed.get("jobs", [])):
                print("Stopping storage suite after failure", flush=True)
                return 1

    elif args.suite == "network":
        base = ["curl", "-4", "--noproxy", "*", "-sS", "--connect-timeout", "5",
                "--max-time", "20", "--output", "/dev/null", "--write-out", "%{json}"]

        def request(direction, size, index):
            if direction == "ingress":
                cmd = shlex.join(base + ["https://speed.cloudflare.com/__down?bytes="
                                         + str(size)])
            else:
                upload = base + ["--request", "POST", "--data-binary", "@-",
                                 "https://speed.cloudflare.com/__up"]
                cmd = "head -c " + str(size) + " /dev/zero | " + shlex.join(upload)
            start = time.monotonic()
            result = remote(cmd, timeout=30)
            elapsed = time.monotonic() - start
            try:
                parsed = json.loads(result.stdout)
            except json.JSONDecodeError:
                parsed = {"stdout": result.stdout}
            parsed.pop("certs", None)
            valid = (result.returncode == 0 and parsed.get("http_code") == 200)
            return {"index": index, "elapsed_seconds": elapsed, "valid": valid,
                    "returncode": result.returncode, "stderr": result.stderr,
                    "curl": parsed}

        for direction, workers, size in [("ingress", 1, 64 * 1024**2),
                                          ("egress", 1, 32 * 1024**2),
                                          ("ingress", 4, 64 * 1024**2),
                                          ("egress", 4, 16 * 1024**2)]:
            name = "cloudflare-" + direction + "-" + str(workers)
            print("Starting " + name, flush=True)
            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                rows = list(pool.map(lambda i: request(direction, size, i), range(workers)))
            elapsed = time.monotonic() - start
            key = "size_download" if direction == "ingress" else "size_upload"
            total = sum(row["curl"].get(key, 0) for row in rows if row["valid"])
            record = {"direction_relative_to": "server", "workers": workers,
                      "wall_seconds_including_ssh": elapsed, "bytes": total,
                      "aggregate_Mbps_including_ssh": total * 8 / elapsed / 1e6,
                      "all_requests_valid": all(row["valid"] for row in rows),
                      "results": rows}
            save(name, record)
            print(json.dumps({k: v for k, v in record.items() if k != "results"}), flush=True)

    elif args.suite == "iperf":
        if not args.iperf:
            parser.error("--iperf is required for the native TCP probe")
        address = args.host.rsplit("@", 1)[-1]
        for direction, workers in [("ingress", 1), ("egress", 1),
                                    ("ingress", 4), ("egress", 4)]:
            name = "mac-iperf-" + direction + "-" + str(workers)
            print("Starting " + name, flush=True)
            command = shlex.join([
                "timeout", "25", "env", "LD_LIBRARY_PATH=" + args.tools
                + "/tools/usr/lib/x86_64-linux-gnu",
                args.tools + "/tools/usr/bin/iperf3", "-s", "-1", "-B", address,
                "-p", "52061", "-i", "1", "--forceflush"])
            server = subprocess.Popen(ssh + [command], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            preamble = []
            while True:
                line = server.stdout.readline()
                if not line:
                    raise RuntimeError("iperf3 server did not become ready: "
                                       + server.stderr.read())
                preamble.append(line)
                if "Server listening" in line:
                    break
            client_command = [args.iperf, "-c", address, "-p", "52061", "-t", "10",
                              "-P", str(workers), "--connect-timeout", "3000",
                              "--get-server-output", "-J"]
            if direction == "egress":
                client_command.append("-R")
            client = subprocess.run(client_command, capture_output=True, text=True,
                                    timeout=20)
            server_out, server_err = server.communicate(timeout=30)
            server_out = "".join(preamble) + server_out
            try:
                parsed = json.loads(client.stdout)
            except json.JSONDecodeError:
                parsed = {"stdout": client.stdout}
            record = {"direction_relative_to": "server", "workers": workers,
                      "returncode": client.returncode, "client": parsed,
                      "client_stderr": client.stderr, "server_stdout": server_out,
                      "server_stderr": server_err, "server_returncode": server.returncode}
            save(name, record)
            end = parsed.get("end", {})
            print(json.dumps({"test": name, "error": parsed.get("error"),
                              "receiver_Mbps": end.get("sum_received", {}).get(
                                  "bits_per_second", 0) / 1e6,
                              "retransmits": end.get("sum_sent", {}).get("retransmits")}),
                  flush=True)
            if client.returncode:
                print("Stopping native TCP suite after failure", flush=True)
                return 1

    elif args.suite == "snapshot":
        code = (
            "import os,json,pathlib,datetime; v=os.statvfs('/data'); "
            "p=pathlib.Path('/sys/class/net/eno5'); "
            "print(json.dumps({'time':datetime.datetime.now().astimezone().isoformat(),"
            "'total_bytes':v.f_blocks*v.f_frsize,"
            "'free_bytes_including_reserved':v.f_bfree*v.f_frsize,"
            "'available_bytes':v.f_bavail*v.f_frsize,"
            "'used_bytes':(v.f_blocks-v.f_bfree)*v.f_frsize,"
            "'inode_free':v.f_favail,'speed_Mbps':(p/'speed').read_text().strip(),"
            "'duplex':(p/'duplex').read_text().strip(),"
            "'network_stats':{f.name:int(f.read_text()) for f in (p/'statistics').iterdir()},"
            "'load':os.getloadavg(),'io_pressure':pathlib.Path('/proc/pressure/io').read_text()}))"
        )
        result = remote(shlex.join(["python3", "-c", code]))
        if result.returncode:
            raise RuntimeError(result.stderr)
        record = json.loads(result.stdout)
        save("server-snapshot", record)
        print(json.dumps(record, indent=2))

    elif args.suite == "model":
        snapshot = json.loads((output / "server-snapshot.json").read_text())
        accessible = snapshot["used_bytes"] + snapshot["available_bytes"]
        rows = []
        for people in [100, 200, 500]:
            for bitrate in [0.5, 1, 2]:
                one_copy = people * bitrate * 1e6 / 8 * 7 * 3600 * 2
                rows.append({
                    "people": people, "total_bitrate_per_person_Mbps": bitrate,
                    "required_ingress_Mbps_with_30pct_margin": people * bitrate * 1.3,
                    "recording_write_MBps": people * bitrate / 8,
                    "two_days_one_copy_TB": one_copy / 1e12,
                    "two_days_two_copies_with_25pct_margin_TB": one_copy * 2.5 / 1e12,
                })
        record = {
            "contest_days": 2, "hours_per_day": 7,
            "bitrates_include_audio": True,
            "recording_and_live_bitrates_equal_only_for_this_model": True,
            "additional_bytes_before_df_85pct": max(0, accessible * 0.85
                                                      - snapshot["used_bytes"]),
            "proposed_initial_recording_budget_bytes": 6 * 1024**4,
            "provisional_server_ingress_budget_Mbps": 400,
            "provisional_mac_route_ingress_budget_Mbps": 100,
            "scenarios": rows,
        }
        save("capacity-model", record)
        print(json.dumps(record, indent=2))

    else:
        total_per_worker = 64 * 1024**2

        def transfer(direction, index):
            start = time.monotonic()
            count = 0
            if direction == "ingress":
                process = subprocess.Popen(ssh + ["wc -c"], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                data = bytes(1024**2)
                while count < total_per_worker:
                    process.stdin.write(data)
                    count += len(data)
                process.stdin.close()
                response = process.stdout.read().decode().strip()
                error = process.stderr.read().decode()
                code = process.wait(timeout=20)
                if response != str(count):
                    raise RuntimeError("Receiver byte count mismatch: " + response)
            else:
                command = "head -c " + str(total_per_worker) + " /dev/zero"
                process = subprocess.Popen(ssh + [command], stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                while True:
                    block = process.stdout.read(1024**2)
                    if not block:
                        break
                    count += len(block)
                error = process.stderr.read().decode()
                code = process.wait(timeout=20)
            return {"index": index, "bytes": count, "returncode": code,
                    "seconds": time.monotonic() - start, "stderr": error}

        for direction, workers in [("ingress", 1), ("egress", 1),
                                    ("ingress", 4), ("egress", 4)]:
            name = "mac-ssh-" + direction + "-" + str(workers)
            print("Starting " + name, flush=True)
            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                rows = list(pool.map(lambda i: transfer(direction, i), range(workers)))
            elapsed = time.monotonic() - start
            total = sum(row["bytes"] for row in rows)
            record = {"direction_relative_to": "server", "workers": workers,
                      "bytes": total, "wall_seconds_including_ssh": elapsed,
                      "aggregate_Mbps_including_ssh": total * 8 / elapsed / 1e6,
                      "results": rows}
            save(name, record)
            print(json.dumps({k: v for k, v in record.items() if k != "results"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
