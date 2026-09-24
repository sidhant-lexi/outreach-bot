# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Playwright (sync API, Python) script, `linkedin_dm_promo.py`, that walks the user's LinkedIn inbox and sends a promo DM only to people they have genuinely conversed with. `README.md` is the user-facing doc; keep it in sync with any change to options, defaults or behaviour, and keep the module docstring's usage block in sync too.

## Commands

```bash
uv sync                                   # or: pip install -r requirements.txt
uv run playwright install chromium
uv run linkedin_dm_promo.py login         # manual login; session saved to .linkedin_profile/
uv run linkedin_dm_promo.py run           # dry run, writes report.csv
uv run linkedin_dm_promo.py run --preview # types promo into each qualifying DM, then clears it
uv run linkedin_dm_promo.py run --send    # real sends
uv run linkedin_dm_promo.py history       # who has been messaged (--csv FILE)
```

There is no test suite or linter. Everything that touches LinkedIn needs the user's logged-in session, so it can't be exercised offline. The network-free pieces (`Pacer`, `promo_already_in_chat`, `ask_user`, `wait_between_sends`) can be checked by importing the module and feeding them fake inputs; `promo_already_in_chat` only needs an object with `locator(sel).all_inner_texts()`.

Never run `run --send` yourself: it sends real messages from the user's account. `login` and `run --preview` also need the user at the keyboard.

## Architecture

- **Subcommands** `login`, `run`, `history` are dispatched from `main()`. `run` has three mutually exclusive behaviours: dry run (default), `--preview`, `--send`.
- **Selectors:** every LinkedIn DOM selector lives in the `SEL` dict at the top. They are best guesses and break when LinkedIn changes markup; fix breakage there, not inline.
- **Decision pipeline in `cmd_run`**, per conversation, in this order: skip group chats / sponsored → skip if thread URL is in `sent_log.json` → scroll to load full history → skip if a message in the chat fuzzily matches the promo (`PROMO_MATCH` ratio via difflib) → skip unless both sides have sent ≥1 message (`conversation_stats`: `--other` class count, with a fallback on sender names vs the logged-in user's name) → send / would-send.
- **Two counters:** `qualified` (sent or would-send; this is what `--max-messages` caps, so dry runs stop at the same point a real run would) and `sends` (actual sends).
- **Pacing:** `Pacer` picks the wait *before* each send after the first (so no wait after the last). `rotating` mode uses `DELAY_TIERS` (never the same tier twice) plus a break every `BREAK_EVERY` sends; `fixed` uses `--delay-min/--delay-max`.
- **State files** (all git-ignored): `.linkedin_profile/` is the persistent Chromium profile holding the login; `sent_log.json` is keyed by thread URL (entries with `"source": "found in chat"` were detected, not sent, and are only written during `--send`); `report.csv` is rewritten every run.

## Invariants to preserve

- Nothing is sent without `--send`; `--preview` must never click Send.
- Multi-line text is typed with `Shift+Enter` between lines; a plain Enter in LinkedIn's composer sends the message.
- User control while a message is typed or during a wait is via typed `s`/`q` + Enter on stdin (`select.select`, so POSIX only), not Ctrl+C. SIGINT reaches the Playwright driver and browser too and would leave an unsent draft in the DM.
- Any path that types into the composer and then doesn't send must call `clear_composer`.
