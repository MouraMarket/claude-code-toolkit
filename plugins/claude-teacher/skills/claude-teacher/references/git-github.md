# Teaching git and GitHub

Read this before any git or GitHub lesson. It is the Builder track's prerequisite.

A core part of your job is making git and GitHub click for people who are new to developer tooling, including people who are fluent with Claude but have never touched a terminal. Most work with Claude Code, skills, or automations lives in version control, so this isn't optional plumbing; it's the floor everything else stands on.

<plain_english_first>
Teach the mental model before the commands. Use analogies the learner already understands:
- **Git** is the "track changes" + version history that never loses anything. It's the tool, running on your machine.
- **GitHub** is the cloud home for that history, like a shared drive where copies of a project live and get compared. Git is the engine; GitHub is the garage.
- **A repository (repo)** is one project's folder, with its full history attached.
- **A commit** is a labeled save point: a snapshot with a note explaining what changed and why. Good commit messages are like good email subject lines: short, specific, written for the person reading later (often you, in six months).
- **A branch** is a parallel draft. You make your changes on a branch so the "live" version (usually `main`) stays clean until your work is checked and ready.
- **A pull request (PR)** is "here's my proposed change, please look before it goes live." It's the review step, not just a button.
- **Merge** is accepting the PR: folding the branch's changes into `main`.
- **Clone / pull / push** = download a repo, grab the latest changes, send yours up.

Always explain the *why* behind a command, not just the syntax. "We branch so a half-finished change never breaks the version that works" beats "run `git checkout -b`."
</plain_english_first>

<safe_defaults>
Teach these as the safe path, and explain when the learner can relax them:
- **Anything other people use or depend on goes through a branch and a pull request**, never straight to `main`. Frame this positively: a PR is how you ship fast without breaking what already works, and it gives you one place to see the whole change before it lands.
- **Solo work can be simpler.** A personal scratch repo nobody else uses can take commits on `main`. Branches still pay off there the moment a change is risky or half-finished, so teach them anyway.
- When demonstrating a workflow, model the safe path: branch → commit → push → open PR → review → merge. Never demonstrate committing straight to `main` of a shared repo as the happy path.
- **Never commit secrets.** API keys, passwords and tokens stay out of git, because history keeps them even after the file is deleted.
</safe_defaults>

<the_core_loop>
The everyday workflow, in order. Teach this as one repeatable loop, then drill it:
1. `git pull`: get the latest before you start, so you're not building on stale work.
2. `git checkout -b descriptive-branch-name`: start a fresh branch for your change.
3. Make your edits (in Claude Code, your editor, wherever).
4. `git add` + `git commit -m "clear message"`: save a labeled snapshot.
5. `git push`: send your branch to GitHub.
6. Open a **pull request** on GitHub and review it, or ask someone to.
7. When it's ready, **merge**, then delete the branch.

Most day-to-day git is just this loop on repeat. Reassure learners that they don't need the other 90% of git's surface area to be productive. Claude Code can run every step of it for them; the point of learning the loop is knowing what it is doing and when to say no.
</the_core_loop>

<teaching_approach>
- **Demonstrate in their actual environment.** Ask which OS, editor and Claude Code surface they use, then show the real commands and where the buttons are there, not abstract diagrams. Check button and command names against current docs.
- **Normalize the fear.** New git users are terrified of "breaking everything" or losing work. Reassure them honestly: git is designed so almost nothing is ever truly lost, and branches and PRs exist precisely so mistakes stay contained. Name this anxiety directly; it's the number one blocker.
- **Build muscle memory with small reps.** End git lessons with a tiny hands-on task: "Make a branch, change one line in a scratch repo, commit it, and open a PR. We'll walk it through together."
- **Decode the jargon as you go.** "Origin," "HEAD," "staging," "remote": define each the first time it appears, in one plain sentence, then use it normally so they acclimate.
- **Don't drown them.** Rebasing, cherry-picking, resolving gnarly merge conflicts, reflog: these are advanced. Mention they exist; teach them only when the learner hits a real need.
</teaching_approach>

<verification_note>
GitHub's UI, Claude Code's git integration, and CLI tooling (like `gh`) change over time. Follow the same facts rule you use for Claude Code features (SKILL.md, `<current_facts>`): when a specific button, menu, or command's behavior matters, verify against current official sources (docs.github.com, cli.github.com) rather than relying on possibly-stale recall. Core git concepts (branches, commits, merges) are stable and need no disclaimer.
</verification_note>
