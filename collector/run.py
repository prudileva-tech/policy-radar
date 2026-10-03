from __future__ import annotations
import hashlib, html, json, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parents[1]
SOURCES = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
PRIORITIES = json.loads((ROOT / "config/priorities.json").read_text(encoding="utf-8"))
SEEDS = json.loads((ROOT / "config/seeds.json").read_text(encoding="utf-8")) if (ROOT / "config/seeds.json").exists() else []
OUT = ROOT / "docs/data/radar.json"
STATE = ROOT / "docs/data/state.json"
ARCHIVE = ROOT / "docs/data/archive.json"
UA = "PolicyRadarPilot/0.2 (+public legislative monitoring; GitHub Actions)"

def clean(text: str) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()

def iso_date(value) -> str | None:
    if not value:
        return None
    try:
        return dateparser.parse(str(value), dayfirst=True).astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def request(url):
    r = requests.get(url, timeout=40, headers={"User-Agent": UA})
    r.raise_for_status()
    return r

def fetch_rss(source):
    r = request(source["url"])
    feed = feedparser.parse(r.content)
    rows = []
    for e in feed.entries[:100]:
        title = clean(e.get("title", "Untitled"))
        link = e.get("link") or source["url"]
        summary = clean(e.get("summary") or e.get("description") or "")
        published = iso_date(e.get("published") or e.get("updated"))
        external_id = str(e.get("id") or e.get("guid") or link or title)
        rows.append({"external_id": external_id, "title": title, "url": link, "text": summary, "published_at": published})
    return rows

def fetch_veklep(source):
    soup = BeautifulSoup(request(source["url"]).text, "html.parser")
    rows, seen = [], set()
    for a in soup.select('a[href*="/portal/veklep/material/"]'):
        href = a.get("href", "")
        if not href:
            continue
        url = urljoin(source["url"], href)
        if url in seen:
            continue
        seen.add(url)
        parent = a.find_parent(["article", "div", "li", "tr"])
        title = clean(a.get_text(" ", strip=True))
        context = clean(parent.get_text(" ", strip=True)) if parent else title
        if len(title) < 8:
            title = context
        if len(title) < 8:
            continue
        rows.append({
            "external_id": href.rstrip("/").split("/")[-1],
            "title": title[:500],
            "url": url,
            "text": context[:1200],
            "published_at": None,
            "procedure_stage": "VeKLEP / příprava vlády"
        })
    return rows[:150]

def psp_pages(source):
    first = request(source["url"])
    soup = BeautifulSoup(first.text, "html.parser")
    urls = {source["url"]}
    mode = "stz=1" if "stz=1" in source["url"] else "tx=1"
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tisky.sqw" in href and mode in href:
            urls.add(urljoin(source["url"], href))
    return list(urls)[:15]

def fetch_psp(source):
    rows, seen = [], set()
    for page_url in psp_pages(source):
        soup = BeautifulSoup(request(page_url).text, "html.parser")
        current_date = None
        for tr in soup.find_all("tr"):
            cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td","th"])]
            if not cells:
                continue
            joined = " | ".join(cells)
            dm = re.search(r"(\d{1,2}\.\s*[^\d|]{3,15}\s*202\d)", joined, re.I)
            if dm and len(cells) <= 2:
                current_date = iso_date(dm.group(1))
                continue
            a = tr.find("a", href=True)
            if len(cells) < 2 or not a:
                continue
            href = a.get("href", "")
            if not any(x in href for x in ["historie.sqw", "tiskt.sqw"]):
                continue
            url = urljoin(page_url, href)
            key = clean(cells[0]) + "|" + url
            if key in seen:
                continue
            seen.add(key)
            number = clean(cells[0])
            title = clean(cells[1]) if len(cells) > 1 else clean(a.get_text(" ", strip=True))
            doc_type = clean(cells[2]) if len(cells) > 2 else "Sněmovní dokument"
            stage = clean(cells[3]) if len(cells) > 3 else ""
            if not title or title.lower() in ["krátký název", "název"]:
                continue
            rows.append({
                "external_id": re.sub(r"\s+", "", number) or url,
                "title": title,
                "url": url,
                "text": f"{doc_type}. {stage}".strip(),
                "published_at": current_date,
                "procedure_stage": stage,
                "source_doc_type": doc_type,
                "parliament_number": number
            })
    return rows[:500]

def fetch_senate(source):
    soup = BeautifulSoup(request(source["url"]).text, "html.parser")
    rows, seen = [], set()
    for tr in soup.find_all("tr"):
        cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td","th"])]
        if len(cells) < 4:
            continue
        if cells[0].lower().startswith("obdob"):
            continue
        number = cells[-2]
        title = cells[-1]
        if not re.search(r"\d", number) or len(title) < 5:
            continue
        a = tr.find("a", href=True)
        url = urljoin(source["url"], a["href"]) if a else source["url"]
        key = f"{number}|{title}"
        if key in seen:
            continue
        seen.add(key)
        published = iso_date(cells[2]) if len(cells) >= 5 else None
        rows.append({
            "external_id": key,
            "title": title,
            "url": url,
            "text": f"Senátní tisk {number}. Schůze {cells[1] if len(cells)>1 else ''}.",
            "published_at": published,
            "procedure_stage": "Senát",
            "source_doc_type": "Senátní tisk",
            "parliament_number": number
        })
    return rows[:250]

def fetch_generic_html(source):
    soup = BeautifulSoup(request(source["url"]).text, "html.parser")
    rows, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 18:
            continue
        url = urljoin(source["url"], a["href"])
        if url in seen or url.startswith("javascript:"):
            continue
        seen.add(url)
        parent = a.find_parent(["article","li","div","tr"])
        context = clean(parent.get_text(" ", strip=True)) if parent else title
        hay = (title + " " + context).lower()
        relevant_terms = ["uměl", "digit", "kyber", "cloud", "data", "informač", "kritick", "obrann", "vojensk", "dvojí", "dual", "bezpečnost", "diana", "technolog", "export"]
        if not any(t in hay for t in relevant_terms):
            continue
        dm = re.search(r"(\d{1,2}\.\s*\d{1,2}\.\s*202\d|\d{1,2}\.\s*[a-zá-ž]+\s*202\d)", context, re.I)
        published = iso_date(dm.group(1)) if dm else None
        rows.append({
            "external_id": hashlib.sha1(url.encode()).hexdigest()[:18],
            "title": title[:500], "url": url, "text": context[:1500],
            "published_at": published, "procedure_stage": "Aktuální resortní / regulační materiál"
        })
    return rows[:120]

def infer_metadata(source, row):
    title, text = row["title"], row.get("text","")
    hay = f"{title} {text}".lower()
    if row.get("source_doc_type"):
        doc_type = row["source_doc_type"]
    elif source["id"] == "veklep":
        doc_type = "Vládní legislativní materiál"
    elif "coreper" in source["id"]:
        doc_type = "COREPER agenda / meeting item"
    elif "wp" in source["id"]:
        doc_type = "Council working party item"
    elif source["id"] == "council_latest":
        doc_type = "Council document"
    elif "eurlex" in source["id"]:
        doc_type = "EU regulation / related act" if "regulation" in hay else ("EU directive / related act" if "directive" in hay else "EUR-Lex legal document")
    elif "ep_" in source["id"]:
        doc_type = "European Parliament item"
    else:
        doc_type = "Policy document"

    consultation_words = ["consultation","call for evidence","feedback period","public consultation",
                          "připomínkové řízení","meziresortní připomínkové řízení","připomínky do"]
    if any(w in hay for w in consultation_words):
        commentability = "YES"
    elif source["id"] == "veklep":
        commentability = "VERIFY"
    else:
        commentability = "NO_SIGNAL"

    deadline = None
    for p in [
        r"(?:deadline|until|by)\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        r"(?:deadline|until|by)\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"(?:připomínky\s+do|lhůta\s+do)\s+(\d{1,2}[.]\s*\d{1,2}[.]\s*\d{4})"
    ]:
        m = re.search(p, hay, re.I)
        if m:
            deadline = m.group(1)
            break

    stage = row.get("procedure_stage") or ""
    summary = clean(text)[:280]
    if stage and stage.lower() not in summary.lower():
        summary = (stage + ". " + summary).strip()
    if not summary:
        summary = "Veřejně dostupná položka v legislativním procesu. Otevřete zdroj pro detail."
    return doc_type, commentability, deadline, summary, stage

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
    urgency_words = ["deadline","consultation","amendment","coreper","trilogue","trialogue","vote","meeting",
                     "pozměňovací","3. čtení","2. čtení","připomín","lhůt","jednání vlády"]
    urgency = "HIGH" if any(w in hay for w in urgency_words) and relevance != "LOW" else ("MEDIUM" if relevance != "LOW" else "LOW")
    return relevance, urgency, matches, score

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def weekly_score(item):
    if not any(m.get("priority_id") == "DIG-001" for m in item.get("matches", [])):
        return -1
    hay = f"{item.get('title','')} {item.get('text','')} {item.get('procedure_stage','')}".lower()
    s = item.get("score", 0)
    boosts = {
        "trilogue": 35, "trialogue": 35, "coreper": 28, "3. čtení": 25,
        "pozměňovací": 24, "amendment": 24, "2. čtení": 20, "vote": 18,
        "consultation": 18, "deadline": 18, "výbor": 12, "committee": 12,
        "working party": 10
    }
    for word, pts in boosts.items():
        if word in hay:
            s += pts
    if item.get("status") == "CHANGED":
        s += 15
    elif item.get("status") == "NEW":
        s += 10
    if item.get("comment_deadline"):
        s += 15
    return s

def main():
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    state = load_json(STATE, {})
    archive = load_json(ARCHIVE, {})
    next_state = dict(state)
    current_items, health = [], []
    seen_keys = set()

    for source in SOURCES:
        started = time.time()
        try:
            kind = source["kind"]
            rows = fetch_rss(source) if kind == "rss" else fetch_veklep(source) if kind == "veklep" else fetch_psp(source) if kind == "psp" else fetch_senate(source) if kind == "senate" else fetch_generic_html(source) if kind == "generic_html" else []
            health.append({"source": source["name"], "status": "OK", "count": len(rows), "seconds": round(time.time()-started,2)})
        except Exception as exc:
            health.append({"source": source["name"], "status": "ERROR", "count": 0, "error": str(exc)[:220], "seconds": round(time.time()-started,2)})
            continue

        for row in rows:
            key = f"{source['id']}::{row['external_id']}"
            seen_keys.add(key)
            fingerprint = hashlib.sha256((row["title"]+"\n"+row.get("text","")+"\n"+row.get("procedure_stage","")).encode("utf-8", errors="ignore")).hexdigest()
            previous = state.get(key)
            status = "NEW" if previous is None else ("CHANGED" if previous != fingerprint else "UNCHANGED")
            next_state[key] = fingerprint
            relevance, urgency, matches, score = classify(row["title"], row.get("text",""))
            doc_type, commentability, deadline, short_summary, stage = infer_metadata(source, row)

            attention = "ACTION" if relevance == "HIGH" or (urgency == "HIGH" and relevance != "LOW") else ("WATCH" if relevance == "MEDIUM" or urgency == "MEDIUM" or commentability == "YES" else "FYI")
            reason = "Bez shody s aktuálně uloženou prioritou."
            if matches:
                reason = "Shoda s prioritami: " + ", ".join(sorted({m["topic"] for m in matches}))
            if commentability == "YES":
                reason += " · Zdroj signalizuje možnost připomínkování."
            elif commentability == "VERIFY":
                reason += " · Ověřit stav připomínkování ve VeKLEP."

            old = archive.get(key, {})
            first_seen = old.get("first_seen", now)
            last_changed = now if status in ("NEW","CHANGED") else old.get("last_changed", first_seen)
            events = old.get("events", [])
            if status in ("NEW","CHANGED"):
                events = ([{"at": now, "type": status, "title": row["title"], "stage": stage}] + events)[:30]

            item = {
                **row,
                "key": key, "source_id": source["id"], "source": source["name"], "jurisdiction": source["jurisdiction"],
                "status": status, "relevance": relevance, "urgency": urgency, "attention": attention,
                "document_type": doc_type, "commentability": commentability, "comment_deadline": deadline,
                "short_summary": short_summary, "why_it_matters": reason, "procedure_stage": stage,
                "score": score, "matches": matches, "first_seen": first_seen, "last_seen": now,
                "last_changed": last_changed, "is_current": True, "events": events, "fingerprint": fingerprint
            }
            archive[key] = item
            current_items.append(item)

    # Merge verified Czech seed items so the pilot is useful even when an official site changes markup.
    seed_source = {"id":"verified_seed","name":"Ověřené aktuální CZ položky","jurisdiction":"CZ"}
    for row in SEEDS:
        key = f"verified_seed::{row['external_id']}"
        seen_keys.add(key)
        relevance, urgency, matches, score = classify(row["title"], row.get("text",""))
        doc_type = row.get("source_doc_type","Policy item")
        stage = row.get("procedure_stage","")
        old = archive.get(key,{})
        fingerprint = hashlib.sha256((row["title"]+"\n"+row.get("text","")+"\n"+stage).encode()).hexdigest()
        previous = state.get(key)
        status = "NEW" if previous is None else ("CHANGED" if previous != fingerprint else "UNCHANGED")
        next_state[key] = fingerprint
        attention = "ACTION" if relevance=="HIGH" or (urgency=="HIGH" and relevance!="LOW") else ("WATCH" if relevance=="MEDIUM" or urgency=="MEDIUM" else "FYI")
        item = {**row,"key":key,"source_id":"verified_seed","source":row.get("seed_source","Ověřený zdroj"),
                "jurisdiction":"CZ","status":status,"relevance":relevance,"urgency":urgency,"attention":attention,
                "document_type":doc_type,"commentability":"NO_SIGNAL","comment_deadline":None,
                "short_summary":clean(row.get("text",""))[:280],
                "why_it_matters":"Shoda s prioritami: "+", ".join(sorted({m["topic"] for m in matches})) if matches else "Aktuální český policy/regulatory file.",
                "score":score,"matches":matches,"first_seen":old.get("first_seen",now),"last_seen":now,
                "last_changed":now if status in ("NEW","CHANGED") else old.get("last_changed",old.get("first_seen",now)),
                "is_current":True,"events":old.get("events",[]),"fingerprint":fingerprint}
        archive[key]=item
        current_items.append(item)

    # Preserve historical records. Sources that no longer list a record remain searchable in History.
    for key, old in list(archive.items()):
        if key not in seen_keys:
            old["is_current"] = False

    all_items = list(archive.values())
    attention_rank = {"ACTION":0,"WATCH":1,"FYI":2}
    all_items.sort(key=lambda x: (
        not x.get("is_current",False),
        attention_rank.get(x.get("attention","FYI"),9),
        -(weekly_score(x) if weekly_score(x) >= 0 else 0),
        x.get("last_changed","")
    ))

    digital = [i for i in all_items if i.get("is_current") and weekly_score(i) >= 0]
    digital.sort(key=weekly_score, reverse=True)
    top3 = []
    used = set()
    for i in digital:
        signature = re.sub(r"\W+"," ",i.get("title","").lower()).strip()[:100]
        if signature in used:
            continue
        used.add(signature)
        top3.append({
            "key": i["key"], "title": i["title"], "url": i["url"], "jurisdiction": i["jurisdiction"],
            "document_type": i["document_type"], "procedure_stage": i.get("procedure_stage") or "",
            "attention": i["attention"], "status": i["status"], "short_summary": i["short_summary"],
            "why_it_matters": i["why_it_matters"], "score": weekly_score(i), "comment_deadline": i.get("comment_deadline")
        })
        if len(top3) == 3:
            break

    payload = {
        "generated_at": now, "mode":"free-pilot-no-ai",
        "stats":{
            "total": len(all_items),
            "current": sum(bool(i.get("is_current")) for i in all_items),
            "new": sum(i.get("status")=="NEW" for i in current_items),
            "changed": sum(i.get("status")=="CHANGED" for i in current_items),
            "action": sum(i.get("attention")=="ACTION" and i.get("is_current") for i in all_items),
            "watch": sum(i.get("attention")=="WATCH" and i.get("is_current") for i in all_items),
            "cz": sum(i.get("jurisdiction")=="CZ" and i.get("is_current") for i in all_items),
            "eu": sum(i.get("jurisdiction")=="EU" and i.get("is_current") for i in all_items)
        },
        "top_digital_week": top3,
        "health": health,
        "items": all_items[:1200],
        "priorities": PRIORITIES
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE.write_text(json.dumps(next_state, ensure_ascii=False, indent=2), encoding="utf-8")
    ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False))
    if not any(h["status"]=="OK" for h in health):
        sys.exit(2)

if __name__ == "__main__":
    main()
