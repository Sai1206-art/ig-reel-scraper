"""
Instagram Reel Comment Scraper - Web App (v3 - Smart Filtering)
Uses Instagram's GraphQL API with session cookies to scrape comments.
Returns all comments as JSON for client-side smart filtering and CSV generation.
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
from flask import Flask, render_template, request, jsonify, Response

import requests

app = Flask(__name__)

COMMENT_QUERY_HASH = "97b41c52301f77ce508f55e66d17620e"


def get_cookies(user_sessionid=None):
    if user_sessionid:
        return {"sessionid": user_sessionid}
    cookies_json = os.environ.get("IG_COOKIES", "")
    if cookies_json:
        try:
            return json.loads(cookies_json)
        except:
            pass
    return {}


def extract_shortcode(url):
    url = url.strip()
    match = re.search(r'/reels?/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(1)
    if re.match(r'^[A-Za-z0-9_-]{5,20}$', url):
        return url
    raise ValueError("Could not extract shortcode from URL")


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

    # Build compact comment list for client-side processing
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

    # Build commenters list
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
