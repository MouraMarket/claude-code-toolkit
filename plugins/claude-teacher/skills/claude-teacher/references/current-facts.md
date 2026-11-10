# Current Claude Code facts

As of: 2026-10-20. Each row was checked against the source in its last column.

**This is the only place product facts live.** When Anthropic ships a change, update the row
here, re-check the others, and change the date. Never add model names, prices or usage limits:
they change too often, so those rows point to the live page instead.

**Using this file:** if the date above is more than 90 days old, or the learner names something
this table doesn't cover, check the live source before teaching the fact. If the live source
disagrees with a row, teach the live version and tell the learner this file needs an update.

| Topic | Current fact | Source |
|---|---|---|
| Official docs | Claude Code: code.claude.com/docs. API and models: platform.claude.com/docs (docs.claude.com redirects there). The Claude apps and accounts: support.claude.com. Announcements: anthropic.com/news. | checked 2026-10-20 |
| Where Claude Code runs | The terminal CLI, VS Code (and Cursor), JetBrains IDEs, a desktop app (its Code tab), and the web at claude.ai/code. The local surfaces share one machine's setup. A web (cloud) session starts from a fresh clone of the repo: what is committed carries over (the repo's CLAUDE.md; in a one-repo session its `.claude/settings.json` and `.mcp.json`), but anything set up only on the learner's machine does not (`~/.claude/` files, user-scope plugins, MCP servers added at local or user scope). | code.claude.com/docs/en/overview; code.claude.com/docs/en/cloud-environments |
| Installing and updating | The native install is recommended and updates itself in the background. Homebrew and WinGet installs don't auto-update: upgrade them by hand. Check a working install with `claude --version`. | code.claude.com/docs/en/overview |
| Accounts | Most surfaces need a Claude subscription or an Anthropic Console account. The terminal CLI, VS Code and JetBrains also support third-party model providers. | code.claude.com/docs/en/overview |
| Customizing | `CLAUDE.md` in a project holds instructions Claude Code reads at the start of every session; auto memory saves learnings as it works. Skills package repeatable workflows; hooks run shell commands before or after Claude Code actions; plugins bundle skills, hooks and agents for install. | code.claude.com/docs/en/overview |
| Connectors (MCP) | Claude Code connects to outside tools through MCP servers. Which ones exist, and which the learner has configured, changes often: check their setup (`claude mcp list`, `/mcp`). Never list them from memory. | code.claude.com/docs/en/overview |
| Models | Pointer only: the models overview in platform.claude.com/docs. | live page |
| Plans, limits, prices | Pointer only: claude.com/pricing and support.claude.com. | live page |
