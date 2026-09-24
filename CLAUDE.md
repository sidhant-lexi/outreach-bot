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

There is no test suite or linter. A fake-page harness (a stand-in `Page`/locator object patched in for `sync_playwright` and `launch`) is the practical way to test `cmd_run` end to end without LinkedIn. Real LinkedIn behaviour (selectors, load timing) can only be checked with the user's logged-in session. The network-free pieces (`Pacer`, `promo_already_in_chat`, `ask_user`, `wait_between_sends`) can be checked by importing the module and feeding them fake inputs; `promo_already_in_chat` only needs an object with `locator(sel).all_inner_texts()`.

Never run `run --send` yourself: it sends real messages from the user's account. `login` and `run --preview` also need the user at the keyboard.

## Architecture

- **Subcommands** `login`, `run`, `history` are dispatched from `main()`. `run` has three mutually exclusive behaviours: dry run (default), `--preview`, `--send`.
- **Selectors:** every LinkedIn DOM selector lives in the `SEL` dict at the top. They are best guesses and break when LinkedIn changes markup; fix breakage there, not inline.
- **Decision pipeline in `cmd_run`**, per conversation, in this order: `check_not_blocked` → skip `looks_like_group_name` / sponsored → read the thread href from the inbox item and skip if `thread_key(href)` is in `sent_log.json` (without opening) → click, then `wait_for_thread` until URL and header name both match → load full history → skip if a message fuzzily matches the promo (`PROMO_MATCH`, difflib) → `conversation_stats` verdict (unsure → skip) → `group_chat_reason` (must come after the verdict, or a mismatched display name of yours looks like a second participant) → not conversed → skip if `draft_in_box` → send / would-send.
- **Thread identity:** `open_thread_matches` (URL key + header name) is re-checked after history loads, before typing and before clicking Send. Playwright's `wait_for_url` is useless here because the URL already matches `/messaging/thread/**` from the previous chat.
- **`conversation_stats`** requires two independent signals to agree: the `--other` CSS class count, and sender-group names vs the logged-in user's name (NFKC/casefold-normalised). Disagreement or unreadable names give `"unsure"`, which never sends; `MAX_UNSURE_IN_A_ROW` consecutive unsure chats stop the run. Never make a fallback that can only *raise* counts.
- **Send accounting:** the log entry is written with `status: "pending"` before the click. `click_send` raises `SendNotClicked` (entry removed, run stops) or returns whether the box emptied. Every click counts toward `sends`/`qualified` (so pacing and the cap apply). An unconfirmed click marks the entry `"unconfirmed"` and stops the run. Any error in the send path stops the run.
- **Stopping on LinkedIn warnings:** `blocked_reason` checks the URL path against `BLOCK_PATHS` and visible dialog/alert/toast text against `WARNING_WORDS`. `check_not_blocked` raises `Blocked` before each chat, inside `wait_for_thread`, after loading history, before typing and before Send; right after a Send a warning marks the entry `unconfirmed` and raises. `Blocked` is caught around the loop: it appends a `(run stopped)` row and never touches the page again. `report.csv` is written in a `finally`, so it survives crashes too.
- **Drafts:** `type_message` raises `DraftInBox` if the composer isn't empty. Callers must catch it *before* their generic `except` that calls `safe_clear`, or they'd delete the user's draft.
- **Two counters:** `qualified` (sent or would-send; this is what `--max-messages` caps, so dry runs stop at the same point a real run would) and `sends` (Send clicks).
- **Pacing:** `Pacer` picks the wait *before* each send after the first (so no wait after the last). `rotating` mode uses `DELAY_TIERS` (never the same tier twice) plus a break every `BREAK_EVERY` sends; `fixed` uses `--delay-min/--delay-max`.
- **State files** (all git-ignored): `.linkedin_profile/` is the persistent Chromium profile holding the login; `sent_log.json` is keyed by `thread_key()` of the thread URL, with an optional `status` of `pending`/`unconfirmed` (missing = sent), and entries with `"source": "found in chat"` were detected, not sent, and are only written during `--send`; `report.csv` is rewritten every run.

## Invariants to preserve

- Nothing is sent without `--send`; `--preview` must never click Send.
- Multi-line text is typed with `Shift+Enter` between lines; a plain Enter in LinkedIn's composer sends the message.
- User control while a message is typed or during a wait is via typed `s`/`q` + Enter on stdin (`select.select`, so POSIX only), not Ctrl+C. SIGINT reaches the Playwright driver and browser too and would leave an unsent draft in the DM.
- Any path that types into the composer and then doesn't send must clear it (`safe_clear`), including on exceptions.
