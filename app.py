"""
Instagram Reel Comment Scraper - Web App (v4 - Discover + Scrape)
Two modes: (1) Discover reels by topic/hashtag, (2) Paste reel URL directly.
Uses Instagram's GraphQL API with session cookies.
"""
import io
import json
import os
import re
import csv
import time
import math
import uuid
import traceback
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, Response

import requests

app = Flask(__name__)

COMMENT_QUERY_HASH = "97b41c52301f77ce508f55e66d17620e"
HASHTAG_QUERY_HASH = "9b498c08113f1e09617a1703c22b2f32"

IG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "X-IG-App-ID": "936619743392459",
}


# ============================================================
# SHARED HELPERS
# ============================================================

def get_cookies(user_sessionid=None):
    if user_sessionid:
        return {"sessionid": user_sessionid}
    cookies_json = os.environ.get("IG_COOKIES", "")
    if cookies_json:
        try:
            return json.loads(cookies_json)
        except Exception:
            pass
    return {}


def extract_shortcode(url):
    url = url.strip()
    match = re.search(r'/(reel|reels|p)/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(2)
    if re.match(r'^[A-Za-z0-9_-]{5,20}$', url):
        return url
    raise ValueError("Could not extract shortcode from URL")


# ============================================================
# COMMENT SCRAPING (existing, unchanged logic)
# ============================================================

def fetch_comments_page(shortcode, first=50, after=None, cookies=None):
    if cookies is None:
        cookies = get_cookies()

    variables = {"shortcode": shortcode, "first": first}
    if after:
        variables["after"] = after

    url = f"https://www.instagram.com/graphql/query/?query_hash={COMMENT_QUERY_HASH}"
    params = {
        "query_hash": COMMENT_QUERY_HASH,
        "variables": json.dumps(variables),
    }

    headers = {**IG_HEADERS, "Referer": f"https://www.instagram.com/reel/{shortcode}/"}
    resp = requests.get(url, params=params, cookies=cookies, headers=headers, timeout=30)

    if resp.status_code == 429:
        raise Exception("Rate limited by Instagram. Please wait a few minutes.")
    if resp.status_code != 200:
        raise Exception(f"Instagram returned HTTP {resp.status_code}")

    data = resp.json()
    if data.get("status") == "fail":
        raise Exception(f"Instagram API error: {data.get('message', 'Unknown error')}")

    media = (data.get("data") or {}).get("shortcode_media")
    if not media:
        raise Exception("Could not find reel data. The session may have expired or the reel is private.")

    comment_edge = media.get("edge_media_to_parent_comment") or media.get("edge_media_to_comment")
    if not comment_edge:
        raise Exception("No comments found on this reel.")

    return comment_edge


def parse_comment_node(node, index):
    owner = node.get("owner", {})
    commenter = {
        "username": owner.get("username", ""),
        "user_id": owner.get("id", ""),
        "full_name": owner.get("full_name", ""),
        "is_verified": owner.get("is_verified", False),
        "profile_pic_url": owner.get("profile_pic_url", ""),
        "is_private": owner.get("is_private", False),
    }

    replies = []
    threaded = node.get("edge_threaded_comments", {})
    for re_node in threaded.get("edges", []):
        rn = re_node.get("node", {})
        ro = rn.get("owner", {})
        reply = {
            "comment_id": rn.get("id", ""),
            "text": rn.get("text", ""),
            "created_at": rn.get("created_at"),
            "created_at_utc": datetime.fromtimestamp(rn.get("created_at", 0), tz=timezone.utc).isoformat() if rn.get("created_at") else "",
            "likes_count": (rn.get("edge_liked_by") or {}).get("count", 0),
            "commenter": {
                "username": ro.get("username", ""),
                "user_id": ro.get("id", ""),
                "full_name": ro.get("full_name", ""),
                "is_verified": ro.get("is_verified", False),
            },
        }
        replies.append(reply)

    return {
        "index": index,
        "comment_id": node.get("id", ""),
        "text": node.get("text", ""),
        "created_at": node.get("created_at"),
        "created_at_utc": datetime.fromtimestamp(node.get("created_at", 0), tz=timezone.utc).isoformat() if node.get("created_at") else "",
        "likes_count": (node.get("edge_liked_by") or {}).get("count", 0),
        "commenter": commenter,
        "reply_count": threaded.get("count", len(replies)),
        "replies": replies,
    }


def scrape_all_comments(shortcode, max_comments=500, cookies=None):
    all_comments = []
    all_commenters = {}
    end_cursor = None
    page = 0
    total_reported = 0
    errors = []

    while True:
        if max_comments and len(all_comments) >= max_comments:
            break
        first = min(50, max_comments - len(all_comments)) if max_comments else 50

        try:
            comment_edge = fetch_comments_page(shortcode, first=first, after=end_cursor, cookies=cookies)
        except Exception as e:
            errors.append(f"Page {page + 1}: {str(e)}")
            if "Rate limited" in str(e) or "expired" in str(e).lower():
                time.sleep(10)
                try:
                    comment_edge = fetch_comments_page(shortcode, first=first, after=end_cursor, cookies=cookies)
                except Exception as e2:
                    errors.append(f"Retry failed: {str(e2)}")
                    break
            else:
                break

        total_reported = comment_edge.get("count", 0)
        edges = comment_edge.get("edges", [])
        page_info = comment_edge.get("page_info", {})

        for edge in edges:
            node = edge.get("node", {})
            comment = parse_comment_node(node, len(all_comments) + 1)
            all_comments.append(comment)

            c = comment["commenter"]
            if c["user_id"] and c["user_id"] not in all_commenters:
                all_commenters[c["user_id"]] = c

            for r in comment.get("replies", []):
                rc = r["commenter"]
                if rc["user_id"] and rc["user_id"] not in all_commenters:
                    all_commenters[rc["user_id"]] = rc

        if not page_info.get("has_next_page") or len(edges) == 0:
            break
        end_cursor = page_info.get("end_cursor")
        page += 1
        time.sleep(0.5)

    total_replies = sum(len(c.get("replies", [])) for c in all_comments)
    return {
        "reel": shortcode,
        "comment_count": len(all_comments),
        "reply_count": total_replies,
        "total_reported": total_reported,
        "unique_commenters": list(all_commenters.values()),
        "comments": all_comments,
        "pages_fetched": page + 1,
        "errors": errors,
    }


# ============================================================
# REEL DISCOVERY (NEW)
# ============================================================

def extract_hashtag_candidates(text):
    """Extract potential hashtag candidates from free text."""
    text = text.lower().strip()
    words = re.findall(r'[a-z0-9]+', text)
    stop = {
        'in', 'the', 'a', 'an', 'of', 'for', 'and', 'or', 'to', 'from', 'at',
        'by', 'with', 'about', 'on', 'is', 'are', 'was', 'were', 'be', 'been',
        'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
        'could', 'should', 'can', 'this', 'that', 'these', 'those', 'i', 'you',
        'he', 'she', 'it', 'we', 'they', 'my', 'your', 'his', 'her', 'its',
        'our', 'their', 'want', 'need', 'find', 'show', 'get', 'looking',
        'me', 'some', 'any', 'all', 'most', 'more', 'less', 'very', 'too',
        'so', 'just', 'only', 'also', 'but', 'not', 'no', 'yes', 'like',
        'where', 'what', 'when', 'how', 'who', 'which', 'why',
    }
    words = [w for w in words if w not in stop and len(w) >= 2]

    candidates = []
    seen = set()

    def add(w):
        if w not in seen and 2 <= len(w) <= 30:
            seen.add(w)
            candidates.append(w)

    # Most specific first: all words joined
    if len(words) >= 2:
        add(''.join(words))
    # Triples
    for i in range(len(words) - 2):
        add(words[i] + words[i + 1] + words[i + 2])
    # Pairs
    for i in range(len(words) - 1):
        add(words[i] + words[i + 1])
    # Singles
    for w in words:
        add(w)

    return candidates


def _parse_media_node_gql(node):
    """Parse a GraphQL media node into a reel dict."""
    shortcode = node.get("shortcode") or node.get("code", "")
    if not shortcode:
        return None
    caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
    caption_text = caption_edges[0]["node"]["text"] if caption_edges else ""
    owner = node.get("owner", {})
    taken_ts = node.get("taken_at_timestamp") or node.get("taken_at", 0)

    return {
        "shortcode": shortcode,
        "caption": caption_text[:400],
        "username": owner.get("username", ""),
        "full_name": owner.get("full_name", ""),
        "profile_pic_url": owner.get("profile_pic_url", ""),
        "like_count": (node.get("edge_liked_by") or {}).get("count", node.get("like_count", 0)),
        "comment_count": (node.get("edge_media_to_comment") or {}).get("count", node.get("comment_count", 0)),
        "play_count": node.get("video_view_count", node.get("play_count", 0)),
        "taken_at": taken_ts,
        "taken_at_iso": datetime.fromtimestamp(taken_ts, tz=timezone.utc).isoformat() if taken_ts else "",
        "thumbnail_url": node.get("display_url") or node.get("thumbnail_src", ""),
        "reel_url": f"https://www.instagram.com/reel/{shortcode}/",
    }


def fetch_hashtag_media(tag_name, cookies, first=50):
    """Fetch hashtag media using multiple strategies. Returns list of reel dicts."""
    tag = tag_name.lstrip("#").lower().strip()
    reels = []
    seen = set()

    def add_reel(r):
        if r and r["shortcode"] not in seen:
            r["hashtag"] = tag
            seen.add(r["shortcode"])
            reels.append(r)

    headers = {
        **IG_HEADERS,
        "Referer": f"https://www.instagram.com/explore/tags/{tag}/",
        "Accept": "application/json",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    # --- Strategy 1: GraphQL query_hash (legacy, may still work on some accounts) ---
    try:
        variables = json.dumps({"tag_name": tag, "first": first})
        url = f"https://www.instagram.com/graphql/query/?query_hash={HASHTAG_QUERY_HASH}&variables={variables}"
        resp = requests.get(url, cookies=cookies, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            hashtag = (data.get("data") or {}).get("hashtag")
            if hashtag:
                for edge_key in ("edge_hashtag_to_top_posts", "edge_hashtag_to_media"):
                    for edge in hashtag.get(edge_key, {}).get("edges", []):
                        node = edge.get("node", {})
                        if not node.get("is_video", False):
                            continue
                        add_reel(_parse_media_node_gql(node))
            if reels:
                return reels
    except Exception:
        pass

    # --- Strategy 2: Scrape hashtag page HTML for embedded JSON ---
    try:
        resp = requests.get(
            f"https://www.instagram.com/explore/tags/{tag}/",
            cookies=cookies, headers={**IG_HEADERS, "Accept": "text/html"}, timeout=15
        )
        if resp.status_code == 200 and "<script" in resp.text:
            # Extract media data from embedded JSON blobs
            # Pattern: "shortcode":"XXXX" ... "is_video":true
            import re as _re
            # Find all media nodes in the page
            pattern = _re.compile(
                r'"(shortcode|code)"\s*:\s*"([A-Za-z0-9_-]+)"[^}]*?"is_video"\s*:\s*(true)'
            )
            for m in pattern.finditer(resp.text):
                sc = m.group(2)
                if sc in seen:
                    continue
                # Extract surrounding context for more fields
                start = max(0, m.start() - 2000)
                end = min(len(resp.text), m.end() + 2000)
                chunk = resp.text[start:end]

                like_match = _re.search(r'"(?:edge_liked_by|like_count)"\s*:\s*(?:\{[^}]*"count"\s*:\s*(\d+)|"count"?\s*:\s*(\d+)|(\d+))', chunk)
                comment_match = _re.search(r'"(?:edge_media_to_comment|comment_count)"\s*:\s*(?:\{[^}]*"count"\s*:\s*(\d+)|"count"?\s*:\s*(\d+)|(\d+))', chunk)
                view_match = _re.search(r'"video_view_count"\s*:\s*(\d+)', chunk)
                taken_match = _re.search(r'"taken_at_timestamp"\s*:\s*(\d+)', chunk)
                owner_match = _re.search(r'"owner"\s*:\s*\{[^}]*"username"\s*:\s*"([^"]*)"', chunk)
                display_match = _re.search(r'"display_url"\s*:\s*"([^"]*)"', chunk)
                caption_match = _re.search(r'"text"\s*:\s*"([^"]{0,400})"', chunk)

                r = {
                    "shortcode": sc,
                    "caption": caption_match.group(1).encode().decode("unicode_escape")[:400] if caption_match else "",
                    "username": owner_match.group(1) if owner_match else "",
                    "full_name": "",
                    "profile_pic_url": "",
                    "like_count": int(like_match.group(1) or like_match.group(2) or like_match.group(3) or 0) if like_match else 0,
                    "comment_count": int(comment_match.group(1) or comment_match.group(2) or comment_match.group(3) or 0) if comment_match else 0,
                    "play_count": int(view_match.group(1)) if view_match else 0,
                    "taken_at": int(taken_match.group(1)) if taken_match else 0,
                    "taken_at_iso": datetime.fromtimestamp(int(taken_match.group(1)), tz=timezone.utc).isoformat() if taken_match else "",
                    "thumbnail_url": display_match.group(1) if display_match else "",
                    "reel_url": f"https://www.instagram.com/reel/{sc}/",
                    "hashtag": tag,
                }
                add_reel(r)
            if reels:
                return reels
    except Exception:
        pass

    # --- Strategy 3: Instagram's /api/v1/tags/ sections endpoint (mobile API) ---
    try:
        api_headers = {
            **IG_HEADERS,
            "Accept": "application/json",
            "X-IG-App-ID": "936619743392459",
            "Referer": f"https://www.instagram.com/explore/tags/{tag}/",
        }
        resp = requests.get(
            f"https://www.instagram.com/api/v1/tags/{tag}/sections/",
            cookies=cookies, headers=api_headers, timeout=15, allow_redirects=True
        )
        if resp.status_code == 200:
            data = resp.json()
            for section in data.get("sections", []):
                lc = section.get("layout_content", {})
                for media_item in lc.get("medias", []):
                    m = media_item.get("media", {})
                    if m.get("media_type") != 2:  # 2 = video/reel
                        continue
                    sc = m.get("code") or m.get("shortcode", "")
                    if not sc or sc in seen:
                        continue
                    cap = m.get("caption", {})
                    caption_text = cap.get("text", "") if isinstance(cap, dict) else str(cap)
                    owner = m.get("user", {})
                    taken_ts = m.get("taken_at", 0)
                    add_reel({
                        "shortcode": sc,
                        "caption": caption_text[:400],
                        "username": owner.get("username", ""),
                        "full_name": owner.get("full_name", ""),
                        "profile_pic_url": owner.get("profile_pic_url", ""),
                        "like_count": m.get("like_count", 0),
                        "comment_count": m.get("comment_count", 0),
                        "play_count": m.get("play_count", 0),
                        "taken_at": taken_ts,
                        "taken_at_iso": datetime.fromtimestamp(taken_ts, tz=timezone.utc).isoformat() if taken_ts else "",
                        "thumbnail_url": m.get("thumbnail_url") or m.get("image_versions", {}).get("candidates", [{}])[0].get("url", ""),
                        "reel_url": f"https://www.instagram.com/reel/{sc}/",
                        "hashtag": tag,
                    })
            if reels:
                return reels
    except Exception:
        pass

    return reels


@app.route("/api/discover", methods=["POST"])
def discover():
    """Search Instagram for reels by topic/hashtag. Returns ranked reel suggestions."""
    data = request.json or {}
    query = data.get("query", "").strip()
    hashtags_input = data.get("hashtags", "").strip()
    sessionid = data.get("sessionid", "").strip()

    if not sessionid:
        return jsonify({"error": "Instagram session ID is required."}), 400
    if not query and not hashtags_input:
        return jsonify({"error": "Please describe what you're looking for or enter hashtags."}), 400

    cookies = get_cookies(sessionid)

    # --- Build hashtag list ---
    tags_to_search = []

    # 1. User-provided explicit hashtags
    if hashtags_input:
        for h in re.split(r'[,\s]+', hashtags_input):
            h = h.strip().lstrip('#').lower()
            if h and h not in tags_to_search:
                tags_to_search.append(h)

    # 2. Extract candidates from free text query
    if query:
        candidates = extract_hashtag_candidates(query)
        for cand in candidates[:8]:
            if cand not in tags_to_search:
                tags_to_search.append(cand)

    # Limit to 5 hashtags to stay within timeout
    tags_to_search = tags_to_search[:5]

    if not tags_to_search:
        return jsonify({"error": "Could not determine hashtags from your query. Try entering hashtags manually."}), 200

    # --- Fetch reels for each hashtag ---
    all_reels = []
    seen_codes = set()
    hashtags_used = []

    for tag in tags_to_search:
        reels = fetch_hashtag_media(tag, cookies)
        if reels:
            hashtags_used.append(tag)
        for r in reels:
            if r["shortcode"] not in seen_codes:
                seen_codes.add(r["shortcode"])
                all_reels.append(r)
        time.sleep(0.4)

    if not all_reels:
        return jsonify({
            "hashtags_searched": tags_to_search,
            "total_found": 0,
            "reels": [],
            "message": "No reels found for these hashtags. Try different keywords or check your session ID.",
        })

    # --- Score and rank (mix of likes, comments, recency) ---
    max_likes = max((r["like_count"] for r in all_reels), default=1) or 1
    max_comments = max((r["comment_count"] for r in all_reels), default=1) or 1
    now = time.time()

    for r in all_reels:
        likes_norm = math.log10(r["like_count"] + 1) / math.log10(max_likes + 1)
        comments_norm = math.log10(r["comment_count"] + 1) / math.log10(max_comments + 1)
        days_old = max(0, (now - r["taken_at"]) / 86400) if r["taken_at"] else 365
        recency = math.exp(-days_old / 60)  # ~60-day half-life
        r["score"] = round(0.3 * likes_norm + 0.4 * comments_norm + 0.3 * recency, 4)

    all_reels.sort(key=lambda x: x["score"], reverse=True)
    top_reels = all_reels[:25]

    return jsonify({
        "hashtags_searched": hashtags_used or tags_to_search,
        "total_found": len(all_reels),
        "reels": top_reels,
    })


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def scrape():
    """Scrape comments and return ALL data as JSON for client-side filtering."""
    data = request.json or {}
    url = data.get("url", "").strip()
    max_comments = data.get("max_comments", 500)
    sessionid = data.get("sessionid", "").strip()

    if not url:
        return jsonify({"error": "Please provide a Reel URL."}), 400

    try:
        shortcode = extract_shortcode(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    cookies = get_cookies(sessionid if sessionid else None)
    if not cookies or not cookies.get("sessionid"):
        return jsonify({"error": "Instagram session required. Provide your sessionid cookie."}), 400

    try:
        result = scrape_all_comments(shortcode, max_comments=max_comments, cookies=cookies)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    comments_out = []
    for c in result["comments"]:
        replies_out = []
        for r in c.get("replies", []):
            replies_out.append({
                "username": r["commenter"]["username"],
                "full_name": r["commenter"].get("full_name", ""),
                "user_id": r["commenter"]["user_id"],
                "text": r["text"],
                "likes": r["likes_count"],
            })
        comments_out.append({
            "index": c["index"],
            "comment_id": c["comment_id"],
            "username": c["commenter"]["username"],
            "full_name": c["commenter"].get("full_name", ""),
            "user_id": c["commenter"]["user_id"],
            "verified": c["commenter"].get("is_verified", False),
            "text": c["text"],
            "likes": c["likes_count"],
            "created_at": c["created_at_utc"],
            "reply_count": len(c.get("replies", [])),
            "profile_url": f"https://www.instagram.com/{c['commenter']['username']}/",
            "replies": replies_out,
        })

    commenters_out = []
    seen = set()
    for c in result["unique_commenters"]:
        if c["username"] in seen:
            continue
        seen.add(c["username"])
        commenters_out.append({
            "username": c["username"],
            "user_id": c["user_id"],
            "full_name": c.get("full_name", ""),
            "verified": c.get("is_verified", False),
            "is_private": c.get("is_private", False),
            "profile_url": f"https://www.instagram.com/{c['username']}/",
        })

    return jsonify({
        "shortcode": shortcode,
        "total_comments": result["comment_count"],
        "total_replies": result["reply_count"],
        "total_reported": result["total_reported"],
        "unique_commenters": len(commenters_out),
        "pages_fetched": result["pages_fetched"],
        "errors": result.get("errors", []),
        "comments": comments_out,
        "commenters": commenters_out,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
