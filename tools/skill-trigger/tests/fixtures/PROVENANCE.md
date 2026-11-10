# Fixture provenance

Both files are **synthetic**. Nothing in them was recorded from a real
session: every id, path, version and line of text is invented. They follow
the event shapes Claude Code writes under

```
claude -p "<prompt>" --include-partial-messages --output-format stream-json --verbose
```

one JSON object per line. Edit them by hand, or regenerate them with a small
script, as long as the properties below still hold: the tests depend on them.

## skill-call.jsonl

Models a session that routes to the build skill.

- One `{"type": "system", "subtype": "init"}` event carrying
  `claude_code_version`, a non-empty `skills` list and a `plugins` list
  (`extract_init` reads these).
- A `Skill` `tool_use` `content_block_start` at index 1 with `input: {}`.
  The arguments arrive only as `partial_json` fragments at that index:
  `""`, `{"skill": "toolkit-`, `build:bu`, `ild", "args": "…"}`. The name is
  split three ways, once inside the `plugin:skill` identifier, so a parser
  that reads one fragment at a time never sees it whole.
- After the Skill block's `content_block_stop`, a later message reuses
  index 1 for an unrelated text block whose text contains
  `"skill": "other:skill"`. A parser must stop at the first Skill block's
  stop event, not keep reading everything at that index.
- Events the parser must skip: a hook event, a rate-limit event, a status
  event, text blocks and a `user` tool result. It ends with a `result` event.

## no-skill.jsonl

Models a session that answers without calling any skill.

- An `init` event as above.
- A `Bash` `tool_use` block at index 1 whose `partial_json` holds a complete
  `"skill": "toolkit-build:build"` key. This is a decoy: a parser that
  accepted any `tool_use` block, not only one named `Skill`, would report it.
- A `Read` `tool_use` block, text blocks, and a final `result` event. No
  `Skill` block anywhere.
