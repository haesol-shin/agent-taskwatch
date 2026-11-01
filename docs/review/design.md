# Design review

Use this file with the common guidelines when the deliverable is a design: a plan, a decision, or a spec for work that is not only a code change.

## What to review

**D-1.** Judge the design against its purpose. A design that is clear and does not serve the purpose is a finding.

**D-2.** Check that there is a way to tell whether the design worked, and that the method matches the purpose. A method that cannot fail, or that measures a different question, is a finding.

**D-3.** If a simpler approach meets the same purpose, report it with evidence from the deliverable. When two approaches are equally valid, accept the author's choice only when an owner decision or a written reason covers it; otherwise the answer to `alternatives` decides. Taste is not a finding.

**D-4.** Report a structure the purpose does not need. Do not ask for a design gate, a framework, or a process the assignment did not require.

**D-9.** A pick in `choices.json` is an implementation choice that the owner decides. A request for permission, deploy, publish, spending or scope in `picks` is a finding: those belong to the approval scope and the approval gate.

## Decision method

**D-5.** If you, reading only the deliverable, cannot explain a distinction or a design reason that the conclusions depend on, that is blocking, even when the large shape is clear.

**D-6.** A problem raised early must be resolved later, or explicitly left open with a reason. A section that repeats another, with no new decision, is a finding.

## Scope

**D-7.** Do not block on a preference. A finding needs evidence in the deliverable, a purpose it fails, and a concrete change.

**D-8.** When nothing in the design fails its purpose, the decision method, or the scope rules, the verdict is correct and there are no findings.

## Sources

Generalized from a deliverable-review role and from the rule that a design is judged on purpose, decision method, and whether a simpler approach would do. Provenance is in the [folder README](README.md).
