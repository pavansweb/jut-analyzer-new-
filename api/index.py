from pathlib import Path
import json, shutil, zipfile

base = Path("/mnt/data/jee-analyser")
(base / "api").mkdir(parents=True, exist_ok=True)
(base / "data").mkdir(parents=True, exist_ok=True)

api = r'''import base64
import json
import os

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")          # owner/repo
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_DATA_PATH = os.environ.get("GITHUB_DATA_PATH", "data/jee_data.json")
API_WRITE_KEY = os.environ.get("API_WRITE_KEY")

DEFAULT_DATA = {"students": [], "tests": []}


def config_error():
    missing = []
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if not GITHUB_REPO:
        missing.append("GITHUB_REPO")
    return missing


def github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
    }


def github_url():
    return (
        f"{GITHUB_API}/repos/{GITHUB_REPO.strip().strip('/')}"
        f"/contents/{GITHUB_DATA_PATH.lstrip('/')}"
    )


def get_data():
    missing = config_error()
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))

    r = requests.get(
        github_url(),
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=10,
    )

    if r.status_code == 404:
        return DEFAULT_DATA.copy(), None

    r.raise_for_status()
    payload = r.json()
    raw = base64.b64decode(payload["content"].replace("\n", "")).decode("utf-8")
    return json.loads(raw), payload["sha"]


def save_data(data, old_sha, message):
    """Update the GitHub JSON file. Retry once if another write changed its SHA."""
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")

    for attempt in range(2):
        payload = {
            "message": message,
            "content": encoded,
            "branch": GITHUB_BRANCH,
            "committer": {
                "name": "JEE Analyser Bot",
                "email": "actions@users.noreply.github.com",
            },
        }
        if old_sha:
            payload["sha"] = old_sha

        r = requests.put(
            github_url(),
            headers=github_headers(),
            json=payload,
            timeout=15,
        )

        if r.status_code in (200, 201):
            return r.json()

        if r.status_code == 409 and attempt == 0:
            _, old_sha = get_data()
            continue

        r.raise_for_status()

    raise RuntimeError("Could not update GitHub data file.")


def require_write_key():
    if not API_WRITE_KEY:
        return jsonify({"error": "Server write key is not configured"}), 503

    if request.headers.get("X-API-Key") != API_WRITE_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    return None


def find_student(data, student_id):
    return next(
        (s for s in data.get("students", []) if s["id"] == student_id),
        None,
    )


def find_test(data, test_no):
    return next(
        (t for t in data.get("tests", []) if t["test_no"] == test_no),
        None,
    )


@app.get("/api/health")
def health():
    missing = config_error()
    return jsonify({
        "ok": not missing,
        "service": "jee-marks-analyser",
        "github_configured": not missing,
        "missing_config": missing,
    })


@app.get("/api/students")
def students():
    data, _ = get_data()
    return jsonify([
        {"id": s["id"], "name": s["name"]}
        for s in data.get("students", [])
    ])


@app.post("/api/students")
def add_student():
    auth = require_write_key()
    if auth:
        return auth

    body = request.get_json(silent=True) or {}
    student_id = str(body.get("id", "")).strip()
    name = str(body.get("name", "")).strip()

    if not student_id or not name:
        return jsonify({"error": "id and name are required"}), 400

    data, sha = get_data()

    if find_student(data, student_id):
        return jsonify({"error": "Student already exists"}), 409

    data.setdefault("students", []).append({"id": student_id, "name": name})
    data["students"].sort(key=lambda s: s["name"].lower())

    save_data(data, sha, f"Add student: {name}")
    return jsonify({"id": student_id, "name": name}), 201


@app.get("/api/tests")
def tests():
    data, _ = get_data()
    return jsonify([
        {
            "test_no": t["test_no"],
            "date": t.get("date"),
            "max_marks": t.get("max_marks", 300),
            "name": t.get("name"),
        }
        for t in sorted(data.get("tests", []), key=lambda x: x["test_no"])
    ])


@app.get("/api/scores")
def scores():
    student_id = request.args.get("student", "").strip()
    subject = request.args.get("subject", "consolidated").strip().lower()

    if not student_id:
        return jsonify({"error": "student query parameter is required"}), 400

    if subject not in {"physics", "chemistry", "maths", "consolidated"}:
        return jsonify({"error": "invalid subject"}), 400

    data, _ = get_data()

    if not find_student(data, student_id):
        return jsonify({"error": "Student not found"}), 404

    result = []

    for test in sorted(data.get("tests", []), key=lambda t: t["test_no"]):
        record = test.get("students", {}).get(student_id, {})

        if subject == "consolidated":
            marks = record.get("total")
            if marks is None and all(
                record.get(s) is not None
                for s in ("physics", "chemistry", "maths")
            ):
                marks = (
                    record["physics"]
                    + record["chemistry"]
                    + record["maths"]
                )
            max_marks = 300
        else:
            marks = record.get(subject)
            max_marks = 100

        if marks is None:
            continue

        entry = {
            "test_no": test["test_no"],
            "date": test.get("date"),
            "marks": marks,
            "max_marks": max_marks,
            "cumulative": False,
        }

        if subject == "consolidated" and record.get("rank") is not None:
            entry["rank"] = record["rank"]

        result.append(entry)

    # The supplied frontend expects a checkpoint row after every 10 tests.
    test_entries = list(result)
    for i in range(10, len(test_entries) + 1, 10):
        window = test_entries[i - 10:i]
        avg_marks = sum(x["marks"] for x in window) / len(window)
        avg_pct = sum(
            (x["marks"] / x["max_marks"]) * 100 for x in window
        ) / len(window)

        result.insert(i + ((i - 1) // 10), {
            "test_no": window[-1]["test_no"],
            "cumulative": True,
            "avg_marks": round(avg_marks, 2),
            "avg_percentage": round(avg_pct, 2),
            "tests_covered": f"{window[0]['test_no']}-{window[-1]['test_no']}",
        })

    return jsonify(result)


@app.post("/api/scores")
def add_score():
    auth = require_write_key()
    if auth:
        return auth

    body = request.get_json(silent=True) or {}
    student_id = str(body.get("student", "")).strip()
    subject = str(body.get("subject", "")).strip().lower()

    try:
        test_no = int(body.get("test_no"))
    except (TypeError, ValueError):
        return jsonify({"error": "test_no must be an integer"}), 400

    if not student_id:
        return jsonify({"error": "student is required"}), 400

    if subject not in {"physics", "chemistry", "maths", "consolidated"}:
        return jsonify({"error": "invalid subject"}), 400

    try:
        marks = float(body.get("marks"))
        if marks.is_integer():
            marks = int(marks)
    except (TypeError, ValueError):
        return jsonify({"error": "marks must be a number"}), 400

    if marks < 0:
        return jsonify({"error": "marks must be >= 0"}), 400

    default_max = 300 if subject == "consolidated" else 100
    try:
        max_marks = float(body.get("max_marks", default_max))
        if max_marks.is_integer():
            max_marks = int(max_marks)
        rank = body.get("rank")
        if rank is not None:
            rank = float(rank)
            if rank.is_integer():
                rank = int(rank)
    except (TypeError, ValueError):
        return jsonify({"error": "max_marks/rank must be numbers"}), 400

    if marks > max_marks or max_marks <= 0:
        return jsonify({"error": "marks must be between 0 and max_marks"}), 400

    data, sha = get_data()

    if not find_student(data, student_id):
        return jsonify({"error": "Student not found"}), 404

    test = find_test(data, test_no)

    if not test:
        test = {
            "test_no": test_no,
            "date": body.get("date"),
            "name": body.get("test_name"),
            "max_marks": 300,
            "students": {},
        }
        data.setdefault("tests", []).append(test)
    else:
        if body.get("date") is not None:
            test["date"] = body["date"]
        if body.get("test_name") is not None:
            test["name"] = body["test_name"]

    record = test.setdefault("students", {}).setdefault(student_id, {})

    if subject == "consolidated":
        record["total"] = marks
        if rank is not None:
            record["rank"] = rank
    else:
        record[subject] = marks

        if all(record.get(s) is not None for s in (
            "physics", "chemistry", "maths"
        )):
            record["total"] = (
                record["physics"]
                + record["chemistry"]
                + record["maths"]
            )

        if rank is not None:
            record["rank"] = rank

    data["tests"] = sorted(
        data.get("tests", []),
        key=lambda t: t["test_no"],
    )

    save_data(
        data,
        sha,
        f"Update {student_id} · test {test_no} · {subject}",
    )

    return jsonify({
        "ok": True,
        "student": student_id,
        "test_no": test_no,
        "subject": subject,
        "marks": marks,
        "rank": record.get("rank"),
        "total": record.get("total"),
    })


@app.get("/")
def root():
    return jsonify({
        "service": "JEE Marks Analyser API",
        "endpoints": [
            "/api/health",
            "/api/students",
            "/api/tests",
            "/api/scores?student=<id>&subject=<subject>",
        ],
    })


if __name__ == "__main__":
    app.run(debug=True)
'''

(base / "api/index.py").write_text(api)

(base / "requirements.txt").write_text(
    "Flask>=3.0,<4\nrequests>=2.31,<3\n"
)

(base / "vercel.json").write_text(
    '{\n  "rewrites": [\n    { "source": "/api/(.*)", "destination": "/api/index.py" }\n  ]\n}\n'
)

demo_data = {
    "students": [{"id": "demo", "name": "Demo Student"}],
    "tests": [{
        "test_no": 1,
        "date": "2026-08-01",
        "name": "Demo Mock",
        "max_marks": 300,
        "students": {
            "demo": {
                "physics": 72,
                "chemistry": 81,
                "maths": 65,
                "total": 218,
                "rank": 1234
            }
        }
    }]
}
(base / "data/jee_data.json").write_text(
    json.dumps(demo_data, indent=2) + "\n"
)

readme = """# JEE Marks Analyser API

Flask API for the supplied JEE marks analyser frontend.

## Environment variables

Set these in Vercel:

```text
GITHUB_TOKEN=...
GITHUB_REPO=OWNER/REPO
GITHUB_BRANCH=main
GITHUB_DATA_PATH=data/jee_data.json
API_WRITE_KEY=...
