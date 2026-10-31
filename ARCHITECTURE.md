# Feedcast

*Your LinkedIn feed, read for you through the day, reduced to the few things that match what you care about, grouped by topic, with the repos and papers people shared pulled out front.*

## Concept

Feedcast opens your LinkedIn home feed in your own Chrome two or three times a day, at a human pace, and keeps what it sees. In the evening it hands you one short page:

1. **Today in your feed**: two or three sentences.
2. **Up to eight items, grouped by topic** ("Voice models · three people on browser-side speech"). Each is the **resources the post shared** as link chips (GitHub repo with stars, paper, model, article, video), a plain one-line takeaway, and *why it's notable*. The resource is the point; the LinkedIn post is just where you found it.
3. **A footer** that says how many posts were skipped as off your interests, with a collapsed peek, and two feedback buttons per item.

Relevance is decided against an **interest profile**: who you are (role, stack, ~15 weighted interests), seeded nightly from what you have been committing, what you have been asking your coding agent about, and your own LinkedIn profile. The seed only describes you; the page is about the feed and never names your repos. There is no "general AI news" section.

### Flavour: a light weather report

The page is written like a short evening weather report: "today in your feed", a "building for 3 days" note when a theme is persistent, "shared by 5 people" when a resource is converging. That is the whole theme. No temperatures, barometers or gauges: they cost scan time and the user asked for 60 seconds. Weather language is used because it is the natural vocabulary for *change over days*, which is the one thing a thrice-daily reader can tell you that a single scroll cannot.

Visual language: cream paper, dark ink, one warm accent for "new today", one cool accent for links. Typography does the work; no gradients, no icon soup.

## Components and data flow

```
                       user's Mac, all local except the three LLM calls marked ●
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │                                                                               │
 │  nightly 02:00     ┌───────────┐                                              │
 │  (+ on demand) ───▶│  PROFILE  │  git roots, ~/.claude/projects titles,       │
 │                    │  ● Haiku  │  pins/mutes  ──▶ ~/.feedcast/profile.yaml    │
 │                    └─────┬─────┘                       (editable)             │
 │                          │ compact profile text                               │
 │  09:00 / 13:30 / 18:45   ▼                                                    │
 │  ┌─────────┐  ┌───────┐  ┌────────┐  ┌───────────┐  ┌─────────────────────┐    │
 │  │ CAPTURE │─▶│ STORE │─▶│ DEDUPE │─▶│ LINKS     │─▶│ RELEVANCE + GIST    │    │
 │  │ browser │  │sqlite │  │ by URN │  │ resolve,  │  │ ● Haiku, per post,  │    │
 │  │ agent   │  └───────┘  └────────┘  │ classify, │  │ scored 0-3 against  │    │
 │  │ in own  │                         │ enrich,   │  │ profile; <2 = skip  │    │
 │  │ Chrome  │                         │ dedupe    │  └──────────┬──────────┘    │
 │  └─────────┘                         └───────────┘             │               │
 │                                                                ▼               │
 │  19:30 ──────────────────────────────────▶ ┌───────────┐  ┌──────────┐         │
 │                                            │  DIGEST   │─▶│ DELIVER  │         │
 │                                            │  ● Opus   │  │ html +   │         │
 │                                            │  one call │  │ notif.   │         │
 │                                            └───────────┘  └──────────┘         │
 │                                                                               │
 │  feedback buttons (not relevant / more like this) ──▶ profile weights         │
 └───────────────────────────────────────────────────────────────────────────────┘
```

| Stage | What it does | Runs as | LLM |
|---|---|---|---|
| **Profile** (new) | Reads local signals, produces a compact work profile. See "Local work profile". | Python, nightly launchd + `feedcast profile` | ● **Haiku 5.5** (`claude-haiku-5-5`), one call |
| **Capture** | Opens `linkedin.com/feed` in the logged-in Chrome, scrolls 30-60 posts over 3-5 min with jittered pauses. Per post: author, headline, text, post URL, reactions/comments/reposts, suggested/promoted flag, posted age, **all outbound URLs** (body, link-preview card, and the author's first comment if already rendered). Never clicks "see more", "load comments", or any link. | Browser agent in the user's Chrome: Claude in Chrome on a schedule (v1). Playwright-over-CDP needs a dedicated Chrome profile: Chrome 136+ ignores `--remote-debugging-port` on the default profile | Browser-agent model (Sonnet tier) |
| **Store** | One row per sighting; raw JSON kept per run. | Python, `sqlite3` | no |
| **Dedupe** | Post key = activity URN from the URL; fallback hash of author + first 200 chars. Engagement per sighting gives velocity. | Python | no |
| **Links** (new) | Resolve `lnkd.in` and other shorteners with a plain HTTP HEAD (no LinkedIn cookies). Classify by domain. Enrich GitHub via API, articles via `<title>`/`og:description`, arXiv via its API, Hugging Face via its API. Dedupe across posts by resolved URL. | Python, `httpx`, `gh` token if present | no |
| **Relevance + gist** (replaces "classify") | Per new post, with the compact profile and this week's topic labels in the prompt: `relevance 0-3`, `topic` (1–3 word reusable label), `why` it's notable (one line, general), `takeaway` (one line), `substance 0-5`, `hype 0-5`. Batched 20 posts per call, strict JSON. Score < 2 = skipped, counted, never shown. | Python + Claude API | ● **Haiku 5.5**, ~$0.01/day |
| **Digest** | One call with the kept posts, their enriched links, the profile and 7 days of history. Writes the 2-3 sentence summary of what the feed was about, orders items, sorts them into 2-4 broad groups with a header line each, writes each item's takeaway and "why it's notable", notes "building for N days" per topic and "shared by N". Returns JSON; the template renders. Counts and links come from the DB, never from the model. | Python + Claude API | ● **Opus 5.5** (`claude-opus-5-5`, adaptive thinking, effort `medium`), ~$0.05-0.10/day |
| **Deliver** | Renders `report-YYYY-MM-DD.html`, opens it, macOS notification with the first sentence. Feedback buttons write to `feedback` and nudge profile weights. | Python, `open`, `osascript`, tiny local handler (file:// + a 20-line localhost listener, or just query-string links the next run reads) | no |

### Schedule (launchd, user's Mac)

| Time | Job |
|---|---|
| 02:00 | profile refresh (also `feedcast profile` on demand) |
| 09:00, 13:30, 18:45 | capture → store → dedupe → links → relevance (relevance runs right after each capture so the evening job is fast) |
| 19:30 | digest → deliver |

Captures skip if Chrome is not running or the Mac is on battery below 20%. The report states how many passes it is based on.

## Link extraction

**Sources, in priority order**

1. URLs in the post body (LinkedIn wraps them as `lnkd.in/...`).
2. The link-preview card under the post (title, domain, image alt; the href is the canonical one).
3. "Link in comments" pattern: only if the author's own first comment is already visible in the feed without clicking. LinkedIn often renders the first comment or the author's pinned comment inline; when it does, we take it.

**What we lose by staying read-only:** posts where the link is in a collapsed comment, in a "see more" fold, or in a carousel/document. The item still appears with the takeaway and "open post"; the digest marks it "link in comments, not captured". We never click to get it. If this turns out to be a large share of good posts, the later option is a *single* extra click per high-relevance post, capped at five per run, which is still well inside human behaviour.

**Resolution and classification**

- Follow `lnkd.in`, `bit.ly`, `t.co` with `HEAD` (fall back to `GET` with no body read) and no cookies. Keep both `url` and `resolved_url`.
- `kind` by domain: `github` (repo, gist, release), `paper` (arxiv.org, openreview, aclanthology, PDFs on `.edu`), `model` (huggingface.co), `article` (substack, medium, personal blogs, docs sites, news), `video` (youtube, loom), `linkedin` (another post or newsletter, kept but low priority), `other`.
- Enrichment, all cheap and cacheable for 24 h: GitHub API for description, stars, language, last push (authenticated with `gh auth token` if available, 60 req/h anonymous otherwise); arXiv API for title, authors, abstract first sentence; HF API for downloads and pipeline tag; articles via one `GET` for `<title>` and `og:description`, 10 s timeout, text only.
- **Cross-post dedupe by `resolved_url`**: five people sharing the same repo collapse into one item with "shared by 5", and `shared_by_count` is a strong ranking signal, stronger than reactions, because it is convergence rather than reach.

## Interest profile (the seed)

A fully local stage. It describes the person, not their projects; its only output that leaves the machine is a few hundred words of compact text with no repo names.

**Signals (configurable in `~/.feedcast/config.yaml`)**

| Signal | What is read | What is *not* read |
|---|---|---|
| Git repos with commits in the last 14 days under `roots: [~/code]` | repo name, README first heading and paragraph, dependency names from `package.json` / `pyproject.toml` / `go.mod` / `Cargo.toml`, last 30 commit subjects | no source files, no diffs, no commit bodies |
| Claude Code / agent sessions under `~/.claude/projects` | project directory names, session titles, first user prompt of each session (truncated to 200 chars), last 14 days | no transcripts, no tool outputs, no file contents |
| Your own LinkedIn profile (`feedcast profile --linkedin`, run once, refreshed when older than 30 days) | headline, visible About, experience titles and companies, visible skills, read by the same read-only browser agent on `linkedin.com/in/me` | nothing clicked, no other pages |
| Manual | `pins:` (topics or repos you always care about), `mutes:` (never) | |

**Compaction (● Haiku 5.5, one call):** raw signals → a profile of `role` (one line), `stack`, ~15 `interests` with weights 0-1, `not_interested`. Written to `~/.feedcast/profile.yaml`, which you can edit by hand; hand edits are preserved on refresh (the model only fills fields marked `auto`).

**Example profile (sample, invented):**

```yaml
role: "AI engineer: agents, MCP, evals, document generation"
stack: [TypeScript, Python, Claude Agent SDK, MCP, Docker, Postgres, Playwright, SQLite]
interests:
  - {topic: agent evals and regression testing, weight: 0.9}
  - {topic: MCP server design and tool search, weight: 0.9}
  - {topic: context engineering / long-context memory, weight: 0.8}
  - {topic: pptx and document generation, weight: 0.7}
  - {topic: browser agents and Playwright, weight: 0.6}
not_interested: [AI hiring takes, "X is dead" posts, fundraising announcements]
pins: [github.com/anthropics/claude-agent-sdk-typescript]
mutes: [crypto, web3]
```

**Privacy, stated plainly:** only `profile.yaml` text and the post text go to the Claude API. Code, file contents, diffs, full transcripts and Chrome cookies never leave the machine. The profile file is readable, so you can see exactly what the model knows about your work.

**Relevance gate:** the per-post Haiku call receives the profile and returns `relevance` (0 none, 1 adjacent, 2 useful, 3 squarely on a top interest), `topic`, `why`. Threshold is 2. Skipped posts are counted and listed in the collapsed footer peek, so a wrong skip is one click to find and one click ("more like this") to fix. **Feedback** writes `(post_key, verdict)` to `feedback`; the nightly profile refresh feeds the last 30 verdicts to the compaction call, which raises or lowers interest weights accordingly.

## Local storage (SQLite, `~/.feedcast/feedcast.db` + two YAML files)

```sql
runs        (id, started_at, ended_at, posts_seen, posts_new, status, notes)
posts       (key PK, author, author_headline, url, text, first_seen_run,
             posted_age_at_first_seen, is_suggested, is_promoted,
             relevance INT, topic TEXT, why TEXT, takeaway TEXT,
             substance INT, hype INT, theme_id NULL)
sightings   (post_key, run_id, reactions, comments, reposts, position)
links       (id, post_key, url, resolved_url, kind, title, description,
             meta_json,            -- stars, lang, last_push, authors, downloads
             fetched_at)
             -- UNIQUE(post_key, resolved_url); "shared_by" = COUNT DISTINCT post_key per resolved_url
themes      (id, slug, name, first_seen_date, last_seen_date)
theme_days  (theme_id, date, post_count, shared_link_count, delta_days_building)
feedback    (post_key, verdict TEXT, at)          -- 'not_relevant' | 'more_like_this'
reports     (date PK, json, html_path, opened_at, streak_count)

-- files
~/.feedcast/config.yaml    roots, schedule, thresholds, pins, mutes
~/.feedcast/profile.yaml   interest profile, auto fields + hand edits
~/.feedcast/linkedin-self.json   your own LinkedIn profile, read once
```

## Delivery

| Option | Verdict |
|---|---|
| **Local HTML opened at 19:30 + macOS notification with the first sentence** | **Default.** Full control over the look, no server, reports accumulate in a folder with an `index.html`. Feedback buttons hit a 20-line localhost listener that runs only while the page is open. |
| Email to self | Fallback if the Mac is usually asleep at 19:30. Buttons become `mailto:`-free links the next run reads from a tiny inbox file; acceptable loss. |
| Phone one-liner (iMessage/Telegram) + link | Later. |
| "Ask a follow-up" over the local DB | Later. |

## Risks

- **LinkedIn ToS.** Automated reading is against the User Agreement whatever the method. This is a personal tool in the user's own browser, the lowest-risk shape, not a zero-risk one. Mitigations: 2-3 passes a day, 30-60 posts each, jittered human-paced scrolling, no clicks into posts, comments or links, no reactions, no parallel sessions, never while the user is using LinkedIn. The agent must recognise a challenge or "unusual activity" screen, abort, and disable the schedule until the user re-enables it.
- **Volume.** Three passes of ~50 posts matches a heavy human reader. No "catch-up" mode.
- **Layout changes.** The browser agent reads the page like a person, so small DOM changes do not break it. Each post is validated (author, text, activity URN). Fewer than 10 valid posts marks the run `degraded`, the report says so. Link-preview cards and inline comments change most often; losing them degrades to "link not captured", not to a failed run.
- **Link resolution.** `lnkd.in` sometimes interstitials; we only ever follow with no cookies and give up after two hops. Dead or paywalled resources still get the chip with the domain and no description.
- **Profile goes stale.** An interest from three weeks ago still scores. Mitigations: 14-day window, interests the model stops returning decay 10%/day, and the colophon shows "matched to: <role>" so staleness is visible. `feedcast profile` rebuilds on demand.
- **Cold start, day one.** No history, so no "building for 3 days", and the profile has not seen any feedback. Day one shows the summary and items only, with a one-line "first report, tell me what is wrong with the buttons below". Themes and persistence appear from day three.
- **Over-filtering.** A strict gate hides good things. The collapsed footer peek lists every skipped post's one-line gist with a "more like this" button, so recovery is one click and feeds the profile. Threshold is a config value.
- **Laptop off / asleep.** launchd runs missed jobs on wake, but captures need Chrome open and logged in. Zero captures = no report and one notification. Reliability beyond that means an always-on Mac, a later decision.
- **Model hallucination.** The digest only writes about posts and links it was given; every chip and count renders from the DB.
- **Privacy.** Strangers' post text and the compact profile go to the API; nothing else does. DB and YAML stay local and unsynced.

## v1 scope (one weekend)

1. `profile.py`: git roots + `~/.claude/projects` titles → one Haiku call → `profile.yaml` with hand-edit preservation. `feedcast profile` command, nightly plist.
2. `capture.py`: Claude in Chrome capture prompt run from a scheduled task (no CDP attach: Chrome 136+ blocks it on the default profile); scroll, extract posts **and outbound URLs** (body, preview card, visible first comment); write raw JSON. Manual trigger first, launchd second.
3. `ingest.py`: SQLite schema, URN dedupe, sightings, **link resolution + classification + GitHub/arXiv/article enrichment, dedupe by resolved URL**, Haiku relevance scoring against the profile.
4. `report.py`: one Opus digest call, Jinja template from `mockup.html`, skipped-posts footer, feedback buttons writing to `feedback`.
5. Three capture plists, 19:30 digest plist, `feedcast doctor` (Chrome reachable, profile age, last run, API keys, `gh` token).

## Later

- Theme persistence ("building for N days") and a weekly Sunday roll-up once a week of data exists.
- One capped extra click for "link in comments" on high-relevance posts.
- "Ask a follow-up" over the local DB.
- Phone one-liner.
- Author memory: people whose shares you keep opening get a small "regular" mark.
- Deterministic Playwright extractor as a cheaper second path, with a golden-sample drift check.

## Open questions for you

1. **Profile roots:** which directories hold the work that counts (`~/code`, others), and is reading `~/.claude/projects` session titles acceptable, or git only?
2. **Browser agent:** Claude in Chrome on a schedule (zero code) or Playwright attached to your real Chrome over CDP (more control, cheaper per run)?
3. **Strictness:** threshold at relevance ≥ 2 ("useful to one of your projects") as designed, or ≥ 3 ("directly about something you are building") for an even shorter page?
4. **Link in comments:** strictly never click (v1), or allow a capped single click per high-relevance post from the start?
5. **Mac awake at 09:00 / 13:30 / 18:45 with Chrome open?** If not, which two times, and fall back to email?
