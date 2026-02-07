import io
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DANCECOMP_DB_PATH", "/data/dancecomp.db"))
PIN_GRANT_TTL_SECONDS = 12 * 60 * 60
PIN_ATTEMPT_WINDOW_SECONDS = 10 * 60
PIN_ATTEMPT_LIMIT = 5
DEFAULT_MEDAL_PRESET = "standard"
MEDAL_PRESETS = {
    "standard": ["Platinum", "High Gold", "Gold", "High Silver", "Silver", "Bronze"],
    "elite": ["Elite Platinum", "Platinum", "High Gold", "Gold", "Silver"],
    "custom": [],
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-secret-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table_name: str, col_name: str) -> bool:
    if not table_exists(conn, table_name):
        return False
    cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(c["name"] == col_name for c in cols)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or "competition"


def ensure_unique_slug(conn: sqlite3.Connection, candidate: str, exclude_id: int | None = None) -> str:
    base = slugify(candidate)
    slug = base
    idx = 2
    while True:
        if exclude_id is None:
            row = conn.execute("SELECT id FROM competitions WHERE slug = ?", (slug,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM competitions WHERE slug = ? AND id != ?",
                (slug, exclude_id),
            ).fetchone()
        if row is None:
            return slug
        slug = f"{base}-{idx}"
        idx += 1


def get_preset_medal_options(preset_name: str) -> list[str]:
    if preset_name not in MEDAL_PRESETS:
        preset_name = DEFAULT_MEDAL_PRESET
    return list(MEDAL_PRESETS.get(preset_name, []))


def normalize_medal_options(raw_options: object, preset_name: str) -> list[str]:
    normalized: list[str] = []
    if isinstance(raw_options, list):
        seen: set[str] = set()
        for value in raw_options:
            item = normalize_space(str(value))
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
    if normalized:
        return normalized
    fallback = get_preset_medal_options(preset_name)
    if fallback:
        return fallback
    return get_preset_medal_options(DEFAULT_MEDAL_PRESET)


def normalize_special_awards(raw_special_awards: object) -> list[dict]:
    if raw_special_awards in ("", None):
        return []
    if not isinstance(raw_special_awards, list):
        raise ValueError("special_awards_json must be a JSON array")

    normalized: list[dict] = []
    for idx, item in enumerate(raw_special_awards, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Special award row {idx} must be an object")
        category = normalize_space(str(item.get("category", "")))
        placement = normalize_space(str(item.get("placement", "")))
        title = normalize_space(str(item.get("title", "")))
        note = normalize_space(str(item.get("note", "")))
        if not category and not title and not note:
            continue

        entry_raw = item.get("entry_num", "")
        if entry_raw in ("", None):
            entry_num = None
        else:
            try:
                entry_num = int(entry_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Special award row {idx} entry_num must be a number") from exc

        sort_raw = item.get("sort_index", idx)
        try:
            sort_index = int(sort_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Special award row {idx} sort_index must be a number") from exc

        normalized.append(
            {
                "category": category,
                "placement": placement,
                "title": title,
                "note": note,
                "entry_num": entry_num,
                "sort_index": sort_index,
            }
        )
    return normalized


def ensure_schema(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "awards"):
        if not column_exists(conn, "awards", "competition_id"):
            conn.execute("ALTER TABLE awards RENAME TO awards_legacy")
        elif not column_exists(conn, "awards", "medal"):
            conn.execute("ALTER TABLE awards RENAME TO awards_legacy_v2")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS competitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            event_start_date TEXT NOT NULL,
            event_end_date TEXT,
            status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'archived')),
            awards_pin_hash TEXT NOT NULL,
            session_labels_json TEXT NOT NULL DEFAULT '{}',
            medal_system_name TEXT NOT NULL DEFAULT 'standard',
            medal_options_json TEXT NOT NULL DEFAULT '[]',
            competition_notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            published_at TEXT
        )
        """
    )
    if not column_exists(conn, "competitions", "medal_system_name"):
        conn.execute("ALTER TABLE competitions ADD COLUMN medal_system_name TEXT NOT NULL DEFAULT 'standard'")
    if not column_exists(conn, "competitions", "medal_options_json"):
        conn.execute("ALTER TABLE competitions ADD COLUMN medal_options_json TEXT NOT NULL DEFAULT '[]'")
    if not column_exists(conn, "competitions", "competition_notes"):
        conn.execute("ALTER TABLE competitions ADD COLUMN competition_notes TEXT NOT NULL DEFAULT ''")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id INTEGER NOT NULL,
            sort_index INTEGER NOT NULL,
            session_num INTEGER,
            is_break INTEGER NOT NULL DEFAULT 0,
            break_text TEXT,
            day TEXT,
            time_text TEXT,
            class_name TEXT,
            age_division TEXT,
            entry_type TEXT,
            style TEXT,
            entry_num INTEGER,
            dance_name TEXT,
            dancers TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS awards (
            competition_id INTEGER NOT NULL,
            entry_num INTEGER NOT NULL,
            medal TEXT NOT NULL DEFAULT '',
            entry_note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (competition_id, entry_num),
            FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS special_awards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            placement TEXT NOT NULL DEFAULT '',
            entry_num INTEGER,
            title TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            sort_index INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_rows_comp_sort ON schedule_rows(competition_id, sort_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_competitions_status_date ON competitions(status, event_start_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_special_awards_comp_sort ON special_awards(competition_id, sort_index, id)"
    )

    default_options_json = json.dumps(get_preset_medal_options(DEFAULT_MEDAL_PRESET))
    conn.execute(
        """
        UPDATE competitions
        SET medal_system_name = CASE
              WHEN COALESCE(TRIM(medal_system_name), '') = '' THEN ?
              ELSE medal_system_name
            END,
            medal_options_json = CASE
              WHEN COALESCE(TRIM(medal_options_json), '') = '' OR medal_options_json = '[]' THEN ?
              ELSE medal_options_json
            END,
            competition_notes = COALESCE(competition_notes, '')
        """,
        (DEFAULT_MEDAL_PRESET, default_options_json),
    )


def db_rows_to_payload(rows: list[sqlite3.Row]) -> list[dict]:
    out = []
    for row in rows:
        if row["is_break"]:
            out.append({"s": row["session_num"], "brk": row["break_text"] or ""})
        else:
            out.append(
                {
                    "s": row["session_num"],
                    "day": row["day"] or "",
                    "t": row["time_text"] or "",
                    "cls": row["class_name"] or "",
                    "age": row["age_division"] or "",
                    "type": row["entry_type"] or "",
                    "style": row["style"] or "",
                    "e": row["entry_num"],
                    "name": row["dance_name"] or "",
                    "d": row["dancers"] or "",
                }
            )
    return out


def persist_schedule_rows(conn: sqlite3.Connection, competition_id: int, rows: list[dict]) -> None:
    conn.execute("DELETE FROM schedule_rows WHERE competition_id = ?", (competition_id,))
    for idx, row in enumerate(rows, start=1):
        if row.get("brk"):
            conn.execute(
                """
                INSERT INTO schedule_rows (competition_id, sort_index, session_num, is_break, break_text)
                VALUES (?, ?, ?, 1, ?)
                """,
                (competition_id, idx, int(row.get("s", 1)), normalize_space(str(row.get("brk", "")))),
            )
            continue
        conn.execute(
            """
            INSERT INTO schedule_rows (
                competition_id, sort_index, session_num, is_break,
                day, time_text, class_name, age_division,
                entry_type, style, entry_num, dance_name, dancers
            )
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                competition_id,
                idx,
                int(row.get("s", 1)),
                normalize_space(str(row.get("day", ""))),
                normalize_space(str(row.get("t", ""))),
                normalize_space(str(row.get("cls", ""))),
                normalize_space(str(row.get("age", ""))),
                normalize_space(str(row.get("type", ""))),
                normalize_space(str(row.get("style", ""))),
                int(row.get("e")),
                normalize_space(str(row.get("name", ""))),
                normalize_space(str(row.get("d", ""))),
            ),
        )


def persist_special_awards(conn: sqlite3.Connection, competition_id: int, special_awards: list[dict]) -> None:
    conn.execute("DELETE FROM special_awards WHERE competition_id = ?", (competition_id,))
    for idx, award in enumerate(special_awards, start=1):
        conn.execute(
            """
            INSERT INTO special_awards (
                competition_id, category, placement, entry_num, title, note, sort_index, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                competition_id,
                award.get("category", ""),
                award.get("placement", ""),
                award.get("entry_num"),
                award.get("title", ""),
                award.get("note", ""),
                int(award.get("sort_index", idx)),
            ),
        )


def parse_legacy_seed_data() -> tuple[list[dict], dict[str, str]] | None:
    candidate = BASE_DIR / "Phoenix, AZ (1).html"
    if not candidate.exists():
        candidate = BASE_DIR / "index.html"
    if not candidate.exists():
        return None

    raw = candidate.read_text(encoding="utf-8", errors="ignore")
    data_match = re.search(r"const DATA = \[(.*?)\];\s*\n\s*const SESSION_LABELS", raw, re.DOTALL)
    label_match = re.search(r"const SESSION_LABELS = \{(.*?)\};", raw, re.DOTALL)
    if not data_match or not label_match:
        return None

    rows = []
    for obj_literal in re.findall(r"\{[^{}]*\}", data_match.group(1), re.DOTALL):
        normalized = re.sub(r"(?<=\{|,)\s*(\w+)\s*:", r'"\1":', obj_literal)
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            continue
        parsed.setdefault("s", 1)
        rows.append(parsed)

    labels_literal = "{" + label_match.group(1) + "}"
    labels_literal = re.sub(r"(?m)^\s*(\d+)\s*:", r'"\1":', labels_literal)
    labels = json.loads(labels_literal)
    return rows, {str(k): str(v) for k, v in labels.items()}


def maybe_seed_initial_competition(conn: sqlite3.Connection) -> None:
    has_comp = conn.execute("SELECT id FROM competitions LIMIT 1").fetchone()
    if has_comp:
        return

    try:
        seed = parse_legacy_seed_data()
    except Exception:  # noqa: BLE001
        seed = None
    if not seed:
        return

    rows, labels = seed
    default_pin = os.getenv("DEFAULT_COMP_PIN", "1234")
    default_medals = json.dumps(get_preset_medal_options(DEFAULT_MEDAL_PRESET))
    comp_id = conn.execute(
        """
        INSERT INTO competitions (
            slug, name, location, event_start_date, event_end_date,
            status, awards_pin_hash, session_labels_json,
            medal_system_name, medal_options_json, competition_notes,
            published_at
        )
        VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, '', CURRENT_TIMESTAMP)
        """,
        (
            ensure_unique_slug(conn, "phoenix-az-2026"),
            "Phoenix, AZ Dance Competition",
            "Phoenix, AZ",
            "2026-02-07",
            "2026-02-08",
            generate_password_hash(default_pin),
            json.dumps(labels),
            DEFAULT_MEDAL_PRESET,
            default_medals,
        ),
    ).lastrowid
    persist_schedule_rows(conn, int(comp_id), rows)



def migrate_legacy_awards(conn: sqlite3.Connection) -> None:
    legacy_tables = []
    if table_exists(conn, "awards_legacy"):
        legacy_tables.append(("awards_legacy", False))
    if table_exists(conn, "awards_legacy_v2"):
        legacy_tables.append(("awards_legacy_v2", True))

    if not legacy_tables:
        return

    comp_rows = conn.execute(
        "SELECT id, medal_options_json FROM competitions ORDER BY id ASC"
    ).fetchall()
    if not comp_rows:
        return
    comp_default_id = int(comp_rows[0]["id"])

    comp_medal_map: dict[int, dict[str, str]] = {}
    for comp in comp_rows:
        comp_id = int(comp["id"])
        try:
            medal_options = json.loads(comp["medal_options_json"] or "[]")
        except json.JSONDecodeError:
            medal_options = []
        normalized_options = normalize_medal_options(medal_options, DEFAULT_MEDAL_PRESET)
        comp_medal_map[comp_id] = {m.lower(): m for m in normalized_options}

    for table_name, includes_comp_id in legacy_tables:
        if includes_comp_id:
            rows = conn.execute(
                f"SELECT competition_id, entry_num, award_text, COALESCE(updated_at, CURRENT_TIMESTAMP) AS updated_at FROM {table_name}"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT entry_num, award_text, COALESCE(updated_at, CURRENT_TIMESTAMP) AS updated_at FROM {table_name}"
            ).fetchall()

        for row in rows:
            if includes_comp_id:
                competition_id = int(row["competition_id"])
            else:
                competition_id = comp_default_id

            if competition_id not in comp_medal_map:
                competition_id = comp_default_id

            raw_award = normalize_space(str(row["award_text"] or ""))
            if not raw_award:
                continue
            match_medal = comp_medal_map[competition_id].get(raw_award.lower(), "")
            entry_note = "" if match_medal else raw_award

            conn.execute(
                """
                INSERT OR REPLACE INTO awards (competition_id, entry_num, medal, entry_note, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    competition_id,
                    int(row["entry_num"]),
                    match_medal,
                    entry_note,
                    row["updated_at"],
                ),
            )

        conn.execute(f"DROP TABLE {table_name}")



def is_admin() -> bool:
    return bool(session.get("is_admin"))



def require_admin(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)

    return wrapped



def cleanup_pin_grants() -> dict[str, float]:
    grants = session.get("pin_grants", {})
    if not isinstance(grants, dict):
        grants = {}
    now = time.time()
    valid = {str(k): float(v) for k, v in grants.items() if float(v) > now}
    if valid != grants:
        session["pin_grants"] = valid
        session.modified = True
    return valid


def has_award_edit_permission(competition_id: int) -> bool:
    if is_admin():
        return True
    grants = cleanup_pin_grants()
    return float(grants.get(str(competition_id), 0)) > time.time()


def grant_pin_permission(competition_id: int) -> None:
    grants = cleanup_pin_grants()
    grants[str(competition_id)] = time.time() + PIN_GRANT_TTL_SECONDS
    session["pin_grants"] = grants
    session.modified = True


def record_pin_attempt(slug: str) -> int:
    all_attempts = session.get("pin_attempts", {})
    if not isinstance(all_attempts, dict):
        all_attempts = {}
    now = time.time()
    attempts = [
        float(t)
        for t in all_attempts.get(slug, [])
        if now - float(t) <= PIN_ATTEMPT_WINDOW_SECONDS
    ]
    attempts.append(now)
    all_attempts[slug] = attempts
    session["pin_attempts"] = all_attempts
    session.modified = True
    return len(attempts)


def clear_pin_attempts(slug: str) -> None:
    all_attempts = session.get("pin_attempts", {})
    if not isinstance(all_attempts, dict):
        return
    if slug in all_attempts:
        all_attempts.pop(slug, None)
        session["pin_attempts"] = all_attempts
        session.modified = True


def verify_admin_password(raw_password: str) -> bool:
    configured = os.getenv("ADMIN_PASSWORD", "admin123")
    if configured.startswith("pbkdf2:"):
        return check_password_hash(configured, raw_password)
    return secrets.compare_digest(configured, raw_password)


def parse_pdf_rows(pdf_bytes: bytes) -> list[dict]:
    if pdfplumber is None:
        raise ValueError("pdfplumber dependency is missing")

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        raw_text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    lines = [normalize_space(line) for line in raw_text.splitlines() if normalize_space(line)]
    rows: list[dict] = []
    current_session = 1
    fallback_entry = 900000

    for line in lines:
        session_match = re.search(r"\bSESSION\s+(\d+)\b", line, re.IGNORECASE)
        if session_match:
            current_session = int(session_match.group(1))
            continue

        if re.search(r"\b(JUDGES BREAK|AWARDS|LUNCH BREAK|DINNER BREAK|IMPROV)\b", line, re.IGNORECASE):
            rows.append({"s": current_session, "brk": line})
            continue

        time_match = re.search(r"\b(\d{1,2}:\d{2}\s?[AP]M)\b", line, re.IGNORECASE)
        if not time_match:
            continue

        time_text = normalize_space(time_match.group(1).upper())
        remainder = normalize_space(line[time_match.end() :]).lstrip("-").strip()

        entry_match = re.search(r"\b(\d{1,4})\b", remainder)
        if entry_match:
            entry_num = int(entry_match.group(1))
            left = remainder[: entry_match.start()]
            right = remainder[entry_match.end() :]
            dance_name = normalize_space(f"{left} {right}")
        else:
            fallback_entry += 1
            entry_num = fallback_entry
            dance_name = normalize_space(f"[REVIEW] {remainder}") or "[REVIEW] Unparsed"

        rows.append(
            {
                "s": current_session,
                "day": "",
                "t": time_text,
                "cls": "",
                "age": "",
                "type": "",
                "style": "",
                "e": entry_num,
                "name": dance_name,
                "d": "",
            }
        )

    if not rows:
        raise ValueError("No rows could be extracted from this PDF")

    return rows


def competition_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "location": row["location"],
        "event_start_date": row["event_start_date"],
        "event_end_date": row["event_end_date"],
        "status": row["status"],
        "published_at": row["published_at"],
    }


def get_competition_by_slug(conn: sqlite3.Connection, slug: str, include_unpublished: bool) -> sqlite3.Row | None:
    if include_unpublished:
        return conn.execute("SELECT * FROM competitions WHERE slug = ?", (slug,)).fetchone()
    return conn.execute(
        "SELECT * FROM competitions WHERE slug = ? AND status = 'published'",
        (slug,),
    ).fetchone()


def parse_iso_date(value: str, required: bool = True) -> str | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError("Date is required")
        return None
    return date.fromisoformat(value).isoformat()


@app.before_request
def bootstrap() -> None:
    if app.config.get("_ready"):
        return
    with connect_db() as conn:
        ensure_schema(conn)
        maybe_seed_initial_competition(conn)
        migrate_legacy_awards(conn)
        conn.commit()
    app.config["_ready"] = True


@app.get("/")
def home():
    with connect_db() as conn:
        latest = conn.execute(
            "SELECT slug FROM competitions WHERE status='published' ORDER BY event_start_date DESC, id DESC LIMIT 1"
        ).fetchone()
    if latest:
        return redirect(url_for("competition_page", slug=latest["slug"]))
    return render_template("public_empty.html", is_admin=is_admin())


@app.get("/c/<slug>")
def competition_page(slug: str):
    with connect_db() as conn:
        comp = get_competition_by_slug(conn, slug, include_unpublished=is_admin())
    if comp is None:
        abort(404)
    if comp["status"] != "published" and not is_admin():
        abort(404)
    return render_template("public_competition.html", competition=comp, is_admin=is_admin())


@app.get("/Phoenix, AZ (1).html")
def legacy_page():
    return redirect(url_for("home"))


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/competitions")
def api_competitions():
    published_param = request.args.get("published")
    include_archived = request.args.get("include_archived") == "1"

    if published_param is None:
        published_only = not is_admin()
    else:
        published_only = published_param == "1"

    query = "SELECT * FROM competitions"
    filters = []
    if published_only:
        filters.append("status = 'published'")
    elif not include_archived:
        filters.append("status != 'archived'")
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY event_start_date DESC, id DESC"

    with connect_db() as conn:
        comps = conn.execute(query).fetchall()

    return jsonify([competition_payload(c) for c in comps])


@app.get("/api/competitions/<slug>/schedule")
def api_schedule(slug: str):
    with connect_db() as conn:
        comp = get_competition_by_slug(conn, slug, include_unpublished=is_admin())
        if comp is None:
            abort(404)
        if comp["status"] != "published" and not is_admin():
            abort(404)
        rows = conn.execute(
            "SELECT * FROM schedule_rows WHERE competition_id = ? ORDER BY sort_index ASC",
            (comp["id"],),
        ).fetchall()
        special_awards = conn.execute(
            """
            SELECT category, placement, entry_num, title, note, sort_index
            FROM special_awards
            WHERE competition_id = ?
            ORDER BY sort_index ASC, id ASC
            """,
            (comp["id"],),
        ).fetchall()

    try:
        medal_options = json.loads(comp["medal_options_json"] or "[]")
    except json.JSONDecodeError:
        medal_options = []
    medal_options = normalize_medal_options(medal_options, comp["medal_system_name"] or DEFAULT_MEDAL_PRESET)

    return jsonify(
        {
            "competition": competition_payload(comp),
            "session_labels": json.loads(comp["session_labels_json"] or "{}"),
            "medal_system_name": comp["medal_system_name"] or DEFAULT_MEDAL_PRESET,
            "medal_options": medal_options,
            "competition_notes": comp["competition_notes"] or "",
            "special_awards": [
                {
                    "category": r["category"] or "",
                    "placement": r["placement"] or "",
                    "entry_num": r["entry_num"],
                    "title": r["title"] or "",
                    "note": r["note"] or "",
                    "sort_index": r["sort_index"] if r["sort_index"] is not None else 0,
                }
                for r in special_awards
            ],
            "rows": db_rows_to_payload(rows),
            "can_edit_awards": has_award_edit_permission(int(comp["id"])),
        }
    )


@app.get("/api/competitions/<slug>/awards")
def api_awards(slug: str):
    with connect_db() as conn:
        comp = get_competition_by_slug(conn, slug, include_unpublished=is_admin())
        if comp is None:
            abort(404)
        if comp["status"] != "published" and not is_admin():
            abort(404)
        rows = conn.execute(
            "SELECT entry_num, medal, entry_note FROM awards WHERE competition_id = ? ORDER BY entry_num ASC",
            (comp["id"],),
        ).fetchall()
    return jsonify(
        {
            str(r["entry_num"]): {
                "medal": r["medal"] or "",
                "entry_note": r["entry_note"] or "",
            }
            for r in rows
        }
    )


@app.post("/api/competitions/<slug>/pin/unlock")
def api_unlock_pin(slug: str):
    with connect_db() as conn:
        comp = get_competition_by_slug(conn, slug, include_unpublished=is_admin())
    if comp is None:
        abort(404)
    if comp["status"] != "published" and not is_admin():
        abort(404)
    if is_admin():
        return jsonify({"ok": True, "editable": True})

    payload = request.get_json(silent=True) or {}
    pin = str(payload.get("pin", "")).strip()
    if not pin:
        return jsonify({"error": "PIN is required"}), 400

    attempts = record_pin_attempt(slug)
    if attempts > PIN_ATTEMPT_LIMIT:
        return jsonify({"error": "Too many attempts. Try again later."}), 429

    if not check_password_hash(comp["awards_pin_hash"], pin):
        return jsonify({"error": "Invalid PIN"}), 401

    clear_pin_attempts(slug)
    grant_pin_permission(int(comp["id"]))
    return jsonify({"ok": True, "editable": True})


@app.put("/api/competitions/<slug>/awards/<int:entry_num>")
def api_put_award(slug: str, entry_num: int):
    with connect_db() as conn:
        comp = get_competition_by_slug(conn, slug, include_unpublished=is_admin())
        if comp is None:
            abort(404)
        if comp["status"] != "published" and not is_admin():
            abort(404)
        if not has_award_edit_permission(int(comp["id"])):
            return jsonify({"error": "Award editing is locked. Unlock with PIN."}), 403

        payload = request.get_json(silent=True) or {}
        medal = payload.get("medal", "")
        entry_note = payload.get("entry_note", "")

        # Backward-compatible fallback for old clients sending {award: "..."}.
        if "award" in payload and "medal" not in payload and "entry_note" not in payload:
            legacy_award = payload.get("award", "")
            if not isinstance(legacy_award, str):
                return jsonify({"error": "award must be a string"}), 400
            entry_note = legacy_award

        if not isinstance(medal, str):
            return jsonify({"error": "medal must be a string"}), 400
        if not isinstance(entry_note, str):
            return jsonify({"error": "entry_note must be a string"}), 400
        if len(entry_note) > 2000:
            return jsonify({"error": "entry_note cannot exceed 2000 characters"}), 400

        try:
            allowed_medals = json.loads(comp["medal_options_json"] or "[]")
        except json.JSONDecodeError:
            allowed_medals = []
        allowed_medals = normalize_medal_options(allowed_medals, comp["medal_system_name"] or DEFAULT_MEDAL_PRESET)
        allowed_lookup = {m.lower(): m for m in allowed_medals}
        medal = normalize_space(medal)
        if medal and medal.lower() not in allowed_lookup:
            return jsonify({"error": "Selected medal is not valid for this competition"}), 400
        medal = allowed_lookup.get(medal.lower(), "") if medal else ""
        entry_note = entry_note.strip()

        conn.execute(
            """
            INSERT INTO awards (competition_id, entry_num, medal, entry_note, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(competition_id, entry_num)
            DO UPDATE SET
              medal = excluded.medal,
              entry_note = excluded.entry_note,
              updated_at = CURRENT_TIMESTAMP
            """,
            (comp["id"], entry_num, medal, entry_note),
        )
        conn.commit()

    return jsonify({"entry_num": entry_num, "medal": medal, "entry_note": entry_note})


@app.put("/api/competitions/<slug>/notes")
def api_put_competition_notes(slug: str):
    with connect_db() as conn:
        comp = get_competition_by_slug(conn, slug, include_unpublished=is_admin())
        if comp is None:
            abort(404)
        if comp["status"] != "published" and not is_admin():
            abort(404)
        if not has_award_edit_permission(int(comp["id"])):
            return jsonify({"error": "Notes editing is locked. Unlock with PIN."}), 403

        payload = request.get_json(silent=True) or {}
        competition_notes = payload.get("competition_notes", "")
        if not isinstance(competition_notes, str):
            return jsonify({"error": "competition_notes must be a string"}), 400
        if len(competition_notes) > 4000:
            return jsonify({"error": "competition_notes cannot exceed 4000 characters"}), 400
        competition_notes = competition_notes.strip()

        conn.execute(
            """
            UPDATE competitions
            SET competition_notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (competition_notes, comp["id"]),
        )
        conn.commit()
    return jsonify({"competition_notes": competition_notes})


def parse_editor_form(form: dict, conn: sqlite3.Connection, existing: sqlite3.Row | None = None):
    name = normalize_space(form.get("name", ""))
    if not name:
        raise ValueError("Competition name is required")

    location = normalize_space(form.get("location", ""))
    start_date = parse_iso_date(form.get("event_start_date", ""), required=True)
    end_date = parse_iso_date(form.get("event_end_date", ""), required=False)
    if end_date and end_date < start_date:
        raise ValueError("End date cannot be before start date")

    slug_candidate = normalize_space(form.get("slug", "")) or f"{name}-{start_date}"
    slug = ensure_unique_slug(conn, slug_candidate, exclude_id=int(existing["id"]) if existing else None)

    pin = form.get("awards_pin", "")
    if not pin and existing is None:
        raise ValueError("Awards PIN is required")
    pin_hash = generate_password_hash(pin) if pin else existing["awards_pin_hash"]

    medal_system_name = normalize_space(form.get("medal_system_name", DEFAULT_MEDAL_PRESET)).lower()
    if medal_system_name not in MEDAL_PRESETS:
        medal_system_name = "custom"

    medal_options_raw_text = form.get("medal_options_json", "").strip()
    if medal_options_raw_text:
        try:
            medal_options_raw = json.loads(medal_options_raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"medal_options_json is invalid JSON: {exc.msg}") from exc
    else:
        medal_options_raw = get_preset_medal_options(medal_system_name)
    medal_options = normalize_medal_options(medal_options_raw, medal_system_name)

    try:
        rows = json.loads(form.get("rows_json", "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"rows_json is invalid JSON: {exc.msg}") from exc
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows_json must be a non-empty JSON array")

    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {idx} must be an object")
        if row.get("brk"):
            row["s"] = int(row.get("s", 1))
            continue
        if "e" not in row:
            raise ValueError(f"Row {idx} is missing entry number 'e'")
        row["e"] = int(row["e"])
        row["s"] = int(row.get("s", 1))

    labels_raw_text = form.get("session_labels_json", "{}").strip() or "{}"
    try:
        labels_raw = json.loads(labels_raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"session_labels_json is invalid JSON: {exc.msg}") from exc
    if not isinstance(labels_raw, dict):
        raise ValueError("session_labels_json must be an object")

    sessions = sorted({int(r.get("s", 1)) for r in rows})
    labels = {str(int(k)): normalize_space(str(v)) for k, v in labels_raw.items()}
    for s in sessions:
        labels.setdefault(str(s), f"Session {s}")

    special_awards_raw_text = form.get("special_awards_json", "[]").strip() or "[]"
    try:
        special_awards_raw = json.loads(special_awards_raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"special_awards_json is invalid JSON: {exc.msg}") from exc
    special_awards = normalize_special_awards(special_awards_raw)

    competition_notes = str(form.get("competition_notes", "")).strip()

    intent = form.get("intent", "save_draft")
    if intent == "publish":
        status = "published"
    elif intent == "archive":
        status = "archived"
    else:
        status = "draft"

    return {
        "name": name,
        "location": location,
        "event_start_date": start_date,
        "event_end_date": end_date,
        "slug": slug,
        "status": status,
        "awards_pin_hash": pin_hash,
        "medal_system_name": medal_system_name,
        "medal_options_json": json.dumps(medal_options),
        "competition_notes": competition_notes,
        "session_labels_json": json.dumps(labels),
        "special_awards": special_awards,
        "rows": rows,
    }


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if verify_admin_password(password):
            session["is_admin"] = True
            session.modified = True
            return redirect(request.form.get("next") or url_for("admin_dashboard"))
        flash("Invalid admin password", "error")
    return render_template("admin_login.html", next=request.args.get("next", ""))


@app.post("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


@app.get("/admin")
@require_admin
def admin_dashboard():
    with connect_db() as conn:
        competitions = conn.execute(
            "SELECT * FROM competitions ORDER BY event_start_date DESC, id DESC"
        ).fetchall()
    return render_template("admin_dashboard.html", competitions=competitions)


@app.get("/admin/competitions/new")
@require_admin
def admin_new_competition():
    return render_template(
        "admin_editor.html",
        mode="create",
        competition=None,
        rows_json=json.dumps([], indent=2),
        session_labels_json=json.dumps({}, indent=2),
        medal_presets=MEDAL_PRESETS,
        medal_system_name=DEFAULT_MEDAL_PRESET,
        medal_options_json=json.dumps(get_preset_medal_options(DEFAULT_MEDAL_PRESET), indent=2),
        special_awards_json=json.dumps([], indent=2),
        competition_notes="",
        source="manual",
    )


@app.post("/admin/competitions/import-pdf")
@require_admin
def admin_import_pdf():
    upload = request.files.get("schedule_pdf")
    if upload is None or not upload.filename:
        flash("Please choose a PDF file", "error")
        return redirect(url_for("admin_new_competition"))

    try:
        rows = parse_pdf_rows(upload.read())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_new_competition"))

    sessions = sorted({int(r.get("s", 1)) for r in rows})
    labels = {str(s): f"Session {s}" for s in sessions}
    prefill = {
        "name": normalize_space(request.form.get("name", "")),
        "location": normalize_space(request.form.get("location", "")),
        "event_start_date": request.form.get("event_start_date", ""),
        "event_end_date": request.form.get("event_end_date", ""),
        "slug": normalize_space(request.form.get("slug", "")),
        "medal_system_name": normalize_space(
            request.form.get("medal_system_name", DEFAULT_MEDAL_PRESET)
        ).lower(),
        "medal_options_json": request.form.get("medal_options_json", "").strip(),
        "special_awards_json": request.form.get("special_awards_json", "[]").strip(),
        "competition_notes": request.form.get("competition_notes", "").strip(),
    }
    if prefill["medal_system_name"] not in MEDAL_PRESETS:
        prefill["medal_system_name"] = "custom"
    if not prefill["medal_options_json"]:
        prefill["medal_options_json"] = json.dumps(
            get_preset_medal_options(prefill["medal_system_name"]),
            indent=2,
        )
    if not prefill["special_awards_json"]:
        prefill["special_awards_json"] = json.dumps([], indent=2)

    return render_template(
        "admin_editor.html",
        mode="create",
        competition=prefill,
        rows_json=json.dumps(rows, indent=2),
        session_labels_json=json.dumps(labels, indent=2),
        medal_presets=MEDAL_PRESETS,
        medal_system_name=prefill["medal_system_name"],
        medal_options_json=prefill["medal_options_json"],
        special_awards_json=prefill["special_awards_json"],
        competition_notes=prefill["competition_notes"],
        source="pdf",
    )


@app.post("/admin/competitions")
@require_admin
def admin_create_competition():
    with connect_db() as conn:
        try:
            parsed = parse_editor_form(request.form, conn)
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
            return render_template(
                "admin_editor.html",
                mode="create",
                competition=request.form,
                rows_json=request.form.get("rows_json", "[]"),
                session_labels_json=request.form.get("session_labels_json", "{}"),
                medal_presets=MEDAL_PRESETS,
                medal_system_name=normalize_space(
                    request.form.get("medal_system_name", DEFAULT_MEDAL_PRESET)
                ).lower(),
                medal_options_json=request.form.get(
                    "medal_options_json",
                    json.dumps(get_preset_medal_options(DEFAULT_MEDAL_PRESET), indent=2),
                ),
                special_awards_json=request.form.get("special_awards_json", "[]"),
                competition_notes=request.form.get("competition_notes", ""),
                source="manual",
            )

        published_at = "CURRENT_TIMESTAMP" if parsed["status"] == "published" else "NULL"
        comp_id = conn.execute(
            f"""
            INSERT INTO competitions (
                slug, name, location, event_start_date, event_end_date,
                status, awards_pin_hash, session_labels_json,
                medal_system_name, medal_options_json, competition_notes,
                published_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {published_at})
            """,
            (
                parsed["slug"],
                parsed["name"],
                parsed["location"],
                parsed["event_start_date"],
                parsed["event_end_date"],
                parsed["status"],
                parsed["awards_pin_hash"],
                parsed["session_labels_json"],
                parsed["medal_system_name"],
                parsed["medal_options_json"],
                parsed["competition_notes"],
            ),
        ).lastrowid
        persist_schedule_rows(conn, int(comp_id), parsed["rows"])
        persist_special_awards(conn, int(comp_id), parsed["special_awards"])
        conn.commit()

    flash("Competition created", "success")
    return redirect(url_for("admin_dashboard"))


@app.get("/admin/competitions/<int:competition_id>/edit")
@require_admin
def admin_edit_competition(competition_id: int):
    with connect_db() as conn:
        competition = conn.execute("SELECT * FROM competitions WHERE id = ?", (competition_id,)).fetchone()
        if competition is None:
            abort(404)
        rows = conn.execute(
            "SELECT * FROM schedule_rows WHERE competition_id = ? ORDER BY sort_index ASC",
            (competition_id,),
        ).fetchall()
        special_awards = conn.execute(
            """
            SELECT category, placement, entry_num, title, note, sort_index
            FROM special_awards
            WHERE competition_id = ?
            ORDER BY sort_index ASC, id ASC
            """,
            (competition_id,),
        ).fetchall()
    try:
        medal_options = json.loads(competition["medal_options_json"] or "[]")
    except json.JSONDecodeError:
        medal_options = []
    medal_system_name = (competition["medal_system_name"] or DEFAULT_MEDAL_PRESET).lower()
    if medal_system_name not in MEDAL_PRESETS:
        medal_system_name = "custom"
    medal_options = normalize_medal_options(medal_options, medal_system_name)

    return render_template(
        "admin_editor.html",
        mode="edit",
        competition=competition,
        rows_json=json.dumps(db_rows_to_payload(rows), indent=2),
        session_labels_json=json.dumps(json.loads(competition["session_labels_json"] or "{}"), indent=2),
        medal_presets=MEDAL_PRESETS,
        medal_system_name=medal_system_name,
        medal_options_json=json.dumps(medal_options, indent=2),
        special_awards_json=json.dumps(
            [
                {
                    "category": r["category"] or "",
                    "placement": r["placement"] or "",
                    "entry_num": r["entry_num"],
                    "title": r["title"] or "",
                    "note": r["note"] or "",
                    "sort_index": r["sort_index"] if r["sort_index"] is not None else 0,
                }
                for r in special_awards
            ],
            indent=2,
        ),
        competition_notes=competition["competition_notes"] or "",
        source="manual",
    )


@app.post("/admin/competitions/<int:competition_id>/edit")
@require_admin
def admin_update_competition(competition_id: int):
    with connect_db() as conn:
        competition = conn.execute("SELECT * FROM competitions WHERE id = ?", (competition_id,)).fetchone()
        if competition is None:
            abort(404)

        try:
            parsed = parse_editor_form(request.form, conn, existing=competition)
        except Exception as exc:  # noqa: BLE001
            flash(str(exc), "error")
            return render_template(
                "admin_editor.html",
                mode="edit",
                competition={**dict(competition), **request.form},
                rows_json=request.form.get("rows_json", "[]"),
                session_labels_json=request.form.get("session_labels_json", "{}"),
                medal_presets=MEDAL_PRESETS,
                medal_system_name=normalize_space(
                    request.form.get("medal_system_name", competition["medal_system_name"] or DEFAULT_MEDAL_PRESET)
                ).lower(),
                medal_options_json=request.form.get(
                    "medal_options_json",
                    competition["medal_options_json"] or json.dumps(
                        get_preset_medal_options(DEFAULT_MEDAL_PRESET), indent=2
                    ),
                ),
                special_awards_json=request.form.get("special_awards_json", "[]"),
                competition_notes=request.form.get("competition_notes", competition["competition_notes"] or ""),
                source="manual",
            )

        if parsed["status"] == "published" and competition["published_at"] is None:
            published_sql = "published_at = CURRENT_TIMESTAMP,"
        elif parsed["status"] != "published":
            published_sql = "published_at = NULL,"
        else:
            published_sql = ""

        conn.execute(
            f"""
            UPDATE competitions
            SET slug = ?,
                name = ?,
                location = ?,
                event_start_date = ?,
                event_end_date = ?,
                status = ?,
                awards_pin_hash = ?,
                session_labels_json = ?,
                medal_system_name = ?,
                medal_options_json = ?,
                competition_notes = ?,
                {published_sql}
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                parsed["slug"],
                parsed["name"],
                parsed["location"],
                parsed["event_start_date"],
                parsed["event_end_date"],
                parsed["status"],
                parsed["awards_pin_hash"],
                parsed["session_labels_json"],
                parsed["medal_system_name"],
                parsed["medal_options_json"],
                parsed["competition_notes"],
                competition_id,
            ),
        )
        persist_schedule_rows(conn, competition_id, parsed["rows"])
        persist_special_awards(conn, competition_id, parsed["special_awards"])
        conn.commit()

    flash("Competition updated", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/competitions/<int:competition_id>/publish")
@require_admin
def admin_publish_competition(competition_id: int):
    with connect_db() as conn:
        exists = conn.execute("SELECT id FROM competitions WHERE id = ?", (competition_id,)).fetchone()
        if exists is None:
            abort(404)
        conn.execute(
            """
            UPDATE competitions
            SET status = 'published',
                published_at = COALESCE(published_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (competition_id,),
        )
        conn.commit()
    flash("Competition published", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/competitions/<int:competition_id>/archive")
@require_admin
def admin_archive_competition(competition_id: int):
    with connect_db() as conn:
        exists = conn.execute("SELECT id FROM competitions WHERE id = ?", (competition_id,)).fetchone()
        if exists is None:
            abort(404)
        conn.execute(
            "UPDATE competitions SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            (competition_id,),
        )
        conn.commit()
    flash("Competition archived", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/competitions/<int:competition_id>/draft")
@require_admin
def admin_draft_competition(competition_id: int):
    with connect_db() as conn:
        exists = conn.execute("SELECT id FROM competitions WHERE id = ?", (competition_id,)).fetchone()
        if exists is None:
            abort(404)
        conn.execute(
            "UPDATE competitions SET status='draft', published_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            (competition_id,),
        )
        conn.commit()
    flash("Competition moved to draft", "success")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    with connect_db() as conn:
        ensure_schema(conn)
        maybe_seed_initial_competition(conn)
        migrate_legacy_awards(conn)
        conn.commit()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
