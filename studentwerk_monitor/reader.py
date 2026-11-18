"""One transparent HTTP GET; verification and redirects require intervention."""

import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .browser import OFFERS_URL, AccessDenied, BrowserError, parse_snapshot


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class HousingHTML(HTMLParser):
    BLOCKS = {"h1", "h2", "h3", "h4", "p", "div", "li", "br", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_main = False
        self.ignored = 0
        self.text = []
        self.anchor = None
        self.offers = []
        self.verification = False

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if attributes.get("id") == "housing-offers-access-form":
            self.verification = True
        if tag == "main":
            self.in_main = True
        if tag in ("script", "style"):
            self.ignored += 1
        if not self.in_main or self.ignored:
            return
        if tag in self.BLOCKS:
            self.text.append("\n")
            if self.anchor:
                self.anchor["parts"].append("\n")
        if tag == "a":
            url = urljoin(OFFERS_URL, attributes.get("href", ""))
            parsed = urlparse(url)
            if parsed.netloc == "www.stwdo.de" and re.fullmatch(r"/freie-zimmer/\d+/?", parsed.path):
                self.anchor = {"url": url, "parts": []}

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
        if not self.in_main or self.ignored:
            return
        if tag in self.BLOCKS:
            self.text.append("\n")
            if self.anchor:
                self.anchor["parts"].append("\n")
        if tag == "a" and self.anchor:
            self.offers.append({"url": self.anchor["url"], "text": "".join(self.anchor["parts"])})
            self.anchor = None
        if tag == "main":
            self.in_main = False

    def handle_data(self, data):
        if self.in_main and not self.ignored:
            clean = " ".join(data.split())
            if clean:
                self.text.append(clean + " ")
                if self.anchor:
                    self.anchor["parts"].append(clean + " ")

    def result(self):
        return {
            "url": OFFERS_URL, "readyState": "complete", "responseStatus": 200,
            "verification": self.verification, "text": "".join(self.text), "offers": self.offers,
        }


def parse_html(html: str):
    parser = HousingHTML()
    parser.feed(html)
    return parse_snapshot(parser.result())


class CloudReader:
    def __init__(self, opener=None):
        self.opener = opener or build_opener(NoRedirect())

    def fetch(self):
        request = Request(OFFERS_URL, headers={
            "User-Agent": "StudentWerkMonitor/0.1 (+https://github.com/tallal02/studentwerk-monitor)",
            "Accept": "text/html",
        })
        try:
            with self.opener.open(request, timeout=25) as response:
                data = response.read(2_000_001)
                content_type = response.headers.get_content_type()
                encoding = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise AccessDenied(f"The housing website refused cloud access (HTTP {exc.code}); further website checks are paused.") from None
            raise BrowserError(f"The housing website returned HTTP {exc.code}. No retry or redirect was attempted.") from None
        except (URLError, OSError, TimeoutError):
            raise BrowserError("The housing website could not be reached. No retry was attempted.") from None
        if len(data) > 2_000_000 or content_type != "text/html":
            raise BrowserError("The housing response has an unexpected size or content type.")
        try:
            html = data.decode(encoding)
        except (LookupError, UnicodeError):
            raise BrowserError("The housing response could not be decoded.") from None
        return parse_html(html)

