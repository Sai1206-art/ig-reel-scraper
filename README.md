# Instagram Reel Comment Scraper

Extract all comments, replies, and commenter profiles from any Instagram Reel. Export to CSV or JSON.

## Features
- 📊 Scrape all comments with pagination (thousands of comments supported)
- ↩️ Threaded replies linked to parent comments
- 👤 Unique commenter profiles (username, user ID, name, verified status)
- 📄 Export to CSV (comments + commenters) or full JSON
- ⚡ Background job processing with progress tracking

## Setup

### Environment Variables
- `IG_COOKIES` — JSON-encoded Instagram session cookies (see below)
- `PORT` — Server port (default: 5000, set by Render automatically)

### Getting Instagram Session Cookies
1. Log into Instagram in your browser
2. Open Developer Tools → Application → Cookies → instagram.com
3. Copy the `sessionid`, `csrftoken`, and `ds_user_id` cookie values
4. Set `IG_COOKIES` env var: `{"sessionid":"...","csrftoken":"...","ds_user_id":"..."}`

## Local Development
```bash
pip install -r requirements.txt
export IG_COOKIES='{"sessionid":"...","csrftoken":"...","ds_user_id":"..."}'
python app.py
```

## Deploy
```bash
gunicorn app:app --timeout 300
```
