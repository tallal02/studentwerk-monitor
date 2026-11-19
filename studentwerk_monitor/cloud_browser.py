"""Normal isolated Chromium session with a bounded, paced consent flow."""

import json
import time
from urllib.parse import urlparse

from .browser import OFFERS_URL, AccessDenied, BrowserError, VerificationRequired
from .reader import parse_html


class CloudBrowserReader:
    def __init__(self, storage_state=None, *, url=OFFERS_URL, delay_seconds=10, executable_path=None):
        self.url = url
        self.storage_state = storage_state
        self.delay = delay_seconds
        self.executable_path = executable_path
        self.request_counts = {}

    def fetch(self):
        from playwright.sync_api import sync_playwright
        allowed_host = urlparse(self.url).hostname
        counts = {"documents": 0, "posts": 0, "requests": 0}
        failures = []
        last_navigation = [None]
        stage = "browser startup"
        try:
            with sync_playwright() as playwright:
                options = {"headless": True}
                if self.executable_path:
                    options["executable_path"] = self.executable_path
                browser = playwright.chromium.launch(**options)
                context = browser.new_context(storage_state=self.storage_state)
                page = context.new_page()

                def route_request(route):
                    request = route.request
                    counts["requests"] += 1
                    if request.is_navigation_request() and request.frame == page.main_frame:
                        if urlparse(request.url).hostname != allowed_host:
                            failures.append("The page attempted an unexpected navigation.")
                            route.abort()
                            return
                        counts["documents"] += 1
                        if request.method == "POST":
                            counts["posts"] += 1
                        if counts["documents"] > 4 or counts["posts"] > 1:
                            failures.append("The page exceeded the one-check navigation budget.")
                            route.abort()
                            return
                        if last_navigation[0] is not None and request.redirected_from is None:
                            remaining = self.delay - (time.monotonic() - last_navigation[0])
                            if remaining > 0:
                                time.sleep(remaining)
                        last_navigation[0] = time.monotonic()
                    route.continue_()

                def response_received(response):
                    host = urlparse(response.url).hostname or ""
                    if host == allowed_host and response.status in (401, 403, 429):
                        failures.append(f"The website refused browser access (HTTP {response.status}).")

                context.route("**/*", route_request)
                page.on("response", response_received)
                stage = "opening the housing page"
                page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
                if failures:
                    raise AccessDenied(failures[0])
                form = page.locator("#housing-offers-access-form")
                if form.count():
                    stage = "loading the consent widget"
                    page.wait_for_timeout(self.delay * 1000)
                if failures:
                    raise AccessDenied(failures[0])
                if form.count():
                    if page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare'], .g-recaptcha, .h-captcha, .cf-turnstile").count():
                        raise VerificationRequired("An interactive challenge requires a person; the cloud worker has paused.")
                    checkbox = form.locator("input[type=checkbox][name^='_mosparo_checkboxField_']:visible")
                    if checkbox.count() != 1:
                        raise VerificationRequired("The known consent checkbox is unavailable; cloud access has paused.")
                    if not checkbox.is_checked():
                        stage = "clicking the consent label"
                        identifier = checkbox.get_attribute("id")
                        label = form.locator("label[for=" + json.dumps(identifier) + "]:visible") if identifier else None
                        if label is not None and label.count() == 1:
                            label.click(timeout=15000, no_wait_after=True)
                        else:
                            checkbox.click(timeout=15000, no_wait_after=True)
                    stage = "waiting for the consent submission"
                    deadline = time.monotonic() + 35
                    submitted = False
                    while time.monotonic() < deadline:
                        page.wait_for_timeout(300)
                        if failures:
                            raise AccessDenied(failures[0])
                        if not form.count():
                            break
                        if counts["posts"] == 0 and not submitted:
                            ready = page.evaluate("""() => {
                                const form = document.getElementById('housing-offers-access-form');
                                return !!form && /Ihre Daten enthalten kein(?:en)? Spam|Your data does not contain (?:any )?spam/i.test(form.innerText) && form.checkValidity();
                            }""")
                            if ready:
                                # Normal validated submission only. Observe actual
                                # POST requests so an automatic submit is not replayed.
                                page.evaluate("() => document.getElementById('housing-offers-access-form')?.requestSubmit()")
                                submitted = True
                    if form.count():
                        raise VerificationRequired("The normal consent flow did not unlock offers; cloud access has paused.")
                stage = "reading the unlocked listings"
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                if failures:
                    raise AccessDenied(failures[0])
                offers = parse_html(page.content())
                self.storage_state = context.storage_state()
                self.request_counts = counts
                context.close()
                browser.close()
                return offers
        except BrowserError:
            self.request_counts = counts
            raise
        except Exception as exc:
            self.request_counts = counts
            if failures:
                raise AccessDenied(failures[0]) from None
            raise BrowserError(f"The cloud browser stopped during {stage} ({type(exc).__name__}); no reload or retry was attempted.") from None

