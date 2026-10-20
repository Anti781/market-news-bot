import os
import re
import time
import json
import feedparser
import requests
import yfinance as yf
import matplotlib.pyplot as plt
from google import genai

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

SEEN_FILE = "seen_news.json"
CHART_FILE = "chart.png"

RSS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
]

TICKERS = {
    "S&P 500": "^GSPC",
    "NASDAQ 100": "^NDX",
    "Bitcoin (BTC)": "BTC-USD",
    "Ethereum (ETH)": "ETH-USD"
}

def load_seen_news():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_seen_news(seen_list):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list[-300:], f, ensure_ascii=False, indent=2)

def get_market_data_and_chart():
    plt.style.use("dark_background")
    fig, axes = plt.subplots(2, 2, figsize=(12, 7))
    fig.patch.set_facecolor("#0d1117")
    axes = axes.flatten()

    summary_lines = []

    for idx, (name, symbol) in enumerate(TICKERS.items()):
        ax = axes[idx]
        ax.set_facecolor("#161b22")
        try:
            hist = yf.Ticker(symbol).history(period="5d", interval="1h")
            if len(hist) >= 2:
                closes = hist["Close"].dropna()
                first_price = closes.iloc[0]
                last_price = closes.iloc[-1]
                change = ((last_price - first_price) / first_price) * 100
                sign = "+" if change >= 0 else ""
                color = "#00ff88" if change >= 0 else "#ff4d4d"

                ax.plot(closes.index, closes.values, color=color, linewidth=2)
                ax.fill_between(
                    closes.index,
                    closes.values,
                    closes.min(),
                    color=color,
                    alpha=0.12
                )
                ax.set_title(
                    f"{name}: {last_price:,.2f} ({sign}{change:.2f}% за 5 дн.)",
                    fontsize=11,
                    color="white",
                    pad=8
                )
                ax.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
                ax.grid(color="#30363d", linestyle="--", linewidth=0.5, alpha=0.6)

                summary_lines.append(f"• {name}: {last_price:,.2f} ({sign}{change:.2f}%).")
        except Exception:
            ax.set_title(f"{name}: нет данных.", fontsize=11, color="gray")

    plt.tight_layout(pad=2.0)
    plt.savefig(CHART_FILE, dpi=150, facecolor=fig.get_facecolor())
    plt.close()

    return "\n".join(summary_lines)

def get_fresh_news(seen_links):
    fresh_items = []
    new_links = []

    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:6]:
            link = entry.get("link", entry.get("title", ""))
            if link and link not in seen_links and link not in new_links:
                title = entry.get("title", "").strip()
                summary = entry.get("summary", "")[:250].strip()
                fresh_items.append(f"- {title}: {summary}")
                new_links.append(link)

    return fresh_items[:8], new_links

def clean_telegram_html(text):
    text = text.replace("```html", "").replace("```", "").strip()
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return text

def generate_russian_post(prices, fresh_news):
    client = genai.Client(api_key=GEMINI_API_KEY)
    news_block = "\n".join(fresh_news)
    prompt = f"""
    Ты — шеф-редактор русскоязычного финансового Telegram-канала про S&P 500, NASDAQ, криптовалюты и макроэкономику США.

    Котировки на этот час:
    {prices}

    Новые англоязычные новости (ранее не публиковались):
    {news_block}

    Твоя задача:
    1. Выбери 3 самые значимые новости, влияющие на американский рынок акций, политику ФРС или крипторынок.
    2. Переведи их смысл на профессиональный русский финансовый язык (без машинного перевода и лишней воды).
    3. Составь компактный пост длиной ДО 850 СИМВОЛОВ:
       - Жирный заголовок (тег <b>).
       - Блок котировок.
       - 3 коротких абзаца с главными новостями и их влиянием на рынок.
       - Каждое предложение и каждый пункт обязательно заканчивай точкой.
    Используй ТОЛЬКО закрытые HTML-теги <b>...</b> и <i>...</i>. Не используй символы < и > вне этих тегов.
    """

    # Собираем список всех подходящих моделей (чтобы при перегрузке 503 сразу брать резервную)
    candidate_models = []
    for m in client.models.list():
        name = m.name.replace("models/", "")
        low = name.lower()
        if ("flash" in low or "pro" in low) and "2.5" not in low and "vision" not in low and "image" not in low and "tts" not in low and "audio" not in low:
            candidate_models.append(name)

    if not candidate_models:
        candidate_models = ["gemini-flash-latest", "gemini-flash-lite-latest"]

    print("Доступные модели для генерации:", candidate_models)

    # Пробуем модели по очереди (до 3 кругов с паузой при пиковой нагрузке 503)
    for round_num in range(3):
        for model_name in candidate_models:
            try:
                print(f"Попытка генерации через {model_name} (круг {round_num + 1})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response and response.text:
                    return clean_telegram_html(response.text)
            except Exception as e:
                print(f"Модель {model_name} временно недоступна: {e}")
                time.sleep(5)
        time.sleep(15)

    raise RuntimeError("Все модели Gemini временно перегружены.")

def send_post_with_chart(text):
    if len(text) <= 1024 and os.path.exists(CHART_FILE):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(CHART_FILE, "rb") as photo:
            res = requests.post(
                url,
                data={"chat_id": CHANNEL_ID, "caption": text, "parse_mode": "HTML"},
                files={"photo": photo},
                timeout=20
            )
        if not res.ok and "can't parse entities" in res.text:
            plain_text = re.sub(r"<[^>]+>", "", text)
            with open(CHART_FILE, "rb") as photo:
                res = requests.post(
                    url,
                    data={"chat_id": CHANNEL_ID, "caption": plain_text},
                    files={"photo": photo},
                    timeout=20
                )
    else:
        if os.path.exists(CHART_FILE):
            url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(CHART_FILE, "rb") as photo:
                requests.post(url_photo, data={"chat_id": CHANNEL_ID}, files={"photo": photo}, timeout=20)

        url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(
            url_msg,
            json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20
        )
        if not res.ok and "can't parse entities" in res.text:
            plain_text = re.sub(r"<[^>]+>", "", text)
            res = requests.post(
                url_msg,
                json={"chat_id": CHANNEL_ID, "text": plain_text, "disable_web_page_preview": True},
                timeout=20
            )

    print("Ответ от Telegram:", res.text)
    if not res.ok:
        raise RuntimeError(f"Ошибка доставки в Telegram: {res.text}")

if __name__ == "__main__":
    seen = load_seen_news()
    fresh_news, new_links = get_fresh_news(seen)

    if not fresh_news:
        print("Новых уникальных новостей пока нет, пропускаем выпуск.")
    else:
        prices = get_market_data_and_chart()
        post_text = generate_russian_post(prices, fresh_news)
        send_post_with_chart(post_text)
        save_seen_news(seen + new_links)
