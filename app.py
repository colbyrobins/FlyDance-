import os
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DANCECOMP_DB_PATH", "/data/dancecomp.db"))

app = Flask(__name__)


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS awards (
                entry_num INTEGER PRIMARY KEY,
                award_text TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


@app.before_request
def ensure_db() -> None:
    if app.config.get("_db_ready"):
        return
    init_db()
    app.config["_db_ready"] = True


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/Phoenix, AZ (1).html")
def legacy_page():
    return send_from_directory(BASE_DIR, "Phoenix, AZ (1).html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/awards")
def get_awards():
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT entry_num, award_text FROM awards ORDER BY entry_num"
        ).fetchall()
    return jsonify({str(row["entry_num"]): row["award_text"] for row in rows})


@app.put("/api/awards/<int:entry_num>")
def put_award(entry_num: int):
    payload = request.get_json(silent=True) or {}
    award = payload.get("award", "")
    if not isinstance(award, str):
        return jsonify({"error": "award must be a string"}), 400
    if len(award) > 200:
        return jsonify({"error": "award cannot exceed 200 characters"}), 400

    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO awards (entry_num, award_text, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(entry_num)
            DO UPDATE SET
                award_text = excluded.award_text,
                updated_at = CURRENT_TIMESTAMP
            """,
            (entry_num, award),
        )
        conn.commit()

    return jsonify({"entry_num": entry_num, "award": award})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
