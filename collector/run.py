from __future__ import annotations
import hashlib, html, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parents[1]
SOURCES = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
PRIORITIES = json.loads((ROOT / "config/priorities.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs/data/radar.json"
STATE = ROOT / "docs/data/state.json"
UA = "PolicyRadarPilot/0.1 (+public legislative monitoring; GitHub Actions)"

def clean(text: str) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()

def iso_date(value) -> str | None:
    if not value:
        return None
    try:
        return dateparser.parse(str(value)).astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def fetch_rss(source):
    r = requests.get(source["url"], timeout=35, headers={"User-Agent": UA})
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    rows = []
    for e in feed.entries[:80]:
        title = clean(e.get("title", "Untitled"))
        link = e.get("link") or source["url"]
        summary = clean(e.get("summary") or e.get("description") or "")
        published = iso_date(e.get("published") or e.get("updated"))
        external_id = str(e.get("id") or e.get("guid") or link or title)
        rows.append({"external_id": external_id, "title": title, "url": link, "text": summary, "published_at": published})
    return rows

def fetch_veklep(source):
    r = requests.get(source["url"], timeout=35, headers={"User-Agent": UA})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows, seen = [], set()
    for a in soup.select('a[href*="/portal/veklep/material/"]'):
        href = a.get("href", "")
        if not href:
            continue
        url = urljoin(source["url"], href)
        if url in seen:
            continue
        seen.add(url)
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 12:
            parent = a.find_parent(["article", "div", "li", "tr"])
            title = clean(parent.get_text(" ", strip=True)) if parent else title
        if len(title) < 12:
            continue
        external_id = href.rstrip("/").split("/")[-1]
        rows.append({"external_id": external_id, "title": title[:500], "url": url, "text": title, "published_at": None})
    return rows[:100]

def infer_metadata(source, title: str, text: str):
    hay = f"{title} {text}".lower()

    # Human-readable document type. This is intentionally heuristic in the free pilot.
    if source["id"] == "veklep":
        doc_type = "CZ legislative material"
    elif "coreper" in source["id"]:
        doc_type = "COREPER agenda / meeting item"
    elif "working parties" in source["name"].lower() or "wp" in source["id"]:
        doc_type = "Council working party item"
    elif "council_latest" == source["id"]:
        doc_type = "Council document"
    elif "eurlex" in source["id"]:
        if "directive" in hay:
            doc_type = "EU directive / related act"
        elif "regulation" in hay:
            doc_type = "EU regulation / related act"
        elif "decision" in hay:
            doc_type = "EU decision / related act"
        else:
            doc_type = "EUR-Lex legal document"
    elif "ep_" in source["id"]:
        doc_type = "European Parliament item"
    else:
        doc_type = "Policy document"

    # Commentability: only assert YES when the feed text itself signals a consultation/call.
    consultation_words = ["consultation", "call for evidence", "feedback period", "public consultation",
                          "připomínkové řízení", "meziresortní připomínkové řízení", "připomínky do"]
    if any(w in hay for w in consultation_words):
        commentability = "YES"
    elif source["id"] == "veklep":
        commentability = "VERIFY"
    else:
        commentability = "NO_SIGNAL"

    # Best-effort deadline extraction near words such as deadline / until / do.
    deadline = None
    patterns = [
        r"(?:deadline|until|by)\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        r"(?:deadline|until|by)\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"(?:připomínky\s+do|lhůta\s+do|do)\s+(\d{1,2}[.]\s*\d{1,2}[.]\s*\d{4})"
    ]
    for p in patterns:
        m = re.search(p, hay, flags=re.IGNORECASE)
        if m:
            deadline = m.group(1)
            break

    # Short deterministic summary for the free phase.
    summary = clean(text)[:260]
    if not summary:
        if "meeting" in doc_type.lower() or "agenda" in doc_type.lower() or "working party" in doc_type.lower():
            summary = "Upcoming EU meeting or working-party item. Open the source for agenda and supporting documents."
        else:
            summary = "New or updated public policy item. Open the source for the full document and procedural context."

    return doc_type, commentability, deadline, summary

def classify(title: str, text: str):
    hay = f"{title} {text}".lower()
    matches, score = [], 0
    for p in PRIORITIES:
        hit = sorted({kw for kw in p["keywords"] if kw.lower() in hay})
        if hit:
            local = min(5, len(hit)) * int(p.get("weight", 3))
            score += local
            matches.append({"priority_id": p["id"], "topic": p["topic"], "owner": p["owner"], "keywords": hit, "weight": p["weight"], "position": p["position"]})
    relevance = "HIGH" if score >= 15 else ("MEDIUM" if score >= 6 else "LOW")
    urgency_words = ["deadline", "consultation", "amendment", "coreper", "trilogue", "vote", "meeting", "připomín", "lhůt", "jednání vlády"]
    urgency = "HIGH" if any(w in hay for w in urgency_words) and relevance != "LOW" else ("MEDIUM" if relevance != "LOW" else "LOW")
    return relevance, urgency, matches, score

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def main():
    now = datetime.now(timezone.utc).isoformat()
    state = load_state()
    next_state = dict(state)
    items, health = [], []
    for source in SOURCES:
        started = time.time()
        try:
            if source["kind"] == "rss":
                rows = fetch_rss(source)
            elif source["kind"] == "veklep":
                rows = fetch_veklep(source)
            else:
                raise ValueError(f"Unknown source kind: {source['kind']}")
            health.append({"source": source["name"], "status": "OK", "count": len(rows), "seconds": round(time.time()-started, 2)})
        except Exception as exc:
            health.append({"source": source["name"], "status": "ERROR", "count": 0, "error": str(exc)[:220], "seconds": round(time.time()-started, 2)})
            continue
        for row in rows:
            key = f"{source['id']}::{row['external_id']}"
            fingerprint = hashlib.sha256((row["title"] + "\n" + row["text"]).encode("utf-8", errors="ignore")).hexdigest()
            previous = state.get(key)
            status = "NEW" if previous is None else ("CHANGED" if previous != fingerprint else "UNCHANGED")
            next_state[key] = fingerprint
            relevance, urgency, matches, score = classify(row["title"], row["text"])
            doc_type, commentability, deadline, short_summary = infer_metadata(source, row["title"], row["text"])

            attention = "FYI"
            if relevance == "HIGH" or (urgency == "HIGH" and relevance != "LOW"):
                attention = "ACTION"
            elif relevance == "MEDIUM" or urgency == "MEDIUM" or commentability == "YES":
                attention = "WATCH"

            reason = "No tracked priority matched yet."
            if matches:
                reason = "Matches: " + ", ".join(sorted({m["topic"] for m in matches}))
            if commentability == "YES":
                reason += " · Public/explicit commenting signal detected."
            elif commentability == "VERIFY":
                reason += " · Check VeKLEP procedure for commenting status."

            if status == "UNCHANGED" and relevance == "LOW":
                continue
            items.append({
                **row,
                "source_id": source["id"],
                "source": source["name"],
                "jurisdiction": source["jurisdiction"],
                "status": status,
                "relevance": relevance,
                "urgency": urgency,
                "attention": attention,
                "document_type": doc_type,
                "commentability": commentability,
                "comment_deadline": deadline,
                "short_summary": short_summary,
                "why_it_matters": reason,
                "score": score,
                "matches": matches,
                "detected_at": now
            })
    attention_rank = {"ACTION": 0, "WATCH": 1, "FYI": 2}
    items.sort(key=lambda x: (attention_rank.get(x["attention"], 9), x["status"] != "NEW", {"HIGH":0,"MEDIUM":1,"LOW":2}[x["relevance"]], x["title"].lower()))
    payload = {
        "generated_at": now,
        "mode": "free-pilot-no-ai",
        "stats": {
            "total": len(items),
            "new": sum(i["status"] == "NEW" for i in items),
            "changed": sum(i["status"] == "CHANGED" for i in items),
            "high": sum(i["relevance"] == "HIGH" for i in items),
            "medium": sum(i["relevance"] == "MEDIUM" for i in items),
            "action": sum(i["attention"] == "ACTION" for i in items),
            "watch": sum(i["attention"] == "WATCH" for i in items),
            "cz": sum(i["jurisdiction"] == "CZ" for i in items),
            "eu": sum(i["jurisdiction"] == "EU" for i in items)
        },
        "health": health,
        "items": items[:300],
        "priorities": PRIORITIES
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE.write_text(json.dumps(next_state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False))
    if not any(h["status"] == "OK" for h in health):
        sys.exit(2)

if __name__ == "__main__":
    main()
