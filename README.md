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
