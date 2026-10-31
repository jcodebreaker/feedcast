# Feedcast v1 — frozen contract (M0)

Every module codes against this. `ARCHITECTURE.md` is the design; this file pins the shapes so four builders can work in parallel. Change it only in its own commit.

## 0. Stack

- Python ≥ 3.11, managed with `uv` (`uv run feedcast …`). One runtime dependency: `pyyaml`. Everything else is stdlib (`sqlite3`, `urllib`, `html`, `http.server`, `json`, `subprocess`).
- Package `feedcast/`, CLI entry `feedcast = feedcast.cli:main` (argparse, one subcommand per module).
- Tests: `uv run python -m unittest discover -s tests` — offline, deterministic, < 20 s per file, temp dirs only.

## 1. Paths (`feedcast/config.py`, already written)

`home()` = `$FEEDCAST_HOME` or `~/.feedcast`. Under it:

| Path | Owner | What |
|---|---|---|
| `config.yaml` | user + setup page | see §2 |
| `profile.yaml` | `profile` | see §3 |
| `linkedin-self.json` | `profile --linkedin` (capture's agent) | the user's own LinkedIn profile, §3 |
| `feedcast.db` | `db.connect()` | see §5 |
| `runs/<YYYY-MM-DD>/<HHMM>.json` | `capture` | raw capture, §4 |
| `reports/<YYYY-MM-DD>.html`, `reports/index.html` | `report` | §7 |
| `media/<sha1(url)>.<jpg\|png\|webp\|gif\|avif>` | `ingest` (`feedcast/media.py`) | images of kept posts; the page links them as `../media/…` |
| `PAUSED` | `capture` | exists ⇒ every capture refuses to run; contains the reason |
| `logs/<job>.log` | launchd | stdout/stderr of scheduled jobs |

Tests always set `FEEDCAST_HOME` to a temp dir. Nothing ever writes under the real `~/.feedcast` in tests.

## 2. config.yaml

`config.load()` merges the file over `config.DEFAULTS`. Keys: `roots` (list of dirs; **empty by default — the user names them, never guess**), `use_sessions` (bool), `schedule` (`profile`, `captures[]`, `digest` as `"HH:MM"`), `relevance_threshold` (2), `max_posts` (50), `max_items` (8), `pins`, `mutes`, `models` (`classify`, `digest`).

## 3. profile.yaml

```yaml
generated_at: "2026-10-30T02:00:00Z"
role: "AI engineer: agents, MCP, voice, evals"
stack: [TypeScript, Python]
interests: [{topic: "agent evals", weight: 0.9, auto: true}]
not_interested: ["hiring takes"]
pins: []
mutes: []
linkedin: {headline: "…", about: "…", experience: ["Title at Company"], skills: ["Python"], read_at: "…Z"}  # when read
```
The profile is a seed describing the person; repo names are read as signals but never stored or sent onward. Entries with `auto: true` are the model's; anything without it is a hand edit and is preserved on refresh. Auto interests the model stops returning fade 10%/day. `profile.compact_text(profile) -> str` (≤ 2,500 chars, no project names) is the **only** profile text ever sent with a relevance or digest call.

`feedcast profile --linkedin` runs capture's browser agent once (`prompts/self.md`, same allowlist, `dontAsk`, one `Edit(//abs/path)` rule, read-only, abort and pause on any check) on `https://www.linkedin.com/in/me/` and writes `linkedin-self.json` (`headline`, `about`, `experience[]`, `skills[]`, `read_at`). Refresh uses it when younger than 30 days.

## 4. Raw capture JSON (`runs/…/HHMM.json`)

```json
{ "run_started_at": "2026-10-30T09:00:02+01:00", "run_ended_at": "…",
  "status": "ok" | "degraded" | "aborted_challenge" | "aborted_not_signed_in" | "aborted_no_chrome",
  "notes": "free text",
  "posts": [ {
    "urn": "urn:li:activity:7251234567890123456" | null,
    "url": "https://www.linkedin.com/feed/update/urn:li:activity:…" | null,
    "author": "Mariana Oyelaran", "author_headline": "…",
    "author_url": "https://www.linkedin.com/in/…" | null,
    "author_avatar": "https://media.licdn.com/…" | null, "image": "https://media.licdn.com/…" | null,
    "text": "visible post text, no 'see more' expansion",
    "posted_age": "3h",
    "reactions": 1240, "comments": 54, "reposts": 12,
    "is_suggested": false, "is_promoted": false,
    "position": 4,
    "links": [ {"url": "https://lnkd.in/eX7qPz", "source": "body"|"preview"|"first_comment", "title": "…"|null} ],
    "link_in_hidden_comment": false
  } ] }
```
`author_url` is the author's `/in/` or `/company/` page, read from the name link without clicking (validation keeps only those, query dropped); it is the way back to a post when the feed gives no permalink. `feedcast.models.validate_capture(obj) -> (clean_obj, errors[])` (written) drops invalid posts and is used by capture (on write) and ingest (on read). Fewer than 10 valid posts on an `ok` run ⇒ status becomes `degraded`.

## 5. SQLite (`feedcast/db.py`, written)

Schema exactly as `db.SCHEMA`. Relevance writes `posts.topic` (a 1–3 word Title Case label, reused across days; the last 7 days' labels go into the prompt); `posts.project` is v1 and no longer written. `db.connect()` adds `topic`, `author_url`, `image` and `author_avatar` to older DBs. A post is promoted when the capture flags it, `models.looks_promoted()` sees a whole-line "Promoted"/"Sponsored", a link title ending in a call-to-action sentence ("… Download now.") or a call-to-action card on a post with no age, or the relevance reply says `is_ad` (then relevance 0, why "ad"). The digest groups the day's items into 2–4 broad groups (`{name, line, keys}`); report validation puts every item in exactly one, at most four, extras in "Also". Post key = URN, else `"h:" + sha1(author + "\n" + db.key_text(text))`: the first 120 characters, lowercased, whitespace collapsed, "…more"/"see more" and a trailing ellipsis dropped, because live reads cut the text past the fold at different places. Rows stored under the older `sha1(author + text[:200])` key keep it (ingest checks `db.legacy_key`). `shared_by` for a resolved URL = `COUNT(DISTINCT post_key)` in `links`. `llm_calls` records every API call for the cost line.

## 6. LLM (`feedcast/llm.py`, written)

`llm.complete(purpose, model, system, user, max_tokens, effort=None) -> str` and `llm.complete_json(...) -> dict|list` (parses the first JSON value in the reply, retries once on parse failure).
- Key: `$ANTHROPIC_API_KEY`, else macOS Keychain item service `feedcast`, account `anthropic`.
- Fakes: `llm.set_fake(fn)` (tests) where `fn(purpose, model, system, user) -> str`; or `$FEEDCAST_FAKE_LLM=<dir>` reads `<dir>/<purpose>.json` (dev runs). With either set, the network is never touched.
- `purpose` ∈ `profile` | `relevance` | `digest`. Every real call is logged to `llm_calls` with token counts and USD.

## 7. Module ownership

| Module | Owns | CLI |
|---|---|---|
| profile | `feedcast/profile.py`, `prompts/profile.md` | `feedcast profile [--dry-run \| --show \| --linkedin]` |
| capture | `feedcast/capture.py`, `prompts/capture.md`, `prompts/self.md`, `feedcast/schedule.py` (launchd plists), `feedcast/doctor.py` | `feedcast init [--terminal]`, `capture [--now] [--from-file F]`, `resume`, `schedule install\|remove\|show`, `doctor` |
| ingest | `feedcast/ingest.py`, `feedcast/links.py`, `feedcast/relevance.py`, `prompts/relevance.md` | `feedcast ingest [RUN_FILE]` (default: all un-ingested runs) |
| report | `feedcast/report.py`, `feedcast/feedback.py`, `feedcast/setup.py`, `feedcast/media.py`, `templates/{report,setup}.html`, `templates/style.css`, `prompts/digest.md` | `feedcast report [--date D] [--no-open] [--no-notify]`, `feedcast serve` (reports, `/media/…`, `/setup`, `/feedback`, `/api/…`), `feedcast setup` |

`feedcast/cli.py` (written) dispatches to `<module>.register(subparsers)`; each module adds its own subcommands. A scheduled capture job runs `feedcast capture && feedcast ingest`.

## 8. Rules for everyone

- Never run a live LinkedIn capture, never call the real Claude API, never run `claude` in tests or while building. The user approves the first live run.
- Only `profile.compact_text()` and post text/link metadata go to relevance and digest calls; the profile call also gets the signals text (`--dry-run` prints it). Never source code, diffs, transcripts beyond the first 200 chars of a session's first prompt, cookies.
- Link resolution never sends LinkedIn cookies (plain `urllib`, no cookie jar), max 2 redirects followed manually, 10 s timeout. Images go through the same fetch: https only, `image/*` minus SVG, ≤ 3 MB; post images and avatars only from `https://media.licdn.com/`. Links record `meta.image` (`og:image`/`twitter:image`, absolute https; GitHub's `opengraph.githubassets.com` card; Hugging Face social thumbnails). The page never loads a remote image: no local copy ⇒ a quote card.
- `feedcast serve` binds 127.0.0.1; every request checks Host, every POST checks Origin, and `POST /api/*` (setup page actions) also needs `X-Feedcast-Token`, a random token per server run embedded only in `/setup`. Setup actions edit `config.yaml`/`profile.yaml` (hand edits carry no `auto`; a role typed there sets `role_hand: true` and survives refresh), install/remove the schedule, and start one background job at a time (`profile --linkedin`, `profile`, or `capture --now && ingest && report --no-open`) through `setup.SPAWN`.
- Counts and URLs on the report come from the DB, never from model output. The model returns item ordering and copy keyed by post key.
- Sample data lives in `fixtures/` and is labelled invented.
