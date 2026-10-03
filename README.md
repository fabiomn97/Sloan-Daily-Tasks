# Sloan Daily Tasks Tracker

One email at 7:00am with everything due and everything to read for the next
seven days, across seven courses that each organize Canvas differently. Shared 
instructions to over 15 members of my class for their own building.

**[Read the instructions (PDF) →](BuildYourOwn.pdf)**

[Download a sample digest (PDF)](canvas-digest-sample.pdf)

## Why

Every course organizes Canvas deliberately, and every course organizes it
differently. One files readings under Assignments. One keeps them in Files,
grouped by week. One posts time-sensitive material as announcements. Several
keep the authoritative schedule in a syllabus PDF that Canvas never sees.

Each of those is a reasonable answer. They just aren't the same answer, and the
cost lands on the student holding all seven at once.

## How it works

Two sources, merged, with Canvas authoritative:

- **`course_schedule.json`** — the semester plan extracted from seven syllabi:
  142 sessions and 42 deadlines, each reading attached to the class it belongs
  to. Canvas has no concept of that relationship.
- **The Canvas REST API** — announcements, live due dates, submission status,
  and anything the syllabus didn't predict.

Where the two disagree, Canvas wins and the digest flags the difference rather
than quietly picking a side. A deadline Canvas has but the syllabus doesn't is
tagged *not on the syllabus*. One that moved is tagged *syllabus said 18 Sep*.

Readings are distinguished from deliverables by asking Canvas whether an item
actually accepts a submission, rather than by matching on titles.

## Requirements

Python 3.8 or newer. No third-party packages — standard library only.

## Setup

See [SETUP.md](SETUP.md) for step-by-step instructions.

Short version:

```bash
cp config.example.json config.json
# fill in your Canvas token, Gmail address and Gmail app password
python canvas_digest.py --check      # confirm courses matched
python canvas_digest.py --dry-run    # build without sending
python canvas_digest.py              # send
```

Then schedule it daily with Task Scheduler (Windows) or cron/launchd (macOS,
Linux).

## Commands

| Command | What it does |
|---|---|
| `python canvas_digest.py` | Build and send the digest |
| `python canvas_digest.py --dry-run` | Build from real data, write to a file, don't send |
| `python canvas_digest.py --sample` | Build from the syllabus only, for previewing |
| `python canvas_digest.py --check` | Show which Canvas courses matched which syllabus |
| `python canvas_digest.py --audit` | List every assignment and how it's classified |

## A note on credentials

`config.json` holds a Canvas access token and a Gmail app password. The Canvas
token carries your full account permissions. It is gitignored and should never
be committed, shared, or pasted anywhere. `config.example.json` ships with
placeholders and is the only config file in this repo.

## Adapting it

`course_schedule.json` is specific to one student's Fall 2026 schedule. To use
this for your own courses, replace the `courses`, `sessions` and `deadlines`
sections with your own. The `match` array on each course maps the syllabus
entry onto whatever Canvas calls that course.

Built for 15.S23 AI Builder Space, MIT Sloan, Fall 2026.
