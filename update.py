"""
SoCal sports food deal tracker.
Runs daily on GitHub Actions: pulls yesterday's results (Pacific time) from
public sports data feeds, checks every deal rule, writes docs/status.json for
the tracker page, and pushes a phone notification via ntfy for followed deals.

Local test:  python update.py --date 2026-09-16 --dry-run
(--date is TODAY's date; the script checks the day before it.)
"""
import argparse, json, os, re, sys, traceback, urllib.request
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
MLB = "https://statsapi.mlb.com/api/v1"
NHL = "https://api-web.nhle.com/v1"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

_cache = {}
def get(url):
    if url not in _cache:
        req = urllib.request.Request(url, headers={"User-Agent": "socal-deal-tracker/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            _cache[url] = json.load(r)
    return _cache[url]

def ymd(d): return d.strftime("%Y-%m-%d")
def compact(d): return d.strftime("%Y%m%d")

# ------------------------------------------------------------------ MLB ----
def mlb_team_games(team_id, d):
    s = get(f"{MLB}/schedule?sportId=1&teamId={team_id}&date={ymd(d)}&hydrate=decisions")
    games = []
    for day in s.get("dates", []):
        for g in day.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final": continue
            if g.get("gameType") not in ("R", "F", "D", "L", "W"): continue
            home = g["teams"]["home"]["team"]["id"] == team_id
            side, opp = ("home", "away") if home else ("away", "home")
            box = get(f"{MLB}/game/{g['gamePk']}/boxscore")["teams"]
            mine, theirs = box[side]["teamStats"], box[opp]["teamStats"]
            won = bool(g["teams"][side].get("isWinner"))
            games.append({
                "home": home, "won": won,
                "runs": mine["batting"].get("runs", 0),
                "sb": mine["batting"].get("stolenBases", 0),
                "k": mine["pitching"].get("strikeOuts", 0),
                "dp": theirs["batting"].get("groundIntoDoublePlay", 0),
                "save": won and "save" in g.get("decisions", {}),
                "desc": f'{"vs" if home else "at"} {g["teams"][opp]["team"]["name"]}, '
                        f'{"W" if won else "L"} {g["teams"][side].get("score",0)}-{g["teams"][opp].get("score",0)}',
            })
    return games

def mlb_rule(team_id, test, why_yes, why_no):
    def check(ctx):
        games = mlb_team_games(team_id, ctx["y"])
        if not games: return False, "No game yesterday"
        for g in games:
            if test(g): return True, f"{g['desc']}: {why_yes(g)}"
        g = games[-1]
        return False, f"{g['desc']}: {why_no(g)}"
    return check

def baja_blast(ctx):
    s = get(f"{MLB}/schedule?sportId=1&date={ymd(ctx['y'])}")
    best = None
    for day in s.get("dates", []):
        for g in day.get("games", []):
            if g.get("gameType") != "R" or g.get("status", {}).get("abstractGameState") != "Final": continue
            for play in get(f"{MLB}/game/{g['gamePk']}/playByPlay").get("allPlays", []):
                if play.get("result", {}).get("eventType") != "home_run": continue
                for ev in play.get("playEvents", []):
                    dist = (ev.get("hitData") or {}).get("totalDistance")
                    if dist and (best is None or dist > best[0]):
                        best = (dist, play.get("matchup", {}).get("batter", {}).get("fullName", "Someone"))
    if best and best[0] >= 420: return True, f"{best[1]} hit a {int(best[0])} ft homer"
    if best: return False, f"Longest homer was {int(best[0])} ft (need 420)"
    return False, "No MLB home runs yesterday"

def firehouse(ctx):
    today = ctx["today"]
    if today.weekday() > 2: return False, "Only redeemable Monday to Wednesday"
    fri = today - timedelta(days=today.weekday() + 3)
    for i in range(3):
        d = fri + timedelta(days=i)
        s = get(f"{MLB}/schedule?sportId=1&date={ymd(d)}&hydrate=linescore")
        for day in s.get("dates", []):
            for g in day.get("games", []):
                innings = len((g.get("linescore") or {}).get("innings", []))
                if g.get("gameType") == "R" and innings > 9:
                    return True, f'{g["teams"]["away"]["team"]["name"]} at {g["teams"]["home"]["team"]["name"]} went {innings} innings'
    return False, "No extra-inning games last weekend"

# ------------------------------------------------------------------ NHL ----
def nhl_games(abbrev, d):
    out = []
    for g in get(f"{NHL}/score/{ymd(d)}").get("games", []):
        if g.get("gameType") not in (2, 3) or g.get("gameState") not in ("OFF", "FINAL"): continue
        h, a = g["homeTeam"], g["awayTeam"]
        if abbrev not in (h.get("abbrev"), a.get("abbrev")): continue
        home = h.get("abbrev") == abbrev
        me, opp = (h, a) if home else (a, h)
        out.append({"home": home, "won": me.get("score", 0) > opp.get("score", 0),
                    "goals": g.get("goals", []), "desc": f'{"vs" if home else "at"} {opp.get("abbrev")}, {me.get("score",0)}-{opp.get("score",0)}'})
    return out

def ducks_home_win(ctx):
    games = nhl_games("ANA", ctx["y"])
    if not games: return False, "No game yesterday"
    g = games[0]
    return (g["home"] and g["won"]), g["desc"]

def kings_late_2nd(ctx):
    games = nhl_games("LAK", ctx["y"])
    if not games: return False, "No game yesterday"
    for g in games:
        for goal in g["goals"]:
            if goal.get("teamAbbrev", {}).get("default", goal.get("teamAbbrev")) == "LAK" \
               and goal.get("period") == 2 and str(goal.get("timeInPeriod", "00:00")) >= "19:00":
                return True, f'{g["desc"]}: goal at {goal["timeInPeriod"]} of the 2nd'
    return False, f'{games[0]["desc"]}: no goal in the final minute of the 2nd'

# ----------------------------------------------------------------- ESPN ----
def espn_events(path, d):
    return get(f"{ESPN}/{path}/scoreboard?dates={compact(d)}").get("events", [])

def espn_team_games(path, names, d):
    out = []
    for ev in espn_events(path, d):
        comp = ev["competitions"][0]
        if not comp.get("status", ev.get("status", {})).get("type", {}).get("completed"): continue
        teams = comp["competitors"]
        me = next((t for t in teams if any(n.lower() in (t["team"].get("displayName","")+" "+t["team"].get("abbreviation","")).lower() for n in names)), None)
        if not me: continue
        opp = next(t for t in teams if t is not me)
        out.append({"event": ev, "comp": comp, "me": me, "opp": opp, "home": me.get("homeAway") == "home",
                    "won": bool(me.get("winner")),
                    "desc": f'{"vs" if me.get("homeAway")=="home" else "at"} {opp["team"].get("displayName")}, {me.get("score")}-{opp.get("score")}'})
    return out

def minute(detail):
    m = re.match(r"(\d+)", (detail.get("clock") or {}).get("displayValue", ""))
    return int(m.group(1)) if m else 999

def soccer(names, test, days_back=1):
    def check(ctx):
        found = []
        for i in range(days_back):
            found += espn_team_games("soccer/usa.1", names, ctx["y"] - timedelta(days=i))
        if not found: return False, "No match yesterday" if days_back == 1 else f"No match in the last {days_back} days"
        for g in found:
            ok = test(g)
            if ok: return True, g["desc"]
        return False, found[0]["desc"]
    return check

def lafc_scored_first(g):
    if not g["home"]: return False
    goals = sorted([x for x in g["comp"].get("details", []) if x.get("scoringPlay")], key=minute)
    return bool(goals) and minute(goals[0]) <= 45 and str(goals[0].get("team", {}).get("id")) == str(g["me"]["team"]["id"])

def nba_fifty(ctx):
    top = None
    for ev in espn_events("basketball/nba", ctx["y"]):
        comp = ev["competitions"][0]
        for t in comp["competitors"]:
            for cat in t.get("leaders", []):
                if cat.get("name") == "points" and cat.get("leaders"):
                    L = cat["leaders"][0]
                    if top is None or L.get("value", 0) > top[0]:
                        top = (L.get("value", 0), L.get("athlete", {}).get("displayName", "Someone"))
    if top is None: return False, "No NBA games yesterday"
    return top[0] >= 50, f"Top scorer: {top[1]}, {int(top[0])} pts"

def lakers_win(ctx):
    games = espn_team_games("basketball/nba", ["Los Angeles Lakers"], ctx["y"])
    if not games: return False, "No game yesterday"
    return games[0]["won"], games[0]["desc"]

def rams_int(ctx):
    games = espn_team_games("football/nfl", ["Los Angeles Rams"], ctx["y"])
    if not games: return False, "No game yesterday"
    g = games[0]
    summ = get(f"{ESPN}/football/nfl/summary?event={g['event']['id']}")
    ints = 0
    for team in summ.get("boxscore", {}).get("players", []):
        if str(team.get("team", {}).get("id")) != str(g["me"]["team"]["id"]): continue
        for cat in team.get("statistics", []):
            if cat.get("name") == "interceptions" and cat.get("totals"):
                try: ints = int(cat["totals"][0])
                except ValueError: pass
    return ints > 0, f'{g["desc"]}: {ints} interception{"s" if ints != 1 else ""}'

# ---------------------------------------------------------------- DEALS ----
# follow=True means you get a phone notification when it's active.
D, A = 119, 108
DEALS = [
  dict(id="panda", team="Dodgers", vendor="Panda Express", offer="$7 Panda Plate", code="DODGERSWIN", when="Dodgers home win", follow=True,
       order='https://www.pandaexpress.com/order',
       near='Panda Express',
       url="https://www.pandaexpress.com/dodgerswin",
       check=mlb_rule(D, lambda g: g["home"] and g["won"], lambda g: "home win", lambda g: "not a home win")),
  dict(id="jitb-dodgers", team="Dodgers", vendor="Jack in the Box", offer="Free Jumbo Jack with a large Coke", code="GODODGERS26", when="Dodgers pitchers strike out 7+", follow=True,
       order='https://www.jackinthebox.com/',
       near='Jack in the Box',
       url="https://www.jackinthebox.com/location",
       check=mlb_rule(D, lambda g: g["k"] >= 7, lambda g: f'{g["k"]} strikeouts', lambda g: f'{g["k"]} strikeouts (need 7)')),
  dict(id="mcd-dodgers", team="Dodgers", vendor="McDonald's", offer="Free 6 pc McNuggets with $2 purchase", how="McDonald's app, Rewards & Deals", when="Dodgers score 6+ runs", follow=True,
       order='https://www.mcdonalds.com/us/en-us/mobile-order-and-pay.html',
       orderLabel='Open app',
       near="McDonald's",
       url="https://www.mcdonalds.com/us/en-us.html",
       check=mlb_rule(D, lambda g: g["runs"] >= 6, lambda g: f'{g["runs"]} runs', lambda g: f'{g["runs"]} runs (need 6)')),
  dict(id="habit", team="Dodgers", vendor="The Habit", offer="Free Double Char with $8 minimum", code="DODGERS26", when="Dodgers turn a double play at home", follow=True, ends="2026-09-27",
       order='https://order.habitburger.com/',
       near='The Habit Burger Grill',
       url="https://www.habitburger.com/dodgers/",
       check=mlb_rule(D, lambda g: g["home"] and g["dp"] >= 1, lambda g: "double play at home", lambda g: "no home double play")),
  dict(id="ampm", team="Dodgers", vendor="ampm", offer="Free Dodger Dog and 16 oz Coke", how="ampm app, scan barcode", when="Dodgers steal a base at home", follow=True,
       order='https://www.ampm.com/',
       orderLabel='Open app',
       near='ampm',
       url="https://www.ampm.com/",
       check=mlb_rule(D, lambda g: g["home"] and g["sb"] >= 1, lambda g: "stolen base at home", lambda g: "no home stolen base")),
  dict(id="sunright", team="Dodgers", vendor="Sunright Tea", offer="Buy one, get one Blue Heaven Frostie", code="ILOVELA", when="Dodgers home win", follow=False,
       order='https://www.snrtea.com/',
       near='Sunright Tea Studio',
       url="https://www.snrtea.com/",
       check=mlb_rule(D, lambda g: g["home"] and g["won"], lambda g: "home win", lambda g: "not a home win")),
  dict(id="uber", team="Dodgers", vendor="Uber Eats", offer="$15 off $20+ (once per account)", code="SAVEAWAY", when="Dodgers save in an away game", follow=False,
       order='https://www.ubereats.com/',
       url="https://www.ubereats.com/",
       check=mlb_rule(D, lambda g: (not g["home"]) and g["save"], lambda g: "away save", lambda g: "no away save")),
  dict(id="elportal", team="Dodgers", vendor="El Portal (Pasadena)", offer="$12 combo plates and $7 margaritas", how="Order online for pickup", when="Any Dodgers win", follow=False,
       order='https://elportal.hrpos.heartland.us/menu',
       near='El Portal Restaurant, 2118 W Washington Blvd, Pasadena, CA',
       url="https://elportal.hrpos.heartland.us/menu",
       check=mlb_rule(D, lambda g: g["won"], lambda g: "win", lambda g: "loss")),
  dict(id="conazucar", team="Dodgers", vendor="Con Azúcar Café (Sylmar)", offer="Free size upgrade and Dodgers latte art", how='Say "DODGERS WIN!" at the register', when="Any Dodgers win", follow=False,
       order='https://order.toasttab.com/online/con-azucar-la-group-1-13739-foothill-boulevard',
       near='Con Azúcar Café, 13739 Foothill Blvd, Sylmar, CA',
       url="https://order.toasttab.com/online/con-azucar-la-group-1-13739-foothill-boulevard",
       check=mlb_rule(D, lambda g: g["won"], lambda g: "win", lambda g: "loss")),
  dict(id="mcd-angels", team="Angels", vendor="McDonald's", offer="Free medium fries with $2 purchase", how="McDonald's app, Rewards & Deals", when="Any Angels win", follow=True,
       order='https://www.mcdonalds.com/us/en-us/mobile-order-and-pay.html',
       orderLabel='Open app',
       near="McDonald's",
       url="https://www.mlb.com/angels/tickets/specials/homestand-partner-offers",
       check=mlb_rule(A, lambda g: g["won"], lambda g: "win", lambda g: "loss")),
  dict(id="baja", team="MLB", vendor="Mountain Dew", offer="Free 20 oz Baja Blast (5 per season)", how="Wallet pass from bajablasthomeruns.com", when="Any MLB homer of 420+ ft", follow=True, ends="2026-09-27",
       url="https://www.bajablasthomeruns.com", check=baja_blast),
  dict(id="firehouse", team="MLB", vendor="Firehouse Subs", offer="Buy one medium sub, get one free (Mon to Wed)", code="EXTRA", when="Any MLB game Fri to Sun goes to extras", follow=False,
       order='https://www.firehousesubs.com/',
       near='Firehouse Subs',
       url="https://www.firehousesubs.com/offer-terms", check=firehouse),
  dict(id="ono", team="LAFC", vendor="Ono Hawaiian BBQ", offer="$5.99 Chicken Plate Lunch", code="LAFCSCORES", when="LAFC scores first in the first half at home", follow=True,
       order='https://order.onohawaiianbbq.com/',
       near='Ono Hawaiian BBQ',
       url="https://onohawaiianbbq.com/lafc/", check=soccer(["LAFC", "Los Angeles FC"], lafc_scored_first)),
  dict(id="doordash-lafc", team="LAFC", vendor="DoorDash", offer="$10 off $20+", how="Code posted on LAFC's Instagram story", when="LAFC win", follow=True,
       order='https://www.doordash.com/',
       url="https://www.instagram.com/stories/lafc/", check=soccer(["LAFC", "Los Angeles FC"], lambda g: g["won"])),
  dict(id="norms", team="Galaxy", vendor="Norms", offer="Free short stack of hotcakes", how="NORMS Rewards, within 48 hours", when="Galaxy score at home", follow=False,
       order='https://norms.com/',
       near='Norms Restaurant',
       url="https://www.lagalaxy.com/norms", check=soccer(["LA Galaxy"], lambda g: g["home"] and int(g["me"].get("score") or 0) > 0, days_back=2)),
  dict(id="carls-sdfc", team="San Diego FC", vendor="Carl's Jr", offer="Free Western Bacon Cheeseburger with large drink", how="Code posted on SDFC socials, 10 AM to 1 PM", when="SDFC home win", follow=False,
       order='https://order.carlsjr.com/',
       near="Carl's Jr",
       url="https://www.instagram.com/stories/sandiegofc/", check=soccer(["San Diego FC"], lambda g: g["home"] and g["won"])),
  dict(id="carls-timbers", team="Timbers", vendor="Carl's Jr", offer="Free signature item, no purchase needed", how="Vote at timbers.com/carlsjr for the code", when="Timbers played", follow=False,
       order='https://order.carlsjr.com/',
       near="Carl's Jr",
       url="https://www.timbers.com/carlsjr", check=soccer(["Portland Timbers"], lambda g: True)),
  dict(id="carls-rams", team="Rams", vendor="Carl's Jr", offer="Free Famous Star with Cheese with large drink", code="RAMS8099", when="Rams get an interception", follow=True,
       order='https://order.carlsjr.com/',
       near="Carl's Jr",
       url="https://www.therams.com/game-day/promotions", check=rams_int),
  dict(id="mcd-ducks", team="Ducks", vendor="McDonald's", offer="$2 Sausage, Egg & Cheese McGriddles", how="McDonald's app, Rewards & Deals", when="Ducks home win", follow=False,
       order='https://www.mcdonalds.com/us/en-us/mobile-order-and-pay.html',
       orderLabel='Open app',
       near="McDonald's",
       url="https://www.nhl.com/ducks/fans/mcdonalds-ducks-win", check=ducks_home_win),
  dict(id="mcd-kings", team="Kings", vendor="McDonald's", offer="Free McFlurry with $2 purchase", how="McDonald's app, Rewards & Deals", when="Kings goal in the last minute of the 2nd", follow=True,
       order='https://www.mcdonalds.com/us/en-us/mobile-order-and-pay.html',
       orderLabel='Open app',
       near="McDonald's",
       url="https://www.mcdonalds.com/us/en-us.html", check=kings_late_2nd),
  dict(id="jitb-lakers", team="Lakers", vendor="Jack in the Box", offer="2 free tacos with any drink", code="LETSGOLAKERS", when="Lakers win", follow=True,
       order='https://www.jackinthebox.com/',
       near='Jack in the Box',
       url="https://www.jackinthebox.com/location", check=lakers_win),
  dict(id="doordash-nba", team="NBA", vendor="DoorDash", offer="50% off one order, up to $10 (DashPass)", code="NBA50", when="Any NBA player scores 50+", follow=False,
       order='https://www.doordash.com/',
       url="https://www.doordash.com/", check=nba_fifty),
]

def run(today):
    ctx = {"today": today, "y": today - timedelta(days=1)}
    results, errors = [], []
    for deal in DEALS:
        info = {k: v for k, v in deal.items() if k not in ("check",)}
        if deal.get("ends"):
            days_left = (date.fromisoformat(deal["ends"]) - today).days
            info["daysLeft"] = days_left
            info["endingSoon"] = 0 <= days_left <= 5
        if deal.get("ends") and ymd(ctx["y"]) > deal["ends"]:
            info.update(active=False, reason="Promo ended for the season")
        else:
            try:
                active, reason = deal["check"](ctx)
                info.update(active=bool(active), reason=reason)
            except Exception as e:
                info.update(active=None, reason="Couldn't check today")
                errors.append({"deal": deal["id"], "error": f"{type(e).__name__}: {e}",
                               "trace": traceback.format_exc(limit=3)})
        results.append(info)
    return {"updated": datetime.now(PT).isoformat(timespec="minutes"), "gameDate": ymd(ctx["y"]),
            "deals": results, "errors": errors}

def notify(topics_str, status, dry):
    hits = [d for d in status["deals"] if d["active"] and d["follow"]]
    ending = [d for d in status["deals"] if d.get("endingSoon") and (d["follow"] or d["active"])]
    lines = [f'- {d["vendor"]}: {d.get("code") or d.get("how")}' for d in hits]
    if ending:
        lines.append("")
        lines += [f'- {d["vendor"]} ends in {d["daysLeft"]} day{"s" if d["daysLeft"] != 1 else ""}' for d in ending]
    if not lines:
        print("Nothing to notify."); return
    n = len(hits)
    title = f'{n} food deal{"s" if n != 1 else ""} active today' if n else "Deals ending soon"
    body = "\n".join(lines)
    if dry or not topics_str:
        print(f"[notification preview] {title}\n{body}"); return
    headers = {"Title": title, "Tags": "hamburger", "Priority": "high"}
    if os.environ.get("PAGE_URL"): headers["Click"] = os.environ["PAGE_URL"]
    for topic in [t.strip() for t in topics_str.split(",") if t.strip()]:
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers, method="POST"), timeout=30)
    print("Sent to", topics_str.count(",") + 1, "topic(s):", title)

# ------------------------------------------------------------- UPCOMING ----
# Rough starting chances based on typical league rates. Once this tracker has
# 8+ games of its own history for a deal, it blends in the real hit rate.
BASE_RATES = {
    "panda": 0.33, "sunright": 0.33,        # home game AND win (home teams win ~55%)
    "jitb-dodgers": 0.62,                    # 7+ strikeouts in a game
    "mcd-dodgers": 0.32,                     # 6+ runs
    "habit": 0.55,                           # double play at home
    "ampm": 0.45,                            # stolen base at home
    "uber": 0.12,                            # away game with a save
    "elportal": 0.58, "conazucar": 0.58, "mcd-angels": 0.45,  # any win
    "ono": 0.30, "doordash-lafc": 0.48, "norms": 0.68, "carls-sdfc": 0.45,
    "carls-timbers": 1.0, "carls-rams": 0.55, "mcd-ducks": 0.42,
    "mcd-kings": 0.08, "jitb-lakers": 0.55,
    "baja": 0.93, "firehouse": 0.55, "doordash-nba": 0.05,
}
HOME_ONLY = {"panda", "sunright", "habit", "ampm", "ono", "norms", "carls-sdfc", "mcd-ducks"}
AWAY_ONLY = {"uber"}

def next_game_mlb(team_id, today):
    s = get(f"{MLB}/schedule?sportId=1&teamId={team_id}&startDate={ymd(today)}&endDate={ymd(today + timedelta(days=1))}")
    for day in s.get("dates", []):
        for g in day.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final" or g.get("gameType") not in ("R", "F", "D", "L", "W"):
                continue
            home = g["teams"]["home"]["team"]["id"] == team_id
            opp = g["teams"]["away" if home else "home"]["team"]["name"]
            return {"date": day["date"], "home": home, "opp": opp}
    return None

def next_game_nhl(abbrev, today):
    for i in range(2):
        d = today + timedelta(days=i)
        for g in get(f"{NHL}/score/{ymd(d)}").get("games", []):
            if g.get("gameType") not in (2, 3) or g.get("gameState") in ("OFF", "FINAL"): continue
            h, a = g["homeTeam"].get("abbrev"), g["awayTeam"].get("abbrev")
            if abbrev in (h, a):
                return {"date": ymd(d), "home": h == abbrev, "opp": a if h == abbrev else h}
    return None

def next_game_espn(path, names, today):
    for i in range(2):
        d = today + timedelta(days=i)
        for ev in espn_events(path, d):
            comp = ev["competitions"][0]
            if comp.get("status", ev.get("status", {})).get("type", {}).get("completed"): continue
            teams = comp["competitors"]
            me = next((t for t in teams if any(n.lower() in (t["team"].get("displayName","")+" "+t["team"].get("abbreviation","")).lower() for n in names)), None)
            if me:
                opp = next(t for t in teams if t is not me)
                return {"date": ymd(d), "home": me.get("homeAway") == "home", "opp": opp["team"].get("displayName")}
    return None

def league_has_games(path_or_mlb, today):
    for i in range(2):
        d = today + timedelta(days=i)
        if path_or_mlb == "mlb":
            s = get(f"{MLB}/schedule?sportId=1&date={ymd(d)}")
            if any(g.get("gameType") == "R" for day in s.get("dates", []) for g in day.get("games", [])):
                return ymd(d)
        elif espn_events(path_or_mlb, d):
            return ymd(d)
    return None

TEAM_NEXT = {
    "Dodgers": lambda t: next_game_mlb(119, t),
    "Angels": lambda t: next_game_mlb(108, t),
    "Kings": lambda t: next_game_nhl("LAK", t),
    "Ducks": lambda t: next_game_nhl("ANA", t),
    "Lakers": lambda t: next_game_espn("basketball/nba", ["Los Angeles Lakers"], t),
    "Rams": lambda t: next_game_espn("football/nfl", ["Los Angeles Rams"], t),
    "LAFC": lambda t: next_game_espn("soccer/usa.1", ["LAFC", "Los Angeles FC"], t),
    "Galaxy": lambda t: next_game_espn("soccer/usa.1", ["LA Galaxy"], t),
    "San Diego FC": lambda t: next_game_espn("soccer/usa.1", ["San Diego FC"], t),
    "Timbers": lambda t: next_game_espn("soccer/usa.1", ["Portland Timbers"], t),
}

def history_rate(hist, deal_id):
    played = [r for r in hist.get(deal_id, []) if r.get("played")]
    if len(played) < 8: return None, len(played)
    return sum(1 for r in played if r["active"]) / len(played), len(played)

def build_upcoming(status, hist, today):
    upcoming, errors = [], []
    next_cache = {}
    for d in status["deals"]:
        if d.get("ends") and ymd(today) > d["ends"]: continue
        base = BASE_RATES.get(d["id"])
        if base is None: continue
        try:
            if d["team"] in TEAM_NEXT:
                if d["team"] not in next_cache:
                    next_cache[d["team"]] = TEAM_NEXT[d["team"]](today)
                g = next_cache[d["team"]]
                if not g: continue
                if d["id"] in HOME_ONLY and not g["home"]: continue
                if d["id"] in AWAY_ONLY and g["home"]: continue
                game_date = g["date"]
                matchup = f'{d["team"]} {"vs" if g["home"] else "at"} {g["opp"]}'
            elif d["id"] == "baja":
                game_date = league_has_games("mlb", today)
                if not game_date: continue
                matchup = "Any MLB game"
            elif d["id"] == "doordash-nba":
                game_date = league_has_games("basketball/nba", today)
                if not game_date: continue
                matchup = "Any NBA game"
            elif d["id"] == "firehouse":
                wd = today.weekday()
                if wd not in (4, 5, 6): continue  # only matters Fri to Sun
                game_date = ymd(today)
                matchup = "Any MLB game this weekend"
            else:
                continue
        except Exception as e:
            errors.append({"deal": d["id"], "error": f"upcoming: {type(e).__name__}: {e}"})
            continue

        rate, n = history_rate(hist, d["id"])
        if rate is None:
            prob, basis = base, "Estimate from typical league rates"
        else:
            prob = (rate * n + base * 10) / (n + 10)
            basis = f"Blend of league rates and this tracker's last {n} games"
        deal_day = date.fromisoformat(game_date) + timedelta(days=1)
        if d["id"] == "firehouse":
            deal_day = today + timedelta(days=(7 - today.weekday()))  # next Monday
        upcoming.append({
            "id": d["id"], "vendor": d["vendor"], "team": d["team"], "offer": d["offer"], "when": d["when"],
            "gameDate": game_date, "dealDate": ymd(deal_day), "matchup": matchup,
            "prob": round(prob, 2), "basis": basis,
        })
    upcoming.sort(key=lambda u: (u["dealDate"], -u["prob"]))
    status["upcoming"] = upcoming
    status["errors"] += errors

def load_history():
    try:
        with open("docs/history.json") as f: return json.load(f)
    except FileNotFoundError:
        return {}

def update_history(status):
    hist = load_history()
    gd = status["gameDate"]
    for d in status["deals"]:
        rec = hist.setdefault(d["id"], [])
        rec[:] = [r for r in rec if r["date"] != gd]  # replace same-day entry on reruns
        played = d["active"] is not None and not str(d.get("reason", "")).startswith(("No game", "No match", "No NBA", "No MLB", "No extra", "Only redeemable", "Promo ended"))
        rec.append({"date": gd, "active": d["active"], "played": played})
        rec.sort(key=lambda r: r["date"])
        del rec[:max(0, len(rec) - 90)]  # keep 90 days
    with open("docs/history.json", "w") as f: json.dump(hist, f, indent=1)
    return hist

def attach_streaks(status, hist):
    today = date.fromisoformat(status["gameDate"])
    cutoff = today - timedelta(days=30)
    for d in status["deals"]:
        rec = hist.get(d["id"], [])
        actives = [r for r in rec if r["active"]]
        if actives:
            last = date.fromisoformat(actives[-1]["date"])
            d["lastActive"] = str(last)
            d["daysSinceActive"] = (today - last).days
        else:
            d["lastActive"] = None
            d["daysSinceActive"] = None
        d["recentCount"] = sum(1 for r in rec if r["active"] and date.fromisoformat(r["date"]) >= cutoff)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="Today's date YYYY-MM-DD (default: today, Pacific)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    today = date.fromisoformat(a.date) if a.date else datetime.now(PT).date()
    status = run(today)
    os.makedirs("docs", exist_ok=True)
    hist = update_history(status)
    attach_streaks(status, hist)
    build_upcoming(status, hist, today)
    with open("docs/status.json", "w") as f: json.dump(status, f, indent=1)
    for d in status["deals"]:
        mark = "ON " if d["active"] else ("?? " if d["active"] is None else "   ")
        print(mark, d["team"], "|", d["vendor"], "|", d["reason"])
    for e in status["errors"]: print("ERROR", e["deal"], e["error"])
    notify(os.environ.get("NTFY_TOPIC"), status, a.dry_run)
