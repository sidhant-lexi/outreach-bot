# Outreach bot

A Playwright script that goes through your LinkedIn inbox and sends a promotional
message **only to people you've actually had a conversation with**, meaning both
you and they have sent at least one message in the chat.

It won't message:

- people who never replied to you
- people who messaged you and never got a reply
- group chats or sponsored messages
- anyone who has an unsent draft sitting in the message box
- anyone who already has the promo, whether the script sent it or you did

It does nothing unless you ask it to: a plain run is a dry run that only reports who
would get the message.

> **Before you use it:** LinkedIn's User Agreement prohibits automated messaging,
> and sending promotional DMs in bulk is a common reason accounts get restricted.
> Keep volumes low, space out your sends and make the message genuinely personal.
> You use this at your own risk.

## Setup

You need Python 3.9+ and either [uv](https://docs.astral.sh/uv/) or pip.

```bash
git clone git@github.com:sidhant-lexi/outreach-bot.git
cd outreach-bot
```

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

Sends the script couldn't confirm are flagged `pending` or `unconfirmed`. Check
those chats by hand.

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

1. **Obvious group chats** are skipped from the inbox name alone: several names
   ("Jane Doe, Bob Ray", "Jane and Bob") or "and 2 others". Letters after one
   person's name don't count, so "Jane Smith, PhD" or "Ravi Kumar, Jr." is still
   one person. A job title after a comma ("Alex Chen, CEO @ Acme") is treated as a
   group and skipped, which errs on the safe side. **Sponsored** messages are
   skipped too.
2. **Already messaged:** the script reads the chat's link from the inbox. If that
   chat is in `sent_log.json`, it's skipped without being opened.
3. **Right chat open?** After clicking, the script waits until the open chat's link
   *and* the name in its header both match the person it clicked. A chat that
   doesn't load in 15 seconds, or where they don't match, is skipped. It checks
   again after loading history, before typing and before clicking Send, so a
   message can't land in the previous chat or one you clicked on during a wait.
4. **Full history:** the script scrolls to the top of the chat so older messages load.
5. **Promo already in the chat?** It reads the chat and skips anyone who already
   has a message closely matching your promo. This covers a fresh start, a deleted
   or missing `sent_log.json`, a run on another computer, and promos you sent by
   hand. During `--send` runs, these people are added to `sent_log.json`, so later
   runs skip them straight away.
6. **Did you both talk?** Two separate signals have to agree that you *and* they
   have each sent at least one message:
   - how LinkedIn styles the other person's messages;
   - the sender name shown above each group of messages, compared with your name.

   If they agree nobody replied, it's "never conversed". If they disagree, or the
   names can't be read, the chat is skipped as **unsure** and never messaged.
   **Three unsure chats in a row stop the run**, since that usually means LinkedIn
   changed its page (see [Troubleshooting](#troubleshooting)).
7. **Group chats, judged by who wrote.** Once the script knows which messages are
   yours, it counts who else has written. A chat is skipped as a group chat if more
   than one other person has written, or if the one other person's name doesn't
   match the chat's name. The second case is how named groups ("Project Team")
   show up.
8. **Unsent draft?** If there's already text in the message box, the chat is
   skipped and the draft is left exactly as it is. The script checks again right
   before typing, in case you typed something during a wait.
9. Everyone left gets the message, until `--max-messages` is reached.

The promo check compares against the current `message.txt`. Small edits to the
message still count as the same promo. If you rewrite it for a new campaign,
people who got the old version will get the new one.

### When LinkedIn pushes back

The script watches for LinkedIn wanting your attention:
- the browser being sent to a security check, login or verification page;
- a pop-up or notice mentioning things like unusual activity, a limit, a
  restriction or verification, or saying a message couldn't be sent.

It checks before each chat, while chats load, before typing, before clicking Send
and right after sending. If it sees any of these, the run **stops straight away**:
- It doesn't touch the page again or try to get past the check.
- `report.csv` is saved, with a final `(run stopped)` row that says what LinkedIn
  showed.
- If a warning appears right after a Send, that chat is marked `unconfirmed`.

Deal with it by hand: run `login`, sort it out in that browser, and give it a day
or two before sending again. If a warning is already showing when you start, the
script won't run at all.

`report.csv` is saved whenever the run ends, including after any other early stop
or a crash.

This is based on keywords, so a harmless pop-up that happens to say "limit" will
also stop the run. That's deliberate. The words are listed in `WARNING_WORDS` at
the top of the script.

### When a send goes wrong

- The chat is written to `sent_log.json` as `pending` *before* Send is clicked, so a
  crash right after the click can't lead to a second message.
- Every Send click counts toward `--max-messages` and the pacing waits, even if the
  script can't confirm it went through.
- If the message box doesn't empty within 10 seconds of clicking Send, the chat is
  marked `unconfirmed` and **the run stops**. Check that chat by hand. It won't be
  messaged again.
- If Send can't be clicked at all, or anything else goes wrong while sending, the
  box is cleared, nothing is logged and **the run stops**.

## Files

| File                  | What it is |
| --------------------- | ---------- |
| `linkedin_dm_promo.py`| The script. |
| `message.txt`         | Your promo text. |
| `report.csv`          | Written at the end of every run, even if it stops early or crashes: each conversation checked, message counts, and why it was sent or skipped. Overwritten each time. |
| `sent_log.json`       | Everyone the script has messaged (`sent`, or `pending`/`unconfirmed` if it couldn't confirm), or found already had the promo. Nobody in it is messaged again. The chat check is a backup if the file is lost, so keep it anyway. |
| `.linkedin_profile/`  | The script's browser profile, including your LinkedIn login. **Treat it like a password.** |
| `pyproject.toml`, `uv.lock`, `requirements.txt` | Dependencies for uv and pip. |

The included `.gitignore` keeps `.linkedin_profile/`, `sent_log.json`, `report.csv`
and `messaged*.csv` out of git, because they contain your login and your contacts.
If you export `history` under another name, don't commit that file either.

If you send the promo to someone by hand, the script still skips them: it finds the
message in the chat. The exception is a promo worded very differently from
`message.txt`.

## Troubleshooting

**"Not logged in. Run `login` first."** Your session expired or LinkedIn logged
you out. Run `uv run linkedin_dm_promo.py login` again.

**Nothing happens, or the terminal says a command is already running.** An
earlier run is probably still waiting for you to press Enter. Finish it or stop it
first. Only one run can use the saved login at a time.

**It finds no conversations, or marks everyone "never conversed" or "unsure".**
LinkedIn changes its page layout from time to time, and the script looks for
elements by their names on the page. Those names are all in the `SEL` dictionary
near the top of `linkedin_dm_promo.py`. Open LinkedIn messaging in the script's
browser, right-click → Inspect on the element that isn't being found, and update
its entry. The reason in `report.csv` says what couldn't be read:

- *couldn't read this chat's link* → `convo_link`
- *the chat that opened isn't the one clicked* → `thread_title` (the name at the top
  of an open chat)
- *couldn't read sender names* → `group_sender_name`
- *messages look like yours but none show your name* → your name above your own
  messages differs from your profile name. Compare the two in the browser.

**The run stopped with "LinkedIn sent the browser to /checkpoint…" or "LinkedIn is
showing: …".** See [When LinkedIn pushes back](#when-linkedin-pushes-back). If the
pop-up turns out to be harmless, you can remove the word that caught it from
`WARNING_WORDS`. Only do that if you're sure it isn't a warning.

**A chat is marked "unconfirmed" or "pending".** Send was clicked but the script
couldn't confirm the message went out (`unconfirmed`), or it stopped mid-send
(`pending`). Open that chat on LinkedIn and check. The script won't message that
chat again either way. `history` lists these.

**A promo was left typed in a DM.** The run was interrupted before the box was
cleared, for example by Ctrl+C. Open that conversation on LinkedIn and delete the
draft by hand.

**"Could not clear the message box".** Same as above: delete that draft by hand.
The report lists which conversation it was.
