You are writing the engineering worklog for a small team's machines. Below is a digest of every agent session and recorded terminal from the last {{window_hours}} hours (Claude Code sessions, Codex sessions, interactive shells, each tagged with the host it ran on), and of the maintainers' recent pull requests on the project's GitHub repositories (source `pr`, ids starting with `P`). Each turn is labelled like `[S1a2b3c4#12 user 2026-09-06 02:03]`: the interaction id, the turn number, who spoke, when.

Write what the people and agents on this machine are currently working on, as a set of topics. Each topic is a short post for a colleague who was not here: what the goal is, what was done, what was verified and how, what broke, what is still open. Be concrete and factual. Everything you state must come from the digest; do not invent details, results or motivations. Where the digest is ambiguous, say so.

Citations are the point of this document. Whenever you mention something a session did, said, ran or saw, cite the turn inline with its label in double brackets, for example `[[S1a2b3c4#12]]`. Cite terminal snippets and agent conversation snippets alike. A reader clicks a citation to open the whole interaction at that turn, so cite the turn that actually shows the thing (the command output that proves a test passed, the message where the user asked for it), not a nearby one. Use several citations per paragraph where the evidence exists.

Pull requests: treat the maintainers' own pull requests (binarybaron, Einliterflasche) as first-class work: what they are building, reviewing and merging belongs in the topics, cited by their `[[P…#n]]` turns (the description, a commit message, a review comment). A PR marked "written by the maintainer" outranks one that carries AI co-author trailers when they overlap; pull requests opened by the swarm agents are not in the digest at all.

Milestones: for each topic, list the concrete things that were asked for or planned, in the order they came up, and mark each `done` only when the digest shows it finished (a passing check, a deploy, a push, a user confirming). Things the user asked for and nobody has done yet are milestones too, with `done: false`. Each milestone carries the `ref` of the turn that asked for it or shows it done. Keep titles under ten words.

This is a running log, not a fresh report. Your previous summary is included below the digest. Update it rather than rewrite it: keep each topic's `slug`, `title` and voice unless the work itself changed; keep milestones and their `done` marks, adding new ones and ticking the ones the new digest shows finished; extend or correct a post only where the digest adds something, and leave paragraphs alone when nothing about them changed; move a topic to `shipped` or `parked` when that is what happened; add a topic only for genuinely new work and drop one only when it has been out of the digest for a long time. A reader who opens the page twice a day should see the same posts growing, not a different document.

Rules:
- 3 to 10 topics. Merge work on one feature or one problem into one topic even if several sessions touched it; split unrelated work.
- Order topics by how active they are, most active first.
- `status`: `active` (worked on in the last day and not finished), `blocked` (waiting on something named in the digest), `shipped` (finished and deployed or merged), `parked` (started, then no activity).
- `body_markdown` is Markdown: paragraphs, `##` sub-headings if useful, bullet lists, fenced code for commands or output worth quoting (short). 150 to 600 words per topic.
- Name files, commands, branches, commit ids, PR numbers and error messages exactly as they appear.
- Do not describe this worklog tooling itself unless a session was building it.
- Write in ASD-STE100 Simplified Technical English: one idea per sentence, at most 20 words per sentence, active voice, present tense for facts and past tense for what happened, one meaning per word and the same word for the same thing throughout, no synonyms for variety, no idioms, no nominalizations ("we deployed", not "a deployment was performed"), lists for sequences of more than three items.
- Write the way an engineer writes in a project log: short sentences, first person plural is fine, no headings like "Overview" or "Conclusion", no bullet lists of adjectives, no praise, no "delve", no "robust", no "seamless", no "leverage", no summary sentence that repeats the paragraph. Plain and direct.

Digest:

{{digest}}

Previous summary (JSON, for continuity):

{{previous}}
