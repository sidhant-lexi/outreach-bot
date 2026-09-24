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
import select
import sys
import time
from datetime import datetime
from pathlib import Path

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
    "convo_link": ".msg-conversation-listitem__link",
    "convo_names": ".msg-conversation-listitem__participant-names",
    "message_list": ".msg-s-message-list",
    "message": "li.msg-s-message-list__event .msg-s-event-listitem",
    "message_other": "li.msg-s-message-list__event .msg-s-event-listitem--other",
    "group_sender_name": ".msg-s-message-group__name",
    "message_body": "li.msg-s-message-list__event .msg-s-event-listitem__body",
    "composer": "div.msg-form__contenteditable[contenteditable='true']",
    "send_button": "button.msg-form__send-button",
}


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


def load_sent_log():
    if SENT_LOG.exists():
        return json.loads(SENT_LOG.read_text())
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


def conversation_stats(page, me):
    """Count messages sent by me and by the other person in the open thread."""
    total = page.locator(SEL["message"]).count()
    theirs = page.locator(SEL["message_other"]).count()
    mine = total - theirs

    # Fallback: sender names on message groups, in case LinkedIn drops the --other class.
    if me and (mine == 0 or theirs == 0):
        names = {n.strip() for n in page.locator(SEL["group_sender_name"]).all_inner_texts() if n.strip()}
        if me in names:
            mine = max(mine, 1)
        if names - {me}:
            theirs = max(theirs, 1)
    return mine, theirs


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


def click_send(page, box):
    button = page.locator(SEL["send_button"]).first
    button.wait_for(state="visible", timeout=5000)
    if button.is_disabled():
        raise RuntimeError("Send button is disabled; the message was not typed into the box")
    button.click()
    human_pause(1.5, 2.5)
    if box.inner_text().strip():
        raise RuntimeError("Composer still has text after clicking send; the message may not have gone")


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

    with sync_playwright() as p:
        ctx = launch(p, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(MESSAGING_URL)
        if "/login" in page.url or "/checkpoint" in page.url or "/authwall" in page.url:
            ctx.close()
            sys.exit("Not logged in. Run `python linkedin_dm_promo.py login` first.")

        me = my_name(page)
        log(f"Logged in as: {me or '(name not found; relying on message classes only)'}")
        if args.send:
            mode = "SEND"
        elif args.preview:
            mode = "PREVIEW (types the promo into each DM, never sends)"
        else:
            mode = "DRY RUN (nothing is sent)"
        log("Mode: " + mode)

        n = load_conversations(page, args.max_threads)
        log(f"Checking {n} conversations")

        for i in range(n):
            if qualified >= args.max_messages:
                log(f"Reached --max-messages={args.max_messages}, stopping")
                break

            item = page.locator(SEL["convo_item"]).nth(i)
            name = item.locator(SEL["convo_names"]).first.inner_text().strip()
            row = {"name": name, "thread": "", "mine": "", "theirs": "", "decision": ""}

            if "," in name or " and " in name:
                row["decision"] = "skip: group chat"
                rows.append(row)
                log(f"{name}: {row['decision']}")
                continue
            if "Sponsored" in item.inner_text():
                row["decision"] = "skip: sponsored"
                rows.append(row)
                log(f"{name}: {row['decision']}")
                continue

            item.locator(SEL["convo_link"]).first.click()
            try:
                page.wait_for_url("**/messaging/thread/**", timeout=10000)
                page.wait_for_selector(SEL["message"], timeout=10000)
            except PWTimeout:
                row["decision"] = "skip: thread did not load"
                rows.append(row)
                log(f"{name}: {row['decision']}")
                continue
            human_pause()

            thread_url = page.url.split("?")[0]
            row["thread"] = thread_url

            if thread_url in sent_log:
                row["decision"] = f"skip: already sent on {sent_log[thread_url]['sent_at']}"
                rows.append(row)
                log(f"{name}: {row['decision']}")
                continue

            load_full_history(page)
            mine, theirs = conversation_stats(page, me)
            row["mine"], row["theirs"] = mine, theirs

            first_name = name.split()[0] if name else "there"
            text = template.replace("{first_name}", first_name).replace("{name}", name)

            if promo_already_in_chat(page, text):
                row["decision"] = "skip: promo already in chat"
                rows.append(row)
                log(f"{name}: {row['decision']}")
                if args.send:   # remember it, so history lists them and later runs skip them straight away
                    sent_log[thread_url] = {
                        "name": name,
                        "sent_at": datetime.now().isoformat(timespec="seconds"),
                        "message": text,
                        "source": "found in chat",
                    }
                    save_sent_log(sent_log)
                continue

            if mine == 0 or theirs == 0:
                row["decision"] = "skip: never conversed"
                rows.append(row)
                log(f"{name}: {row['decision']} (me={mine}, them={theirs})")
                continue

            if not args.send:
                qualified += 1
                row["decision"] = "would send"
                log(f"{name}: would send (me={mine}, them={theirs})")
                if args.preview:
                    try:
                        type_message(page, text)
                        answer = ask_user(name, "clear it and continue", args.pause)
                        clear_composer(page)
                    except Exception as e:
                        row["decision"] = f"would send (preview error: {e})"
                        log(f"{name}: preview error: {e}")
                        answer = ""
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

            try:
                box = type_message(page, text)
                answer = ask_user(name, "send it", args.pause) if args.pause is not None else ""
                if answer in ("s", "q"):
                    clear_composer(page)
                    row["decision"] = "skip: skipped by you"
                    rows.append(row)
                    log(f"{name}: {row['decision']}")
                    if answer == "q":
                        log("Stopped by you")
                        break
                    continue
                click_send(page, box)
            except Exception as e:
                row["decision"] = f"error: {e}"
                rows.append(row)
                log(f"{name}: {row['decision']}")
                continue

            sends += 1
            qualified += 1
            sent_log[thread_url] = {
                "name": name,
                "sent_at": datetime.now().isoformat(timespec="seconds"),
                "message": text,
            }
            save_sent_log(sent_log)
            row["decision"] = "sent"
            rows.append(row)
            log(f"{name}: SENT ({sends}/{args.max_messages})")

        ctx.close()

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
            w.writerow(["name", "sent_at", "source", "thread", "message"])
            for url, e in entries:
                w.writerow([e["name"], e["sent_at"], e.get("source", "sent by script"), url, e.get("message", "")])
        print(f"Exported {len(entries)} people to {args.csv}")
        return

    width = max(len(e["name"]) for _, e in entries)
    for url, e in entries:
        note = "  (found in chat; date is when it was found)" if e.get("source") == "found in chat" else ""
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
