
import csv
import os
import re
import shlex
import signal
import shutil
import subprocess
import threading
import uuid

from collections import Counter
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_file
from tracer import trace_command, CSV_FILE

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "trace_logs"
LOG_DIR.mkdir(exist_ok=True)

# Commands permitted by this local demonstration application.
ALLOWED_COMMANDS = {
    "ls", "cat", "pwd", "uname", "whoami",
    "date", "id", "echo", "sleep", "head", "wc"
}

sessions = {}
sessions_lock = threading.Lock()
latest_result = None

SYSCALL_PATTERN = re.compile(
    r"^\s*(?:\d+\s+)?"
    r"(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\("
)

RESULT_PATTERN = re.compile(
    r"\)\s+=\s+(.+?)(?:\s+<([\d.]+)>)?\s*$"
)


def parse_trace_line(line):
    """Convert a completed strace record into structured data."""
    match = SYSCALL_PATTERN.match(line)

    if not match:
        return None

    if "<unfinished ...>" in line or line.lstrip().startswith("<..."):
        return None

    parsed = RESULT_PATTERN.search(line)
    if not parsed:
        return None

    value = parsed.group(1)
    duration = float(parsed.group(2) or 0)
    failed = value.startswith("-1 ")

    return {
        "system_call": match.group(1),
        "result": value,
        "status": "FAILED" if failed else "SUCCESS",
        "duration_seconds": duration,
        "raw_trace": line.strip()
    }


def make_summary(command, trace_path, returncode, stdout, stderr):
    records = []

    with trace_path.open("r", errors="replace") as trace:
        for line in trace:
            record = parse_trace_line(line)
            if record:
                records.append(record)

    counts = Counter(row["system_call"] for row in records)
    failures = sum(row["status"] == "FAILED" for row in records)

    with Path(CSV_FILE).open("w", newline="") as file:
        fields = [
            "system_call", "result", "status",
            "duration_seconds", "raw_trace"
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)

    return {
        "command": command,
        "records": records,
        "counts": counts,
        "total": len(records),
        "failures": failures,
        "successes": len(records) - failures,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode
    }


def run_session(session_id):
    """Wait for the trace process, enforce a timeout, and save results."""
    global latest_result

    session = sessions[session_id]
    process = session["process"]

    try:
        stdout, stderr = process.communicate(timeout=30)
        returncode = process.returncode
        timed_out = False

    except subprocess.TimeoutExpired:
        timed_out = True

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        stdout, stderr = process.communicate()
        returncode = process.returncode

    summary = make_summary(
        session["command"],
        session["trace_path"],
        returncode,
        stdout or "",
        stderr or ""
    )

    with sessions_lock:
        session["summary"] = summary
        session["timed_out"] = timed_out
        session["finished"] = True
        latest_result = summary


@app.route("/", methods=["GET", "POST"])
def dashboard():
    global latest_result

    command = latest_result["command"] if latest_result else "ls -la"
    error = ""

    # Keep a basic non-JavaScript fallback.
    if request.method == "POST":
        command = request.form.get("command", "").strip()
        try:
            latest_result = trace_command(command)
            return render_template(
                "index.html",
                result=latest_result,
                error="",
                command=command
            )
        except Exception as exc:
            error = str(exc)

    return render_template(
        "index.html",
        result=latest_result,
        error=error,
        command=command
    )


@app.route("/api/start", methods=["POST"])
def start_trace():
    command_text = request.json.get("command", "")
    if not isinstance(command_text, str):
        return jsonify({"error": "Command must be text."}), 400

    try:
        args = shlex.split(command_text)
    except ValueError:
        return jsonify({"error": "Invalid command syntax."}), 400

    if not args:
        return jsonify({"error": "Enter a command."}), 400

    if args[0] not in ALLOWED_COMMANDS:
        return jsonify({
            "error": "This demo allows: " + ", ".join(sorted(ALLOWED_COMMANDS))
        }), 400

    if any(token in command_text for token in (";", "|", "&", ">", "<", "`")):
        return jsonify({
            "error": "Shell operators are not allowed."
        }), 400

    tracer = shutil.which("strace")
    if not tracer:
        return jsonify({"error": "Install strace first."}), 500

    session_id = uuid.uuid4().hex
    trace_path = LOG_DIR / f"{session_id}.log"

    try:
        process = subprocess.Popen(
            [
                tracer, "-f", "-tt", "-T",
                "-o", str(trace_path), "--"
            ] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True
        )
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500

    session = {
        "command": command_text,
        "process": process,
        "trace_path": trace_path,
        "finished": False,
        "timed_out": False,
        "summary": None
    }

    with sessions_lock:
        sessions[session_id] = session

    threading.Thread(
        target=run_session,
        args=(session_id,),
        daemon=True
    ).start()

    return jsonify({
        "session_id": session_id,
        "command": command_text,
        "status": "running"
    })


@app.route("/api/status/<session_id>")
def trace_status(session_id):
    offset = request.args.get("offset", default=0, type=int)
    offset = max(0, offset)

    with sessions_lock:
        session = sessions.get(session_id)

    if not session:
        return jsonify({"error": "Trace session not found."}), 404

    # Read only newly appended bytes from the trace log.
    path = session["trace_path"]
    new_lines = []
    next_offset = offset

    if path.exists():
        with path.open("rb") as file:
            file.seek(offset)
            data = file.read()
            next_offset = file.tell()

        new_lines = data.decode("utf-8", errors="replace").splitlines()

    records = [
        record for line in new_lines
        if (record := parse_trace_line(line)) is not None
    ]

    with sessions_lock:
        finished = session["finished"]
        timed_out = session["timed_out"]
        summary = session["summary"]

    response = {
        "status": "finished" if finished else "running",
        "timed_out": timed_out,
        "lines": new_lines,
        "records": records,
        "offset": next_offset
    }

    if summary:
        response["summary"] = {
            "total": summary["total"],
            "successes": summary["successes"],
            "failures": summary["failures"],
            "returncode": summary["returncode"]
        }

    return jsonify(response)


@app.route("/api/stop/<session_id>", methods=["POST"])
def stop_trace(session_id):
    with sessions_lock:
        session = sessions.get(session_id)

    if not session:
        return jsonify({"error": "Trace session not found."}), 404

    if not session["finished"]:
        try:
            os.killpg(session["process"].pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    return jsonify({"status": "stopping"})


@app.route("/download")
def download():
    if not Path(CSV_FILE).exists():
        return "Run a trace before downloading a report.", 404

    return send_file(
        CSV_FILE,
        as_attachment=True,
        download_name="system_call_report.csv"
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
