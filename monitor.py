#!/usr/bin/env python3
"""Monitor TC de Uithof's training cancellation page for levels 7/8.

Designed for GitHub-hosted Ubuntu runners. It uses the Chrome already installed on
those runners to render the JavaScript-driven page, extracts matching available
training cards, sends a push via ntfy, and stores a small deduplication state.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

PAGE_URL = os.getenv("PAGE_URL", "https://tcdeuithof.nl/cancel/index.html")
LEVELS = tuple(
    sorted(
        {int(x.strip()) for x in os.getenv("TARGET_LEVELS", "7,8").split(",") if x.strip()}
    )
)
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

BLOCK_TAGS = {
    "article", "aside", "button", "div", "form", "li", "main", "section",
    "table", "tbody", "td", "tr", "ul", "ol", "p", "span", "a"
}

WEEKDAY_OR_MONTH = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|ma|di|wo|do|vr|za|zo|maandag|dinsdag|woensdag|donderdag|"
    r"vrijdag|zaterdag|zondag|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"mei|okt)\b",
    re.I,
)
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])[:.]\d{2}\b")
DATE_RE = re.compile(r"\b\d{1,2}[-/ ](?:\d{1,2}|[A-Za-z]{3,10})(?:[-/ ]\d{2,4})?\b")


def normalise(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def level_pattern(level: int) -> re.Pattern[str]:
    # Handles "Level 7", "niveau: 7", "7 level", etc.
    return re.compile(
        rf"(?:\b(?:level|niveau|rating|speelsterkte)\b\s*[:#\-]?\s*{level}\b|"
        rf"\b{level}\s*(?:level|niveau|rating|speelsterkte)\b)",
        re.I,
    )


LEVEL_PATTERNS = {level: level_pattern(level) for level in LEVELS}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)

    def text(self) -> str:
        out: list[str] = []
        for child in self.children:
            out.append(child if isinstance(child, str) else child.text())
        return normalise(" ".join(out))


class DOMParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # Chrome's dumped DOM should be balanced; be defensive anyway.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data.strip():
            self.stack[-1].children.append(data)


def find_chrome() -> str:
    candidates = [
        os.getenv("CHROME_BIN", ""),
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return shutil.which(candidate) or candidate
    raise RuntimeError(
        "Chrome/Chromium not found. GitHub's ubuntu-24.04 runner includes Google Chrome."
    )


def render_page(url: str) -> str:
    chrome = find_chrome()
    cmd = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--blink-settings=imagesEnabled=false",
        "--virtual-time-budget=10000",
        "--dump-dom",
        url,
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Chrome failed with exit code {result.returncode}: {result.stderr[-1500:]}"
        )
    if not result.stdout.strip():
        raise RuntimeError("Chrome returned an empty document")
    return result.stdout


def contains_target_level(text: str) -> bool:
    return any(pattern.search(text) for pattern in LEVEL_PATTERNS.values())


def is_plausible_spot(text: str) -> bool:
    t = normalise(text)
    lower = t.lower()
    if not contains_target_level(t):
        return False
    if "cancel your spot" in lower or "cancel spot" in lower:
        return False
    if "your name" in lower or "your email" in lower:
        return False
    if len(t) < 18 or len(t) > 650:
        return False

    # A real availability card should contain some context beyond just "Level 7".
    has_when = bool(TIME_RE.search(t) or DATE_RE.search(t) or WEEKDAY_OR_MONTH.search(t))
    context_words = sum(
        word in lower
        for word in (
            "book", "spot", "training", "trainer", "lesson", "les", "vrij", "free",
            "available", "plek", "baan", "court",
        )
    )
    return has_when or (len(t) >= 30 and context_words >= 2)


def walk(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from walk(child)


def candidate_nodes(node: Node) -> list[Node]:
    """Return the smallest DOM blocks that still look like complete spot cards."""
    out: list[Node] = []

    def recurse(cur: Node) -> bool:
        child_qualifies = False
        for child in cur.children:
            if isinstance(child, Node) and recurse(child):
                child_qualifies = True
        qualifies = cur.tag in BLOCK_TAGS and is_plausible_spot(cur.text())
        if qualifies and not child_qualifies:
            out.append(cur)
        return qualifies or child_qualifies

    recurse(node)
    return out


def rendered_training_text(dom: str) -> str:
    parser = DOMParser()
    parser.feed(dom)
    whole_text = parser.root.text()
    prefix = whole_text.split("Cancel Your Spot", 1)[0]
    return normalise(prefix)


def extract_spots(dom: str) -> list[str]:
    parser = DOMParser()
    parser.feed(dom)

    whole_text = parser.root.text()
    if "Training Spots" not in whole_text:
        raise RuntimeError("Expected 'Training Spots' heading was not found; the website may have changed.")

    cards = candidate_nodes(parser.root)
    texts = [normalise(card.text()) for card in cards]

    # Deduplicate nested/similar DOM echoes while preserving useful detail.
    unique: list[str] = []
    for text in sorted(set(texts), key=len, reverse=True):
        if any(text == seen for seen in unique):
            continue
        unique.append(text)

    # If the page has not finished loading, make that visible in logs rather than silently alerting.
    training_prefix = whole_text.split("Cancel Your Spot", 1)[0]
    if not unique and "Loading..." in training_prefix:
        raise RuntimeError(
            "The page still showed 'Loading...' after rendering. The site may be slow or blocking the runner."
        )

    return sorted(unique)


def spot_id(text: str) -> str:
    return hashlib.sha256(normalise(text).lower().encode("utf-8")).hexdigest()[:20]


def load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        spots = raw.get("spots", {})
        if isinstance(spots, dict):
            return {str(k): str(v) for k, v in spots.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_state(spots: dict[str, str]) -> None:
    payload = {
        "spots": spots,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def notify_ntfy(message: str, *, title: str = "TC de Uithof: training spot available") -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is not set. Add it as a GitHub Actions repository secret.")
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "5",
            "Tags": "tennis_ball",
            "Click": PAGE_URL,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status >= 300:
                raise RuntimeError(f"ntfy returned HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not send ntfy notification: {exc}") from exc


def main() -> int:
    if "--test-notification" in sys.argv:
        notify_ntfy(
            "Test successful. You will receive another notification when a level "
            f"{', '.join(map(str, LEVELS))} training spot appears.\n\n{PAGE_URL}",
            title="TC de Uithof monitor: test successful",
        )
        print("Test notification sent successfully.")
        return 0

    print(f"Checking {PAGE_URL} for levels {', '.join(map(str, LEVELS))}...")
    dom = render_page(PAGE_URL)
    current_texts = extract_spots(dom)
    current = {spot_id(text): text for text in current_texts}
    previous = load_state()

    print(f"Matching spots now: {len(current)}")
    for text in current.values():
        print(f"  - {text}")
    if not current:
        print("Rendered Training Spots section (for diagnostics):")
        print("  " + rendered_training_text(dom)[:1500])

    new_ids = [sid for sid in current if sid not in previous]
    if new_ids:
        lines = [
            f"New level {'/'.join(map(str, LEVELS))} training spot{'s' if len(new_ids) != 1 else ''}:",
            "",
        ]
        lines.extend(f"- {current[sid]}" for sid in new_ids)
        lines.extend(["", f"Open immediately: {PAGE_URL}"])
        notify_ntfy("\n".join(lines))
        print(f"Notification sent for {len(new_ids)} new spot(s).")
    else:
        print("No newly available matching spots.")

    save_state(current)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
