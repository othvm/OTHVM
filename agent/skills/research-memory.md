# Research Memory

Use when the operator asks to research a topic, verify a fact, compare options, or remember something for later.

## Search
1. `memory` `context` latest and `memory` `knowledge` search first.
2. For current facts, use `info` search/news/fetch/wiki; prefer primary sources.
3. Cross-check two independent sources for any claim that could be wrong.
4. Note the publication date; old facts may be stale.

## Answer
- Egyptian Arabic unless the operator writes English.
- Lead with the answer in one or two lines, then two to four supporting bullets.
- Give source links; never fabricate a citation or a number.
- Separate fact from inference: say "according to X" or "I infer".

## Remember
- Save durable facts through `memory` `db` with a clear key and the right category:
  - `personal` namespace: personal_info, preferences, daily_patterns, personal_context.
  - `system` namespace: capabilities, workflows, connected_tools, operational_rules.
- Check for an existing key first; update it instead of duplicating. Reusing a key versions the item.
- Confirm `ok:true` and `embedded:true`; if embedding failed, tell the operator the save is keyword-searchable only.
- Use `memory` `airtable` only for structured tables the operator already keeps there.
- For long research, save a summary in `notes` `local` (or `notes` `notion`) and link it in the reply.
