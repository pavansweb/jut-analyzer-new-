import base64
import json
import os
from functools import wraps

import requests
from flask import Flask, jsonify, request


# ============================================================
# APP
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

GITHUB_API = "https://api.github.com"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")          # owner/repository
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_DATA_PATH = os.environ.get(
    "GITHUB_DATA_PATH",
    "data/jee_data.json"
)

# Used to protect POST endpoints.
API_WRITE_KEY = os.environ.get("API_WRITE_KEY")


# ============================================================
# CONSTANTS
# ============================================================

VALID_SUBJECTS = {
    "physics",
    "chemistry",
    "maths",
    "consolidated",
}

SUBJECT_MAX_MARKS = {
    "physics": 100,
    "chemistry": 100,
    "maths": 100,
    "consolidated": 300,
}

EMPTY_DATA = {
    "students": [],
    "tests": [],
}


# ============================================================
# HELPERS
# ============================================================

def github_configured():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
    }


def github_file_url():
    repo = GITHUB_REPO.strip().strip("/")

    return (
        f"{GITHUB_API}/repos/{repo}"
        f"/contents/{GITHUB_DATA_PATH.lstrip('/')}"
    )


def json_error(message, status=400):
    return jsonify({
        "ok": False,
        "error": message,
    }), status


def require_github_config():
    missing = []

    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")

    if not GITHUB_REPO:
        missing.append("GITHUB_REPO")

    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )


def require_write_key():
    """
    Protects endpoints that modify GitHub data.

    Frontend/public users should NOT be given the GitHub token.
    They would only need the API write key if you expose POST
    requests from the frontend.
    """

    if not API_WRITE_KEY:
        return json_error(
            "API_WRITE_KEY is not configured on the server",
            503,
        )

    supplied_key = request.headers.get("X-API-Key")

    if supplied_key != API_WRITE_KEY:
        return json_error("Unauthorized", 401)

    return None


def get_github_data():
    """
    Read data/jee_data.json from GitHub.

    Returns:
        data, sha

    sha is needed by GitHub when updating the file.
    """

    require_github_config()

    response = requests.get(
        github_file_url(),
        headers=github_headers(),
        params={
            "ref": GITHUB_BRANCH,
        },
        timeout=10,
    )

    # File doesn't exist yet.
    if response.status_code == 404:
        return json.loads(json.dumps(EMPTY_DATA)), None

    response.raise_for_status()

    payload = response.json()

    content = payload.get("content", "")
    sha = payload.get("sha")

    if not content:
        return json.loads(json.dumps(EMPTY_DATA)), sha

    decoded = base64.b64decode(
        content.replace("\n", "")
    ).decode("utf-8")

    data = json.loads(decoded)

    # Protect against malformed/empty JSON structures.
    if not isinstance(data, dict):
        raise RuntimeError("GitHub data file must contain a JSON object")

    data.setdefault("students", [])
    data.setdefault("tests", [])

    return data, sha


def save_github_data(data, sha, commit_message):
    """
    Create/update the JSON file in GitHub.

    If another request changed the file between our GET and PUT,
    GitHub can return 409. We re-read the SHA and retry once.
    """

    require_github_config()

    body = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    encoded = base64.b64encode(
        body.encode("utf-8")
    ).decode("ascii")

    current_sha = sha

    for attempt in range(2):

        payload = {
            "message": commit_message,
            "content": encoded,
            "branch": GITHUB_BRANCH,
        }

        if current_sha:
            payload["sha"] = current_sha

        response = requests.put(
            github_file_url(),
            headers=github_headers(),
            json=payload,
            timeout=15,
        )

        if response.status_code in (200, 201):
            return response.json()

        # Somebody changed the file between GET and PUT.
        if response.status_code == 409 and attempt == 0:
            _, current_sha = get_github_data()
            continue

        response.raise_for_status()

    raise RuntimeError("Could not update GitHub data file")


def find_student(data, student_id):
    for student in data.get("students", []):
        if str(student.get("id")) == str(student_id):
            return student

    return None


def find_test(data, test_no):
    for test in data.get("tests", []):
        if int(test.get("test_no")) == int(test_no):
            return test

    return None


def clean_number(value):
    """
    Keep integers as integers in JSON responses.

    72.0 -> 72
    72.5 -> 72.5
    """

    if isinstance(value, float) and value.is_integer():
        return int(value)

    return value


def calculate_total(record):
    subjects = ("physics", "chemistry", "maths")

    if not all(record.get(subject) is not None for subject in subjects):
        return None

    return clean_number(
        record["physics"]
        + record["chemistry"]
        + record["maths"]
    )


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(_error):
    return json_error("Endpoint not found", 404)


@app.errorhandler(405)
def method_not_allowed(_error):
    return json_error("Method not allowed", 405)


@app.errorhandler(500)
def internal_error(_error):
    return json_error("Internal server error", 500)


# ============================================================
# HEALTH
# ============================================================

@app.route("/api/health", methods=["GET"])
def health():
    missing = []

    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")

    if not GITHUB_REPO:
        missing.append("GITHUB_REPO")

    return jsonify({
        "ok": len(missing) == 0,
        "service": "jee-marks-analyser",
        "github_configured": len(missing) == 0,
        "missing_config": missing,
    })


# ============================================================
# STUDENTS
# ============================================================

@app.route("/api/students", methods=["GET"])
def get_students():
    try:
        data, _ = get_github_data()

        students = []

        for student in data.get("students", []):
            students.append({
                "id": student["id"],
                "name": student["name"],
            })

        return jsonify(students)

    except requests.RequestException:
        return json_error(
            "Could not communicate with GitHub",
            502,
        )

    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/students", methods=["POST"])
def create_student():
    auth_error = require_write_key()

    if auth_error:
        return auth_error

    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return json_error("Request body must be JSON")

    student_id = str(body.get("id", "")).strip()
    name = str(body.get("name", "")).strip()

    if not student_id:
        return json_error("id is required")

    if not name:
        return json_error("name is required")

    if len(student_id) > 100:
        return json_error("id is too long")

    if len(name) > 200:
        return json_error("name is too long")

    try:
        data, sha = get_github_data()

        if find_student(data, student_id):
            return json_error(
                "Student already exists",
                409,
            )

        data.setdefault("students", []).append({
            "id": student_id,
            "name": name,
        })

        data["students"].sort(
            key=lambda student: student["name"].lower()
        )

        save_github_data(
            data,
            sha,
            f"Add student: {name}",
        )

        return jsonify({
            "ok": True,
            "student": {
                "id": student_id,
                "name": name,
            },
        }), 201

    except requests.RequestException:
        return json_error(
            "Could not communicate with GitHub",
            502,
        )

    except Exception as exc:
        return json_error(str(exc), 500)


# ============================================================
# TEST METADATA
# ============================================================

@app.route("/api/tests", methods=["GET"])
def get_tests():
    try:
        data, _ = get_github_data()

        tests = []

        for test in sorted(
            data.get("tests", []),
            key=lambda item: item["test_no"],
        ):
            tests.append({
                "test_no": test["test_no"],
                "date": test.get("date"),
                "name": test.get("name"),
                "max_marks": test.get(
                    "max_marks",
                    300,
                ),
            })

        return jsonify(tests)

    except requests.RequestException:
        return json_error(
            "Could not communicate with GitHub",
            502,
        )

    except Exception as exc:
        return json_error(str(exc), 500)


# ============================================================
# SCORES
# ============================================================

@app.route("/api/scores", methods=["GET"])
def get_scores():
    student_id = request.args.get(
        "student",
        "",
    ).strip()

    subject = request.args.get(
        "subject",
        "consolidated",
    ).strip().lower()

    if not student_id:
        return json_error(
            "student query parameter is required"
        )

    if subject not in VALID_SUBJECTS:
        return json_error(
            "subject must be one of: "
            "physics, chemistry, maths, consolidated"
        )

    try:
        data, _ = get_github_data()

        student = find_student(
            data,
            student_id,
        )

        if not student:
            return json_error(
                "Student not found",
                404,
            )

        result = []

        sorted_tests = sorted(
            data.get("tests", []),
            key=lambda item: item["test_no"],
        )

        for test in sorted_tests:

            test_no = test["test_no"]

            students_data = test.get(
                "students",
                {},
            )

            record = students_data.get(
                student_id,
                {},
            )

            if subject == "consolidated":

                marks = record.get("total")

                # Automatically calculate total if all three
                # subject scores exist.
                if marks is None:
                    marks = calculate_total(record)

                max_marks = 300

            else:

                marks = record.get(subject)
                max_marks = 100

            if marks is None:
                continue

            entry = {
                "test_no": test_no,
                "date": test.get("date"),
                "marks": clean_number(marks),
                "max_marks": max_marks,
                "cumulative": False,
            }

            # Rank belongs to the overall/consolidated result.
            if (
                subject == "consolidated"
                and record.get("rank") is not None
            ):
                entry["rank"] = clean_number(
                    record["rank"]
                )

            result.append(entry)

        # ----------------------------------------------------
        # Insert 10-test average checkpoints.
        #
        # The supplied frontend expects a cumulative object
        # after tests 10, 20, 30, etc.
        # ----------------------------------------------------

        final_result = []

        for index, entry in enumerate(result, start=1):

            final_result.append(entry)

            if index % 10 == 0:

                window = result[index - 10:index]

                avg_marks = (
                    sum(
                        item["marks"]
                        for item in window
                    )
                    / len(window)
                )

                avg_percentage = (
                    sum(
                        (
                            item["marks"]
                            / item["max_marks"]
                        ) * 100
                        for item in window
                    )
                    / len(window)
                )

                final_result.append({
                    "test_no": entry["test_no"],
                    "cumulative": True,
                    "avg_marks": round(
                        avg_marks,
                        2,
                    ),
                    "avg_percentage": round(
                        avg_percentage,
                        2,
                    ),
                    "tests_covered": (
                        f"{window[0]['test_no']}"
                        f"-"
                        f"{window[-1]['test_no']}"
                    ),
                })

        return jsonify(final_result)

    except requests.RequestException:
        return json_error(
            "Could not communicate with GitHub",
            502,
        )

    except Exception as exc:
        return json_error(str(exc), 500)


# ============================================================
# ADD / UPDATE SCORE
# ============================================================

@app.route("/api/scores", methods=["POST"])
def create_or_update_score():
    auth_error = require_write_key()

    if auth_error:
        return auth_error

    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return json_error("Request body must be JSON")

    student_id = str(
        body.get("student", "")
    ).strip()

    subject = str(
        body.get("subject", "")
    ).strip().lower()

    if not student_id:
        return json_error(
            "student is required"
        )

    if subject not in VALID_SUBJECTS:
        return json_error(
            "subject must be one of: "
            "physics, chemistry, maths, consolidated"
        )

    # --------------------------------------------------------
    # Test number
    # --------------------------------------------------------

    try:
        test_no = int(body.get("test_no"))
    except (TypeError, ValueError):
        return json_error(
            "test_no must be an integer"
        )

    if test_no < 1:
        return json_error(
            "test_no must be >= 1"
        )

    # --------------------------------------------------------
    # Marks
    # --------------------------------------------------------

    try:
        marks = float(body.get("marks"))
    except (TypeError, ValueError):
        return json_error(
            "marks must be a number"
        )

    if marks < 0:
        return json_error(
            "marks cannot be negative"
        )

    # --------------------------------------------------------
    # Maximum marks
    # --------------------------------------------------------

    default_max = SUBJECT_MAX_MARKS[subject]

    try:
        max_marks = float(
            body.get(
                "max_marks",
                default_max,
            )
        )
    except (TypeError, ValueError):
        return json_error(
            "max_marks must be a number"
        )

    if max_marks <= 0:
        return json_error(
            "max_marks must be greater than zero"
        )

    if marks > max_marks:
        return json_error(
            "marks cannot be greater than max_marks"
        )

    # --------------------------------------------------------
    # Rank
    # --------------------------------------------------------

    rank = body.get("rank")

    if rank is not None:

        try:
            rank = float(rank)
        except (TypeError, ValueError):
            return json_error(
                "rank must be a number"
            )

        if rank <= 0:
            return json_error(
                "rank must be greater than zero"
            )

    # --------------------------------------------------------
    # Date / test metadata
    # --------------------------------------------------------

    date = body.get("date")
    test_name = body.get("test_name")

    try:
        data, sha = get_github_data()

        # Student must exist.
        if not find_student(
            data,
            student_id,
        ):
            return json_error(
                "Student not found",
                404,
            )

        # Find existing test or create it.
        test = find_test(
            data,
            test_no,
        )

        if test is None:

            test = {
                "test_no": test_no,
                "date": date,
                "name": test_name,
                "max_marks": 300,
                "students": {},
            }

            data.setdefault(
                "tests",
                []
            ).append(test)

        else:

            if date is not None:
                test["date"] = date

            if test_name is not None:
                test["name"] = test_name

        # Make sure the students object exists.
        test.setdefault(
            "students",
            {}
        )

        record = test["students"].setdefault(
            student_id,
            {}
        )

        # ----------------------------------------------------
        # Save subject marks / total
        # ----------------------------------------------------

        if subject == "consolidated":

            record["total"] = clean_number(
                marks
            )

            if rank is not None:
                record["rank"] = clean_number(
                    rank
                )

        else:

            record[subject] = clean_number(
                marks
            )

            # Automatically calculate total whenever all
            # three subjects have been entered.
            calculated_total = calculate_total(
                record
            )

            if calculated_total is not None:
                record["total"] = calculated_total

            # Rank is overall rank, so keep it on the
            # student's test record.
            if rank is not None:
                record["rank"] = clean_number(
                    rank
                )

        # ----------------------------------------------------
        # Keep tests ordered.
        # ----------------------------------------------------

        data["tests"].sort(
            key=lambda item: item["test_no"]
        )

        save_github_data(
            data,
            sha,
            (
                f"Update {student_id} · "
                f"Test {test_no} · "
                f"{subject}"
            ),
        )

        return jsonify({
            "ok": True,
            "student": student_id,
            "test_no": test_no,
            "subject": subject,
            "marks": clean_number(marks),
            "rank": record.get("rank"),
            "total": record.get("total"),
        }), 200

    except requests.RequestException:
        return json_error(
            "Could not communicate with GitHub",
            502,
        )

    except Exception as exc:
        return json_error(
            str(exc),
            500,
        )


# ============================================================
# ROOT
# ============================================================

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "JEE Marks Analyser API",
        "status": "online",
        "endpoints": {
            "health": "/api/health",
            "students": "/api/students",
            "tests": "/api/tests",
            "scores": "/api/scores",
        },
    })


# ============================================================
# LOCAL DEVELOPMENT ONLY
# ============================================================

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )
