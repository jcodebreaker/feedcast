# Feedcast — agent brief

Read this first, then `ARCHITECTURE.md` (the design source of truth), then open `fixtures/report/sample-report.html` (what the person sees; `mockup.html` is the older v1 look).

## The goal

The user is an AI engineer. Their LinkedIn feed carries real signal (new repos, papers and tools, often from people they don't follow and surfaced by the algorithm), but they aren't on LinkedIn enough to catch it. Feedcast reads their feed for them two or three times a day and, every evening, hands them one page: **a summary of what was in today's feed that matches their interests, grouped by topic, with direct links to the repos, papers and articles that were shared.** Their local work and their own LinkedIn profile are only a seed for knowing who they are ("AI engineer: agents, MCP, voice, evals"); the page is about the feed and never names their repos.

It's a personal, playful tool. The user builds projects like this to make people's lives easier, so it should feel like a small delight, not a report generator.

## What success looks like

- The evening page reads in **60 seconds**. Items are grouped under short topic labels ("Voice models", "MCP"). Every item has **link chips** to the shared resource first, a one-line takeaway and a *why it's notable* line. The resource matters more than the post.
- **Relevance to the user's interests is the filter.** A post off their interests is skipped. It shows up only in the "N skipped" footer peek, never as an item. There is no generic "AI news" slot.
- The same repo shared by five people becomes one item, "shared by 5", and that's a ranking signal.
- Weather is light flavour in the wording ("three days running"), never charts, gauges or numbers.

## Decisions already made (keep them; the reason sits beside each)

| Decision | Why |
|---|---|
| Read the feed with a **browser agent in the user's own logged-in Chrome** (Claude in Chrome on a schedule for v1) | The LinkedIn API has no home-feed access. Hosted or headless scrapers get accounts restricted. Following a fixed list of people misses the suggested posts, which is half the value. |
| Use **Claude in Chrome rather than a Playwright CDP attach** | Chrome 136+ ignores `--remote-debugging-port` on the default profile. CDP would need a separate profile that is logged in separately. |
| **Read-only, human-paced, low volume**: 2–3 runs a day, 30–60 posts each, jittered scrolling, no clicks on "see more", comments or links. Abort on any challenge or "unusual activity" screen. | Automation is against LinkedIn's terms, so this keeps the shape as close to one person scrolling as possible. The user's account is what's at risk. |
| **Everything local**: SQLite and YAML under `~/.feedcast/`, an HTML report opened at 19:30, a macOS notification | No server, no account, nothing to host. |
| **Only the compact profile text and post text go to the Claude API**, never code, file contents, full transcripts or cookies | The user's local work is private. The profile is a short, editable description of the person with no project names. |
| Haiku 5.5 for classification and relevance; one Opus 5.5 call writes the digest | Cheap per-post work, one strong call for the copy. Budget is under $0.20/day. |
| Counts and links in the report come from the DB; the model only writes copy about posts it was given | No invented numbers or links. |
| **The profile is a seed, the page is about the feed.** Local repos, sessions and the user's own LinkedIn profile describe who they are (role, stack, interests); posts are scored against that and labelled by topic. The page never says "for <project>". | v1 tied every item to a local repo; live, that read as a project report, not a feed summary, and hid good posts that matched the person but no current repo. |

## How the user works (applies to every task here)

- **Simplest road; reuse first.** Use existing primitives (scheduled tasks, Claude in Chrome, the Claude API, launchd, stdlib `sqlite3`) before writing new machinery.
- **Mock-up before UI changes.** Any change to the look gets a prototype the user approves before it's built. The approved v2 design lives in `templates/style.css` (shared by the evening page, the index and the setup page).
- **Taste matters.** The page has one clear intent: what the feed shared, resources first. Polish every surface you touch.
- **Real results, not "it should work."** Prove a stage by running it end to end and showing the output.
- **Sample data stays in fixtures**, clearly labelled. The live DB only ever holds what was really captured.
- **Ask before anything that costs money or touches the account in bulk**, such as a first live LinkedIn capture or a large batch of API calls.

## Status (2026-10-30)

v1 is built and had one live run. After it, the page moved from "items for your projects" to "your feed, by topic, matched to who you are" (decision above). Post permalinks now come from `read_page` hrefs; that and `feedcast profile --linkedin` still need a supervised live run.

v2 UI (branch `ui/v2`): a visual evening page (hero card, topic tabs, images downloaded locally, quote card when there is none) and a setup page at `feedcast serve`'s `/setup` that replaces the terminal `init` (still behind `feedcast init --terminal`). Post keys without a URN now survive the "…more" fold.

Open questions waiting on the user are in `ARCHITECTURE.md` → "Open questions for you". Until answered, build with these defaults and say so: work roots = the user names them (don't guess), relevance threshold = 2, never click for "link in comments", captures at 09:00 / 13:30 / 18:45, delivery = local HTML.
