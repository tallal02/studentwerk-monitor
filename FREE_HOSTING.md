# Free, laptop-independent hosting

Requirement: no paid subscription, and no need to leave the Mac awake. A larger gap than one hour is acceptable. The cloud workflow is prepared for two-hour checks in the Monday 09:00–Friday 23:30 Europe/Berlin window. Live access stays off until a successful cloud validation.

## Cloud worker

The prepared workflow is `.github/workflows/monitor.yml`. Its schedules use `Europe/Berlin` directly, including summer/winter time:

- Monday: 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00, 23:00.
- Tuesday–Thursday: 01:00, 03:00, and every two hours through 23:00.
- Friday: 01:00, 03:00, and every two hours through 21:00, then the final check at 23:30.
- Weekends: no checks.

This is 56 planned jobs per week. Actual GitHub scheduling can be delayed. A shared two-hour cooldown prevents delayed or manual jobs from causing a burst; some slots may be skipped to preserve spacing.

`LIVE_HOUSING_ENABLED` is initially `false`, so scheduled jobs do not start a runner or fetch the website. Manual `status` mode only validates state handling. A fresh or missing state cache always starts paused, regardless of that variable.

The cloud reader uses one transparent HTTP GET, follows no redirects, and does not click verification controls or submit forms. A verification screen, refused access, network error or unexpected response pauses access. A failed workflow also disables itself through GitHub's API. Cloud access must be validated separately from the earlier browser test: a verification-only response means unattended reading is not yet possible.

Listing history, the pause and independent notification delivery records are stored in an authenticated, encrypted GitHub Actions cache. The encryption key is a separate `CLOUD_STATE_KEY` Actions secret. Only the encrypted cache file is uploaded; credentials and the plaintext database are excluded. The repository's standard 10 GB free cache limit is not increased. The cloud dependency is pinned in `requirements-cloud.txt`.

The manual `recover-access` operation requires `manual_access_confirmed=true` and performs at most one request. It does not enable the two-hour schedule; successful validation and persisted state must be verified before setting `LIVE_HOUSING_ENABLED=true`.

## Prepared next step

The separate workflow `.github/workflows/notification-test.yml` runs only when manually dispatched. It sends two clearly labelled **cloud test** notifications, includes the real GitHub run link, and does not fetch the housing website. It does not enable recurring checks. The Mac browser adapter remains a local prototype; the cloud workflow uses `reader.py` instead.

Verified on 18 November 2026 at approximately 22:08 Europe/Berlin: the [cloud notification test](https://github.com/tallal02/studentwerk-monitor/actions/runs/44398260018) succeeded on GitHub's standard `ubuntu-latest` runner. All 32 offline tests passed, and Telegram and Gmail accepted their clearly labelled cloud test messages. This is evidence of cloud execution and notification sending, not evidence of working live housing access. The earlier 22:00 local calendar test did not trigger and was removed.

Approved repository: `tallal02/studentwerk-monitor`. The user approved publication of the reviewed source and storage of notification credentials in GitHub Actions secrets. Using a **public repository with a standard Linux GitHub-hosted runner** keeps runner execution free. Private repositories have an included allowance and potentially paid overages, so they require a verified zero-spend configuration before activation under the free-only constraint.

The [repository is now published](https://github.com/tallal02/studentwerk-monitor), and the five notification values below are stored in encrypted GitHub Actions secrets. No paid runners, subscriptions or recurring housing checks were enabled.

The proposed publication includes source, tests and documentation only. Runtime configuration, credentials, databases, reports and logs stay in the user's private Application Support folder. Notification credentials would be copied directly to encrypted GitHub Actions secrets; secret values must not appear in commits, workflow definitions or logs.

Required secret names:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GMAIL_SENDER`
- `GMAIL_RECIPIENT`
- `GMAIL_APP_PASSWORD`

The public workflow is manual-only, uses a pinned official checkout action, has read-only repository permissions, and does not run on pull requests. It does not upload artifacts or use a paid/larger runner.

## Remaining live-access issue

The user reported that normal manual access returned after restarting the router. No further housing-page requests have been made from the Mac. Cloud hosting must not be presented as a way to solve an access block: its verification/session behaviour still needs a supported, reliable access method before live monitoring can be enabled. A deployment is not complete merely because a test notification was delivered.

On 18 November 2026, both [initial cloud state validation](https://github.com/tallal02/studentwerk-monitor/actions/runs/44403913193) and [encrypted state restoration](https://github.com/tallal02/studentwerk-monitor/actions/runs/44404262350) passed without housing access. The latter passed all 50 offline tests.

The subsequent [single live cloud validation](https://github.com/tallal02/studentwerk-monitor/actions/runs/44404616264) reached the site's manual spam-verification page. It did not solve or submit that verification. The worker recorded a persistent pause, both notification services accepted the error alerts, the encrypted paused state was saved, and the workflow disabled itself. `LIVE_HOUSING_ENABLED` remains `false`. No repeated or scheduled housing request followed. The remaining issue is an authorized access method that works unattended, such as an official feed or supported integration. The user’s working browser session is not transferred to GitHub.

The cloud reader, encrypted persistent listing history and schedule have offline tests. Live validation remains necessary. The cloud schedule must never automatically retry the site after an access refusal.

GitHub's current documentation supports timezone-aware schedules but warns that scheduled jobs can be delayed or dropped under load; exact-time execution cannot be promised. Approximately two-hour checks would create about 220–240 invocations per month, before any exceptional tests. A longer interval can reduce invocations further if needed.

## References

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): public standard runners are free; private repositories share the account's included compute allowance and can incur overages.
- [Scheduled workflow behaviour](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule): timezone support, delays and dropped jobs.

