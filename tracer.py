
import csv
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TRACE_FILE = BASE_DIR / "trace.log"
CSV_FILE = BASE_DIR / "report.csv"


def trace_command(command):
    tracer = shutil.which("strace")
    if not tracer:
        raise RuntimeError("strace is not installed.")

    if isinstance(command, str):
        import shlex
        command = shlex.split(command)

    if not command:
        raise ValueError("Enter a command to trace.")

    try:
        result = subprocess.run(
            [tracer, "-f", "-tt", "-T", "-o",
             str(TRACE_FILE), "--"] + command,
            capture_output=True,
            text=True,
            timeout=30
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Command exceeded the 30-second limit.")

    records = []
    counts = Counter()

    pattern = re.compile(
        r"^\s*(?:\d+\s+)?"
        r"(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?"
        r"([A-Za-z_][A-Za-z0-9_]*)\("
    )

    with TRACE_FILE.open("r", errors="replace") as trace:
        for line in trace:
            match = pattern.match(line)
            if not match:
                continue

            if "<unfinished ...>" in line or line.lstrip().startswith("<..."):
                continue

            parsed = re.search(
                r"\)\s+=\s+(.+?)(?:\s+<([\d.]+)>)?\s*$",
                line
            )
            if not parsed:
                continue

            name = match.group(1)
            value = parsed.group(1)
            duration = float(parsed.group(2) or 0)
            failed = value.startswith("-1 ")

            records.append({
                "system_call": name,
                "result": value,
                "status": "FAILED" if failed else "SUCCESS",
                "duration_seconds": duration,
                "raw_trace": line.strip()
            })
            counts[name] += 1

    with CSV_FILE.open("w", newline="") as file:
        fields = [
            "system_call", "result", "status",
            "duration_seconds", "raw_trace"
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)

    failures = sum(r["status"] == "FAILED" for r in records)

    return {
        "command": " ".join(command),
        "records": records,
        "counts": counts,
        "total": len(records),
        "failures": failures,
        "successes": len(records) - failures,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode
    }
