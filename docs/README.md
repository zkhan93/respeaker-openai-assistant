# Docs

| Read this | When |
| --- | --- |
| [PRODUCT.md](PRODUCT.md) | "What are we actually shipping, and what is left?" The agreed scope for the macOS product, with a status table. Start here. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | "How does this fit together, and what am I allowed to touch?" Layers, boundaries, diagrams, repo layout, the protocol, and the memory/CPU rules. |
| [ROADMAP.md](ROADMAP.md) | "Where are we and what's next?" Current status and ordered next steps. |
| [DECISIONS.md](DECISIONS.md) | "Why is it like this?" `AD-1`…`AD-18`, each with the alternatives that were rejected. Amend in place; never delete rationale. |
| [LEARNINGS.md](LEARNINGS.md) | "Is my assumption right?" Measured facts, most of which contradict the obvious answer. |
| [DIARIZATION-SPEC.md](DIARIZATION-SPEC.md) | Speaker diarization design, if the meeting product happens. |

Two readmes live outside `docs/` because they belong next to what they describe:
[protocol/README.md](../protocol/README.md) is the wire contract's only
authority, and [tools/README.md](../tools/README.md) explains the split between
things that assert, things a human reads, and things that demonstrate.

**About to change a boundary?** Read the relevant `AD-n` first. Several were paid
for with bugs that took hours to find, and the rationale is written down so
nobody has to re-derive it.
