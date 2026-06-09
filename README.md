# World Cup 2026 Sweepstake

A standalone web app for running a World Cup sweepstake with mates. Friends open
the link, add their own names, and the organiser draws the teams: the strongest N
teams (N = number of players) are handed out one each, the rest sit it out. Once
the tournament starts, the page shows who's still in, who's out, and a leaderboard
by how far each team has gone. Built to be thrown away after the tournament.

## Files
- `main.py` - the app (serves the page + a small JSON API + live scores)
- `index.html` - the page
- `teams.json` - the 48 teams, groups and seed ranks
- `requirements.txt` - what to install

## Settings (environment variables)
Set these wherever you host it:
- `DATABASE_URL` - your Supabase connection string (use the **Session pooler**, URI form). Required.
- `API_FOOTBALL_KEY` - your API-Football key. Optional, but needed for live scores.
- `ORGANISER_PIN` - the PIN that unlocks Draw and Reset. Defaults to `1966` if unset - change it.

## Run it locally (to try it before going live)
```bash
pip install -r requirements.txt
export DATABASE_URL="your-supabase-session-pooler-uri"
export API_FOOTBALL_KEY="your-key"
export ORGANISER_PIN="your-pin"
uvicorn main:app --reload
```
Open http://localhost:8000.

## How it works
- Share the link. Mates add their names (they can remove their own before the draw).
- When everyone's in, open **Organiser**, enter the PIN, and hit **Draw the teams**. That closes the list and reveals who got who.
- During the tournament the page updates itself: each team shows Still in / Out / Champion, and the order is by how far each team has reached. Scores refresh about every 15 minutes (kept low on purpose to stay inside API-Football's free 100-requests-a-day limit).
- **Reset everything** (PIN required) clears all names and the draw.

## Notes
- The shared data lives in Supabase, so nothing is lost if the app restarts.
- The app talks to the database with a connection string that has full access to that Supabase project - which is why it should live in its own project, not alongside anything important.
- Live scores degrade gracefully: if API-Football is unreachable or no key is set, the draw and leaderboard still work, just without live status.

## Tear it down afterwards
Stop the service, delete the host, and delete the Supabase project. Nothing left behind.
