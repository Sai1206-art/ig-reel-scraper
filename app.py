"""
Instagram Reel Comment Scraper - Web App (v2 - Synchronous mode)
Uses Instagram's GraphQL API with session cookies to scrape comments.
No instaloader dependency — just requests + Flask.
"""
import io
import json
import os
import re
import csv
import time
import uuid
import traceback
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, send_file, Response

import requests

app = Flask(__name__)

# --- Global state for background scraping jobs (kept for compat) ---
jobs = {}
jobs_lock = __import__("threading").Lock()

# Instagram GraphQL query hash for comments
COMMENT_QUERY_HASH = "97b41c52301f77ce508f55e66d17620e"

# Default cookies (from environment variable, JSON-encoded)
DEFAULT_COOKIES = {}

def get_cookies(user_sessionid=None):
    """Get Instagram session cookies from env, user input, or defaults."""
    if user_sessionid:
        return {"sessionid": user_sessionid}
    cookies_json = os.environ.get("IG_COOKIES", "")
    if cookies_json:
        try:
            return json.loads(cookies_json)
        except:
            pass
    return DEFAULT_COOKIES.copy()


def extract_shortcode(url):
    """Extract shortcode from Instagram reel URL."""
    url = url.strip()
    match = re.search(r'/reels?/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(1)
    if re.match(r'^[A-Za-z0-9_-]{5,20}$', url):
        return url
    raise ValueError("Could not extract shortcode from URL")


def fetch_comments_page(shortcode, first=50, after=None, cookies=None):
    """Fetch a single page of comments from Instagram's GraphQL API."""
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

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"https://www.instagram.com/reel/{shortcode}/",
        "X-IG-App-ID": "936619743392459",
    }

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
    """Parse a single comment node into a flat dict."""
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


def scrape_all_comments(shortcode, max_comments=10000, cookies=None, job_id=None):
    """Scrape all comments from a reel, paginating through GraphQL."""
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

        # Update job progress if running in background mode
        if job_id:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["comments_fetched"] = len(all_comments)
                    jobs[job_id]["total_reported"] = total_reported
                    jobs[job_id]["pages_done"] = page + 1

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


def generate_comments_csv(result):
    """Generate CSV with all comments and replies."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "index", "type", "comment_id", "parent_comment_id",
        "username", "full_name", "user_id", "verified",
        "text", "likes", "created_at", "reply_count",
        "profile_url"
    ])

    for c in result["comments"]:
        writer.writerow([
            c["index"], "comment", c["comment_id"], "",
            c["commenter"]["username"], c["commenter"]["full_name"],
            c["commenter"]["user_id"], c["commenter"]["is_verified"],
            c["text"], c["likes_count"], c["created_at_utc"],
            c["reply_count"],
            f"https://www.instagram.com/{c['commenter']['username']}/"
        ])
        for i, r in enumerate(c.get("replies", [])):
            writer.writerow([
                f"{c['index']}.{i+1}", "reply", r["comment_id"], c["comment_id"],
                r["commenter"]["username"], r["commenter"].get("full_name", ""),
                r["commenter"]["user_id"], r["commenter"].get("is_verified", False),
                r["text"], r["likes_count"], r.get("created_at_utc", ""),
                "", ""
            ])

    output.seek(0)
    return output.getvalue()


def generate_commenters_csv(result):
    """Generate CSV with unique commenter profiles."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "username", "user_id", "full_name", "verified",
        "is_private", "profile_url", "profile_pic_url"
    ])

    seen = set()
    for c in result["unique_commenters"]:
        if c["username"] in seen:
            continue
        seen.add(c["username"])
        writer.writerow([
            c["username"], c["user_id"], c.get("full_name", ""),
            c.get("is_verified", False), c.get("is_private", False),
            f"https://www.instagram.com/{c['username']}/",
            c.get("profile_pic_url", "")
        ])

    output.seek(0)
    return output.getvalue()


# --- Synchronous scrape result storage (in-memory, per request) ---
# We store results keyed by a short ID so the download endpoints work
_results_store = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def scrape():
    """Scrape comments synchronously and return results directly."""
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

    # Store result for download endpoints
    result_id = str(uuid.uuid4())[:8]
    _results_store[result_id] = result

    # Build preview
    preview_comments = []
    for c in result["comments"][:50]:
        preview_comments.append({
            "index": c["index"],
            "username": c["commenter"]["username"],
            "full_name": c["commenter"].get("full_name", ""),
            "verified": c["commenter"].get("is_verified", False),
            "text": c["text"],
            "likes": c["likes_count"],
            "created_at": c["created_at_utc"],
            "reply_count": len(c.get("replies", [])),
            "profile_url": f"https://www.instagram.com/{c['commenter']['username']}/",
            "replies": [
                {
                    "username": r["commenter"]["username"],
                    "text": r["text"],
                    "likes": r["likes_count"],
                }
                for r in c.get("replies", [])[:5]
            ]
        })

    return jsonify({
        "result_id": result_id,
        "shortcode": shortcode,
        "total_comments": result["comment_count"],
        "total_replies": result["reply_count"],
        "total_reported": result["total_reported"],
        "unique_commenters": len(result["unique_commenters"]),
        "pages_fetched": result["pages_fetched"],
        "errors": result.get("errors", []),
        "comments_preview": preview_comments,
        "has_more": result["comment_count"] > 50,
    })


@app.route("/api/results/<result_id>/download/<filetype>")
def download(result_id, filetype):
    """Download results as CSV or JSON."""
    result = _results_store.get(result_id)
    if not result:
        return jsonify({"error": "Results expired. Please scrape again."}), 404

    shortcode = result["reel"]

    if filetype == "comments":
        csv_data = generate_comments_csv(result)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=ig_{shortcode}_comments.csv"}
        )
    elif filetype == "commenters":
        csv_data = generate_commenters_csv(result)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=ig_{shortcode}_commenters.csv"}
        )
    elif filetype == "json":
        json_data = json.dumps(result, indent=2, ensure_ascii=False)
        return Response(
            json_data,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=ig_{shortcode}_full.json"}
        )
    else:
        return jsonify({"error": "Invalid file type"}), 400


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
