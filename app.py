import os
import re
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from docx import Document
except Exception:
    Document = None

BASE = Path(__file__).resolve().parent
DB = BASE / "job_hunter.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(BASE), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

JOOBLE_KEY = os.environ.get("JOOBLE_API_KEY", "").strip()
JOOBLE_URL = "https://hr.jooble.org/api/" + JOOBLE_KEY if JOOBLE_KEY else ""

PROFILE_DEFAULT = {
    "name": "Toni Skender",
    "target_roles": [
        "Project Manager", "Business Analyst", "Process Manager",
        "Internal Control", "Information Security", "Governance"
    ],
    "location": "Zagreb",
    "work_modes": ["Hybrid", "Office", "Remote"],
    "excluded": ["helpdesk", "developer", "servis", "skladište", "tehničar"],
    "skills": [
        "project management", "project coordination", "business analysis",
        "process management", "process coordination", "internal control",
        "internal supervision", "security", "governance", "risk",
        "stakeholder", "team coordination", "technical operations",
        "operations", "reporting", "monitoring"
    ],
    "cv_filename": "",
    "cv_text": ""
}

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY CHECK (id=1),
        data TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS saved (
        id TEXT PRIMARY KEY,
        job TEXT NOT NULL,
        status TEXT DEFAULT 'saved',
        created_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    row = con.execute("SELECT data FROM profile WHERE id=1").fetchone()
    if not row:
        con.execute("INSERT INTO profile(id,data) VALUES(1,?)", (json.dumps(PROFILE_DEFAULT, ensure_ascii=False),))
    con.commit()
    con.close()

def get_profile():
    con = db()
    row = con.execute("SELECT data FROM profile WHERE id=1").fetchone()
    con.close()
    return json.loads(row["data"]) if row else PROFILE_DEFAULT.copy()

def save_profile(p):
    con = db()
    con.execute("UPDATE profile SET data=? WHERE id=1", (json.dumps(p, ensure_ascii=False),))
    con.commit()
    con.close()

def event(kind):
    con = db()
    con.execute("INSERT INTO events(kind,created_at) VALUES(?,?)",
                (kind, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()

def extract_cv(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if not PdfReader:
            raise RuntimeError("PDF parser nije instaliran.")
        reader = PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if suffix == ".docx":
        if not Document:
            raise RuntimeError("DOCX parser nije instaliran.")
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    raise ValueError("Podržani su samo PDF i DOCX.")

def terms(text):
    return set(re.findall(r"[a-zA-ZčćžšđČĆŽŠĐ0-9+.-]{3,}", text.lower()))

def score_job(job, profile):
    title = (job.get("title") or "").lower()
    desc = (job.get("snippet") or "").lower()
    company = (job.get("company") or "").lower()
    location = (job.get("location") or "").lower()
    blob = f"{title} {desc} {company}"

    role_hits = sum(1 for r in profile["target_roles"] if any(
        part.strip().lower() in blob for part in r.split()
    ))
    skill_hits = sum(1 for s in profile["skills"] if s.lower() in blob)

    title_bonus = 25 if any(r.lower() in title for r in profile["target_roles"]) else 0
    loc_bonus = 10 if profile["location"].lower() in location else 0

    negative = sum(1 for x in profile["excluded"] if x.lower() in blob)
    score = 35 + min(30, role_hits * 10) + min(25, skill_hits * 3) + title_bonus + loc_bonus - min(35, negative * 10)
    score = max(0, min(100, int(score)))

    reasons = []
    if title_bonus: reasons.append("naziv pozicije je vrlo blizu ciljanom profilu")
    if role_hits: reasons.append(f"prepoznato {role_hits} ciljano područje")
    if skill_hits: reasons.append(f"prepoznato {skill_hits} relevantnih vještina")
    if loc_bonus: reasons.append("lokacija odgovara")
    if negative: reasons.append("sadrži elemente koje si označio kao neželjene")
    if not reasons: reasons.append("podudaranje je temeljeno na opisu i ključnim riječima")

    return score, reasons

@app.get("/")
def home():
    return send_from_directory(BASE, "index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "api_configured": bool(JOOBLE_KEY)})

@app.get("/api/profile")
def profile_api():
    p = get_profile()
    safe = dict(p)
    safe["cv_text"] = bool(safe.get("cv_text"))
    return jsonify(safe)

@app.post("/api/profile")
def profile_update():
    p = get_profile()
    data = request.get_json(silent=True) or {}
    for k in ["target_roles", "location", "work_modes", "excluded", "skills"]:
        if k in data:
            p[k] = data[k]
    save_profile(p)
    event("profile_updated")
    return jsonify({"ok": True, "profile": {**p, "cv_text": bool(p.get("cv_text"))}})

@app.post("/api/cv")
def cv_upload():
    f = request.files.get("cv")
    if not f or not f.filename:
        return jsonify({"error": "Nije odabran CV."}), 400
    name = secure_filename(f.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in [".pdf", ".docx"]:
        return jsonify({"error": "CV mora biti PDF ili DOCX."}), 400

    path = UPLOADS / name
    f.save(path)
    try:
        text = extract_cv(path)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    p = get_profile()
    p["cv_filename"] = name
    p["cv_text"] = text[:100000]

    # CV-derived terms strengthen the profile; keep only useful professional terms.
    cv_low = text.lower()
    extra = [s for s in p["skills"] if s.lower() in cv_low]
    for role in p["target_roles"]:
        if role.lower() in cv_low and role not in extra:
            extra.append(role)
    p["skills"] = list(dict.fromkeys(p["skills"] + extra))

    save_profile(p)
    event("cv_uploaded")
    return jsonify({
        "ok": True,
        "filename": name,
        "characters": len(text),
        "profile": {**p, "cv_text": True}
    })

@app.post("/api/search")
def search():
    if not JOOBLE_KEY:
        return jsonify({"error": "Jooble API ključ nije postavljen na serveru."}), 500

    data = request.get_json(silent=True) or {}
    keywords = (data.get("keywords") or "Project Manager, Business Analyst, Process Manager").strip()
    location = (data.get("location") or "Zagreb").strip()
    radius = str(data.get("radius") or "40")

    payload = {
        "keywords": keywords,
        "location": location,
        "radius": radius,
        "page": "1",
        "ResultOnPage": 30,
        "companysearch": False
    }

    try:
        r = requests.post(JOOBLE_URL, json=payload, timeout=25)
    except requests.RequestException as e:
        return jsonify({"error": f"Greška pri povezivanju s Joobleom: {e}"}), 502

    if r.status_code != 200:
        return jsonify({"error": f"Jooble je vratio HTTP {r.status_code}."}), r.status_code

    raw = r.json()
    profile = get_profile()
    jobs = []

    for j in raw.get("jobs", []):
        score, reasons = score_job(j, profile)
        job = {
            "id": str(j.get("id") or j.get("link") or ""),
            "title": j.get("title") or "Bez naziva",
            "company": j.get("company") or "Nepoznat poslodavac",
            "location": j.get("location") or "",
            "snippet": j.get("snippet") or "",
            "salary": j.get("salary") or "",
            "type": j.get("type") or "",
            "source": j.get("source") or "Jooble",
            "updated": j.get("updated") or "",
            "link": j.get("link") or "",
            "score": score,
            "reasons": reasons
        }
        jobs.append(job)

    jobs.sort(key=lambda x: (-x["score"], x["title"]))
    event("search")
    return jsonify({
        "totalCount": raw.get("totalCount", len(jobs)),
        "jobs": jobs,
        "profile_used": bool(profile.get("cv_text"))
    })

@app.get("/api/saved")
def saved_list():
    con = db()
    rows = con.execute("SELECT id,job,status,created_at FROM saved ORDER BY created_at DESC").fetchall()
    con.close()
    return jsonify([{"id": r["id"], "job": json.loads(r["job"]), "status": r["status"], "created_at": r["created_at"]} for r in rows])

@app.post("/api/saved")
def saved_add():
    data = request.get_json(silent=True) or {}
    job = data.get("job") or {}
    jid = str(job.get("id") or job.get("link") or "")
    if not jid:
        return jsonify({"error": "Nedostaje ID oglasa."}), 400
    con = db()
    con.execute("INSERT OR REPLACE INTO saved(id,job,status,created_at) VALUES(?,?,?,?)",
                (jid, json.dumps(job, ensure_ascii=False), data.get("status", "saved"),
                 datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    event("job_saved")
    return jsonify({"ok": True})

@app.delete("/api/saved/<path:jid>")
def saved_delete(jid):
    con = db()
    con.execute("DELETE FROM saved WHERE id=?", (jid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.get("/api/analytics")
def analytics():
    con = db()
    searches = con.execute("SELECT COUNT(*) c FROM events WHERE kind='search'").fetchone()["c"]
    saved = con.execute("SELECT COUNT(*) c FROM saved").fetchone()["c"]
    applied = con.execute("SELECT COUNT(*) c FROM saved WHERE status='applied'").fetchone()["c"]
    viewed = con.execute("SELECT COUNT(*) c FROM events WHERE kind='viewed'").fetchone()["c"]
    con.close()
    return jsonify({"searches": searches, "saved": saved, "applied": applied, "viewed": viewed})

@app.post("/api/view")
def viewed():
    event("viewed")
    return jsonify({"ok": True})

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
