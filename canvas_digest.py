#!/usr/bin/env python3
"""
Canvas daily digest — a seven-day agenda.

Two sources, merged:

  course_schedule.json   the semester plan taken from your syllabi: which
                         class meets when, and what to read for it
  Canvas API             announcements, live assignment due dates, and
                         anything the syllabus didn't predict

Canvas wins every disagreement. When Canvas shows a deadline the syllabus
didn't have, or shows one on a different date, the digest says so.

Standard library only -- nothing to install.

    python3 canvas_digest.py              send today's digest
    python3 canvas_digest.py --dry-run    build from real data, don't send
    python3 canvas_digest.py --sample     syllabus only, for previewing
    python3 canvas_digest.py --check      show how Canvas courses matched
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape, unescape

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SCHEDULE_PATH = os.path.join(HERE, "course_schedule.json")
TIMEOUT = 30

DEFAULTS = {
    "days_ahead": 7,
    "announcement_lookback_days": 3,
    "ignore_course_ids": [],
    "send_when_empty": True,
}


# --------------------------------------------------------------------------
# Config and schedule
# --------------------------------------------------------------------------


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("No config.json found. Copy config.example.json to config.json "
                 "and fill it in.")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"config.json isn't valid JSON: {e}")
    merged = dict(DEFAULTS)
    merged.update(cfg)
    missing = [k for k in ("canvas_host", "canvas_token", "smtp_user",
                           "smtp_password", "mail_to") if not merged.get(k)]
    if missing:
        sys.exit("config.json is missing: " + ", ".join(missing))
    merged["canvas_host"] = (merged["canvas_host"]
                             .replace("https://", "").replace("http://", "").strip("/"))
    return merged


def load_schedule():
    if not os.path.exists(SCHEDULE_PATH):
        return {"courses": {}, "sessions": [], "deadlines": []}
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"course_schedule.json isn't valid JSON: {e}")


# --------------------------------------------------------------------------
# Canvas API
# --------------------------------------------------------------------------


class CanvasError(Exception):
    pass


class Canvas:
    def __init__(self, host, token):
        self.base = f"https://{host}/api/v1"
        self.token = token
        self.warnings = []

    def _request(self, url):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "canvas-digest/2.0",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.headers.get("Link", "")

    @staticmethod
    def _next_link(header):
        for part in header.split(","):
            bits = part.split(";")
            if len(bits) > 1 and 'rel="next"' in bits[1].replace(" ", ""):
                return bits[0].strip().strip("<>")
        return None

    def get(self, path, params=None, quiet=False):
        query = urllib.parse.urlencode(params or [], doseq=True)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        results, pages = [], 0
        while url and pages < 20:
            try:
                data, link = self._request(url)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise CanvasError(
                        "Canvas rejected the access token (401). Generate a new "
                        "one in Canvas settings and update config.json.")
                if not quiet:
                    self.warnings.append(f"{path} returned HTTP {e.code}")
                return results
            except urllib.error.URLError as e:
                raise CanvasError(f"Couldn't reach Canvas: {e.reason}")
            results.extend(data if isinstance(data, list) else [data])
            url = self._next_link(link)
            pages += 1
        return results


def chunked(seq, size=10):
    """Canvas accepts at most 10 context codes per request."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def normalise(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def similar(a, b):
    """Rough title match -- enough to spot the same assignment twice."""
    wa = {w for w in normalise(a).split() if len(w) > 2}
    wb = {w for w in normalise(b).split() if len(w) > 2}
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def strip_html(raw, limit=200):
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>|</p>", " ", raw, flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "\u2026"
    return text


def clock(when):
    """12-hour time without a leading zero. Built by hand because the
    strftime code for that differs between Windows and everything else."""
    hour = when.hour % 12 or 12
    suffix = "am" if when.hour < 12 else "pm"
    return f"{hour}{'' if when.minute == 0 else ':%02d' % when.minute}{suffix}"


def long_date(when):
    return f"{when.strftime('%A')}, {when.day} {when.strftime('%B')}"


def short_date(when):
    return f"{when.strftime('%a')} {when.day} {when.strftime('%b')}"


def day_heading(day, today):
    delta = (day - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta < 7:
        return day.strftime("%A")
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def relative_day(when, now):
    delta = (now.date() - when.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return short_date(when)


# --------------------------------------------------------------------------
# Course matching
# --------------------------------------------------------------------------


def match_course(canvas_name, schedule):
    """Map a Canvas course onto a syllabus entry by course number or name."""
    haystack = normalise(canvas_name)
    for code, meta in schedule["courses"].items():
        for needle in meta.get("match", []) + [code]:
            if normalise(needle) and normalise(needle) in haystack:
                return code
    return None


def short_name(code, schedule):
    meta = schedule["courses"].get(code)
    return meta["short"] if meta else code


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def fetch_courses(api, cfg, schedule):
    raw = api.get("/courses", [("enrollment_state", "active"), ("per_page", 100),
                               ("include[]", "term")])
    now = datetime.now(timezone.utc)
    courses = {}
    for c in raw:
        if c.get("access_restricted_by_date") or c.get("id") in cfg["ignore_course_ids"]:
            continue
        end = parse_time(c.get("end_at"))
        term_end = parse_time((c.get("term") or {}).get("end_at"))
        if (end and end < now) or (term_end and term_end < now):
            continue
        full = f"{c.get('course_code', '')} {c.get('name', '')}"
        code = match_course(full, schedule)
        courses[c["id"]] = {
            "id": c["id"],
            "code": code,
            "label": short_name(code, schedule) if code else (
                (c.get("course_code") or c.get("name") or "Course").strip()),
            "canvas_name": (c.get("name") or "").strip(),
        }
    return courses


def fetch_assignment_meta(api, courses):
    """How each assignment behaves: does it actually take a submission?

    Readings posted into the Assignments area are nearly always ungraded or
    accept no submission, which distinguishes them without guessing at titles.
    """
    meta = {}
    for cid in courses:
        for a in api.get(f"/courses/{cid}/assignments",
                         [("per_page", 100)], quiet=True):
            meta[a.get("id")] = {
                "course_id": cid,
                "name": (a.get("name") or "").strip(),
                "submission_types": a.get("submission_types") or [],
                "grading_type": a.get("grading_type") or "",
                "points": a.get("points_possible"),
            }
    return meta


NON_SUBMITTING = {"none", "not_graded", "external_tool", "wiki_page"}


def is_reading(title, code, schedule, meta=None):
    """Some courses post readings in the Assignments area. Those are readings.

    A course with a deliverable_titles allowlist is strict: anything not on the
    list is treated as reading. Other courses fall back to keyword matching.
    """
    # Strongest signal first: Canvas itself says nothing gets handed in.
    if meta:
        types = set(meta.get("submission_types") or [])
        if meta.get("grading_type") == "not_graded":
            return True
        if types and types <= NON_SUBMITTING:
            return True
        if types & {"online_upload", "online_text_entry", "online_url",
                    "online_quiz", "media_recording", "on_paper",
                    "student_annotation", "discussion_topic"}:
            return False

    text = (title or "").lower()
    rules = (schedule.get("canvas_rules") or {}).get(code or "", {})
    allowlist = rules.get("deliverable_titles")
    if allowlist:
        return not any(good in text for good in allowlist)
    return any(word in text for word in schedule.get("reading_keywords", []))


def fetch_canvas_deadlines(api, cfg, courses, now, window_end, schedule, meta=None):
    items = api.get("/planner/items", [
        ("start_date", now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("end_date", window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("per_page", 100),
    ])
    out = []
    for it in items:
        if (it.get("plannable_type") or "") == "calendar_event":
            continue
        plannable = it.get("plannable") or {}
        due = parse_time(plannable.get("due_at") or it.get("plannable_date")
                         or plannable.get("todo_date"))
        if not due:
            continue
        subs = it.get("submissions")
        submitted = bool(isinstance(subs, dict) and (subs.get("submitted")
                                                     or subs.get("graded")))
        course = courses.get(it.get("course_id"), {})
        kind = {"quiz": "quiz", "discussion_topic": "discussion",
                "wiki_page": "page", "assessment_request": "peer review"}.get(
                    it.get("plannable_type") or "", "")
        out.append({
            "title": (plannable.get("title") or "Untitled").strip(),
            "course": course.get("label", "Canvas"),
            "code": course.get("code"),
            "due": due,
            "kind": kind,
            "points": plannable.get("points_possible"),
            "url": canvas_url(cfg, it.get("html_url")),
            "source": "canvas",
            "note": "",
            "submitted": submitted,
            "reading": is_reading(plannable.get("title"), course.get("code"), schedule,
                                  (meta or {}).get(plannable.get("id"))),
        })
    return out


def canvas_url(cfg, path):
    if not path:
        return None
    return path if path.startswith("http") else f"https://{cfg['canvas_host']}{path}"


def fetch_announcements(api, cfg, courses, now):
    if not courses:
        return []
    start = now - timedelta(days=cfg["announcement_lookback_days"])
    raw = []
    for group in chunked(courses):
        params = [
            ("start_date", start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("end_date", now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("per_page", 50),
        ]
        for cid in group:
            params.append(("context_codes[]", f"course_{cid}"))
        raw.extend(api.get("/announcements", params))

    out = []
    for a in raw:
        posted = parse_time(a.get("posted_at") or a.get("created_at"))
        if not posted:
            continue
        cid = None
        code = a.get("context_code") or ""
        if code.startswith("course_"):
            try:
                cid = int(code.split("_", 1)[1])
            except ValueError:
                pass
        out.append({
            "title": (a.get("title") or "Announcement").strip(),
            "course": courses.get(cid, {}).get("label", "Canvas"),
            "author": ((a.get("author") or {}).get("display_name") or "").strip(),
            "posted": posted,
            "snippet": strip_html(a.get("message")),
            "url": canvas_url(cfg, a.get("html_url")),
        })
    out.sort(key=lambda x: x["posted"], reverse=True)
    return out


# --------------------------------------------------------------------------
# Merging syllabus and Canvas
# --------------------------------------------------------------------------


def syllabus_deadlines(schedule, start_day, end_day, tz):
    out = []
    for d in schedule.get("deadlines", []):
        try:
            day = date.fromisoformat(d["date"])
        except (KeyError, ValueError):
            continue
        if not (start_day <= day <= end_day):
            continue
        hour, minute = 23, 59
        if d.get("time"):
            try:
                hour, minute = (int(x) for x in d["time"].split(":"))
            except ValueError:
                pass
        out.append({
            "title": d["title"],
            "course": short_name(d["course"], schedule),
            "code": d["course"],
            "due": datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz),
            "kind": "",
            "points": None,
            "url": None,
            "source": "syllabus",
            "note": "",
        })
    return out


def merge_deadlines(canvas_items, syllabus_items, schedule, tz, today):
    """Canvas is authoritative. Syllabus fills gaps and flags differences."""
    all_syllabus = []
    for d in schedule.get("deadlines", []):
        try:
            all_syllabus.append((d, date.fromisoformat(d["date"])))
        except (KeyError, ValueError):
            continue

    merged = []
    for item in canvas_items:
        for entry, day in all_syllabus:
            if entry["course"] != item["code"]:
                continue
            if not similar(entry["title"], item["title"]):
                continue
            if day != item["due"].date():
                item["note"] = f"syllabus said {day.day} {day.strftime('%b')}"
            break
        else:
            if item["code"]:
                item["note"] = "not on the syllabus"
        merged.append(item)

    # Syllabus items Canvas hasn't surfaced yet.
    for item in syllabus_items:
        if any(c["code"] == item["code"] and similar(c["title"], item["title"])
               for c in canvas_items):
            continue
        merged.append(item)

    merged.sort(key=lambda x: x["due"])
    return merged


def sessions_in_window(schedule, start_day, end_day):
    by_day = {}
    for s in schedule.get("sessions", []):
        try:
            day = date.fromisoformat(s["date"])
        except (KeyError, ValueError):
            continue
        if start_day <= day <= end_day:
            entry = dict(s)
            entry["course_label"] = short_name(s["course"], schedule)
            by_day.setdefault(day, []).append(entry)
    return by_day


def build_agenda(deadlines, sessions_by_day, start_day, end_day):
    days = []
    cursor = start_day
    while cursor <= end_day:
        same_day = [d for d in deadlines if d["due"].date() == cursor]
        due = [d for d in same_day if not d.get("reading")]
        readings = [d for d in same_day if d.get("reading")]
        classes = sessions_by_day.get(cursor, [])
        if due or classes or readings:
            days.append({"date": cursor, "due": due, "classes": classes,
                         "readings": readings})
        cursor += timedelta(days=1)
    return days


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

INK = "#14181d"
MUTED = "#6a727c"
FAINT = "#9aa2ac"
RULE = "#dde1e6"
HAIRLINE = "#f0f2f4"
PAPER = "#ffffff"
BACKDROP = "#eff1f3"
READING = "#17558a"
DELIVERABLE = "#a4271a"
ACTIVITY = "#1f6b47"
URGENT = "#a4271a"

SERIF = "Georgia, 'Times New Roman', serif"
SANS = "-apple-system, BlinkMacSystemFont, 'Helvetica Neue', Helvetica, Arial, sans-serif"


def legend():
    swatches = (("Reading", READING), ("Deliverable", DELIVERABLE), ("Class or prep", ACTIVITY))
    cells = ""
    for label, colour in swatches:
        cells += (f'<td width="9" bgcolor="{colour}" style="width:9px;height:9px;'
                  f'font-size:0;line-height:0;">&nbsp;</td>'
                  f'<td style="font-family:{SANS};font-size:11.5px;color:{MUTED};'
                  f'padding:0 18px 0 6px;white-space:nowrap;">{label}</td>')
    return (f'<tr><td style="padding:14px 0 0 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
            f'<tr>{cells}</tr></table></td></tr>')


def section_title(text):
    return (f'<tr><td style="padding:34px 0 10px 0;border-bottom:1px solid {RULE};">'
            f'<span style="font-family:{SERIF};font-size:19px;color:{INK};">'
            f'{escape(text)}</span></td></tr>')


def empty_note(text):
    return (f'<tr><td style="padding:16px 0 2px 0;font-family:{SANS};font-size:14px;'
            f'color:{FAINT};">{escape(text)}</td></tr>')


def render_html(data, now):
    today = now.date()
    rows = []

    def group_label(text, colour):
        return (f'<tr><td style="padding:16px 0 2px 0;font-family:{SANS};font-size:11.5px;'
                f'font-weight:700;color:{colour};">{escape(text)}</td></tr>')

    def deliverable_row(item):
        done = item.get("submitted")
        tone = FAINT if done else DELIVERABLE
        urgent = (not done) and (item["due"] - now).total_seconds() / 3600 <= 24
        title = escape(item["title"])
        if item.get("url"):
            title = (f'<a href="{escape(item["url"])}" style="color:{tone};'
                     f'text-decoration:none;">{title}</a>')
        meta = escape(item["course"])
        if done:
            meta += ", submitted"
        if item.get("kind"):
            meta += f', {escape(item["kind"])}'
        if item.get("points"):
            pts = item["points"]
            meta += f", {int(pts) if float(pts).is_integer() else pts} points"
        if item.get("note"):
            meta += f' \u2014 {escape(item["note"])}'
        return (
            f'<tr><td style="padding:8px 0;border-bottom:1px solid {HAIRLINE};">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
            f'<td width="76" valign="top" style="font-family:{SANS};font-size:13px;'
            f'font-weight:{"600" if urgent else "400"};color:{tone};'
            f'white-space:nowrap;padding-top:2px;">due {escape(clock(item["due"]))}</td>'
            f'<td valign="top" style="font-family:{SANS};font-size:15px;color:{tone};'
            f'line-height:1.4;font-weight:600;">{title}'
            f'<div style="font-size:12.5px;color:{MUTED};padding-top:3px;font-weight:400;">'
            f'{meta}</div></td></tr></table></td></tr>')

    def class_row(klass):
        nested = ""
        readings = klass.get("readings") or []
        prep = klass.get("prep") or []
        for r in readings:
            nested += (f'<div style="font-family:{SANS};font-size:13px;color:{READING};'
                       f'line-height:1.5;padding-top:3px;">{escape(r)}</div>')
        for pitem in prep:
            nested += (f'<div style="font-family:{SANS};font-size:13px;color:{ACTIVITY};'
                       f'line-height:1.5;padding-top:3px;">{escape(pitem)}</div>')
        if not nested:
            nested = (f'<div style="font-family:{SANS};font-size:13px;color:{FAINT};'
                      f'padding-top:3px;">No reading listed.</div>')
        return (
            f'<tr><td style="padding:10px 0;border-bottom:1px solid {HAIRLINE};">'
            f'<div style="font-family:{SANS};font-size:15px;font-weight:600;color:{ACTIVITY};'
            f'line-height:1.4;">{escape(klass["course_label"])}</div>'
            f'<div style="font-family:{SANS};font-size:14px;color:{INK};line-height:1.4;'
            f'padding-top:2px;">{escape(klass["title"])}</div>'
            f'<div style="padding-left:16px;padding-top:4px;">{nested}</div></td></tr>')

    if not data["agenda"]:
        rows.append(section_title("The next seven days"))
        rows.append(empty_note("Nothing scheduled and nothing due."))

    for day in data["agenda"]:
        is_today = day["date"] == today
        rows.append(
            f'<tr><td style="padding:{"26" if day is data["agenda"][0] else "34"}px 0 8px 0;'
            f'border-bottom:1px solid {RULE};">'
            f'<span style="font-family:{SERIF};font-size:19px;color:{INK};'
            f'{"font-weight:bold;" if is_today else ""}">'
            f'{escape(day_heading(day["date"], today))}</span></td></tr>')

        if day["due"]:
            rows.append(group_label("Deliverables", DELIVERABLE))
            for item in day["due"]:
                rows.append(deliverable_row(item))

        if day["classes"] or day.get("readings"):
            rows.append(group_label("Class and readings", ACTIVITY))
            for klass in day["classes"]:
                rows.append(class_row(klass))
            for r in day.get("readings", []):
                title = escape(r["title"])
                if r.get("url"):
                    title = (f'<a href="{escape(r["url"])}" style="color:{READING};'
                             f'text-decoration:none;">{title}</a>')
                tag = ", submitted" if r.get("submitted") else ""
                rows.append(
                    f'<tr><td style="padding:10px 0;border-bottom:1px solid {HAIRLINE};">'
                    f'<div style="font-family:{SANS};font-size:15px;font-weight:600;'
                    f'color:{ACTIVITY};line-height:1.4;">{escape(r["course"])}</div>'
                    f'<div style="font-family:{SANS};font-size:13px;color:{READING};'
                    f'line-height:1.5;padding:4px 0 0 16px;">{title}'
                    f'<span style="color:{MUTED};">{escape(tag)}</span></div></td></tr>')

    rows.append(section_title("Announcements"))
    if not data["announcements"]:
        rows.append(empty_note("No new posts from instructors."))
    for a in data["announcements"]:
        title = escape(a["title"])
        if a.get("url"):
            title = (f'<a href="{escape(a["url"])}" style="color:{INK};'
                     f'text-decoration:none;">{title}</a>')
        byline = escape(a["course"])
        if a["author"]:
            byline += f", {escape(a['author'])}"
        byline += f", {escape(relative_day(a['posted'], now))}"
        snippet = ""
        if a["snippet"]:
            snippet = (f'<div style="font-family:{SANS};font-size:14px;color:{MUTED};'
                       f'line-height:1.55;padding-top:5px;">{escape(a["snippet"])}</div>')
        rows.append(
            f'<tr><td style="padding:15px 0;border-bottom:1px solid {HAIRLINE};">'
            f'<div style="font-family:{SANS};font-size:15px;font-weight:600;color:{INK};'
            f'line-height:1.4;">{title}</div>'
            f'<div style="font-family:{SANS};font-size:12.5px;color:{FAINT};'
            f'padding-top:3px;">{byline}</div>{snippet}</td></tr>')

    warnings = ""
    if data.get("warnings"):
        lines = "<br>".join(escape(w) for w in data["warnings"])
        warnings = (f'<tr><td style="padding:22px 0 0 0;font-family:{SANS};font-size:12px;'
                    f'color:{FAINT};line-height:1.6;">Some things couldn\'t be read:<br>'
                    f'{lines}</td></tr>')

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Canvas digest</title></head>
<body style="margin:0;padding:0;background-color:{BACKDROP};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background-color:{BACKDROP};padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
       style="max-width:600px;width:100%;background-color:{PAPER};">
<tr><td style="padding:40px 40px 44px 40px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="padding-bottom:18px;border-bottom:2px solid {INK};">
      <div style="font-family:{SERIF};font-size:30px;line-height:1.15;color:{INK};">
        {escape(long_date(now))}</div>
      <div style="font-family:{SANS};font-size:14px;color:{MUTED};padding-top:8px;">
        {escape(build_summary(data, now))}</div>
    </td></tr>
    {legend()}
    {''.join(rows)}
    {warnings}
    <tr><td style="padding:30px 0 0 0;font-family:{SANS};font-size:11.5px;color:{FAINT};
                   line-height:1.6;">
      Classes and readings come from your syllabi. Deadlines and announcements
      come from Canvas, which overrides the syllabus where they differ.
    </td></tr>
  </table>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def build_summary(data, now):
    today = now.date()
    due_today = [d for day in data["agenda"] if day["date"] == today for d in day["due"]]
    total_due = sum(len(day["due"]) for day in data["agenda"])
    classes_today = [c for day in data["agenda"] if day["date"] == today
                     for c in day["classes"]]
    parts = []
    if due_today:
        parts.append(f"{len(due_today)} due today, {total_due} this week")
    elif total_due:
        parts.append(f"{total_due} due this week")
    else:
        parts.append("Nothing due this week")
    if classes_today:
        parts.append(f"{len(classes_today)} class"
                     + ("es" if len(classes_today) != 1 else "") + " today")
    if data["announcements"]:
        n = len(data["announcements"])
        parts.append(f"{n} new announcement" + ("s" if n != 1 else ""))
    return ". ".join(parts) + "."


def render_text(data, now):
    today = now.date()
    lines = [long_date(now), build_summary(data, now), ""]
    if not data["agenda"]:
        lines.append("Nothing scheduled and nothing due.")
    for day in data["agenda"]:
        lines.append(day_heading(day["date"], today).upper())
        if day["due"]:
            lines.append("  Deliverables")
            for item in day["due"]:
                note = f" [{item['note']}]" if item.get("note") else ""
                if item.get("submitted"):
                    note += " [submitted]"
                lines.append(f"    due {clock(item['due'])}  {item['title']} "
                             f"({item['course']}){note}")
        if day["classes"] or day.get("readings"):
            lines.append("  Class and readings")
            for r in day.get("readings", []):
                tag = " [submitted]" if r.get("submitted") else ""
                lines.append(f"    {r['course']} - Reading: {r['title']}{tag}")
            for klass in day["classes"]:
                lines.append(f"    {klass['course_label']} - {klass['title']}")
                for r in (klass.get("readings") or []):
                    lines.append(f"        Reading: {r}")
                for pitem in (klass.get("prep") or []):
                    lines.append(f"        Prep: {pitem}")
        lines.append("")

    lines += ["", "ANNOUNCEMENTS"]
    if not data["announcements"]:
        lines.append("  No new posts from instructors.")
    for a in data["announcements"]:
        lines.append(f"  {a['course']}: {a['title']}")
        if a["snippet"]:
            lines.append(f"    {a['snippet']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def send_email(cfg, subject, html, text):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("mail_from") or cfg["smtp_user"]
    msg["To"] = cfg["mail_to"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"),
                      int(cfg.get("smtp_port", 587)), timeout=TIMEOUT) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(cfg["smtp_user"], cfg["smtp_password"].replace(" ", ""))
        server.send_message(msg)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Email a seven-day Canvas digest.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--audit", action="store_true",
                        help="list every Canvas assignment and how it is classified")
    parser.add_argument("--out", default="digest_preview.html")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    now = datetime.now().astimezone()
    tz = now.tzinfo
    schedule = load_schedule()
    start_day = now.date()
    end_day = start_day + timedelta(days=DEFAULTS["days_ahead"])

    if args.sample:
        syll = syllabus_deadlines(schedule, start_day, end_day, tz)
        syll.sort(key=lambda x: x["due"])
        data = {
            "agenda": build_agenda(syll, sessions_in_window(schedule, start_day, end_day),
                                   start_day, end_day),
            "announcements": [],
            "warnings": [],
        }
    else:
        cfg = load_config()
        end_day = start_day + timedelta(days=cfg["days_ahead"])
        api = Canvas(cfg["canvas_host"], cfg["canvas_token"])
        try:
            courses = fetch_courses(api, cfg, schedule)
            if args.check:
                print(f"{len(courses)} active Canvas courses:\n")
                for c in courses.values():
                    status = f"matched {c['code']}" if c["code"] else "NO SYLLABUS MATCH"
                    print(f"  {c['canvas_name'][:50]:52} {status}")
                return
            meta = fetch_assignment_meta(api, courses)
            if args.audit:
                print("Every assignment Canvas lists, and how the digest reads it.\n")
                for cid, c in sorted(courses.items(), key=lambda kv: kv[1]["label"]):
                    print(f"=== {c['label']} ({c.get('code') or 'no syllabus'}) ===")
                    shown = 0
                    for aid, m in sorted(meta.items(), key=lambda kv: kv[1]["name"]):
                        if m.get("course_id") != cid:
                            continue
                        verdict = "READING    " if is_reading(
                            m["name"], c.get("code"), schedule, m) else "DELIVERABLE"
                        pts = "" if m["points"] in (None, 0) else f"  {m['points']}pt"
                        print(f"  {verdict}  {m['name'][:58]}{pts}")
                        shown += 1
                    if not shown:
                        print("  (none listed)")
                    print()
                return

            window_end = datetime.combine(end_day, datetime.max.time()).replace(tzinfo=tz)
            canvas_items = fetch_canvas_deadlines(api, cfg, courses, now, window_end,
                                                  schedule, meta)
            syll = syllabus_deadlines(schedule, start_day, end_day, tz)
            deadlines = merge_deadlines(canvas_items, syll, schedule, tz, start_day)
            data = {
                "agenda": build_agenda(deadlines,
                                       sessions_in_window(schedule, start_day, end_day),
                                       start_day, end_day),
                "announcements": fetch_announcements(api, cfg, courses, now),
                "warnings": api.warnings,
            }
        except CanvasError as e:
            print(f"[{now:%Y-%m-%d %H:%M}] {e}", file=sys.stderr)
            sys.exit(1)

    html = render_html(data, now)
    text = render_text(data, now)

    if args.sample or args.dry_run:
        path = os.path.abspath(args.out)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Wrote {path}")
        return

    due_today = [d for day in data["agenda"] if day["date"] == now.date() for d in day["due"]]
    subject = f"Canvas: {short_date(now)}"
    if due_today:
        subject += f" — {len(due_today)} due today"

    if not data["agenda"] and not data["announcements"] and not cfg["send_when_empty"]:
        print(f"[{now:%Y-%m-%d %H:%M}] Nothing to report, skipping send.")
        return

    try:
        send_email(cfg, subject, html, text)
    except smtplib.SMTPAuthenticationError:
        print("Email login failed. Check smtp_user and the app password.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Couldn't send email: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[{now:%Y-%m-%d %H:%M}] Sent. {len(data['agenda'])} active days, "
          f"{len(data['announcements'])} announcements.")


if __name__ == "__main__":
    main()
