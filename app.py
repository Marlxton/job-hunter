import os, re, json, sqlite3
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

KEY = os.environ.get("JOOBLE_API_KEY", "").strip()
JOOBLE_URL = f"https://hr.jooble.org/api/{KEY}" if KEY else ""

DEFAULT_PROFILE = {
    "name": "Toni Skender",
    "target_roles": ["Project Manager","Business Analyst","Process Manager","Internal Control","Information Security","Security Governance","Operations Manager"],
    "location": "Zagreb",
    "work_modes": ["Hybrid","Office","Remote"],
    "excluded": ["helpdesk","developer","servis","skladište","tehničar"],
    "skills": [],
    "cv_filename": "",
    "cv_text": "",
    "analysis": {}
}

ROLE_MAP = {
    "Project Manager": ["project management","project manager","projekt","projektn","vođenje","koordinacija","planiranje","implementacija"],
    "Business Analyst": ["business analysis","business analyst","poslovna analiza","zahtjev","requirements","stakeholder","analiza podataka","proces"],
    "Process Manager": ["process management","process manager","upravljanje procesima","poslovni proces","unapređenje procesa","procedure","procedur"],
    "Internal Control": ["internal control","internal audit","unutarnj","kontrola","nadzor","compliance","usklađen"],
    "Information Security": ["information security","cyber security","informacijska sigurnost","sigurnost","security","it security","mrež","videonadzor"],
    "Security Governance": ["governance","security governance","risk","rizik","kontrole","controls","policy","politike"],
    "Operations Manager": ["operations","operativ","operativno upravljanje","organizacija rada","upravljanje","koordinacija zaposlenika"]
}

SKILL_MAP = {
    "Project Management":["project management","project manager","projekt","projektn"],
    "Process Management":["process management","process manager","proces","procedure","procedur","unapređen"],
    "Business Analysis":["business analysis","business analyst","poslovna analiza","zahtjev","requirements","stakeholder"],
    "Internal Control":["internal control","internal audit","unutarnj","nadzor","kontrola"],
    "Risk Management":["risk","rizik","operativni rizik","sigurnosni rizik"],
    "Information Security":["information security","informacijska sigurnost","it security","security"],
    "Security Systems":["videonadzor","kontrola pristupa","sigurnosni sustav","security systems"],
    "IT Infrastructure":["it infrastrukt","mrež","network","windows","mikrotik"],
    "Data & Reporting":["analiza podataka","izvještaj","reporting","evidenc"],
    "Team Coordination":["koordinacija","upravljanje zaposlen","organizacija rada","team"],
    "Problem Solving":["rješavanje problema","analiza problema","korektiv","problem solving"],
    "Technical Operations":["tehnič","tehnička","oprema","sustav","implementacija","testiranje"]
}

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    c=db()
    c.execute("CREATE TABLE IF NOT EXISTS profile(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS saved(id TEXT PRIMARY KEY, job TEXT NOT NULL, status TEXT DEFAULT 'saved', created_at TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, created_at TEXT NOT NULL)")
    if not c.execute("SELECT 1 FROM profile WHERE id=1").fetchone():
        c.execute("INSERT INTO profile(id,data) VALUES(1,?)",(json.dumps(DEFAULT_PROFILE,ensure_ascii=False),))
    c.commit(); c.close()

def profile():
    c=db(); r=c.execute("SELECT data FROM profile WHERE id=1").fetchone(); c.close()
    return json.loads(r["data"]) if r else DEFAULT_PROFILE.copy()

def save_profile(p):
    c=db(); c.execute("UPDATE profile SET data=? WHERE id=1",(json.dumps(p,ensure_ascii=False),)); c.commit(); c.close()

def event(k):
    c=db(); c.execute("INSERT INTO events(kind,created_at) VALUES(?,?)",(k,datetime.now(timezone.utc).isoformat())); c.commit(); c.close()

def extract(path):
    if path.suffix.lower()==".pdf":
        if not PdfReader: raise RuntimeError("PDF parser nije dostupan.")
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if path.suffix.lower()==".docx":
        if not Document: raise RuntimeError("DOCX parser nije dostupan.")
        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    raise ValueError("Podržani su PDF i DOCX.")

def count_hits(text, phrases):
    t=text.lower()
    return sum(t.count(p.lower()) for p in phrases)

def analyse_cv(text):
    t=text.lower()
    skills=[]
    for label, phrases in SKILL_MAP.items():
        hits=count_hits(t,phrases)
        if hits: skills.append({"name":label,"hits":hits})
    skills.sort(key=lambda x:x["hits"],reverse=True)

    roles=[]
    for role, phrases in ROLE_MAP.items():
        hits=count_hits(t,phrases)
        if hits:
            strength=min(99,55+hits*7)
            roles.append({"name":role,"hits":hits,"strength":strength})
    roles.sort(key=lambda x:x["strength"],reverse=True)

    years=[]
    for m in re.finditer(r"(20\d{2})\s*[–-]\s*(20\d{2}|danas|present)",t):
        start=int(m.group(1)); end=2026 if m.group(2) in ("danas","present") else int(m.group(2))
        if 2000<=start<=2026: years.append(max(0,end-start))
    exp_years=max(years) if years else 0

    leadership = min(100, 45 + count_hits(t,["voditelj","upravljanje","koordinacija","organizacija rada","zaposlenika","vođenje"])*7)
    process = min(100, 45 + count_hits(t,["proces","procedure","unapređenje","kontrola"])*7)
    technical = min(100, 40 + count_hits(t,["it","mrež","videonadzor","tehnič","sustav"])*6)
    analysis = min(100, 40 + count_hits(t,["analiza","problem","izvještaj","podaci","zahtjev"])*7)

    return {
        "skills":skills[:12],
        "roles":roles[:8],
        "experience_years":exp_years,
        "leadership":leadership,
        "process":process,
        "technical":technical,
        "analysis":analysis,
        "text_length":len(text)
    }

def job_score(j,p):
    blob=(" ".join(str(j.get(k,"")) for k in ["title","snippet","company","location"])).lower()
    title=(j.get("title") or "").lower()
    a=p.get("analysis") or {}
    role_scores=[]
    for role in p.get("target_roles",[]):
        phrases=ROLE_MAP.get(role, [role])
        hits=count_hits(blob,phrases)
        if hits: role_scores.append(min(100,50+hits*12))
    role_match=max(role_scores) if role_scores else 25

    cv_skills=[x["name"] for x in a.get("skills",[])]
    matched=[s for s in cv_skills if any(q in blob for q in SKILL_MAP.get(s,[]))]
    skill_match=min(100,40+len(matched)*10) if matched else 25

    loc=100 if p.get("location","").lower() in (j.get("location") or "").lower() else 55
    senior=100 if any(x in title for x in ["manager","voditelj","lead","senior","head","specialist","specijalist","analyst","analiti"]) else 65

    negative=sum(1 for x in p.get("excluded",[]) if x.lower() in blob)
    penalty=min(45,negative*15)

    # Weighted score: role 35%, skills 30%, experience/profile 20%, location 10%, seniority 5%.
    score=round(role_match*.35+skill_match*.30+((a.get("analysis",0) if isinstance(a.get("analysis"),(int,float)) else 0) or 70)*.20+loc*.10+senior*.05-penalty)
    score=max(0,min(100,score))

    reasons=[]
    if role_match>=70: reasons.append("pozicija je vrlo bliska ciljanoj ulozi")
    if matched: reasons.append("podudaraju se: "+", ".join(matched[:3]))
    if a.get("experience_years"): reasons.append(f"CV pokazuje ~{a['experience_years']}+ godina relevantnog iskustva")
    if loc==100: reasons.append("lokacija odgovara")
    if negative: reasons.append("sadrži neželjeni element")
    if not reasons: reasons.append("podudaranje prema nazivu i opisu oglasa")

    breakdown={
        "role":round(role_match),
        "skills":round(skill_match),
        "experience":round((a.get("leadership",65)+a.get("process",65)+a.get("analysis",65))/3),
        "location":round(loc),
        "seniority":round(senior)
    }
    return score,reasons,breakdown

@app.get("/")
def home(): return send_from_directory(BASE,"index.html")

@app.get("/api/health")
def health(): return jsonify({"ok":True,"api_configured":bool(KEY)})

@app.get("/api/profile")
def get_profile_api():
    p=profile()
    out={**p,"cv_text":bool(p.get("cv_text"))}
    return jsonify(out)

@app.post("/api/profile")
def update_profile():
    p=profile(); d=request.get_json(silent=True) or {}
    for k in ["target_roles","location","work_modes","excluded","skills"]:
        if k in d: p[k]=d[k]
    save_profile(p); event("profile_updated")
    return jsonify({"ok":True,"profile":{**p,"cv_text":bool(p.get("cv_text"))}})

@app.post("/api/cv")
def cv():
    f=request.files.get("cv")
    if not f or not f.filename: return jsonify({"error":"Nije odabran CV."}),400
    name=secure_filename(f.filename); suffix=Path(name).suffix.lower()
    if suffix not in [".pdf",".docx"]: return jsonify({"error":"Podržani su PDF i DOCX."}),400
    path=UPLOADS/name; f.save(path)
    try: text=extract(path)
    except Exception as e: return jsonify({"error":str(e)}),400

    analysis=analyse_cv(text)
    p=profile()
    p["cv_filename"]=name; p["cv_text"]=text[:120000]; p["analysis"]=analysis
    detected=[x["name"] for x in analysis["skills"]]
    p["skills"]=detected
    detected_roles=[x["name"] for x in analysis["roles"]]
    # Keep strong user targets, then append roles actually detected in the CV.
    p["target_roles"]=list(dict.fromkeys(p.get("target_roles",[])+detected_roles))
    save_profile(p); event("cv_uploaded")
    return jsonify({"ok":True,"filename":name,"analysis":analysis,"profile":{**p,"cv_text":True}})

@app.post("/api/search")
def search():
    if not KEY: return jsonify({"error":"Jooble API ključ nije postavljen na serveru."}),500
    d=request.get_json(silent=True) or {}
    payload={"keywords":(d.get("keywords") or "Project Manager, Business Analyst, Process Manager").strip(),
             "location":(d.get("location") or "Zagreb").strip(),
             "radius":str(d.get("radius") or "40"),"page":"1","ResultOnPage":30,"companysearch":False}
    try: r=requests.post(JOOBLE_URL,json=payload,timeout=25)
    except requests.RequestException as e: return jsonify({"error":f"Greška pri povezivanju s Joobleom: {e}"}),502
    if r.status_code!=200: return jsonify({"error":f"Jooble je vratio HTTP {r.status_code}."}),r.status_code
    raw=r.json(); p=profile(); jobs=[]
    for j in raw.get("jobs",[]):
        s,why,bd=job_score(j,p)
        jobs.append({"id":str(j.get("id") or j.get("link") or ""), "title":j.get("title") or "Bez naziva",
          "company":j.get("company") or "Nepoznat poslodavac","location":j.get("location") or "",
          "snippet":j.get("snippet") or "","salary":j.get("salary") or "","type":j.get("type") or "",
          "source":j.get("source") or "Jooble","updated":j.get("updated") or "","link":j.get("link") or "",
          "score":s,"reasons":why,"breakdown":bd})
    jobs.sort(key=lambda x:(-x["score"],x["title"]))
    event("search")
    return jsonify({"totalCount":raw.get("totalCount",len(jobs)),"jobs":jobs,"profile_used":bool(p.get("cv_text"))})

@app.get("/api/saved")
def saved():
    c=db(); rows=c.execute("SELECT id,job,status,created_at FROM saved ORDER BY created_at DESC").fetchall(); c.close()
    return jsonify([{"id":r["id"],"job":json.loads(r["job"]),"status":r["status"],"created_at":r["created_at"]} for r in rows])

@app.post("/api/saved")
def save_job():
    d=request.get_json(silent=True) or {}; j=d.get("job") or {}; jid=str(j.get("id") or j.get("link") or "")
    if not jid:return jsonify({"error":"Nedostaje ID oglasa."}),400
    c=db(); c.execute("INSERT OR REPLACE INTO saved(id,job,status,created_at) VALUES(?,?,?,?)",(jid,json.dumps(j,ensure_ascii=False),d.get("status","saved"),datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
    event("job_saved"); return jsonify({"ok":True})

@app.delete("/api/saved/<path:jid>")
def delete_saved(jid):
    c=db(); c.execute("DELETE FROM saved WHERE id=?",(jid,)); c.commit(); c.close(); return jsonify({"ok":True})

@app.post("/api/view")
def view(): event("viewed"); return jsonify({"ok":True})

@app.get("/api/analytics")
def analytics():
    c=db()
    out={k:c.execute("SELECT COUNT(*) c FROM events WHERE kind=?",(k,)).fetchone()["c"] for k in ["search","viewed"]}
    out["saved"]=c.execute("SELECT COUNT(*) c FROM saved").fetchone()["c"]
    out["applied"]=c.execute("SELECT COUNT(*) c FROM saved WHERE status='applied'").fetchone()["c"]
    c.close(); return jsonify(out)

init()
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
