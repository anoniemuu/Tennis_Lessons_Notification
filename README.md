# TC de Uithof training spot monitor

Checks the TC de Uithof **Training Spots** page and sends a push notification when a newly available training spot for **level 7 or 8** appears.

Website: <https://tcdeuithof.nl/cancel/index.html>

## What it does

- Runs on GitHub Actions.
- Opens the JavaScript-driven page in headless Chrome.
- Looks only for level 7 and 8 availability.
- Sends a phone push notification through [ntfy](https://ntfy.sh/).
- Remembers currently visible spots in `state.json`, so the same opening is not sent every 5 minutes.
- If a spot disappears and later becomes available again, it can alert again.

## 1. Put this repository on GitHub

Create a new repository and push these files to it.

The workflow is in `.github/workflows/check.yml`.

## 2. Install ntfy on your phone

Install the **ntfy** app from your phone's app store.

Choose a long, hard-to-guess topic name, for example:

```text
tc-uithof-7-8-CHANGE-THIS-TO-A-LONG-RANDOM-STRING
```

In the ntfy app, subscribe to:

```text
https://ntfy.sh/YOUR-TOPIC
```

Treat the topic name like a password: anyone who knows it can subscribe to that topic.

## 3. Add the topic to GitHub Secrets

In your GitHub repository:

1. **Settings**
2. **Secrets and variables**
3. **Actions**
4. **New repository secret**
5. Name: `NTFY_TOPIC`
6. Value: only your topic name (not the full URL)

Example value:

```text
tc-uithof-7-8-a8d317f6c1b94a7eb53c...
```

## 4. Test the notification

On GitHub:

1. Open **Actions**.
2. Select **Check tennis training spots**.
3. Click **Run workflow**.
4. Enable **Only send a test phone notification**.
5. Run it.

Your phone should receive a test notification.

## 5. Test a real page check

Run the workflow manually again with the test option disabled.

Open the run log and check the `Check TC de Uithof` step. It prints the number of level 7/8 spots that it found and the text of matching cards.

## Schedule

The included workflow uses:

```yaml
cron: '2-57/5 * * * *'
```

That means approximately every 5 minutes. The `:02, :07, :12, ...` offset avoids the busiest exact start-of-hour times.

GitHub scheduled workflows are **not guaranteed to run exactly on time**. GitHub documents that scheduled jobs may be delayed, and under high load some queued jobs may even be dropped.

## Change the levels

In `.github/workflows/check.yml`, change:

```yaml
TARGET_LEVELS: '7,8'
```

For example, to level 6 and 7:

```yaml
TARGET_LEVELS: '6,7'
```

## If the TC de Uithof page changes

The monitor deliberately does not depend on a private booking endpoint. It renders the public page and detects availability cards from their visible text. This is more resilient to backend changes, but a major redesign of the page can still require an update.

If the workflow says:

```text
Expected 'Training Spots' heading was not found
```

or

```text
The page still showed 'Loading...'
```

the page structure or loading behaviour has probably changed.

## Important GitHub Actions note

GitHub technically supports a minimum scheduled interval of five minutes, but using hosted Actions as a permanent high-frequency website monitor is not an ideal fit for GitHub Actions' current product terms. Public repositories also have scheduled workflows disabled after 60 days without repository activity. For a long-term monitor, moving the polling to a small always-on/self-hosted service is more reliable.
