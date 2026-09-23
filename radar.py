#!/usr/bin/env python3
"""job-radar: scan 50 company career boards for new SRE/DevOps/Cloud jobs and alert.

  python radar.py run          # scan, alert on new jobs, write docs/data.json
  python radar.py validate     # show which company boards work
  python radar.py test-alert   # send a sample alert to your phone

Runs on GitHub Actions every 2 hours (see .github/workflows/radar.yml).
Alert channels come from environment variables / GitHub Secrets:
  NTFY_TOPIC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import json
import logging
import os
import platform
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
STATE_PATH = BASE / "state" / "state.json"
DATA_PATH = BASE / "docs" / "data.json"
TIMEOUT = 25
SEARCH_TERMS = ["site reliability", "devops", "cloud engineer", "platform engineer", "infrastructure"]
COUNTRY = {"in": "India", "ae": "United Arab Emirates"}
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-alert script)", "Accept": "application/json"}
TIER_LIMITS = [(10, 1), (30, 2), (40, 3), (45, 4), (50, 5)]
FORGET_AFTER_DAYS = 120

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("job-radar")


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def job(jid, title, location, url):
    return {"id": str(jid), "title": (title or "").strip(), "location": (location or "").strip(), "url": url}


def tier(rank):
    return next(t for limit, t in TIER_LIMITS if rank <= limit)


# ---------------- adapters: each returns a list of job dicts ----------------
def greenhouse(c, s):
    r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs", timeout=TIMEOUT)
    r.raise_for_status()
    return [job(j["id"], j["title"], (j.get("location") or {}).get("name"), j["absolute_url"])
            for j in r.json().get("jobs", [])]


def lever(c, s):
    r = s.get(f"https://api.lever.co/v0/postings/{c['token']}", params={"mode": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        out.append(job(j["id"], j["text"], ", ".join(l for l in locs if l), j["hostedUrl"]))
    return out


def ashby(c, s):
    r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location") or ""] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        out.append(job(j["id"], j["title"], ", ".join(l for l in locs if l), j["jobUrl"]))
    return out


def smartrecruiters(c, s):
    out = {}
    for term in SEARCH_TERMS:
        r = s.get(f"https://api.smartrecruiters.com/v1/companies/{c['token']}/postings",
                  params={"q": term, "limit": 100}, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("content", []):
            loc = j.get("location") or {}
            country = COUNTRY.get((loc.get("country") or "").lower(), loc.get("country"))
            parts = [loc.get("city"), country, "Remote" if loc.get("remote") else None]
            out[j["id"]] = job(j["id"], j.get("name"), ", ".join(p for p in parts if p),
                               f"https://jobs.smartrecruiters.com/{c['token']}/{j['id']}")
    return list(out.values())


def workday(c, s):
    base = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    out = {}
    for term in SEARCH_TERMS:
        r = s.post(f"{base}/wday/cxs/{c['tenant']}/{c['site']}/jobs",
                   json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobPostings", []):
            path = j.get("externalPath", "")
            out[path] = job(path, j.get("title"), j.get("locationsText"), f"{base}/en-US/{c['site']}{path}")
    return list(out.values())


def oracle_hcm(c, s):
    host, site = c["host"], c["site"]
    out = {}
    for term in SEARCH_TERMS:
        finder = (f'findReqs;siteNumber={site},facetsList=NONE,limit=50,'
                  f'keyword="{term}",sortBy=POSTING_DATES_DESC')
        r = s.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                  params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder},
                  timeout=TIMEOUT)
        r.raise_for_status()
        items = r.json().get("items") or [{}]
        for j in items[0].get("requisitionList") or []:
            locs = [j.get("PrimaryLocation") or ""] + [x.get("Name", "") for x in j.get("secondaryLocations") or []]
            out[str(j["Id"])] = job(j["Id"], j.get("Title"), ", ".join(l for l in locs if l),
                                    f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j['Id']}")
    return list(out.values())


def amazon(c, s):
    out = {}
    for term in SEARCH_TERMS:
        r = s.get("https://www.amazon.jobs/en/search.json",
                  params={"base_query": term, "sort": "recent", "result_limit": 100,
                          "normalized_country_code[]": ["IND", "ARE"]}, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            jid = j.get("id_icims") or j.get("id")
            out[jid] = job(jid, j.get("title"), j.get("normalized_location") or j.get("location"),
                           "https://www.amazon.jobs" + j.get("job_path", ""))
    return list(out.values())


def uber(c, s):
    out = {}
    for term in SEARCH_TERMS:
        r = s.post("https://www.uber.com/api/loadSearchJobsResults?localeCode=en",
                   headers={"x-csrf-token": "x"},
                   json={"params": {"query": term, "location": [{"country": "IND"}, {"country": "ARE"}]},
                         "page": 0, "limit": 50}, timeout=TIMEOUT)
        r.raise_for_status()
        for j in (r.json().get("data") or {}).get("results") or []:
            loc = j.get("location") or {}
            out[str(j["id"])] = job(j["id"], j.get("title"),
                                    ", ".join(x for x in [loc.get("city"), loc.get("countryName")] if x),
                                    f"https://www.uber.com/global/en/careers/list/{j['id']}/")
    return list(out.values())


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "workday": workday, "oracle_hcm": oracle_hcm, "amazon": amazon, "uber": uber}


# ---------------- scanning ----------------
def load_config():
    with open(BASE / "companies.yaml") as f:
        return yaml.safe_load(f)


def fetch(company):
    s = requests.Session()
    s.headers.update(HEADERS)
    return ADAPTERS[company["ats"]](company, s)


def is_match(j, f):
    title = j["title"].lower()
    if not any(re.search(p, title) for p in f["title_include"]):
        return False
    if any(re.search(p, title) for p in f["title_exclude"]):
        return False
    loc = j["location"].lower()
    if not loc or re.search(r"\d+\s+locations", loc):
        return f.get("allow_unknown_location", True)
    return any(k in loc for k in f["location_include"])


def scan_all(cfg):
    auto = [c for c in cfg["companies"] if c["ats"] in ADAPTERS]
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {c["name"]: (c, pool.submit(fetch, c)) for c in auto}
        for name, (c, fut) in futures.items():
            try:
                results[name] = (c, fut.result(), None)
            except Exception as e:  # one broken board must not stop the rest
                results[name] = (c, [], str(e)[:160])
    return results


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seeded": [], "seen": {}, "open": {}}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def run(cfg):
    state = load_state()
    ts = now_utc().isoformat()
    new_jobs, health = [], []

    for name, (c, jobs, err) in scan_all(cfg).items():
        if err:
            log.warning("%s: fetch failed (%s)", name, err)
            health.append({"company": name, "ok": False, "error": err})
            continue  # keep last known open jobs for this company
        matched = [j for j in jobs if is_match(j, cfg["filters"])]
        health.append({"company": name, "ok": True, "open": len(jobs), "matching": len(matched)})
        seen = state["seen"].setdefault(name, {})
        first_time = name not in state["seeded"]
        for j in matched:
            if j["id"] not in seen:
                seen[j["id"]] = None if first_time else ts  # None = was already open when tracking began
                if not first_time:
                    new_jobs.append({"company": name, **j})
        state["open"][name] = matched
        if first_time:
            state["seeded"].append(name)
            log.info("%s: first scan, recorded %d existing matching jobs without alerting", name, len(matched))

    prune(state)
    save_json(STATE_PATH, state)
    write_dashboard_data(cfg, state, health, ts)
    log.info("scan complete: %d new matching jobs", len(new_jobs))
    if new_jobs:
        alert(new_jobs, cfg.get("alerts", {}))


def prune(state):
    """Forget closed jobs after FORGET_AFTER_DAYS so the state file stays small."""
    cutoff = (now_utc() - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    for name, seen in state["seen"].items():
        open_ids = {j["id"] for j in state["open"].get(name, [])}
        for jid in [k for k, v in seen.items() if k not in open_ids and (v or "") < cutoff]:
            del seen[jid]


def write_dashboard_data(cfg, state, health, ts):
    by_name = {c["name"]: c for c in cfg["companies"]}
    jobs = []
    for name, open_jobs in state["open"].items():
        if name not in by_name:
            continue
        c = by_name[name]
        for j in open_jobs:
            jobs.append({**j, "company": name, "rank": c["rank"], "tier": tier(c["rank"]),
                         "first_seen": state["seen"].get(name, {}).get(j["id"])})
    manual = [{"company": c["name"], "rank": c["rank"], "careers": c.get("careers", "")}
              for c in cfg["companies"] if c["ats"] == "manual"]
    save_json(DATA_PATH, {"generated_at": ts, "jobs": jobs, "health": health, "manual": manual})


# ---------------- alerts ----------------
def alert(jobs, a):
    cap = a.get("max_items_per_alert", 10)
    head = f"{len(jobs)} new SRE/DevOps job{'s' if len(jobs) > 1 else ''}"
    lines = [f"{j['company']}: {j['title']} ({j['location'] or 'location n/a'})\n{j['url']}" for j in jobs[:cap]]
    if len(jobs) > cap:
        lines.append(f"+{len(jobs) - cap} more on your dashboard")
    body = "\n\n".join(lines)
    sent = send_ntfy(head, body, jobs[0]["url"] if len(jobs) == 1 else os.getenv("DASHBOARD_URL"))
    sent |= send_telegram(f"🔔 {head}\n\n{body}")
    sent |= mac_popup(head, jobs[0]["title"] + " at " + jobs[0]["company"])
    if not sent:
        print(f"[ALERT] {head}\n{body}")


def send_ntfy(title, body, click=None):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return False
    headers = {"Title": title.encode("utf-8"), "Tags": "rotating_light", "Priority": "high"}
    if click:
        headers["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers,
                      timeout=15).raise_for_status()
        return True
    except Exception as e:
        log.warning("ntfy failed: %s", e)
        return False


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
                      timeout=15).raise_for_status()
        return True
    except Exception as e:
        log.warning("telegram failed: %s", e)
        return False


def mac_popup(title, message):
    if platform.system() != "Darwin" or os.getenv("CI"):
        return False
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                   check=False, timeout=10)
    return True


# ---------------- validate ----------------
def validate(cfg):
    results = scan_all(cfg)
    ok = bad = 0
    lines = [f"{'#':>3}  {'Company':<22} {'Board':<16} {'Status':<7} {'Open':>6} {'Match':>6}"]
    for c in cfg["companies"]:
        if c["ats"] == "manual":
            lines.append(f"{c['rank']:>3}  {c['name']:<22} {'manual':<16} {'-':<7} {'-':>6} {'-':>6}")
            continue
        _, jobs, err = results[c["name"]]
        if err:
            bad += 1
            lines.append(f"{c['rank']:>3}  {c['name']:<22} {c['ats']:<16} {'FAIL':<7} {'-':>6} {'-':>6}  {err[:60]}")
        else:
            ok += 1
            m = sum(is_match(j, cfg["filters"]) for j in jobs)
            lines.append(f"{c['rank']:>3}  {c['name']:<22} {c['ats']:<16} {'OK':<7} {len(jobs):>6} {m:>6}")
    lines.append(f"\n{ok} boards working, {bad} failing.")
    report = "\n".join(lines)
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Board check\n```\n" + report + "\n```\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["run", "validate", "test-alert"])
    cmd = p.parse_args().cmd
    cfg = load_config()
    if cmd == "run":
        run(cfg)
    elif cmd == "validate":
        validate(cfg)
    else:
        alert([{"company": "Test Co", "title": "Site Reliability Engineer II", "location": "Bengaluru, India",
                "url": os.getenv("DASHBOARD_URL") or "https://example.com"}], cfg.get("alerts", {}))


if __name__ == "__main__":
    sys.exit(main())
