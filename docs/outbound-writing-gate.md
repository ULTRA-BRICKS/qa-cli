# Outbound writing gate

Purpose: lightweight anti-slop check for short human messages, especially Slack/Telegram replies.

## What already exists

Rig's anti-slop rules are source-code rules delivered through Oxlint. They catch TypeScript/JavaScript patterns such as chained assertions, unsafe dictionary types and unknown parameters. They do not lint prose.
Source of truth: rig-cli/riglib/lint_policy.py and the vendored anti-slop Oxlint plugin.

This file fills the prose gap. It is intentionally small and reviewable; it is not presented as an AI detector.

## Gate for an outgoing message

Fail or rewrite when any of these are true:

- A factual/technical claim that the recipient may act on has no adjacent proof link when a proof link exists.
- The text says more than the evidence supports: "done", "fixed", "faster", "safer", "ready", "always", "never".
- The same point is repeated in a summary after already being stated.
- There is a ceremonial intro ("honestly", "to be blunt", "great question", "here's the thing") before the point.
- There are vague business nouns where a concrete noun works ("strategy", "synergy", "lever", "framework" without meaning).
- A paragraph mixes several unrelated actions that should be separate messages.
- A link is dropped without saying why it matters, unless the recipient already asked for only the link.
- The message invents an answer for the other person instead of asking when the decision is genuinely theirs.

## Style target for Alex -> Danny

Prefer the pattern already common in the DM history:
- short direct statement;
- one question if a decision is needed;
- casual contractions are fine;
- :) / lol / wow are fine when natural, but don't manufacture them;
- technical terms are fine when both already use them;
- use a reaction instead of a reply when there is no new information;
- for a larger update, start with the conclusion and keep evidence links next to the claims.

Do not intentionally reproduce typos. Match brevity and directness, not errors.

## Manual pre-send pass

1. Claim check: underline every claim. Can each be proven or clearly marked as opinion/plan?
2. Link check: attach the proof/spec/PR/research URL next to the relevant claim.
3. Compression: delete any sentence whose removal changes no decision or information.
4. Recipient check: is there a concrete answer/action/question for Danny?
5. Tone check: read it once as a normal DM, not a project report.
