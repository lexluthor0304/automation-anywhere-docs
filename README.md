# Automation Anywhere Docs Skill

A compact Codex/ChatGPT skill that searches Automation Anywhere's live official documentation and returns a small set of citable topic excerpts. It requires no Python packages, can use `curl` 8.4+ as a bounded fallback for older system TLS stacks, and does not copy the documentation corpus into the skill.

## Install for Codex

```bash
gh repo clone lexluthor0304/automation-anywhere-docs ~/.agents/skills/automation-anywhere-docs
```

Restart Codex if the skill does not appear automatically, then invoke it with `$automation-anywhere-docs` or ask an Automation Anywhere documentation question.

## Test the retriever

```bash
python3 scripts/search_docs.py -- Control Room OAuth authentication API
python3 -m unittest discover -s tests -v
```

The retriever queries Automation Anywhere's public Knowledge Hub API, accepts only `https://docs.automationanywhere.com` URLs, fetches a bounded number of topics, and truncates output before it enters model context.
