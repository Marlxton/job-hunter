import os, re, sqlite3, hashlib
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timezone
import requests
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
try:
    from config import JOOBLE_API_KEY as CONFIG_JOOBLE_API_KEY
except Exception:
    CONFIG_JOOBLE_API_KEY = ""

app = Flask(__name__)
DB = os.getenv("JOBHUNTER_DB", "jobhunter.db")
init_needed = True
JOOBLE_ENDPOINT = "https://hr.jooble.org/api/{key}"

PROFILE = {
    "positive": [
        "project manager", "voditelj projekta", "voditelj projekata", "voditelj projektnog",
        "project coordinator", "pmo", "business analyst", "poslovni analitičar",
        "process manager", "process specialist", "business process", "operational excellence",
        "operations manager", "operativni manager", "internal control", "unutarnja kontrola",
        "internal audit", "unutarnja revizija", "governance", "risk", "compliance",
        "corporate security", "security governance", "facility manager", "facility management",
        "program manager", "portfolio", "product manager", "product owner", "koordinator",
        "voditelj", "lead", "manager", "head"
    ],
    "negative": [
        "tehničar", "tehnicar", "technician", "helpdesk", "servisni tehničar",
        "servisni tehnicar", "developer", "programer", "software engineer", "skladištar",
        "skladistar", "warehouse", "operater", "monter"
    ]
}

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT UNIQUE, title TEXT, company TEXT, location TEXT, description TEXT,
        salary TEXT, source TEXT, url TEXT, posted TEXT, deadline TEXT,
        score INTEGER, reasons TEXT, fetched_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS saved(job_id INTEGER PRIMARY KEY, status TEXT DEFAULT 'saved')""")
    c.commit(); c.close()
    # Preconfigure the supplied Jooble key on first run, while allowing
    # an environment variable to override it on hosted deployments.
    if os.getenv("JOOBLE_API_KEY"):
        set_setting("JOOBLE_API_KEY", os.getenv("JOOBLE_API_KEY"))
    elif CONFIG_JOOBLE_API_KEY and not get_setting("JOOBLE_API_KEY"):
        set_setting("JOOBLE_API_KEY", CONFIG_JOOBLE_API_KEY)

def get_setting(key, default=""):
    c = db(); row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone(); c.close()
    return row["value"] if row else default

def set_setting(key, value):
    c = db(); c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)); c.commit(); c.close()

# Initialize the database when the module is imported by Gunicorn/Render as well as when run directly.
init_db()

def clean_url(url):
    if not url: return ""
    try:
        p = urlparse(url.strip())
        return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url.strip()

def is_direct_job_url(url):
    if not url or not url.startswith(("http://", "https://")): return False
    p = urlparse(url.lower())
    path = p.path.rstrip("/")
    if not path or path in ("", "/"):
        return False
    blocked = ["/poslovi", "/jobs", "/search", "/pretraga", "/trazeni-izraz", "/zanimanja/", "/djelatnosti/", "/kategorije/"]
    return not any(path == x or path.startswith(x + "/") for x in blocked)

def score_job(title, description, location):
    text = f"{title} {description}".lower()
    score = 45; reasons=[]
    positives = sum(1 for x in PROFILE["positive"] if x in text)
    negatives = sum(1 for x in PROFILE["negative"] if x in text)
    leadership = sum(1 for x in ["voditelj", "manager", "lead", "head", "project"] if x in title.lower())
    process = sum(1 for x in ["proces", "process", "koordin", "project", "governance", "control", "audit", "risk", "security"] if x in text)
    score += min(25, positives * 3)
    score += min(15, leadership * 4)
    score += min(12, process * 2)
    score -= min(40, negatives * 12)
    loc = location.lower()
    if "zagreb" in loc: score += 8; reasons.append("Zagreb")
    elif "hybrid" in loc or "remote" in loc: score += 7; reasons.append("hibridno/remote")
    if leadership: reasons.append("upravljačka/projektna razina")
    if process: reasons.append("procesi, koordinacija, kontrola ili sigurnost")
    if negatives: reasons.append("tehnički/hands-on elementi")
    return max(0,min(100,round(score))), "; ".join(reasons) or "osnovno podudaranje prema profilu"

def upsert_jobs(items):
    c=db(); now=datetime.now(timezone.utc).isoformat(); added=0; skipped=0
    for j in items:
        url=clean_url(j.get("link") or "")
        if not is_direct_job_url(url): skipped += 1; continue
        title=(j.get("title") or "").strip()
        if not title: skipped += 1; continue
        company=(j.get("company") or "").strip(); location=(j.get("location") or "").strip()
        desc=(j.get("snippet") or "").strip(); score,reasons=score_job(title,desc,location)
        uid=hashlib.sha256(url.encode()).hexdigest()
        c.execute("""INSERT INTO jobs(uid,title,company,location,description,salary,source,url,posted,deadline,score,reasons,fetched_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET title=excluded.title,company=excluded.company,
        location=excluded.location,description=excluded.description,salary=excluded.salary,source=excluded.source,
        url=excluded.url,posted=excluded.posted,deadline=excluded.deadline,score=excluded.score,reasons=excluded.reasons,fetched_at=excluded.fetched_at""",
        (uid,title,company,location,desc,str(j.get("salary") or ""),str(j.get("source") or "Jooble"),url,str(j.get("updated") or ""),"",score,reasons,now)); added+=1
    c.commit(); c.close(); return added,skipped

def fetch_jooble(api_key, keywords, location, radius="40"):
    if not api_key: raise RuntimeError("Nedostaje Jooble API ključ.")
    payload={"keywords":keywords,"location":location,"radius":radius,"page":1,"ResultOnPage":50,"companysearch":False}
    r=requests.post(JOOBLE_ENDPOINT.format(key=api_key),json=payload,timeout=30)
    if r.status_code == 403: raise RuntimeError("Jooble je odbio API ključ (403). Provjeri ključ za hr.jooble.org.")
    if r.status_code == 404: raise RuntimeError("Jooble API endpoint nije pronađen (404).")
    r.raise_for_status(); data=r.json(); return data.get("jobs",[]), data.get("totalCount",0)

@app.get("/api-status")
def api_status():
    key = get_setting("JOOBLE_API_KEY")
    if not key:
        return jsonify(ok=False, configured=False, message="API ključ nije postavljen."), 200
    return jsonify(ok=True, configured=True, message="Jooble API ključ je konfiguriran. Za provjeru valjanosti pokreni pretragu."), 200

@app.route("/")
def index():
    c=db(); jobs=c.execute("SELECT * FROM jobs ORDER BY score DESC,id DESC").fetchall(); saved={r["job_id"] for r in c.execute("SELECT job_id FROM saved")}; c.close()
    
    with open(os.path.join(os.path.dirname(__file__), "index.html",), "r", encoding="utf-8") as f:
        template = f.read()
    return render_template_string(template, jobs=jobs, saved=saved, has_key=bool(get_setting("JOOBLE_API_KEY")))

@app.post("/settings")
def settings():
    key=request.form.get("api_key","").strip()
    if key: set_setting("JOOBLE_API_KEY",key)
    return redirect(url_for("index"))

@app.post("/refresh")
def refresh():
    key=get_setting("JOOBLE_API_KEY")
    if not key: return jsonify(ok=False,error="Prvo spremi Jooble API ključ u Postavkama."),400
    keywords=request.form.get("keywords", "project manager, business analyst, process manager, internal control, corporate security, operations manager, voditelj")
    location=request.form.get("location","Zagreb")
    radius=request.form.get("radius","40")
    try:
        items,total=fetch_jooble(key,keywords,location,radius); added,skipped=upsert_jobs(items)
        return jsonify(ok=True,received=len(items),total=total,saved=added,skipped=skipped)
    except Exception as e: return jsonify(ok=False,error=str(e)),500

@app.post("/save/<int:job_id>")
def save(job_id):
    c=db(); c.execute("INSERT OR REPLACE INTO saved(job_id,status) VALUES(?,?)",(job_id,"saved")); c.commit(); c.close(); return redirect(url_for("index"))

@app.post("/unsave/<int:job_id>")
def unsave(job_id):
    c=db(); c.execute("DELETE FROM saved WHERE job_id=?",(job_id,)); c.commit(); c.close(); return redirect(url_for("index"))

@app.get("/health")
def health(): return jsonify(ok=True,jooble_configured=bool(get_setting("JOOBLE_API_KEY")))

if __name__=="__main__":
    init_db(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
