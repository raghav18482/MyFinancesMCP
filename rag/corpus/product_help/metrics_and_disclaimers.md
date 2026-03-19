# MyFinanceMCP product context

## P&L definitions

**Holding P&L** is unrealized profit or loss on demat holdings: current market value minus total amount invested at average buy price, per symbol and in aggregate.

**Day P&L** comes from the broker's open positions for the trading day (intraday and carry-forward positions as reported by Angel One).

**Overall P&L %** is total current value minus total invested, divided by total invested, expressed as a percentage.

## Data freshness

Portfolio numbers (LTP, holdings, funds) are live from Angel One SmartAPI at query time. News and sentiment may lag depending on the news provider and pipeline.

## Sentiment (FinBERT)

The app can label news articles as positive, neutral, or negative using a FinBERT-style model. Sentiment is indicative only and can be wrong on sarcasm or headlines.

## Disclaimer

Outputs are for education and organization. They are not investment, tax, or legal advice. You are responsible for your own trading decisions.
