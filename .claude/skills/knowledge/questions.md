# Project knowledge questions

The catalogue `/knowledge` interviews from. Every section is one question with
a stable `key:` — that key is what `dark_army_knowledge_write` is given (the
same verb is `bob_*`-prefixed in a session started before the rename), and it is
what makes a re-answer replace the old answer instead of adding a second one.
Never renumber or rename a key: a renamed key is a new question, and the old
answer stays behind under the old name for ever.

The contract the skill relies on:

- **At most three questions per run.** More than that and people stop
  answering; the command is meant to be run again later.
- **Ask the next questions nobody has answered**, in the order below. Skip
  every key `dark_army_knowledge_read` already returned.
- **One at a time**, in the host's own question form, and wait for the answer
  before asking the next.
- **Answer from the tree first.** A question the codebase already settles is a
  wasted turn — read the code, propose the answer you found, and let the person
  correct it.
- The bullets under each prompt are **options to offer, not a closed set**. A
  person's own words always win.

This file is yours. It is written into a project once, the first time Dark Army sets
it up, and never touched again — Dark Army compares nothing and replaces nothing. Add
sections, delete the ones that do not apply, reword the prompts. If you ever
want the shipped version back, delete the file and let Dark Army set the project up
again.

---

## A. What it is for

key: `purpose`

> In one or two sentences, what is this project for?

- A product other people use
- An internal tool for one team
- A library or a service something else builds on
- An experiment that has not decided yet

## B. Who it is for

key: `audience`

> Who actually uses this, and what do they already know?

- Developers on this team
- Developers outside this team
- People who are not developers at all
- Nobody yet — it is upstream of a product

## C. How it is really used

key: `usage`

> Walk through the thing people do with this most often.

- Run it once and read the output
- Leave it running and glance at it
- Call it from other code
- Something the team does by hand today

## D. What it must never do

key: `constraints`

> What is this project not allowed to do, however convenient it would be?

- Touch a particular file, folder or service
- Cost money, send data anywhere, or act without being asked
- Break a published interface other things depend on
- Run anywhere but one platform

## E. The shape of it

key: `architecture`

> What are the main pieces, and what talks to what?

- One process doing everything
- A background service plus one or more front ends
- A library other projects import
- Several services with a defined boundary between them

## F. What is deliberately absent

key: `deliberate_omissions`

> What did this project decide **not** to build, and why?

- A feature that was considered and rejected
- A dependency that was refused on purpose
- A platform or a surface that is out of scope
- Nothing yet — everything absent is just not written

## G. How success is judged

key: `success`

> How do you know this project is doing its job?

- A number somebody watches
- A person's judgement, on a regular look
- Whether a particular failure has stopped happening
- Nothing measures it today

## H. What has been tried and reverted

key: `reverted`

> What has been built here and then taken back out?

- An approach that did not scale
- A dependency that was removed again
- A surface nobody used
- Nothing has been reverted yet

## I. Where the risk is

key: `risk`

> Which part of this breaks worst when it breaks?

- Data somebody cannot get back
- Something running unattended
- A published interface other people depend on
- Nothing here is dangerous; the worst case is an error message

## J. How work gets in

key: `workflow`

> How does a change get from an idea into this project?

- Somebody writes it and opens a pull request
- Through a planning step first, then implementation
- Straight onto the main branch
- There is no agreed route yet
