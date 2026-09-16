# SoCal Deal Tracker

Every morning around 8 AM Pacific, this checks yesterday's games for the
Dodgers, Angels, LAFC, Galaxy, San Diego FC, Timbers, Rams, Kings, Ducks,
Lakers, and all of MLB and the NBA, marks which food deals are active, updates
your tracker page, and sends your phone a notification. Free. About 15 minutes
to set up, easiest on a computer.

## 1. Notifications app
Install **ntfy** (App Store or Google Play). Tap **+** and subscribe to a
long, hard-to-guess topic name, like `socal-deals-8k2p9x`.

## 2. Make the repo
1. Sign in at github.com and click **New repository**. Name it `deals`.
   Choose **Public** (free GitHub Pages requires it; your ntfy topic stays secret). Create it.
2. Unzip `socal-deals.zip` on your computer.
3. On the repo page, click **uploading an existing file** and drag in `update.py`,
   `README.md`, and the whole `docs` folder. Commit.
4. Click **Add file > Create new file**. Name it exactly
   `.github/workflows/update.yml`, paste in that file's contents, commit.

## 3. Settings (two things)
- **Settings > Secrets and variables > Actions > New repository secret.**
  Name `NTFY_TOPIC`, value = your topic name.
- **Settings > Pages.** Source: **Deploy from a branch**. Branch: **main**, folder **/docs**. Save.

## 4. First run
**Actions** tab > enable workflows if asked > **Update deals** > **Run workflow**.
When it turns green (about 1 minute), open `https://YOUR-USERNAME.github.io/deals/`.
On your phone, use Share > **Add to Home Screen** so it opens like an app.

If any deal says "Couldn't check," open the run log, copy the lines starting
with ERROR, and send them to Claude to fix.

## Choosing notifications
In `update.py`, each deal has `follow=True` or `follow=False`. True means you get
a phone notification. Stars on the page are separate and just save favorites.
Edit the file right on GitHub with the pencil icon.

## Sharing alerts with someone else
Have them install ntfy and subscribe to their own topic name. Then edit the
`NTFY_TOPIC` secret (Settings > Secrets and variables > Actions) and put both
topics in, separated by a comma, no spaces: `your-topic,their-topic`. Both
phones will get the same morning notification.

## Order now
When a deal is active, cards for vendors with online ordering show an
**Order now** button that opens their site. A few vendors (McDonald's, ampm,
Norms) are app-only or in-store-only, so those just show how to redeem
instead.

## Ending soon
Deals tied to the MLB regular season (which ends Sept 27, 2026) show an
"Ends in Nd" tag once they're within 5 days of their end date, and a banner
appears at the top of the page. Your morning notification will mention them
too, even on a day when nothing is active.

## History and streaks
Every day's results are saved to `docs/history.json` (last 90 days per deal).
Each card shows "Last active 3 days ago" and, when it comes around often,
"Nx in the last 30 days." A deal with no hits in 90 days says so. The very
first run has nothing to compare against yet, so streak lines fill in after
a day or two.

## Possible upcoming deals
The page lists games happening today or tomorrow that could unlock a deal,
with a rough percent chance for each. Chances start from typical league rates
(for example, how often an MLB team strikes out 7+ batters). After a deal has
8 or more games in `docs/history.json`, its chance blends in the real hit
rate. They're estimates for planning, not guarantees.

## Live now
When a game with a deal is in progress, a **Live now** box appears at the top
of the page. It checks scores every 30 seconds while the page is open and
shows each deal's progress, like "8 strikeouts, unlocked" or "Leading 3-1,
needs to hold on." It pauses when the page is in the background and checks
every 5 minutes when nothing is live. This runs in your browser, so it doesn't
use the GitHub workflow. If a sports feed blocks browser requests, that team's
games just won't appear in the box; the morning update still covers them.

## Notes
- DoorDash LAFC and Carl's Jr SDFC codes only appear on team socials. The page
  tells you when they trigger and links to where the code is posted.
- GitHub pauses scheduled runs after 60 days with no repo activity. The daily
  update commit counts as activity, so this normally won't happen.
