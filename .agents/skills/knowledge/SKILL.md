---
name: knowledge
description: Interview the person for this project's standing knowledge and
  file it with Dark Army — what the project is for, who uses it, what it must never
  do — asking at most three unanswered questions per run. Use when the user
  says /knowledge, or asks to record or read the project's context.
---

# /knowledge

One short interview, run deliberately. It asks the next few questions nobody
has answered yet, files the answers with Dark Army, and stops. It never runs itself,
never adds a turn to ordinary work, and never asks something the project has
already answered.

The answers live with Dark Army, per project, keyed on the question's `key` from
`.claude/skills/knowledge/questions.md`. Two tools reach them:

- `dark_army_knowledge_read` — this project's stored notes. No arguments; it can
  only ever return this project's own.
- `dark_army_knowledge_write` — file one answer. Takes `key`, `question` and
  `answer`. Writing a key that is already there replaces that answer; there is
  no delete.

A session started before the rename carries the same verbs as `bob_*`; a
session keeps the tool list it was born with.

**Both tools are Claude-only today.** If they are not available in this
session, say exactly that — "Dark Army's knowledge tools are not available in this
session, so nothing was read or filed" — and do nothing else. Do not write the
answers into a file instead, and do not ask the questions anyway: an interview
whose answers go nowhere costs a person's time for nothing.

## Method

1. **Read first.** Call `dark_army_knowledge_read`. Show the person a one-line
   summary of what is already on file — how many answers, and which questions
   they cover. If nothing is stored yet, say so.
2. **Pick the next three.** Read `.claude/skills/knowledge/questions.md`, walk
   its sections in order, and take the first **three at most** whose `key` is
   not already answered. If every key is answered, say so and stop — do not
   invent questions the catalogue does not have.
3. **Answer from the tree first.** For each question you are about to ask, look
   for the answer in the codebase — the README, the architecture notes, the
   code itself. Where you find one, ask the question with your finding as the
   proposed answer so the person is correcting rather than composing.
4. **Ask them one at a time**, in this host's own question form, and wait for
   each answer before asking the next. The catalogue's bullets are options to
   offer, not a closed set: the person's own words always win.
5. **File each answer as it arrives.** One `dark_army_knowledge_write` per answer,
   with the catalogue's `key` and the question's own wording. Keep the answer
   short — it is stored at 4000 characters and anything past that is dropped
   without a warning.
6. **Say what you filed, and stop.** List the keys you wrote and nothing else.
   Do not carry on into other work, do not open a card, and do not offer to
   ask more: running `/knowledge` again is how the person asks for more.

## Rules

- **At most three questions per run.** Run it again later to go deeper.
- **Never re-ask an answered key.** The read is what tells you; if it failed,
  stop rather than guessing.
- **Never write a key the catalogue does not declare.** A key nobody can find
  again is a note nobody will read.
- **Never edit this file or the catalogue on the person's behalf.** They are
  the project's own to change.

## This file is yours

Dark Army writes `SKILL.md` and `questions.md` into a project **once**, the first
time it sets that project up, and never touches them again — it compares
nothing and replaces nothing, so your edits are safe for ever. The other half
of that bargain: a later improvement to the shipped version will never reach
you. If you want the current shipped copy, delete the file and let Dark Army set the
project up again.

**There are two copies, and editing one does not update the other.**
`.claude/skills/knowledge/` is what Claude reads; `.agents/skills/knowledge/`
is the Codex and Grok mirror. Dark Army seeds both from the same text and then
leaves both alone, so a question you add or reword in the `.claude` catalogue
will **not** appear in the `.agents` one, and the two can end up asking
different questions under different keys. Either edit both by hand, or delete
the `.agents` copy and let the next pack pass re-seed it from the shipped
text — which is a resync to *shipped*, not to yours. In this project today
the knowledge tools are Claude's anyway, so the mirror is unread; the drift
starts to matter the day that changes.
