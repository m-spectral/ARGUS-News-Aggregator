# Automated Reporting, Gathering, and Updates System (ARGUS) - News Reporting

A reusable AI news briefing built with Python, RSS feeds, and Gemini. It collects articles, removes repeated links, checks the model's citations against supplied feed text, and writes a sourced digest.

## Brief format

Up to five sourced stories per section:

1. Market-Moving Headlines: market impact.
2. AI + Business: operational takeaway.
3. AI + Missteps: the lesson.
4. AI + New Research: why it matters.

The script limits to twenty sources and are derived from RSS or Atom content and will be shorter than the full article. 

## Start locally

Python 3.9 or newer is required. The collector and briefing code use the standard library. The notebook is optional.

```bash
cp .env.example .env
```

Set your own `GEMINI_API_KEY` in `.env`. `GEMINI_MODEL` defaults to `gemini-3.5-flash-lite`; select a model available to your account. Do not commit `.env` or a real webhook URL.

```bash
python3 -m unittest -q
python3 collector.py collect --days 1
python3 desk.py --dry-run
python3 desk.py
```

The dry run prints the selection and call budget without contacting Gemini. The final command makes Gemini requests and may incur charges. With the default one-day window, the desk analyzes at most 120 articles in groups of four and permits at most 50 logical calls. Transient server errors may cause additional HTTP attempts. The input can be limited further with `--max-articles` and `--max-calls`.

The generated brief is in `analysis/standard/brief.md`. See `analysis/standard/review_queue.json` for items held due to missing or unsupported evidence and `reports/feed_health.json` for feed errors. These generated files, the SQLite database, and model cache are ignored by Git.

## Optional Slack delivery

Set your own `SLACK_WEBHOOK_URL` in `.env`. An incoming webhook sends to the conversation selected when that webhook was created. Review the brief first, then run:

```bash
python3 desk.py --send-brief
```

The program sends only the formatted brief. It splits large briefs into messages and stores local delivery state. Network ambiguity can still cause a duplicate send.

## Sources and optional providers

The RSS and Atom feeds are listed in `sources.json`. Some may be unavailable, paywalled, or broad enough to require filtering. Check the current feed responses with:

```bash
python3 collector.py check
```

RSS works without news API keys. The NewsAPI.org, GNews, and NewsData.io adapters are included but disabled by default. NewsAPI.API is listed separately and has no working adapter until its endpoint is documented. To use a supported adapter, explicitly enable it in `sources.json`, set its corresponding key in `.env` (`NEWSAPI_KEY`, `GNEWS_KEY`, or `NEWSDATA_KEY`), and run `python3 collector.py collect --apis`. Review each provider's terms and usage limits.

`instructions.md` contains the editorial instructions. The model ranks items and proposes interpretations; the local checks verify some formatting and source excerpts but cannot establish whether a claim is true. Review allegations, figures, research design, and market impact against original sources.

## Notebook

```bash
python3 -m pip install -r requirements-notebook.txt
python3 -m jupyterlab notebooks/desk.ipynb
```

The notebook uses local results or labelled synthetic examples and requires no Gemini calls.

## Public repository and private runs

This repository is source code and synthetic examples. It has one CI workflow that runs local unit tests and scans tracked files for common credential patterns. It has no automated research schedule, production webhook, saved article database, or live-run artifacts.

Keep any operating deployment with API secrets, model cache, run logs, and potentially sensitive source material in a separate private repository or local environment. GitHub Secrets are appropriate for a private deployment; never put actual values into `.env.example` or a commit. The scanner is a guardrail and cannot detect every secret or clean a previously exposed commit.

## License

MIT. Third-party articles and feeds retain their own rights.
