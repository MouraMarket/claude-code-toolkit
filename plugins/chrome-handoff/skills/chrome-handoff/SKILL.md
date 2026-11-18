---
name: chrome-handoff
description: Use when work has to happen by hand in a live, logged-in web app that no connector or API can do — editing a dashboard or report layout, changing a setting, or checking what a platform's screen actually shows in GA4, Google Ads, Meta Ads Manager, Google Tag Manager, Looker Studio, Zapier, a CMS or any site behind a login. Also when someone wants click-by-click steps for a platform's interface ("how do I change that in Ads Manager," "walk me through clicking this"), asks for "a Claude in Chrome prompt," "hand this to Chrome," "write me the Chrome prompt," "can you check that in the browser," or pastes back a report from a Claude in Chrome run to check. Do NOT use to read numbers a connector or API already returns, to write ad copy, or to learn how Claude in Chrome works.
allowed-tools: mcp__claude-in-chrome
---

# Chrome Handoff

Gets live-UI work done in the user's own logged-in Chrome, then checks the
result. There are two routes and no third: **nothing pushes a task from Claude
Code or a chat into the Claude in Chrome side panel** (no API or link, as of
2026-10). A side-panel session can be continued elsewhere, but not the reverse.
So either this session drives Chrome itself, or it writes a prompt the user
pastes into the side panel.

## Step 1 — Take out what doesn't need a browser

Before any browser work, do every part a connector or API can do (reading
numbers, creating a metric, pulling a setting). Hand to Chrome only what lives
in the UI alone: layout and card edits, settings with no API, checks that need
a login.

**Why:** an API change can be read back and repeated; a click can't. Mixed jobs
split. Do the API half here, then hand over the rest, with the API's numbers
as the expected values.

## Step 2 — Pick the route

Check your own tool list. Only tools that act **in the user's own Chrome**
count, because that is where their logins are.

| You have | Route |
|---|---|
| Claude Code's `claude-in-chrome` tools, or the **Claude in Chrome** connector (Claude Desktop, Cowork) | **A: offer to drive.** Ask: "I can do this in your Chrome now, or give you a prompt to paste. Which?" |
| Anything else: claude.ai on the web, Claude Code without `/chrome`, web search or fetch, a built-in browser with no logins | **B: paste-ready prompt** |

Offer A, don't assume it. Driving uses this session's usage and context. The
user may also want to watch it, or run it later.

**Keep route A inside one turn.** In Claude Code this skill pre-approves the
Chrome tools, but only until the user's next message. So ask the route question
with your structured question tool if you have one (its answer keeps the turn
open; a plain message ends it), then drive straight through to the report. If
the user sends a new message mid-run, invoke this skill again before the next
browser action. **Why:** without the approval, every click asks the user, and
a long click-through that needs a yes per click is worse than one paste.

**In Claude Desktop or Cowork, and for every route B prompt,** tell the user
once, before they start: "Set Claude in Chrome's mode toggle (by the chat box)
to **Automatically approve**." It starts on Manually approve, which asks before
every action, and no setting can switch it for them.

**If browser actions still ask for approval on every step, offer route B
instead.**

In Claude Code with no browser tools, say once that `/chrome` (or `@browser`
in VS Code) turns route A on, then go ahead with B.

## The rules both routes follow

Route A obeys these directly; route B writes them into the prompt.

1. **Read the live screen before trusting any menu or button name.** UIs change
   faster than Claude's knowledge, so every click path from memory is a guess
   to check, never a step to follow.
2. **Read-only unless the job says edit.** Say which, in the first line.
3. **Never publish, send, schedule, delete, or change spend, budgets or bids
   unless the user named that exact action in this conversation.** Name any
   allowed delete exactly ("delete the card X, nothing else"). Ask before any
   other. **Why:** this skill pre-approves every browser action in Claude Code,
   and Automatically approve screens for unsafe actions, not consequential
   ones, so this rule is the only gate.
4. **Give every step an expected value** the screen can be checked against,
   and a stop condition where it could go wrong ("If no GA4 source offers
   Transactions, stop and tell me"; "If it shows 195% or 0.02%, stop").
5. **Logins, 2FA and CAPTCHAs belong to the user.** On a login screen, stop and
   report COULDN'T CHECK. Never try to get past a CAPTCHA.
6. **Pop-ups freeze the page.** If an alert or confirm dialog appears, stop and
   ask the user to dismiss it.
7. **No personal data in the report.** No customer or lead names, emails or
   phone numbers. Field names and values only.
8. **Mark each finding VERIFIED (seen on screen) or COULDN'T CHECK.** Never fill
   a gap with a guess.

## Route A — drive it yourself

Follow the rules above. Discover the page first, then act one step at a time.
Stop at rule 3's actions and ask. Finish with the same Markdown report file
route B asks for (below), saved where the user can open it, then go to Step 4.

What worked in a live test (2026-10):
- **Read page text before taking screenshots.** GA4 tables come back as text
  and cost little. Google Ads tables don't, so Ads needs screenshots.
- **Click elements found by name, not by screen position.** Position clicks
  missed while the page was still loading.
- **Google Ads accounts under a manager account:** open the manager account,
  then use its account switcher. A direct account link stopped at a picker
  that didn't list them.

## Route B — write the paste-ready prompt

**Above the prompt, in your own message:**
- Which account and page to open first ("Log in to Zapier, then paste").
- The mode-toggle line from Step 2 (Automatically approve), and that Chrome
  asks before saving the report file: say yes.
- Every value filled in. Never leave a `[bracket]` for the user to complete.
  If you don't know a value, ask before you write the prompt.

**The prompt, in this order:**
1. Scope and mode in one line: the site, the account, READ-ONLY or EDIT, and
   what is off limits.
2. Context the Chrome session can't know: what changed, the IDs, today's date,
   and the date range to set so the numbers can be checked.
3. Rule 1, in words: "Look at the live screen before assuming any menu or
   button name."
4. Numbered steps, one per line, each with its expected value and any stop
   condition.
5. What not to touch.
6. Rules 3, 5, 6, 7 and 8, in plain words.
7. The report-back format, always a **Markdown file to download**, named
   `<site>-<task>-<YYYY-MM-DD>.md`: a title line, the date, then a table with
   columns `# | Where | Item | What changed or what you saw | Value shown | Matches expected?`.
   If it can't save a file, it prints the same Markdown in one code block.
   **Why:** a file moves between the user, Chrome and this session whole; a
   table copied out of the side panel loses its formatting.

**Format:**
- **One prompt for all the session's UI work.** A new side-panel task starts
  with none of this conversation, so the prompt must stand alone. No "as
  discussed."
- One code block, so the copy button takes all of it.
- **The code block in chat is the delivery.** Never also write the prompt to a
  file, even if a CLAUDE.md or memory rule says to save finished work as files:
  a prompt is not a deliverable. It is pasted once and thrown away, and the
  files pile up.
- Under ~25 lines. If it's longer, split by platform into two prompts.
- End your message with "Attach its report file here (or paste it) and I'll
  check it."

## Step 4 — When the report comes back

1. Read the report file (in Claude Code, from the user's Downloads folder if
   they name it). Check every row against its expected value. Explain each
   mismatch and propose the fix.
2. **Confirm it with a source of your own where you have one** (an API or
   connector read-back) before calling it done. **Why:** a Chrome "done" is a
   claim. Chrome also adds cards in the wrong section and reads stale screens.
3. COULDN'T CHECK rows are open work, not done. Say so.
4. For anything left, write a follow-up prompt in the same shape, covering only
   what's left.

## Example (invented shop)

> Written for: Claude in Chrome, signed in to Looker Studio.

```
You're editing Harbor Coffee's Looker Studio report "Harbor Coffee Overview". EDIT mode. Never publish, send or schedule anything, and don't change budgets or spend. Ask me before deleting anything except the chart in step 3.
Context: the Orders scorecards count purchasers, not orders. Today is Oct 15, 2026. Set the date range to Sep 1–30, 2026 so the numbers below can be checked.
Look at the live screen before assuming any menu or button name.
1. Overview page > "Orders" scorecard: rebuild it on the GA4 metric Transactions. It should read 164. If no GA4 data source offers Transactions, stop and tell me.
2. Google Ads page: before changing anything, write down the campaign filter on each "Orders" chart. Then switch them to the calculated fields "Brand Search Orders" (68.4) and "PMax Orders" (21.3).
3. Overview page: delete the chart "1st Time Purchasers".
Don't touch "Look to Book" or the theme.
If a login screen or CAPTCHA appears, stop and mark that step COULDN'T CHECK. If a pop-up dialog appears, stop and tell me. Don't copy any names, emails or phone numbers into your report. Mark every step VERIFIED (seen on screen) or COULDN'T CHECK; never guess.
Finish by saving a Markdown file to download, harbor-coffee-looker-orders-2026-10-15.md: a title, today's date, then a table: # | Where | Item | What changed | Value shown | Matches expected? If you can't save a file, print the same Markdown in one code block.
```

Attach its report file here (or paste it) and I'll check it.

## Claude in Chrome's limits (as of 2026-10)

Plan around these. They come from Anthropic's help pages, and Anthropic says
its own lists are incomplete.

- **Always refused:** purchases, creating accounts, entering card or ID
  numbers, trades, CAPTCHAs, permanent deletions of the user's data. It asks
  before downloads and before entering sensitive information.
- **Blocked sites:** adult and pirated content. Financial sites are blocked or
  need permission, depending on which help page you read.
- **Long sessions drop.** In Claude Code, `/chrome` → "Reconnect extension".
- **Usage:** browser runs draw on the same plan limits as chat, and cost more.
- **Tested for reading only:** in one test (2026-10), GA4 and Google Ads
  figures matched the API exactly. Edits there, and Meta Ads Manager and
  Google Sheets at all, are untested. Treat a first run on one as a test, and
  check its report hardest.
