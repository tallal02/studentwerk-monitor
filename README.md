# Dortmund housing monitor

Local prototype for [Studierendenwerk Dortmund's current housing offers](https://www.stwdo.de/wohnen/aktuelle-wohnangebote), using your normal Brave browser. Python 3.11+ and macOS are required; there are no third-party Python dependencies. The final deployment must run while the laptop is off and cost nothing; this prototype does not yet satisfy that hosting requirement. A longer cloud check interval is acceptable to stay within a free service's allowance.

The isolated [GitHub Actions cloud notification test passed](https://github.com/tallal02/studentwerk-monitor/actions/runs/44398260018): 32 tests passed on Linux, and both notification services accepted their test messages without any housing-site access. Live housing monitoring remains paused. See [free hosting status and remaining work](FREE_HOSTING.md).

## Agreed behaviour

- German time (`Europe/Berlin`), following summer/winter time automatically.
- Active from Monday 09:00 through Friday 23:30, including overnight.
- One website check per hour, on the hour, plus a final Friday 23:30 check.
- Dortmund offers only; Telegram and Gmail receive the same new-offer alert.
- The first successful check establishes a baseline; existing offers do not generate "new listing" alerts.
- Each offer ID is announced once, even across restarts, reordered results, price edits, or disappearance and reappearance. A newly published offer with a new ID produces a new alert.
- Verification, unexpected page structure, and load failures are reported as errors. They do not erase listing history or become "no offers" results.
- HTTP 401/403/429 pauses website access persistently. Scheduled ticks stop fetching until access has been recovered manually and an explicit `resume-access` test succeeds. No recurring job is currently enabled; background access testing encountered HTTP 403.
- A shared, durable cooldown covers manual checks, scheduled checks, inspection and recovery tests. Attempts are normally separated by about an hour, including failed attempts and process restarts. The agreed Friday 23:30 extra check allows a 30-minute gap; it is skipped if the previous check was too recent. Cooldown and calendar slots prevent catch-up bursts after waking.

Your Mac must be awake, logged in, and connected to the internet. Brave may be opened by the monitor. Sleep can delay checks; after waking in the active window, one current check catches up without replaying missed hours. Offers that disappear between checks cannot be recovered. Hosted monitoring would need its own browser-access test.

## Finish setup

Double-click **Setup Notifications.command** in this project folder. It will:

1. Ask for your Telegram bot token using hidden input. Create a bot through [BotFather](https://t.me/BotFather) with `/newbot`, then open your bot and tap **Start**. Setup discovers the private chat ID.
2. Ask for your Gmail sender and recipient addresses (the recipient defaults to the sender), then a **Gmail app password** using hidden input. Create that password at [Google Account → App passwords](https://myaccount.google.com/apppasswords). Google requires [2-Step Verification](https://support.google.com/accounts/answer/185833); availability depends on your account. Use the app password for this integration.
3. Send one clearly labelled test alert to Telegram and one to Gmail.

This setup connects notifications only. It does not access the housing website or enable the monitor. The persistent access pause remains in place. Activation is a separate step after manual website access has recovered and an explicitly requested recovery test succeeds.

Enter tokens/passwords in that local setup window. Credentials are stored with mode `0600` in a private folder (`0700`) at:

```
~/Library/Application Support/StudentWerk Monitor/
```

Source code contains no credentials. The setup saves completed steps, so you can rerun it after fixing a failed step. Rerunning setup stops an existing monitor; it stays off until separately activated.

## How it runs

The app reads only the housing tab's page content via AppleScript. Brave needs **View → Developer → Allow JavaScript from Apple Events**. macOS may also request Automation permission for the process controlling Brave; allow it under **System Settings → Privacy & Security → Automation** if prompted.

Each check initiates one page navigation. If a tab must be opened, that opening is the navigation; the monitor does not immediately load it again. Reading a page snapshot never opens or reloads a missing tab. Local DOM polling does not fetch the website again. The browser will still load normal page resources, so one navigation is not a claim of one HTTP request in total.

When verification is shown, the monitor pauses for manual intervention. Automatic verification clicks and form submissions have been removed. HTTP access refusals also pause future website access. A manual browser visit must recover before an explicit recovery test is attempted; changing hosts is not a fix for the unresolved access restriction.

Offer IDs come from links such as `/freie-zimmer/6824`. Navigation and footer changes do not trigger alerts. Telegram and Gmail have independent delivery records: if Gmail fails after Telegram succeeds, only Gmail is retried. Delivery retries use increasing delays up to an hour, during the active window. SMTP/API acceptance does not guarantee inbox delivery; check spam filters and device notification settings during setup. A network failure after a service accepts a message can occasionally cause a duplicate on retry.

A per-user LaunchAgent wakes every five minutes to evaluate the schedule and retry pending alerts. **That is not a five-minute website check:** a durable slot record allows only the agreed hourly fetches and Friday's final check. Outside the active window it does not access the browser or send queued alerts. Logs rotate automatically. The runtime is copied into Application Support so background execution does not depend on Desktop-folder access.

The site's [published notice](https://www.stwdo.de/ueber-uns/impressum/) says automated reading requires prior written approval; account for that access condition when using the monitor. Its [application FAQ](https://www.stwdo.de/wohnen/infos-zur-bewerbung/) explains selection and document submission. Alerts contain offer links; submitting a housing application remains your action.

## Status and stopping

Double-click **Monitor Status.command** to see the last successful check, current Dortmund count, active incident, next scheduled check, pending deliveries, and whether the agent is loaded. **Stop Monitor.command** disables the agent while retaining settings and listing history.

The LaunchAgent is `~/Library/LaunchAgents/de.studentwerk.dortmund-monitor.plist`. Logs are under `~/Library/Application Support/StudentWerk Monitor/logs/`.

From this directory:

```sh
python3 -m studentwerk_monitor inspect       # Fresh browser read, no alerts or baseline change
python3 -m studentwerk_monitor baseline      # Initial baseline, before notifications are configured
python3 -m studentwerk_monitor setup         # Local credential entry and channel tests only; no housing access
python3 -m studentwerk_monitor status
python3 -m studentwerk_monitor check         # Manual check + alerts, including outside normal hours
python3 -m studentwerk_monitor test-alerts   # Send explicit test messages to both channels
python3 -m studentwerk_monitor install       # Enable after successful setup/tests
python3 -m studentwerk_monitor stop
python3 -m studentwerk_monitor resume-access --manual-access-confirmed # One explicit recovery test after manual access is healthy
python3 -m unittest discover -v
```

After editing source, run the tests, then `install` to refresh the runtime copy. Keep the setup/status/stop launchers in this folder. Changing Gmail credentials may require a new app password; rerun setup to update it.

## Research references

- [Telegram bot creation](https://core.telegram.org/bots/tutorial#obtain-your-bot-token) and [Bot API](https://core.telegram.org/bots/api).
- [Google app passwords](https://support.google.com/accounts/answer/185833) and [Gmail SMTP settings](https://developers.google.com/workspace/gmail/imap/imap-smtp).
- [Python time zones](https://docs.python.org/3.13/library/zoneinfo.html).
- The installed macOS `launchd.plist(5)` manual documents calendar triggers and wake-from-sleep behaviour.

