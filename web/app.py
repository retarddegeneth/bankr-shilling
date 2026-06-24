import sqlite3
import os
import re
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import (
    Flask, g, render_template, request, redirect, url_for, flash, jsonify
)

app = Flask(__name__)
app.secret_key = "bankr-shilling-leaderboard"

DB_PATH = os.path.join(app.root_path, "bankr.db")

# X handles mapped to shiller user id
# We auto-insert a user row on first submission from a handle.


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            x_handle TEXT UNIQUE NOT NULL,
            display_name TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tweets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            tweet_url TEXT NOT NULL,
            posted_at TIMESTAMP,
            likes INTEGER NOT NULL DEFAULT 0,
            retweets INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            views INTEGER NOT NULL DEFAULT 0,
            quote_count INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT,
            entered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS daily_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payout_date DATE NOT NULL,
            rank INTEGER NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount_usd REAL NOT NULL,
            method TEXT NOT NULL DEFAULT 'manual',
            note TEXT
        );
        """
    )
    db.commit()


def extract_tweet_id(url: str) -> str | None:
    url = url.strip()
    # Patterns like https://x.com/.../status/123 or https://twitter.com/.../status/123
    m = re.search(r"(?:twitter\.com|x\.com)/[^/]+/status/(\d+)", url)
    if m:
        return m.group(1)
    # Bare id
    if re.fullmatch(r"\d{10,}", url):
        return url
    return None


def compute_score(likes, retweets, replies, views, quote_count=0, age_hours=1):
    # Avoid division by zero
    if views <= 0:
        views = max(likes + retweets + replies, 1)
    engagement_rate = (likes + 2 * retweets + replies + quote_count) / views
    # log smooth raw engagement
    raw = (likes + 2 * retweets + replies * 1.5 + quote_count * 2)
    log_raw = math.log1p(raw)
    age_penalty = 1.0 / (1.0 + age_hours / 24.0)
    # Simple quality heuristic: prefer higher ER and balanced interaction
    quality = (engagement_rate ** 0.5) * (1 + min(replies, 50) / 100)
    score = (0.6 * log_raw + 0.25 * quality + 0.15 * (1 if views > 500 else views / 500)) * age_penalty * 100
    return round(float(score), 4)


@app.before_request
def before():
    init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    if request.method == "POST":
        handle = request.form.get("x_handle", "").strip().lstrip("@")
        tweet_url = request.form.get("tweet_url", "").strip()
        likes = int(request.form.get("likes", "0") or "0")
        retweets = int(request.form.get("retweets", "0") or "0")
        replies = int(request.form.get("replies", "0") or "0")
        views = int(request.form.get("views", "0") or "0")
        quote_count = int(request.form.get("quote_count", "0") or "0")
        entered_at_raw = request.form.get("entered_at", "").strip()
        if not handle or not tweet_url:
            flash("Missing handle or tweet URL", "error")
            return redirect(url_for("index"))
        tweet_id = extract_tweet_id(tweet_url)
        if not tweet_id:
            flash("Could not parse tweet ID from that URL", "error")
            return redirect(url_for("index"))
        # Upsert user
        user_row = db.execute("SELECT * FROM users WHERE lower(x_handle)=lower(?)", (handle,)).fetchone()
        if user_row is None:
            db.execute(
                "INSERT INTO users (x_handle) VALUES (?)",
                (handle,),
            )
            db.commit()
            user_row = db.execute("SELECT * FROM users WHERE lower(x_handle)=lower(?)", (handle,)).fetchone()
        user_id = user_row["id"]
        # Parse entered_at
        if entered_at_raw:
            try:
                entered_at = datetime.strptime(entered_at_raw, "%Y-%m-%dT%H:%M")
                entered_at = entered_at.replace(tzinfo=None)
            except Exception:
                entered_at = datetime.now()
        else:
            entered_at = datetime.now()
        age_hours = max((datetime.now() - entered_at).total_seconds() / 3600.0, 0.1)
        score = compute_score(likes, retweets, replies, views, quote_count, age_hours)
        try:
            db.execute(
                """
                INSERT INTO tweets (tweet_id, user_id, tweet_url, posted_at,
                                    likes, retweets, replies, views, quote_count,
                                    entered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET
                    likes=excluded.likes,
                    retweets=excluded.retweets,
                    replies=excluded.replies,
                    views=excluded.views,
                    quote_count=excluded.quote_count,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    tweet_id,
                    user_id,
                    tweet_url,
                    entered_at.isoformat(),
                    likes,
                    retweets,
                    replies,
                    views,
                    quote_count,
                    entered_at.isoformat(),
                    datetime.now(),
                ),
            )
            db.commit()
        except Exception as e:
            flash(f"DB error: {e}", "error")
            return redirect(url_for("index"))
        flash(f"Inserted tweet for @{handle}", "success")
        return redirect(url_for("index"))

    # Leaderboard
    rows = db.execute(
        """
        SELECT u.id, u.x_handle, u.display_name,
               COUNT(t.id) AS tweet_count,
               COALESCE(SUM(t.likes),0) AS total_likes,
               COALESCE(SUM(t.retweets),0) AS total_retweets,
               COALESCE(SUM(t.replies),0) AS total_replies,
               COALESCE(SUM(t.views),0) AS total_views,
               COALESCE(AVG(
                 CASE
                   WHEN ? IS NOT NULL THEN
                     (t.likes + 2*t.retweets + 1.5*t.replies + 2*t.quote_count) * 1.0 / NULLIF(t.views,0)
                   ELSE (t.likes + 2*t.retweets + 1.5*t.replies + 2*t.quote_count) * 1.0 / NULLIF(t.views,0)
                 END
               ), 0) AS avg_er
        FROM users u
        LEFT JOIN tweets t ON t.user_id = u.id
        GROUP BY u.id
        ORDER BY total_retweets DESC, avg_er DESC
        LIMIT 100
        """,
        (datetime.now(),),
    ).fetchall()
    enriched = []
    # Recompute score per user with recency weighting on most recent tweet
    for r in rows:
        latest_ts = db.execute(
            "SELECT MAX(entered_at) AS mx FROM tweets WHERE user_id=?", (r["id"],)
        ).fetchone()["mx"]
        age_hours = 1
        if latest_ts:
            age_hours = max((datetime.now() - datetime.fromisoformat(latest_ts)).total_seconds() / 3600.0, 0.1)
        s = compute_score(r["total_likes"], r["total_retweets"], r["total_replies"], r["total_views"], age_hours=age_hours)
        enriched.append((r, s, latest_ts))
    enriched.sort(key=lambda x: x[1], reverse=True)
    top10 = enriched[:10]
    top10_with_rank = []
    for idx, (r, score, latest_ts) in enumerate(top10, start=1):
        top10_with_rank.append({
            "rank": idx,
            "id": r["id"],
            "x_handle": r["x_handle"],
            "display_name": r["display_name"],
            "tweet_count": r["tweet_count"],
            "total_likes": r["total_likes"],
            "total_retweets": r["total_retweets"],
            "total_replies": r["total_replies"],
            "total_views": r["total_views"],
            "avg_er": round(r["avg_er"] * 100, 3),
            "score": round(score, 2),
            "latest": latest_ts,
        })
    return render_template("index.html", leaderboard=top10_with_rank)


@app.route("/tweets")
def tweets_view():
    db = get_db()
    user_id = request.args.get("user_id", type=int)
    q = request.args.get("q", "").strip()
    if user_id:
        rows = db.execute(
            "SELECT t.*, u.x_handle FROM tweets t JOIN users u ON u.id=t.user_id WHERE t.user_id=? ORDER BY t.entered_at DESC",
            (user_id,),
        ).fetchall()
    elif q:
        rows = db.execute(
            "SELECT t.*, u.x_handle FROM tweets t JOIN users u ON u.id=t.user_id WHERE t.tweet_url LIKE ? OR t.tweet_id LIKE ? ORDER BY t.entered_at DESC LIMIT 200",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT t.*, u.x_handle FROM tweets t JOIN users u ON u.id=t.user_id ORDER BY t.entered_at DESC LIMIT 200"
        ).fetchall()
    return render_template("tweets.html", tweets=rows, q=q, user_id=user_id)


@app.route("/payments")
def payments_view():
    db = get_db()
    rows = db.execute(
        """
        SELECT p.*, u.x_handle
        FROM daily_payouts p
        JOIN users u ON u.id = p.user_id
        ORDER BY p.payout_date DESC, p.rank ASC
        LIMIT 200
        """
    ).fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("payments.html", payouts=rows, today=today)


@app.post("/payments/manual")
def payments_manual():
    db = get_db()
    data = request.get_json(force=True) or {}
    date_str = data.get("date", "").strip()
    rank = data.get("rank")
    amount = data.get("amount")
    note = data.get("note", "").strip()
    handle = data.get("handle", "").strip().lstrip("@")
    if not date_str or rank is None or amount is None or not handle:
        return jsonify({"ok": False, "error": "date, rank, amount, and handle are required"}), 400
    user_row = db.execute("SELECT id FROM users WHERE lower(x_handle)=lower(?)", (handle,)).fetchone()
    if not user_row:
        return jsonify({"ok": False, "error": "user not found"}), 404
    db.execute(
        "INSERT INTO daily_payouts (payout_date, rank, user_id, amount_usd, method, note) VALUES (?, ?, ?, ?, ?, ?)",
        (date_str, int(rank), user_row["id"], float(amount), "manual", note),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/delete_user", methods=["POST"])
def delete_user():
    db = get_db()
    user_id = request.form.get("user_id", type=int)
    if not user_id:
        flash("Missing user id", "error")
        return redirect(url_for("index"))
    db.execute("DELETE FROM tweets WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash("User deleted", "success")
    return redirect(url_for("index"))


@app.route("/leaderboard/data")
def leaderboard_data():
    db = get_db()
    rows = db.execute(
        """
        SELECT u.id, u.x_handle,
               COUNT(t.id) AS tweet_count,
               COALESCE(SUM(t.likes),0) AS total_likes,
               COALESCE(SUM(t.retweets),0) AS total_retweets,
               COALESCE(SUM(t.replies),0) AS total_replies,
               COALESCE(SUM(t.views),0) AS total_views
        FROM users u
        LEFT JOIN tweets t ON t.user_id = u.id
        GROUP BY u.id
        ORDER BY total_retweets DESC
        """
    ).fetchall()
    enriched = []
    for r in rows:
        latest_ts = db.execute(
            "SELECT MAX(entered_at) AS mx FROM tweets WHERE user_id=?", (r["id"],)
        ).fetchone()["mx"]
        age_hours = 1
        if latest_ts:
            age_hours = max((datetime.now() - datetime.fromisoformat(latest_ts)).total_seconds() / 3600.0, 0.1)
        s = compute_score(r["total_likes"], r["total_retweets"], r["total_replies"], r["total_views"], age_hours=age_hours)
        enriched.append({
            "x_handle": r["x_handle"],
            "tweet_count": r["tweet_count"],
            "total_likes": r["total_likes"],
            "total_retweets": r["total_retweets"],
            "total_replies": r["total_replies"],
            "total_views": r["total_views"],
            "score": round(s, 2),
        })
    enriched.sort(key=lambda x: x["score"], reverse=True)
    return jsonify(enriched[:50])


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
