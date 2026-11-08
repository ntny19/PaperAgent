# Skill: paper-qa

Answering questions about the user's papers:
1. Never answer paper facts from memory. Call `search_papers` first (several queries if needed, with different keywords).
   When a table, hyperparameter or exact number matters, follow up with `read_paper` on the page to read the full text.
2. Cite every claim as `[Paper title p.N]`. If sources disagree or nothing was found, say so. Do not invent numbers.
3. When the user wants "which paper should I look at", answer with a short ranked list: title, page, one line on why.
4. Distinguish what the paper reports from what you infer. Mark inferences as such.
5. Papers are long and the user has read many: be concise, lead with the answer, then the evidence.
6. Reply in the language the user writes in (English or Vietnamese). Keep paper titles and code identifiers unchanged.
