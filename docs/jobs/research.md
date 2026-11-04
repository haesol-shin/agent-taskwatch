## Job type: research (review `source`)

A research job answers a question from sources and shows where each statement comes from.

- Inputs: the question (the prompt), the intent, the spec (no template headings are required), and the result of any predecessor, listed in the message.
- Result: the files named in `outputs`, usually `result.md`. Name `claims.json` in `outputs` as well: its presence turns on the source check, and a design job that lists it also answers to the source review rules.
- Check: the job's `check_command`, the required files and the checklist, the source check below, then the brief check.
- Review: the common rules and `source.md`. The reviewer judges support only for the claim and quote pairs the source check found present.
- Brief: say how sure each conclusion is and what you searched for and did not find. Search terms that found nothing belong in `Choices made` or `Where to look`. The brief shows how many claims rest on search-result snippets only.

### What to save

Save what you read, as it was returned, so a quote can be checked without the network.

- `sources/<id>.txt`: the text you read, verbatim, one file per source. An id is letters, digits, `_` and `-`.
- `sources/index.json`: `{"S1": {"url": "...", "retrieved": "2026-11-01", "kind": "full", "sha256": "<hash of sources/S1.txt>"}}`. `kind` is `full` for a page you read and `snippet` for a search result you only saw.
- `claims.json`: a list `[{"id": "C1", "claim": "...", "source": "S1", "quote": "...", "type": "fact|number|absence|derived", "searched": [...], "from": [...]}]`.
- In the result, cite each claim as `[C1]`. Every claim is cited, and every citation has a claim.

A `fact` or `number` claim needs a `source` and a `quote` of at least 8 words (a `number` claim whose quote holds the number may be shorter). An `absence` claim ("nothing exists", "no one has done X") needs the search terms you tried in `searched`. A `derived` claim (a sum, a comparison) names the claims it comes from in `from`.

### What the check does

The check is plain code with no network and no model. It fails when a source file is missing, its hash differs from the index, or its `url`, `retrieved` or `kind` is wrong; a quote is not in its source after whitespace, quote marks and dashes are normalized; a number in a claim is not in its quote (unless the claim is `derived`); a claim is cited nowhere or a citation has no claim; or an `absence` claim lists no search terms. Claims that rest only on `snippet` sources are allowed and counted. The check writes `source-table.md` in the job folder, one row per claim with its quote, source and whether the quote was found, for the reviewer.

It cannot tell that the saved text is what the URL served (you wrote the file), that a quote supports its claim, that the sources cover the question, that a computed number is right, or that a search found everything. The reviewer and the owner judge those.
