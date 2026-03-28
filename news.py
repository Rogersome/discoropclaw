import feedparser
import time
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

analyzer = SentimentIntensityAnalyzer()

NEWS_FEEDS = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "MarketWatch":   "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
}

def fetch_news(max_per_feed=5):
    articles = []
    for source, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "").strip()
                if title:
                    articles.append({"source": source, "title": title})
        except Exception as e:
            print(f"Feed error ({source}): {e}")
    return articles

def analyze_sentiment(articles):
    scores = [analyzer.polarity_scores(a["title"])["compound"] for a in articles]
    if not scores:
        return 0, "➡️ Neutral"
    avg = sum(scores) / len(scores)
    if avg >= 0.05:    trend = "📈 Bullish"
    elif avg <= -0.05: trend = "📉 Bearish"
    else:              trend = "➡️ Neutral"
    return round(avg, 3), trend

def build_daily_report(articles, sentiment_score, trend, performance):
    headlines = "\n".join(f"• [{a['source']}] {a['title']}" for a in articles[:10])
    return f"""📰 Daily Crypto & Market Report
{time.strftime('%Y-%m-%d')}

🔍 Sentiment: {sentiment_score} → {trend}

📊 Performance Summary
Total Trades : {performance['total_trades']}
Wins / Losses: {performance['wins']} / {performance['losses']}
Win Rate     : {performance['win_rate']}%
Total PnL    : ${performance['total_pnl']:+.2f}

Top Headlines:
{headlines}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC"""
