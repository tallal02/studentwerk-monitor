"""Read only the requested website in a normal, local Brave session."""

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlparse
from uuid import uuid4

OFFERS_URL = "https://www.stwdo.de/wohnen/aktuelle-wohnangebote"


class BrowserError(Exception):
    pass


class VerificationRequired(BrowserError):
    pass


class AccessDenied(BrowserError):
    pass


@dataclass(frozen=True)
class Offer:
    id: str
    url: str
    title: str
    details: str

    @property
    def is_dortmund(self) -> bool:
        return bool(re.match(r"^Dortmund(?:\s*[,–—-]|\s*$)", self.title, re.IGNORECASE))

    def to_dict(self) -> dict:
        return asdict(self)


SNAPSHOT_JS = r"""
JSON.stringify((() => {
    const main = document.querySelector('main');
    const text = main?.innerText || '';
    return {
        url: location.href,
        readyState: document.readyState,
        refreshMarker: window.__studentwerkMonitorRefresh || null,
        responseStatus: performance.getEntriesByType('navigation').at(-1)?.responseStatus || 0,
        verification: !!document.getElementById('housing-offers-access-form'),
        text: main ? text : (document.body?.innerText || '').slice(0, 1000),
        offers: main ? Array.from(main.querySelectorAll('a[href]'))
            .filter(a => {
                const u = new URL(a.href);
                return u.origin === location.origin && /^\/freie-zimmer\/\d+\/?$/.test(u.pathname);
            })
            .map(a => ({url: a.href, text: a.innerText.trim()})) : []
    };
})())
"""

def _valid_location(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc == "www.stwdo.de" and parsed.path.rstrip("/") == "/wohnen/aktuelle-wohnangebote"


def parse_snapshot(snapshot: dict) -> list[Offer]:
    if not _valid_location(snapshot.get("url", "")):
        raise BrowserError("The housing tab is not on the expected website.")
    if snapshot.get("responseStatus") in (401, 403, 429):
        raise AccessDenied(f"The website refused access (HTTP {snapshot['responseStatus']}). Website checks are paused; allow access to recover before a manual visit.")
    if snapshot.get("verification"):
        raise VerificationRequired("The website needs manual spam verification. Website checks are paused until manual recovery.")
    if snapshot.get("responseStatus", 0) >= 500:
        raise BrowserError("The housing website returned a server error; no offers were processed.")
    if snapshot.get("readyState") != "complete":
        raise BrowserError("The housing page is still loading.")
    text = snapshot.get("text", "")
    if not isinstance(text, str) or not re.search(r"Freie Zimmer in unseren Wohnanlagen", text, re.I):
        raise BrowserError("The listings page has an unexpected structure; no listing state was changed.")
    raw_offers = snapshot.get("offers")
    if not isinstance(raw_offers, list):
        raise BrowserError("The page did not return a valid offers list.")
    if not raw_offers and "Mehr Informationen" in text:
        raise BrowserError("Offer content exists but its links have changed; the page needs inspection.")
    offers = {}
    for item in raw_offers:
        if not isinstance(item, dict):
            raise BrowserError("An offer has an unexpected structure.")
        parsed = urlparse(item.get("url", ""))
        match = re.fullmatch(r"/freie-zimmer/(\d+)/?", parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "www.stwdo.de" or not match:
            raise BrowserError("An offer has an unexpected link.")
        lines = [line.strip() for line in item.get("text", "").splitlines() if line.strip()]
        if not lines or not re.match(r"^[\wÄÖÜäöüß -]+,", lines[0]):
            raise BrowserError("An offer is missing its city or title.")
        details = "\n".join(line for line in lines[1:] if line != "Mehr Informationen")
        offer_id = match.group(1)
        offers[offer_id] = Offer(offer_id, f"https://www.stwdo.de/freie-zimmer/{offer_id}", lines[0], details)
    return list(offers.values())


class BraveBrowser:
    def __init__(self, timeout: float = 40):
        self.timeout = timeout

    def execute(self, javascript: str, *, navigate: bool = False) -> str:
        # subprocess input, never a shell command: page content cannot become shell code.
        command = "return execute housingTab javascript " + json.dumps(javascript, ensure_ascii=False)
        if navigate:
            command = "execute housingTab javascript " + json.dumps(javascript, ensure_ascii=False) + '\n    set URL of housingTab to "' + OFFERS_URL + '"\n    return "navigation-requested"'
        missing_tab = 'return "HOUSING_TAB_NOT_OPEN"'
        if navigate:
            missing_tab = '''if (count of windows) is 0 then make new window
        make new tab at end of tabs of front window with properties {URL:"''' + OFFERS_URL + '''"}
        return "navigation-created"'''
        script = '''
tell application "Brave Browser"
    set housingTab to missing value
    repeat with browserWindow in windows
        repeat with browserTab in tabs of browserWindow
            set tabAddress to URL of browserTab
            if tabAddress is "''' + OFFERS_URL + '''" or tabAddress is "''' + OFFERS_URL + '''/" then
                set housingTab to browserTab
                exit repeat
            end if
        end repeat
        if housingTab is not missing value then exit repeat
    end repeat
    if housingTab is missing value then
        ''' + missing_tab + '''
    end if
    ''' + command + '''
end tell
'''
        try:
            result = subprocess.run(["/usr/bin/osascript", "-"], input=script, text=True, capture_output=True, timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise BrowserError("Brave did not respond; check for a browser or macOS permission dialog.") from exc
        if result.returncode:
            if "JavaScript through AppleScript is turned off" in result.stderr:
                raise BrowserError("Enable Brave > View > Developer > Allow JavaScript from Apple Events.")
            if "Not authorized" in result.stderr or "not allowed" in result.stderr or "(-1743)" in result.stderr:
                raise BrowserError("macOS blocked browser automation. Allow it in System Settings > Privacy & Security > Automation.")
            raise BrowserError("Could not read Brave; check that it can open and that browser automation is allowed.")
        output = result.stdout.strip()
        if output == "HOUSING_TAB_NOT_OPEN":
            raise BrowserError("The housing tab was closed during the check; it was not reopened.")
        return output

    def snapshot(self) -> dict:
        try:
            value = json.loads(self.execute(SNAPSHOT_JS))
        except (ValueError, TypeError) as exc:
            raise BrowserError("Brave did not return a readable page snapshot.") from exc
        if not isinstance(value, dict):
            raise BrowserError("Brave returned an invalid page snapshot.")
        return value

    def fetch(self) -> list[Offer]:
        marker = uuid4().hex
        # Set the URL through the browser's navigation API. Reloading a page
        # produced by the verification POST can replay an already-used form.
        self.execute("window.__studentwerkMonitorRefresh = " + json.dumps(marker) + "; 'marking-navigation'", navigate=True)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            snapshot = self.snapshot()
            if snapshot.get("refreshMarker") == marker:
                # A slow navigation can leave the old, already-complete page
                # visible. Wait until the refresh actually replaces the document.
                continue
            if snapshot.get("readyState") != "complete":
                continue
            return parse_snapshot(snapshot)
        raise BrowserError("The housing page did not finish loading before the check timed out.")

