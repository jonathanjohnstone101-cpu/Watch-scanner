# Watch deal scanner

Searches eBay UK and Vinted UK for the watches in `watches.json` at or under your max price and sends new listings to Telegram.

## Setup

1. **Telegram bot:** message `@BotFather` in Telegram, send `/newbot`, and copy the token. Then message your new bot once and open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat.id`.
2. **eBay keys:** sign up at https://developer.ebay.com, create a *Production* keyset and copy the App ID (Client ID) and Cert ID (Client Secret).
3. Put your chat id in `watches.json` (`telegram_chat_id`) and set the environment variables `TELEGRAM_BOT_TOKEN`, `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.
4. Allow network access to `www.vinted.co.uk`, `api.ebay.com` and `api.telegram.org`.

## Run

```
python3 scanner.py --test-alert   # check Telegram works
python3 scanner.py --dry-run      # print matches without sending
python3 scanner.py                # scan + alert (run hourly)
```

## Hourly runs on GitHub Actions

`.github/workflows/scan.yml` runs the scanner every hour. It needs the repository secrets `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` and `TELEGRAM_BOT_TOKEN` (Settings → Secrets and variables → Actions). To run it straight away, open the Actions tab, pick **Watch scan** and press **Run workflow**.

## Notes

- Only listings posted in the last `lookback_minutes` are checked. The window is longer than an hour because GitHub can run scheduled jobs late. `seen.json` stops the same listing being alerted twice.
- eBay results are Buy It Now / Best Offer only. Auction prices change, so they're excluded.
- Vinted is off by default (`"sites": ["ebay"]` in `watches.json`) because Vinted blocks cloud servers. For Vinted, use a saved search with notifications in the Vinted app. It can be switched on (`"sites": ["ebay", "vinted"]`) when running from a home connection, but it relies on an unofficial endpoint.
- Facebook Marketplace isn't included: it has no API and blocks bots. Use saved-search alerts in the Facebook app instead.
- Before alerting on an eBay listing, the scanner reads its full description and item specifics. It skips parts ("dial only", "fits a … movement", "spares or repairs"), and, for watches whose exclude list has "ladies" or "plated", listings marked Department: Women or with a gold-plated case. Skipped listings are logged with the reason.
- Always check authenticity (serials, movement photos, seller history) before buying.

# Gold weekly outlook

`gold_outlook.py` sends a gold forecast for the coming week to the same Telegram chat every Sunday. It gives a verdict (**bullish**, **bearish** or **consolidation**), a score from −100 to +100 and a confidence level. The score is built from three parts:

| Part | Weight | What it looks at |
|---|---|---|
| Technical | 35% | Gold futures vs. its 20/50/200-day averages, 1-week and 1-month returns, RSI, ADX trend strength, Bollinger band squeeze |
| Macro | 35% | US dollar index, US 10-year real yield (FRED), VIX, S&P 500, silver, oil |
| News | 30% | The past 7 days of headlines from Google News searches (Fed, inflation, jobs, war, tariffs, central bank gold buying…) plus BBC, Guardian, Al Jazeera, NYT, CNBC, MarketWatch, FXStreet and Investing.com |

A weak or mixed score, or a squeezed, trendless market, comes out as **consolidation**. The report also gives the 20-day range and the typical weekly move.

**News scoring:** with no key, a keyword model scores the headlines (for example "rate cut", "dollar slides" and "central bank buys gold" count as bullish; "no rate cut", "yields climb" and "ceasefire" count as bearish). It's rough. For a much better read, add an `ANTHROPIC_API_KEY` repository secret (from https://console.anthropic.com). Claude then reads every headline and writes a short summary, the bullish and bearish drivers, and what to watch next week. It's one request a week and costs pennies.

```
python3 gold_outlook.py --dry-run   # print the report without sending
python3 gold_outlook.py --no-ai     # force the keyword news model
python3 gold_outlook.py             # build and send to Telegram
```

`.github/workflows/gold.yml` runs it on Sundays at 17:23 UTC. It uses the `TELEGRAM_BOT_TOKEN` secret you already have, plus `ANTHROPIC_API_KEY` if you add it. To try it now, open Actions → **Gold outlook** → **Run workflow**. Feeds, search topics and weights are in `gold_config.json`.

Prices are Friday's close (markets are shut on Sunday). This isn't financial advice: the scores are rules-based and can be wrong, especially around big data releases.
