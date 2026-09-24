"""
LinkedIn DM promo sender (Playwright).

Goes through your LinkedIn inbox, opens each 1:1 conversation, and checks whether
you have actually conversed with that person: both of you have sent at least one
message. It sends the promotional message only to those people.

Dry run by default. Nothing is sent unless you pass --send.
With --preview, the dry run also types the promo into each qualifying DM so you can
see it, waits for you, then clears it. Send is never clicked in preview.

--pause SECONDS makes it hands-free: preview shows each message for that long, then
clears it and moves on; --send shows each message for that long, then sends it.
While a message is showing, type s + Enter to skip that person or q + Enter to stop.

Sends are spaced out with rotating random delays: each wait is short, medium or long
(never the same kind twice in a row), with a longer break every 4-8 sends.
Use --delay-mode fixed for a plain random wait between --delay-min and --delay-max.

Setup (either one):
    uv sync && uv run playwright install chromium
    pip install -r requirements.txt && playwright install chromium

Usage (with uv, swap `python` for `uv run`):
    python linkedin_dm_promo.py login                 # log in once, by hand; the session is saved
    python linkedin_dm_promo.py run                    # dry run: report who qualifies
    python linkedin_dm_promo.py run --preview          # dry run + type the promo into each DM, never send
    python linkedin_dm_promo.py run --preview --pause 5   # same, moves on by itself every 5s
    python linkedin_dm_promo.py run --send --pause 10     # shows each message 10s, then sends it
    python linkedin_dm_promo.py run --send             # actually send
    python linkedin_dm_promo.py run --send --max-messages 5 --max-threads 50
    python linkedin_dm_promo.py history                # everyone messaged so far (--csv FILE to export)
"""

import argparse
import csv
import difflib
import json
import random
import re
import select
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".linkedin_profile"   # persistent browser profile (keeps you logged in)
SENT_LOG = HERE / "sent_log.json"          # threads already messaged, so nobody gets it twice
REPORT_CSV = HERE / "report.csv"
MESSAGING_URL = "https://www.linkedin.com/messaging/"

# LinkedIn changes its markup often. If the script stops finding things, fix these first.
SEL = {
    "me_photo": "img.global-nav__me-photo",
    "convo_list": "ul.msg-conversations-container__conversations-list",
    "convo_item": "li.msg-conversation-listitem",
    "convo_link": "a[href*='/messaging/thread/']",
    "convo_names": ".msg-conversation-listitem__participant-names",
    "thread_title": "h2.msg-entity-lockup__entity-title",
    "message_list": ".msg-s-message-list",
    "message": "li.msg-s-message-list__event .msg-s-event-listitem",
    "message_other_class": "msg-s-event-listitem--other",   # a class name, not a selector
    "group_sender_name": ".msg-s-message-group__name",
    "message_body": "li.msg-s-message-list__event .msg-s-event-listitem__body",
    "message_body_in_item": ".msg-s-event-listitem__body",
    "composer": "div.msg-form__contenteditable[contenteditable='true']",
    "send_button": "button.msg-form__send-button",
    # Pop-ups and notices that could carry a LinkedIn warning. Their text is checked for WARNING_WORDS.
    "warning": "[role='alertdialog'], [role='dialog'], [role='alert'], .artdeco-modal, .artdeco-toast-item",
}

# Seeing any of these means LinkedIn wants you to act. The run stops and leaves the page alone.
BLOCK_PATHS = ("/checkpoint", "/login", "/authwall", "/uas/", "/challenge")
WARNING_WORDS = (
    "unusual activity", "suspicious", "restricted", "restriction", "temporarily", "limit",
    "verify", "verification", "security check", "captcha", "too many", "try again later",
    "couldn't send", "could not send", "failed to send", "not sent",
)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def human_pause(lo=1.0, hi=2.5):
    time.sleep(random.uniform(lo, hi))


# Rotating delays between sends. name: (min seconds, max seconds, how often it's picked)
DELAY_TIERS = {
    "short": (20, 45, 5),
    "medium": (45, 120, 4),
    "long": (120, 240, 2),
}
BREAK_EVERY = (4, 8)        # take a longer break after this many sends, re-picked after each break
BREAK_LENGTH = (300, 900)   # 5 to 15 minutes


class Pacer:
    """Picks the wait before each send after the first."""

    def __init__(self, mode, fixed_min, fixed_max):
        self.mode = mode
        self.fixed = (fixed_min, fixed_max)
        self.last_tier = None
        self.until_break = random.randint(*BREAK_EVERY)

    def next_delay(self):
        """Returns (seconds, label). Call once per completed send."""
        if self.mode == "fixed":
            return random.uniform(*self.fixed), "fixed"

        self.until_break -= 1
        if self.until_break <= 0:
            self.until_break = random.randint(*BREAK_EVERY)
            self.last_tier = None
            return random.uniform(*BREAK_LENGTH), "break"

        tiers = [t for t in DELAY_TIERS if t != self.last_tier]   # never the same tier twice in a row
        tier = random.choices(tiers, weights=[DELAY_TIERS[t][2] for t in tiers])[0]
        self.last_tier = tier
        lo, hi, _ = DELAY_TIERS[tier]
        return random.uniform(lo, hi), tier


def wait_between_sends(seconds, label):
    """Sleep before the next send. Returns 'q' if you typed q + Enter to stop, else ''."""
    mins, secs = divmod(round(seconds), 60)
    log(f"Waiting {f'{mins}m ' if mins else ''}{secs}s ({label}) before the next send. q+Enter to stop")
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([sys.stdin], [], [], remaining)
        if not ready:
            break
        line = sys.stdin.readline()
        if not line or line.strip().lower().startswith("q"):   # EOF also stops rather than spinning
            return "q"
        print("  (still waiting; type q + Enter to stop)", flush=True)
    return ""


def thread_key(url):
    """One canonical form per thread URL: absolute, decoded, no query or fragment, one trailing slash."""
    url = urljoin("https://www.linkedin.com", url).split("#")[0].split("?")[0]
    return unquote(url).rstrip("/") + "/"


def load_sent_log():
    if SENT_LOG.exists():
        return {thread_key(k): v for k, v in json.loads(SENT_LOG.read_text()).items()}
    return {}


def save_sent_log(data):
    SENT_LOG.write_text(json.dumps(data, indent=2))


def launch(p, headless=False):
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1400, "height": 900},
    )


# --------------------------------------------------------------------------- login

def cmd_login(_args):
    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login")
        input("Log in to LinkedIn in the browser window, then press Enter here... ")
        page.goto(MESSAGING_URL)
        if "/login" in page.url or "/checkpoint" in page.url:
            log("Still not logged in. Run `login` again.")
        else:
            log(f"Logged in. Session saved to {PROFILE_DIR}")
        ctx.close()


# --------------------------------------------------------------------------- inbox helpers

def my_name(page):
    try:
        return (page.locator(SEL["me_photo"]).first.get_attribute("alt", timeout=5000) or "").strip()
    except PWTimeout:
        return ""


def load_conversations(page, max_threads):
    """Scroll the inbox list until max_threads conversations are loaded, or no more appear."""
    page.wait_for_selector(SEL["convo_item"], timeout=20000)
    last = -1
    while True:
        count = page.locator(SEL["convo_item"]).count()
        if count >= max_threads or count == last:
            return min(count, max_threads)
        last = count
        page.locator(SEL["convo_list"]).evaluate("el => el.scrollTo(0, el.scrollHeight)")
        human_pause(1.5, 2.5)


def load_full_history(page, max_scrolls=15):
    """Scroll a thread to the top so older messages load. Stops once nothing new appears."""
    last = -1
    for _ in range(max_scrolls):
        count = page.locator(SEL["message"]).count()
        if count == last:
            break
        last = count
        page.locator(SEL["message_list"]).first.evaluate("el => el.scrollTo(0, 0)")
        human_pause(1.0, 1.8)


def norm_name(text):
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def names_match(a, b):
    a, b = norm_name(a), norm_name(b)
    return bool(a and b) and (a == b or a in b or b in a)


def open_thread_matches(page, key, name):
    """True if the open chat is the one we meant: same thread URL, and its header shows the same person."""
    if thread_key(page.url) != key:
        return False
    try:
        header = page.locator(SEL["thread_title"]).first.inner_text(timeout=1000)
    except PWTimeout:
        return False
    return names_match(header, name)


class Blocked(Exception):
    """LinkedIn is showing a security check, restriction or warning. Stop and leave the page alone."""


VISIBLE_TEXTS_JS = """(sel) => [...document.querySelectorAll(sel)]
    .filter(el => el.getClientRects().length > 0)
    .map(el => el.innerText.trim())
    .filter(Boolean)"""


def blocked_reason(page):
    """Why LinkedIn wants attention right now, or None if all looks normal."""
    path = urlparse(page.url).path
    if any(path.startswith(p) for p in BLOCK_PATHS):
        return f"LinkedIn sent the browser to {path}"
    for text in page.evaluate(VISIBLE_TEXTS_JS, SEL["warning"]):
        if any(w in text.lower() for w in WARNING_WORDS):
            return "LinkedIn is showing: " + " ".join(text.split())[:200]
    return None


def check_not_blocked(page):
    reason = blocked_reason(page)
    if reason:
        raise Blocked(reason)


def is_credential(part):
    """'PhD', 'MBA', 'M.D.', 'SHRM-CP', 'Jr': letters after a single person's name."""
    t = part.replace(".", "").strip()
    return " " not in t and len(t) <= 10 and (
        sum(c.isupper() for c in t) >= 2 or t.lower() in ("jr", "sr", "ii", "iii", "iv")
    )


def looks_like_group_name(name):
    """True for inbox names listing several people ('Jane Doe, Bob Ray', 'Jane and 2 others'),
    but not for one person with credentials ('Jane Smith, PhD')."""
    if re.search(r"\b\d+\s+others?\b", name, re.IGNORECASE):
        return True
    parts = [p.strip() for p in re.split(r",|&|\band\b", name, flags=re.IGNORECASE) if p.strip()]
    return any(not is_credential(p) for p in parts[1:])


def group_chat_reason(page, me, name):
    """Why the open chat looks like a group, judged by who has written in it. None if it's one-to-one.

    More than one other person writing means a group. So does the one other person's name not
    matching the chat's name, which is how a named group ('Project Team') shows up.
    """
    me_n = norm_name(me)
    others = {norm_name(n) for n in page.locator(SEL["group_sender_name"]).all_inner_texts()}
    others -= {"", me_n, "you"}
    if len(others) > 1:
        return f"{len(others)} other people have written in it"
    if others and not names_match(next(iter(others)), name):
        return "the person writing in it isn't the chat's name, so it's probably a named group"
    return None


def draft_in_box(page):
    """Text already sitting in the message box (an unsent draft), or ''."""
    box = page.locator(SEL["composer"]).first
    try:
        return box.inner_text(timeout=2000).strip()
    except PWTimeout:
        return ""


class DraftInBox(Exception):
    """The message box already had text in it, so nothing was typed."""


def wait_for_thread(page, key, name, timeout=15):
    """Wait until the chat we clicked is really open, not the previous one still on screen."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_not_blocked(page)
        if open_thread_matches(page, key, name) and page.locator(SEL["message"]).count() > 0:
            human_pause(0.8, 1.5)   # let the message list finish swapping over
            if open_thread_matches(page, key, name):
                return True
        time.sleep(0.3)
    return False


COUNT_MESSAGES_JS = """([itemSel, otherClass, bodySel]) => {
    let mine = 0, theirs = 0;
    for (const el of document.querySelectorAll(itemSel)) {
        const body = el.querySelector(bodySel);
        if (!body || !body.innerText.trim()) continue;   // system notices, deleted messages
        if (el.classList.contains(otherClass)) theirs++; else mine++;
    }
    return [mine, theirs];
}"""


def conversation_stats(page, me):
    """Work out who sent what in the open thread. Returns (mine, theirs, verdict, reason).

    verdict is "conversed", "not conversed" or "unsure". Two independent signals must agree:
      1. LinkedIn's CSS class on the other person's messages (the rest, with text, are mine).
      2. The sender name shown on each group of messages, compared with my name.
    Anything they disagree on, or can't be read, is "unsure" and never gets a message.
    """
    mine, theirs = page.evaluate(
        COUNT_MESSAGES_JS, [SEL["message"], SEL["message_other_class"], SEL["message_body_in_item"]]
    )
    me_n = norm_name(me)
    names = [norm_name(n) for n in page.locator(SEL["group_sender_name"]).all_inner_texts()]
    names = [n for n in names if n]
    if not me_n:
        return mine, theirs, "unsure", "couldn't read your own name from LinkedIn"
    if not names:
        return mine, theirs, "unsure", "couldn't read sender names in this chat"

    i_named = any(n in (me_n, "you") for n in names)
    they_named = any(n not in (me_n, "you") for n in names)
    by_class = mine > 0 and theirs > 0

    if not i_named:
        if mine == 0:
            return mine, theirs, "not conversed", "you never sent a message"
        return mine, theirs, "unsure", f"messages look like yours but none show your name ({me})"
    if by_class and they_named:
        return mine, theirs, "conversed", ""
    if not by_class and not they_named:
        return mine, theirs, "not conversed", "they never sent a message"
    return mine, theirs, "unsure", "message styling and sender names disagree"


MAX_UNSURE_IN_A_ROW = 3   # stop the run if this many chats in a row can't be read reliably
PROMO_MATCH = 0.8   # how similar a message in the chat must be to the promo to count as already sent


def normalize(text):
    return " ".join(text.lower().split())


def promo_already_in_chat(page, promo):
    """True if any message in the open thread closely matches the promo text.

    Catches promos the sent log doesn't know about: a fresh start, a deleted log, another
    machine, or a promo you sent by hand. Needs the thread history loaded first.
    """
    target = normalize(promo)
    for body in page.locator(SEL["message_body"]).all_inner_texts():
        text = normalize(body)
        if not text:
            continue
        if target in text or difflib.SequenceMatcher(None, text, target).ratio() >= PROMO_MATCH:
            return True
    return False


def type_message(page, text):
    box = page.locator(SEL["composer"]).first
    if draft_in_box(page):   # never splice the promo into someone's draft
        raise DraftInBox("there's an unsent draft in the message box")
    box.click()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Shift+Enter")   # a plain Enter would send early
    human_pause(0.8, 1.5)
    return box


def clear_composer(page):
    box = page.locator(SEL["composer"]).first
    box.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.press("Backspace")
    human_pause(0.5, 1.0)
    if box.inner_text().strip():
        raise RuntimeError("Could not clear the message box; delete the draft by hand")


def ask_user(name, action, seconds):
    """Wait while a typed message is on screen. Returns '' to go ahead, 's' to skip, 'q' to quit.

    With seconds=None, waits for Enter. Otherwise goes ahead by itself after that many seconds.
    Uses typed commands rather than Ctrl+C, which would also kill the browser and leave a draft behind.
    """
    if seconds is None:
        prompt = f"  {name}: message typed. Enter to {action}, s+Enter to skip, q+Enter to stop: "
        return input(prompt).strip().lower()[:1]
    print(f"  {name}: message typed. Will {action} in {seconds:g}s (s+Enter to skip, q+Enter to stop)", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], seconds)
    return sys.stdin.readline().strip().lower()[:1] if ready else ""


def safe_clear(page):
    """Clear the message box, reporting instead of raising. Returns True if it's empty."""
    try:
        clear_composer(page)
        return True
    except Exception as e:
        log(f"  Could not clear the message box ({e}); delete the draft in that chat by hand")
        return False


class SendNotClicked(Exception):
    """Send was never clicked, so nothing can have gone out."""


def click_send(page, box):
    """Click Send. Returns True if the box emptied afterwards, the usual sign it went through.

    Raises SendNotClicked if the button couldn't be clicked at all.
    """
    button = page.locator(SEL["send_button"]).first
    try:
        button.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        raise SendNotClicked("Send button not found")
    if button.is_disabled():
        raise SendNotClicked("Send button is disabled; the message wasn't typed into the box")
    button.click()
    deadline = time.monotonic() + 10   # slow connections can take a while to clear the box
    while time.monotonic() < deadline:
        if not box.inner_text().strip():
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- run

def cmd_run(args):
    template = Path(args.message_file).read_text().strip()
    if not template:
        sys.exit(f"{args.message_file} is empty")
    if args.preview and args.send:
        sys.exit("--preview never sends; drop --send to use it")
    if args.preview and args.headless:
        sys.exit("--preview needs a visible browser; drop --headless")
    if args.pause is not None and not (args.preview or args.send):
        sys.exit("--pause only applies with --preview or --send")
    if args.pause is not None and args.pause < 0:
        sys.exit("--pause must be 0 or more seconds")
    if not 0 <= args.delay_min <= args.delay_max:
        sys.exit("--delay-min must be 0 or more and no bigger than --delay-max")
    pacer = Pacer(args.delay_mode, args.delay_min, args.delay_max)

    sent_log = load_sent_log()
    rows = []
    sends = 0        # messages actually sent this run
    qualified = 0    # sent, or would be sent in a dry run; this is what --max-messages caps

    try:
        with sync_playwright() as p:
            ctx = launch(p, headless=args.headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(MESSAGING_URL)
            reason = blocked_reason(page)
            if reason:
                ctx.close()
                if "/login" in reason or "/authwall" in reason:
                    sys.exit("Not logged in. Run `python linkedin_dm_promo.py login` first.")
                sys.exit(f"LinkedIn needs attention before this can run: {reason}\n"
                         "Run `python linkedin_dm_promo.py login` and deal with it by hand in that browser.")

            me = my_name(page)
            log(f"Logged in as: {me or '(name not found; every chat will come out unsure)'}")
            if args.send:
                mode = "SEND"
            elif args.preview:
                mode = "PREVIEW (types the promo into each DM, never sends)"
            else:
                mode = "DRY RUN (nothing is sent)"
            log("Mode: " + mode)

            current = None
            try:
                n = load_conversations(page, args.max_threads)
                log(f"Checking {n} conversations")

                unsure_in_a_row = 0

                def unsure(row, reason):
                    """Skip a chat we can't read reliably. Returns True if the run should stop."""
                    nonlocal unsure_in_a_row
                    unsure_in_a_row += 1
                    row["decision"] = f"skip: unsure ({reason})"
                    rows.append(row)
                    log(f"{row['name']}: {row['decision']}")
                    if unsure_in_a_row >= MAX_UNSURE_IN_A_ROW:
                        log(f"Stopping: {unsure_in_a_row} chats in a row couldn't be read reliably. "
                            "LinkedIn's page has probably changed; check SEL at the top of the script.")
                        return True
                    return False

                def stop_with_error(row, msg):
                    row["decision"] = f"error: {msg}"
                    rows.append(row)
                    log(f"{row['name']}: {row['decision']}")
                    log("Stopping the run so nothing else goes out while something is wrong.")

                for i in range(n):
                    if qualified >= args.max_messages:
                        log(f"Reached --max-messages={args.max_messages}, stopping")
                        break
                    check_not_blocked(page)

                    item = page.locator(SEL["convo_item"]).nth(i)
                    name = item.locator(SEL["convo_names"]).first.inner_text().strip()
                    row = {"name": name, "thread": "", "mine": "", "theirs": "", "decision": ""}
                    current = row

                    if looks_like_group_name(name):
                        row["decision"] = "skip: group chat (several names)"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        continue
                    if "Sponsored" in item.inner_text():
                        row["decision"] = "skip: sponsored"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        continue

                    # Read the thread's link from the inbox before clicking, so we know which chat must open.
                    link = item.locator(SEL["convo_link"]).first
                    href = link.get_attribute("href") if link.count() else None
                    if not href:
                        if unsure(row, "couldn't read this chat's link in the inbox"):
                            break
                        continue
                    key = thread_key(href)
                    row["thread"] = key

                    if key in sent_log:   # known already, no need to open it
                        entry = sent_log[key]
                        status = entry.get("status", "sent")
                        row["decision"] = f"skip: already in sent log ({status}, {entry['sent_at']})"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        continue

                    link.click()
                    if not wait_for_thread(page, key, name):
                        if unsure(row, "the chat that opened isn't the one clicked, or it didn't load"):
                            break
                        continue

                    load_full_history(page)
                    check_not_blocked(page)
                    if not open_thread_matches(page, key, name):
                        if unsure(row, "the open chat changed while loading history"):
                            break
                        continue
                    mine, theirs, verdict, reason = conversation_stats(page, me)
                    row["mine"], row["theirs"] = mine, theirs

                    first_name = name.split()[0] if name else "there"
                    text = template.replace("{first_name}", first_name).replace("{name}", name)

                    if promo_already_in_chat(page, text):
                        unsure_in_a_row = 0
                        row["decision"] = "skip: promo already in chat"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        if args.send:   # remember it, so history lists them and later runs skip them straight away
                            sent_log[key] = {
                                "name": name,
                                "sent_at": datetime.now().isoformat(timespec="seconds"),
                                "message": text,
                                "source": "found in chat",
                            }
                            save_sent_log(sent_log)
                        continue

                    if verdict == "unsure":
                        if unsure(row, reason):
                            break
                        continue
                    unsure_in_a_row = 0
                    # Only once we know which sender is you, or the group check would count you as someone else.
                    group = group_chat_reason(page, me, name)
                    if group:
                        row["decision"] = f"skip: group chat ({group})"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        continue
                    if verdict == "not conversed":
                        row["decision"] = f"skip: never conversed ({reason})"
                        rows.append(row)
                        log(f"{name}: {row['decision']} (me={mine}, them={theirs})")
                        continue

                    if draft_in_box(page):
                        row["decision"] = "skip: unsent draft already in the message box (left untouched)"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        continue

                    if not args.send:
                        qualified += 1
                        row["decision"] = "would send"
                        log(f"{name}: would send (me={mine}, them={theirs})")
                        if args.preview:
                            if not open_thread_matches(page, key, name):
                                stop_with_error(row, "the open chat changed before typing the preview")
                                break
                            check_not_blocked(page)
                            try:
                                type_message(page, text)
                                answer = ask_user(name, "clear it and continue", args.pause)
                            except DraftInBox:
                                qualified -= 1
                                row["decision"] = "skip: unsent draft already in the message box (left untouched)"
                                rows.append(row)
                                log(f"{name}: {row['decision']}")
                                continue
                            except Exception as e:
                                row["decision"] = f"would send (preview error: {e})"
                                log(f"{name}: preview error: {e}")
                                answer = ""
                            safe_clear(page)
                            if answer == "q":
                                rows.append(row)
                                log("Stopped by you")
                                break
                        rows.append(row)
                        continue

                    if sends > 0:
                        seconds, label = pacer.next_delay()
                        if wait_between_sends(seconds, label) == "q":
                            row["decision"] = "skip: stopped by you"
                            rows.append(row)
                            log("Stopped by you")
                            break

                    # The chat must still be the right one: you might have clicked around during the wait.
                    check_not_blocked(page)
                    if not open_thread_matches(page, key, name):
                        stop_with_error(row, "the open chat changed before typing")
                        break
                    try:
                        box = type_message(page, text)
                        answer = ask_user(name, "send it", args.pause) if args.pause is not None else ""
                    except DraftInBox:   # someone typed in this chat during the wait; leave it alone
                        row["decision"] = "skip: unsent draft already in the message box (left untouched)"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        continue
                    except Exception as e:
                        safe_clear(page)
                        stop_with_error(row, f"couldn't type the message ({e})")
                        break
                    if answer in ("s", "q"):
                        safe_clear(page)
                        row["decision"] = "skip: skipped by you"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        if answer == "q":
                            log("Stopped by you")
                            break
                        continue
                    check_not_blocked(page)
                    if not open_thread_matches(page, key, name):
                        safe_clear(page)
                        stop_with_error(row, "the open chat changed before sending")
                        break

                    # Log before clicking, so a crash right after the click can't lead to a second send.
                    sent_log[key] = {
                        "name": name,
                        "sent_at": datetime.now().isoformat(timespec="seconds"),
                        "message": text,
                        "status": "pending",
                    }
                    save_sent_log(sent_log)
                    try:
                        confirmed = click_send(page, box)
                    except SendNotClicked as e:
                        del sent_log[key]
                        save_sent_log(sent_log)
                        safe_clear(page)
                        stop_with_error(row, str(e))
                        break
                    except Exception as e:   # the click may or may not have happened
                        log(f"{name}: error around the Send click: {e}")
                        confirmed = False

                    # Every Send click counts toward the cap and the waits, confirmed or not.
                    sends += 1
                    qualified += 1
                    warning = blocked_reason(page)   # e.g. a "couldn't send" or limit notice after the click
                    sent_log[key]["status"] = "sent" if confirmed and not warning else "unconfirmed"
                    save_sent_log(sent_log)
                    if warning:
                        row["decision"] = "unconfirmed: LinkedIn showed a warning right after Send; check this chat by hand"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        raise Blocked(warning)
                    if not confirmed:
                        row["decision"] = "unconfirmed: Send was clicked but the box didn't empty; check this chat by hand"
                        rows.append(row)
                        log(f"{name}: {row['decision']}")
                        log("Stopping the run so nothing else goes out while something is wrong.")
                        break
                    row["decision"] = "sent"
                    rows.append(row)
                    log(f"{name}: SENT ({sends}/{args.max_messages})")
            except Blocked as e:
                if current is not None and current not in rows:
                    current["decision"] = "not finished: run stopped"
                    rows.append(current)
                rows.append({"name": "(run stopped)", "thread": "", "mine": "", "theirs": "",
                             "decision": f"stopped: {e}"})
                log(f"STOPPED: {e}")
                log("The browser has been left alone. Nothing more was typed or sent.")
                log("Deal with it by hand: run `python linkedin_dm_promo.py login`, sort it out in that "
                    "browser, and wait a day or two before sending again.")
            ctx.close()
    finally:   # written even if the run stops early or crashes
        if rows:
            with REPORT_CSV.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["name", "thread", "mine", "theirs", "decision"])
                w.writeheader()
                w.writerows(rows)
            log(f"Done. {qualified} qualified, {sends} sent. Report: {REPORT_CSV}")


# --------------------------------------------------------------------------- history

def cmd_history(args):
    sent_log = load_sent_log()
    if not sent_log:
        print("Nobody has been messaged yet.")
        return
    entries = sorted(sent_log.items(), key=lambda kv: kv[1]["sent_at"])

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "sent_at", "source", "status", "thread", "message"])
            for url, e in entries:
                w.writerow([e["name"], e["sent_at"], e.get("source", "sent by script"),
                            e.get("status", "sent"), url, e.get("message", "")])
        print(f"Exported {len(entries)} people to {args.csv}")
        return

    width = max(len(e["name"]) for _, e in entries)
    for url, e in entries:
        note = ""
        if e.get("source") == "found in chat":
            note = "  (found in chat; date is when it was found)"
        elif e.get("status") in ("pending", "unconfirmed"):
            note = f"  ({e['status']}: may not have gone through, check this chat by hand)"
        print(f"{e['sent_at']}  {e['name']:<{width}}  {url}{note}")
    print(f"\n{len(entries)} people messaged in total")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Open a browser to log in by hand; the session is saved")

    r = sub.add_parser("run", help="Scan the inbox and message people you have conversed with")
    r.add_argument("--send", action="store_true", help="Actually send (default is a dry run)")
    r.add_argument("--preview", action="store_true",
                   help="Dry run that types the promo into each qualifying DM, waits for Enter, then clears it")
    r.add_argument("--pause", type=float, metavar="SECONDS",
                   help="Show each typed message this long, then move on (preview) or send it (--send). "
                        "Type s+Enter to skip a person, q+Enter to stop")
    r.add_argument("--message-file", default=str(HERE / "message.txt"),
                   help="Promo text; {first_name} and {name} are filled in")
    r.add_argument("--max-threads", type=int, default=40, help="How many inbox conversations to check")
    r.add_argument("--max-messages", "--max-sends", dest="max_messages", type=int, default=10,
                   help="Stop after this many messages (in a dry run: this many would-sends)")
    r.add_argument("--delay-mode", choices=["rotating", "fixed"], default="rotating",
                   help="rotating: short/medium/long waits plus a break every 4-8 sends (default). "
                        "fixed: a random wait between --delay-min and --delay-max")
    r.add_argument("--delay-min", type=float, default=30, help="Minimum seconds between sends (fixed mode)")
    r.add_argument("--delay-max", type=float, default=90, help="Maximum seconds between sends (fixed mode)")
    r.add_argument("--headless", action="store_true", help="Hide the browser window")

    h = sub.add_parser("history", help="List everyone the script has messaged")
    h.add_argument("--csv", metavar="FILE", help="Export the list to a CSV file instead of printing it")

    args = ap.parse_args()
    {"login": cmd_login, "run": cmd_run, "history": cmd_history}[args.cmd](args)


if __name__ == "__main__":
    main()
