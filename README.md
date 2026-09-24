# LinkedIn DM promo sender

A Playwright script that goes through your LinkedIn inbox and sends a promotional
message **only to people you've actually had a conversation with**, meaning both
you and they have sent at least one message in the chat.

It won't message:

- people who never replied to you
- people who messaged you and never got a reply
- group chats or sponsored messages
- anyone who already has the promo, whether the script sent it or you did

It does nothing unless you ask it to: a plain run is a dry run that only reports who
would get the message.

> **Before you use it:** LinkedIn's User Agreement prohibits automated messaging,
> and sending promotional DMs in bulk is a common reason accounts get restricted.
> Keep volumes low, space out your sends and make the message genuinely personal.
> You use this at your own risk.

## Setup

You need Python 3.9+ and either [uv](https://docs.astral.sh/uv/) or pip.

**With uv**

```bash
uv sync
uv run playwright install chromium
```

**With pip**

```bash
pip install -r requirements.txt
playwright install chromium
```

With pip, use `python linkedin_dm_promo.py` wherever this README says
`uv run linkedin_dm_promo.py`.

## Quick start

**1. Log in once.**

```bash
uv run linkedin_dm_promo.py login
```

A browser window opens. Log in to LinkedIn there, including any verification code
it asks for, then go back to the terminal and press Enter. Your session is saved in
`.linkedin_profile/`, so you won't need to log in again.

The script uses its own browser, separate from your everyday Chrome. Being logged
in to LinkedIn in Chrome doesn't carry over, so you do need this step.

**2. Write your message.**

Edit [`message.txt`](message.txt). These placeholders are filled in for each person:

| Placeholder    | Becomes                 |
| -------------- | ----------------------- |
| `{first_name}` | Their first name (Jane) |
| `{name}`       | Their full name (Jane Doe) |

Replace the `[PRODUCT]` and `[ONE-LINE VALUE PROP]` text before sending anything.
Line breaks are kept.

**3. Dry run: see who qualifies.**

```bash
uv run linkedin_dm_promo.py run --max-threads 20
```

This only reads your messages. Check the `report.csv` it writes against your inbox.

**4. Preview: see the message in each DM.**

```bash
uv run linkedin_dm_promo.py run --preview --max-messages 3
```

The script types the promo into each qualifying DM so you can see exactly how it
will look, then clears it. **It never clicks Send.**

**5. Send for real, starting small.**

```bash
uv run linkedin_dm_promo.py run --send --pause 10 --max-messages 2
```

## Modes

| Command             | Opens DMs | Types the message | Sends |
| ------------------- | :-------: | :---------------: | :---: |
| `run`               | ✓ | | |
| `run --preview`     | ✓ | ✓, then clears it | |
| `run --send`        | ✓ | ✓ | ✓ |

### Controlling it while it runs

When a message has been typed into a DM (in `--preview`, or in `--send` with
`--pause`), the terminal asks what to do:

| Type in the terminal | Preview                    | Send                         |
| -------------------- | -------------------------- | ---------------------------- |
| Enter (or wait for the countdown to end if you set `--pause`) | Clear it, go to the next person | Send it, go to the next person |
| `s` then Enter       | Clear it, go to the next person | Clear it, **don't send**, go to the next person |
| `q` then Enter       | Clear it, stop the run     | Clear it, **don't send**, stop the run |

Without `--pause`, preview waits for you each time. With `--pause SECONDS`, it goes
ahead by itself after that many seconds.

Type these **in the terminal, not in the LinkedIn window.** Pressing Enter inside
LinkedIn's message box sends the message.

**Don't use Ctrl+C to cancel a message.** It stops the script before it clears the
box and can close the browser, leaving the promo behind as an unsent draft that
you'd have to delete by hand. Use `s` or `q` instead.

## All options

### `run`

| Option                 | Default | What it does |
| ---------------------- | ------- | ------------ |
| `--send`               | off     | Actually send messages. Without it, nothing is ever sent. |
| `--preview`            | off     | Type the promo into each qualifying DM, then clear it. Can't be combined with `--send`. |
| `--pause SECONDS`      | none    | Show each typed message for this long, then move on (preview) or send it (`--send`). Without it, preview waits for Enter and send goes immediately. |
| `--max-messages N`     | 10      | Stop after this many messages. In a dry run or preview, this many "would send" results. (`--max-sends` also works.) |
| `--max-threads N`      | 40      | How many inbox conversations to check, newest first. |
| `--delay-mode MODE`    | `rotating` | How sends are spaced out. See [Pacing](#pacing). |
| `--delay-min SECONDS`  | 30      | Shortest wait between two sends, in `fixed` mode. |
| `--delay-max SECONDS`  | 90      | Longest wait between two sends, in `fixed` mode. |
| `--message-file PATH`  | `message.txt` | Where to read the promo text from. |
| `--headless`           | off     | Hide the browser window. Can't be combined with `--preview`. |

### `history`

```bash
uv run linkedin_dm_promo.py history
uv run linkedin_dm_promo.py history --csv messaged.csv
```

Lists everyone the script has sent the promo to, with the time and a link to the
conversation. `--csv FILE` exports the list, including the exact text each person
got.

It also lists people whose chat already had the promo when the script checked it
during a `--send` run. These are marked "found in chat", and their date is when the
script found it, not when the promo was sent.

### `login`

Opens the browser so you can log in by hand. Run it again if your session expires
or LinkedIn logs you out.

## Pacing

With `--send`, the script waits between messages so they don't go out at a steady,
machine-like rate. The default `rotating` mode:

| Wait       | Length          | How often |
| ---------- | --------------- | --------- |
| Short      | 20–45 seconds   | Most often |
| Medium     | 45 seconds – 2 minutes | Often |
| Long       | 2–4 minutes     | Sometimes |
| Break      | 5–15 minutes    | After every 4–8 sends (picked at random each time) |

- Each wait is picked at random, and the same kind is never used twice in a row.
- There's no wait before the first send or after the last one.
- The terminal shows each wait as it starts, for example
  `Waiting 1m 13s (medium) before the next send`. Type `q` + Enter during a wait to
  stop the run.

`--delay-mode fixed` goes back to a single random wait between `--delay-min` and
`--delay-max` (30–90 seconds by default), with no breaks. To change the rotating
ranges, edit `DELAY_TIERS`, `BREAK_EVERY` and `BREAK_LENGTH` near the top of the
script.

## How it decides who to message

For each conversation in your inbox, newest first:

1. **Group chats** (several names) and **sponsored** messages are skipped.
2. **Already messaged:** if the conversation is in `sent_log.json`, it's skipped.
3. **Full history:** the script scrolls to the top of the chat so older messages load.
4. **Promo already in the chat?** It reads the chat and skips anyone who already
   has a message closely matching your promo. This covers a fresh start, a deleted
   or missing `sent_log.json`, a run on another computer, and promos you sent by
   hand. During `--send` runs, these people are added to `sent_log.json`, so later
   runs skip them straight away.
5. **Did you both talk?** It counts the messages you sent and the messages they
   sent. Both have to be at least 1, otherwise it's skipped as "never conversed".
6. Everyone left gets the message, until `--max-messages` is reached.

The promo check compares against the current `message.txt`. Small edits to the
message still count as the same promo. If you rewrite it for a new campaign,
people who got the old version will get the new one.

## Files

| File                  | What it is |
| --------------------- | ---------- |
| `linkedin_dm_promo.py`| The script. |
| `message.txt`         | Your promo text. |
| `report.csv`          | Written every run: each conversation checked, message counts, and why it was sent or skipped. Overwritten each time. |
| `sent_log.json`       | Everyone the script has messaged, or found already had the promo. It's a quick record of who to skip. The chat check is a backup if the file is lost, so keep it anyway. |
| `.linkedin_profile/`  | The script's browser profile, including your LinkedIn login. **Treat it like a password.** |
| `pyproject.toml`, `uv.lock`, `requirements.txt` | Dependencies for uv and pip. |

If you put this folder in git, don't commit `.linkedin_profile/`, `sent_log.json`
or `report.csv`, since they contain your login and your contacts. Commit `uv.lock`.

If you send the promo to someone by hand, the script still skips them: it finds the
message in the chat. The exception is a promo worded very differently from
`message.txt`.

## Troubleshooting

**"Not logged in. Run `login` first."** Your session expired or LinkedIn logged
you out. Run `uv run linkedin_dm_promo.py login` again.

**Nothing happens, or the terminal says a command is already running.** An
earlier run is probably still waiting for you to press Enter. Finish it or stop it
first. Only one run can use the saved login at a time.

**It finds no conversations, or marks everyone "never conversed".** LinkedIn
changes its page layout from time to time, and the script looks for elements by
their names on the page. Those names are all in the `SEL` dictionary near the top
of `linkedin_dm_promo.py`. Open LinkedIn messaging in the script's browser,
right-click → Inspect on the element that isn't being found, and update its entry.

**A promo was left typed in a DM.** The run was interrupted before the box was
cleared, for example by Ctrl+C. Open that conversation on LinkedIn and delete the
draft by hand.

**"Could not clear the message box".** Same as above: delete that draft by hand.
The report lists which conversation it was.
