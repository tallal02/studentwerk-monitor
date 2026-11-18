# Free, laptop-independent hosting

Requirement: no paid subscription, and no need to leave the Mac awake. A larger gap than one hour is acceptable. Start planning with roughly two-hour checks in the Monday 09:00–Friday 23:30 Europe/Berlin window; choose exact times only after access works and the deployment is ready.

## Prepared next step

The workflow `.github/workflows/notification-test.yml` runs only when manually dispatched. It sends two clearly labelled **cloud test** notifications, includes the real GitHub run link, and does not fetch the housing website. It does not enable recurring checks. The Mac-only browser adapter is not a working cloud housing reader.

Approved repository: `tallal02/studentwerk-monitor`. The user approved publication of the reviewed source and storage of notification credentials in GitHub Actions secrets. Using a **public repository with a standard Linux GitHub-hosted runner** keeps runner execution free. Private repositories have an included allowance and potentially paid overages, so they require a verified zero-spend configuration before activation under the free-only constraint.

The proposed publication includes source, tests and documentation only. Runtime configuration, credentials, databases, reports and logs stay in the user's private Application Support folder. Notification credentials would be copied directly to encrypted GitHub Actions secrets; secret values must not appear in commits, workflow definitions or logs.

Required secret names:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GMAIL_SENDER`
- `GMAIL_RECIPIENT`
- `GMAIL_APP_PASSWORD`

The public workflow is manual-only, uses a pinned official checkout action, has read-only repository permissions, and does not run on pull requests. It does not upload artifacts or use a paid/larger runner.

## Remaining live-access issue

The housing website still returns HTTP 403. No more housing requests are being made. Cloud hosting must not be presented as a way to solve that block: its verification/session behaviour still needs a supported, reliable access method before live monitoring can be enabled. A deployment is not complete merely because a test notification was delivered.

After access is resolved, the cloud reader and persistent listing history need implementation and validation. The shared cooldown, manual verification pause and new-listing rules already have offline tests in the local prototype. They must apply in the cloud too. The cloud schedule should never automatically retry the site after an access refusal.

GitHub's current documentation supports timezone-aware schedules but warns that scheduled jobs can be delayed or dropped under load; exact-time execution cannot be promised. Approximately two-hour checks would create about 220–240 invocations per month, before any exceptional tests. A longer interval can reduce invocations further if needed.

## References

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): public standard runners are free; private repositories share the account's included compute allowance and can incur overages.
- [Scheduled workflow behaviour](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule): timezone support, delays and dropped jobs.

