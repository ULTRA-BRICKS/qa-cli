# Auto-QA track: evidence-driven adversarial UI testing

Status: research/adoption plan, 2026-09-29.

## Trigger

Danny shared Emil Kowalski's example of asking AI to actively break a UI with lots of data, long names, unusual emails and other worst cases:
https://x.com/emilkowalski/status/2103516287452483885

This is useful, but it should become a reproducible QA system rather than a prompt that says "try weird inputs".

## Existing Auto-QA research we are reusing

The 2026-09-11 Auto-QA white paper defines QA as an evidence system, not an LLM browser operator.
The core is deterministic analysis, state exploration, property/fuzz/mutation testing and reproducible evidence.
LLM/VLM calls are escalation layers for cases where deterministic evidence cannot decide the oracle.

The canonical evidence chain is:
requirement/spec -> AST/types/routes -> DOM + accessibility -> computed layout/hit testing ->
runtime/network/console -> VSLG/VSML -> design references -> tests/coverage/mutants/incidents.

VSLG means Visual Semantic Layout Graph: typed nodes for perceived UI plus relations such as
containment, overlap, occlusion, reading order, alignment, spacing and visual dominance.
VSML is the strict typed/versioned machine representation used by deterministic oracles.

## First qa-cli vertical slice

1. Capture one canonical runtime observation per state: DOM/Shadow DOM, accessibility tree, geometry, computed style, focus, hit targets, console/network events and the action that produced the state.
2. Add adversarial fixtures for empty/huge collections; very long names/labels/URLs/emails; Unicode/CJK/RTL/IME; missing media; narrow/mobile/zoomed layouts; keyboard-only navigation; slow/failed/reordered/duplicate requests; double click; malformed paste; refresh during submit; back/forward; multiple tabs; retry after failure.
3. Start with deterministic oracles: overflow/occlusion; unreachable or no-op controls; focus-order mismatch; invisible errors; endless loaders; success-after-failure; state loss; console/network failures; visual-vs-accessibility disagreement.
4. Explore a state graph instead of a fixed happy-path script.
5. When a bug is found, shrink the action/data sequence to the smallest reproducer.
6. Report evidence, not prose confidence: exact state/action, observation, provenance, screenshot/DOM/a11y/network artifacts, severity and reproducibility.
7. Use a VLM only when deterministic oracles cannot decide a visual-semantic question.

## Emil idea as a reusable fixture family

Name: hostile-content.

Every editable/control surface should be testable against the same generated fixture set instead of hand-authoring "long string" tests per application. Values include boundary lengths, whitespace, emoji/graphemes, bidi/RTL, combining marks, URLs, emails, duplicate labels, empty values, very large numbers, zero/negative numbers and intentionally similar identifiers.

The fixture runner must also generate hard negatives: suspicious-looking values that are valid. A rule is mature only after both seeded positives and hard negatives are tested.

## Relationship to current qa-cli

The current qa-cli is still primarily a Telegram/browser transport harness. This document does not pretend the Auto-QA system is implemented. The first implementation should reuse the existing browser/session plumbing, then add evidence capture + hostile-content fixtures + 10-20 deterministic oracles before any general autonomous tester loop.

The old standalone Auto-QA initiative remains paused/unmerged. We are importing the useful architecture incrementally into qa-cli so each slice can be exercised on real products.

## Acceptance for the first implementation

- One command can run hostile-content fixtures against a declared web surface without modifying production data.
- Findings include a deterministic reproducer and evidence bundle.
- At least 10 deterministic UI/state oracles run before any model call.
- Seeded bugs and hard negatives are both part of CI fixtures.
- A model finding without deterministic evidence is labelled hypothesis, never pass/fail authority.
- The same fixture contract can be used by Storybook and Playwright.
- Reports distinguish SUT bug, environment failure and inconclusive oracle.

## References

- Emil Kowalski hostile-input idea: https://x.com/emilkowalski/status/2103516287452483885
- Playwright: https://playwright.dev/
- axe-core: https://github.com/dequelabs/axe-core
- WAI-ARIA APG: https://www.w3.org/WAI/ARIA/apg/
- BrowserGym: https://github.com/ServiceNow/BrowserGym
- WebArena: https://github.com/web-arena-x/webarena
- Hypothesis: https://hypothesis.readthedocs.io/
- RESTler: https://github.com/microsoft/restler-fuzzer
