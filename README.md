# Western Housing Hunt (self-running)

Every day a GitHub Action downloads the listing sites, has a free Gemini model pull the listings out into structured data,
merges them with yesterday (new, price drops, gone) and commits `public/data/*.json`. Vercel serves the site.
Votes, comments, tags and your priorities live in Supabase so you and your friend see the same thing.
You do not need to be involved after setup.

## One-time setup (about 20 minutes)

1. **GitHub.** Create a new private repo and push this folder to it.
2. **Free API key.** Go to https://aistudio.google.com/apikey and create a key with a Google account. No card needed.
   In the repo: Settings > Secrets and variables > Actions > New repository secret, name `GEMINI_API_KEY`.
   Do not enable billing on that Google project, then it cannot charge you.
3. **First run.** Actions tab > "Daily listing refresh" > Run workflow. Check the log. It then runs daily by itself.
4. **Supabase (shared votes).** Create a free project at supabase.com. SQL editor: paste `supabase/schema.sql` and run it.
   Project settings > API: copy the Project URL and the `anon` public key into `public/config.js`, commit.
   Skip this step to try the site solo first. Votes are then saved in your browser only.
5. **Vercel.** Import the GitHub repo. Framework preset "Other". It reads `vercel.json` and serves `public/`.
   Each daily data commit redeploys the site. Send your friend the link, they type their name on first visit.

## Run it yourself
```
pip install -r requirements.txt
GEMINI_API_KEY=... python scripts/refresh.py --dry            # print what it found, write nothing
GEMINI_API_KEY=... python scripts/refresh.py --only "Zumper"   # one source
python scripts/test_refresh.py                                    # offline logic tests, no key needed
cd public && python -m http.server 8000                           # view the site
```

## Change what it searches
Edit `config.json`: add a site under `sources` (any page that lists rentals works, the extraction is done by the AI model
so you do not write scrapers). Remove a source by deleting its entry.

## Cost and limits
Costs nothing on the Gemini free tier. A run uses about 40 requests (5 apartment pages, up to 21 board pages, up to 15 detail pages).
Google changes free quotas from time to time. If the daily quota runs out the script stops early, keeps what it has,
and never marks listings gone from a source it did not finish. `maxRequestsPerRun` and `llmDelaySeconds` in `config.json` tune this.
Free tier inputs may be used by Google to improve its products, which is fine here because listing pages are public.
To use Claude instead set `"provider": "anthropic"`, a Claude model name and an `ANTHROPIC_API_KEY` secret (that one is paid).

## Known limits
- Plain HTTP fetch only. A site that needs JavaScript to show listings, or blocks datacenter IPs, returns nothing.
  The run log shows which source failed and the others still update. Rentals.ca and Kijiji are the likely ones.
- Distances to campus only appear when the listing states one. Nothing is guessed.
- Supabase policies are open to anyone with the key, so keep the site link private and store nothing sensitive.
- If the Action is stuck after 60 days of repo inactivity, GitHub pauses scheduled workflows. Push any commit to wake it.
