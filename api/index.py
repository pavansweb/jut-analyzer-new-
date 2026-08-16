import base64
import json
import os

import requests
from flask import Flask, jsonify, render_template, request


# ============================================================
# FLASK APP
# ============================================================

app = Flask(
    __name__,
    template_folder="../templates"
)


# ============================================================
# GITHUB CONFIG
# ============================================================

GITHUB_API = "https://api.github.com"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_DATA_PATH = os.environ.get(
    "GITHUB_DATA_PATH",
    "data/jee_data.json"
)

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

MAX_MARKS = {
    "physics": 100,
    "chemistry": 100,
    "maths": 100,
    "consolidated": 300,
}


# ============================================================
# FRONTEND
# ============================================================

@app.route("/")
def home():
    """
    Main website.

    Flask renders templates/index.html.
    """

    return render_template("index.html")


# ============================================================
# GITHUB HELPERS
# ============================================================

def github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_url():
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPO}/contents/"
        f"{GITHUB_DATA_PATH}"
    )


def check_github_config():
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


def load_data():
    """
    Download jee_data.json from GitHub.
    """

    check_github_config()

    response = requests.get(
        github_url(),
        headers=github_headers(),
        params={
            "ref": GITHUB_BRANCH
        },
        timeout=10,
    )

    if response.status_code == 404:
        return {
            "students": [],
            "tests": [],
        }, None

    response.raise_for_status()

    payload = response.json()

    content = payload["content"]
    sha = payload["sha"]

    decoded = base64.b64decode(
        content.replace("\n", "")
    ).decode("utf-8")

    data = json.loads(decoded)

    data.setdefault("students", [])
    data.setdefault("tests", [])

    return data, sha


def save_data(data, sha, message):
    """
    Upload updated jee_data.json to GitHub.
    """

    check_github_config()

    content = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    encoded = base64.b64encode(
        content.encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": message,
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }

    if sha:
        payload["sha"] = sha

    response = requests.put(
        github_url(),
        headers=github_headers(),
        json=payload,
        timeout=15,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# AUTH FOR WRITE REQUESTS
# ============================================================

def check_write_key():

    if not API_WRITE_KEY:
        return jsonify({
            "error": "API_WRITE_KEY is not configured"
        }), 503

    provided = request.headers.get("X-API-Key")

    if provided != API_WRITE_KEY:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    return None


# ============================================================
# HEALTH
# ============================================================

@app.route("/api/health")
def health():

    missing = []

    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")

    if not GITHUB_REPO:
        missing.append("GITHUB_REPO")

    return jsonify({
        "ok": len(missing) == 0,
        "github_configured": len(missing) == 0,
        "missing": missing,
    })


# ============================================================
# STUDENTS
# ============================================================

@app.route("/api/students", methods=["GET"])
def get_students():

    try:

        data, _ = load_data()

        students = [
            {
                "id": student["id"],
                "name": student["name"],
            }
            for student in data["students"]
        ]

        return jsonify(students)

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


@app.route("/api/students", methods=["POST"])
def add_student():

    auth_error = check_write_key()

    if auth_error:
        return auth_error

    body = request.get_json(
        silent=True
    ) or {}

    student_id = str(
        body.get("id", "")
    ).strip()

    name = str(
        body.get("name", "")
    ).strip()

    if not student_id:
        return jsonify({
            "error": "id is required"
        }), 400

    if not name:
        return jsonify({
            "error": "name is required"
        }), 400

    try:

        data, sha = load_data()

        for student in data["students"]:

            if student["id"] == student_id:

                return jsonify({
                    "error": "Student already exists"
                }), 409

        student = {
            "id": student_id,
            "name": name,
        }

        data["students"].append(student)

        save_data(
            data,
            sha,
            f"Add student: {name}"
        )

        return jsonify({
            "ok": True,
            "student": student,
        }), 201

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


# ============================================================
# SCORES
# ============================================================

@app.route("/api/scores", methods=["GET"])
def get_scores():

    student_id = request.args.get(
        "student",
        ""
    ).strip()

    subject = request.args.get(
        "subject",
        "consolidated"
    ).strip().lower()

    if not student_id:

        return jsonify({
            "error": "student is required"
        }), 400

    if subject not in VALID_SUBJECTS:

        return jsonify({
            "error": "Invalid subject"
        }), 400

    try:

        data, _ = load_data()

        student_exists = any(
            student["id"] == student_id
            for student in data["students"]
        )

        if not student_exists:

            return jsonify({
                "error": "Student not found"
            }), 404

        results = []

        tests = sorted(
            data["tests"],
            key=lambda test: test["test_no"]
        )

        for test in tests:

            record = test.get(
                "students",
                {}
            ).get(
                student_id,
                {}
            )

            if subject == "consolidated":

                marks = record.get("total")

                if marks is None:

                    subjects = [
                        record.get("physics"),
                        record.get("chemistry"),
                        record.get("maths"),
                    ]

                    if all(
                        value is not None
                        for value in subjects
                    ):

                        marks = sum(subjects)

                max_marks = 300

            else:

                marks = record.get(subject)

                max_marks = MAX_MARKS[subject]

            if marks is None:
                continue

            result = {
                "test_no": test["test_no"],
                "date": test.get("date"),
                "marks": marks,
                "max_marks": max_marks,
                "cumulative": False,
            }

            if (
                subject == "consolidated"
                and record.get("rank") is not None
            ):

                result["rank"] = record["rank"]

            results.append(result)

        # ----------------------------------------------------
        # 10-test average rows
        # ----------------------------------------------------

        final_results = []

        for index, result in enumerate(
            results,
            start=1
        ):

            final_results.append(result)

            if index % 10 == 0:

                window = results[
                    index - 10:index
                ]

                average_marks = (
                    sum(
                        item["marks"]
                        for item in window
                    )
                    / len(window)
                )

                average_percentage = (
                    sum(
                        (
                            item["marks"]
                            / item["max_marks"]
                        ) * 100
                        for item in window
                    )
                    / len(window)
                )

                final_results.append({

                    "test_no": result["test_no"],

                    "cumulative": True,

                    "avg_marks": round(
                        average_marks,
                        2
                    ),

                    "avg_percentage": round(
                        average_percentage,
                        2
                    ),

                    "tests_covered":
                        f"{window[0]['test_no']}"
                        f"-"
                        f"{window[-1]['test_no']}",
                })

        return jsonify(final_results)

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


# ============================================================
# ADD / UPDATE SCORE
# ============================================================

@app.route("/api/scores", methods=["POST"])
def save_score():

    auth_error = check_write_key()

    if auth_error:
        return auth_error

    body = request.get_json(
        silent=True
    ) or {}

    student_id = str(
        body.get("student", "")
    ).strip()

    subject = str(
        body.get("subject", "")
    ).strip().lower()

    if not student_id:

        return jsonify({
            "error": "student is required"
        }), 400

    if subject not in VALID_SUBJECTS:

        return jsonify({
            "error": "Invalid subject"
        }), 400

    try:

        test_no = int(
            body.get("test_no")
        )

    except (TypeError, ValueError):

        return jsonify({
            "error": "test_no must be an integer"
        }), 400

    try:

        marks = float(
            body.get("marks")
        )

    except (TypeError, ValueError):

        return jsonify({
            "error": "marks must be a number"
        }), 400

    max_marks = float(
        body.get(
            "max_marks",
            MAX_MARKS[subject]
        )
    )

    if marks < 0:

        return jsonify({
            "error": "marks cannot be negative"
        }), 400

    if marks > max_marks:

        return jsonify({
            "error":
                "marks cannot exceed max_marks"
        }), 400

    rank = body.get("rank")

    if rank is not None:

        try:
            rank = float(rank)

        except (TypeError, ValueError):

            return jsonify({
                "error": "rank must be a number"
            }), 400

    try:

        data, sha = load_data()

        student_exists = any(
            student["id"] == student_id
            for student in data["students"]
        )

        if not student_exists:

            return jsonify({
                "error": "Student not found"
            }), 404

        # Find test.
        test = next(
            (
                test
                for test in data["tests"]
                if test["test_no"] == test_no
            ),
            None
        )

        # Create test if needed.
        if test is None:

            test = {
                "test_no": test_no,
                "date": body.get("date"),
                "name": body.get("test_name"),
                "max_marks": 300,
                "students": {},
            }

            data["tests"].append(test)

        else:

            if body.get("date") is not None:
                test["date"] = body["date"]

            if body.get("test_name") is not None:
                test["name"] = body["test_name"]

        test.setdefault(
            "students",
            {}
        )

        record = test["students"].setdefault(
            student_id,
            {}
        )

        # Save marks.
        if subject == "consolidated":

            record["total"] = (
                int(marks)
                if marks.is_integer()
                else marks
            )

        else:

            record[subject] = (
                int(marks)
                if marks.is_integer()
                else marks
            )

        # Calculate total automatically.
        if all(
            record.get(subject) is not None
            for subject in (
                "physics",
                "chemistry",
                "maths",
            )
        ):

            total = (
                record["physics"]
                + record["chemistry"]
                + record["maths"]
            )

            record["total"] = total

        # Save rank.
        if rank is not None:

            record["rank"] = (
                int(rank)
                if rank.is_integer()
                else rank
            )

        data["tests"].sort(
            key=lambda test:
                test["test_no"]
        )

        save_data(
            data,
            sha,
            (
                f"Update {student_id} "
                f"Test {test_no} "
                f"{subject}"
            )
        )

        return jsonify({
            "ok": True,
            "student": student_id,
            "test_no": test_no,
            "subject": subject,
            "marks": marks,
            "total": record.get("total"),
            "rank": record.get("rank"),
        })

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
    )
