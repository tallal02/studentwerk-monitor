# Deployment status

The next single paced browser validation is planned for **Thursday 19 November 2026 at 15:00 CET** on GitHub Actions. GitHub may delay scheduled jobs. The Mac is not required.

The previous browser validation timed out and paused itself. The code now clicks the visible consent label rather than a potentially covered native input, avoids waiting on a removed form during navigation, and follows normal server redirects without delaying short-lived session cookies. All 54 offline tests pass, including an isolated browser fixture where the native input cannot receive pointer events.

No further live validation is attempted before the planned time and the shared two-hour cooldown. If it succeeds, an activation notice is sent to Telegram and Gmail and the two-hour schedule continues. On a refusal, interactive challenge, unrecognized page or failed browser flow, the workflow pauses and disables itself; error alerts are sent. Live access is **not yet validated**.

The only live cloud validations made so far were the documented raw HTTP validation and the single browser validation. No housing-page access is being made from the Mac.
