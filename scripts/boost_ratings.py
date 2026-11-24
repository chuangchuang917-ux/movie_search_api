"""
scripts/boost_ratings.py
Automated Ratings Booster for GitHub Actions.
Safely queries OMDb API and Douban with Chrome TLS impersonation,
updating hami_movies_review.csv directly in the GitHub repository.
"""
import os
import sys
import time
import random
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

CSV_PATH = "hami_movies_review.csv"

# Load API keys from environment variables or fallback defaults
api_keys = [
    os.getenv("OMDB_API_KEY", "f2e23cb6"),
    os.getenv("OMDB_API_KEY_2", "e8819fae"),
    os.getenv("OMDB_API_KEY_3", "8d8ccf3e")
]
api_keys = [k.strip() for k in api_keys if k and k.strip()]

def clean_title(title):
    if not isinstance(title, str):
        return ""
    pattern = r"\s*[\(（](?:日語|國語|粵語|日語版|國語版|粵語版|數位修復|闔家歡樂版|院線版|一刀未剪版|日|國|粵|國\s*/\s*日|雙語|25周年4K數位修復版|數位修復版|4K修復版)[\)）]\s*$"
    return re.sub(pattern, "", title, flags=re.IGNORECASE).strip().rstrip("-").strip()

def main():
    if not os.path.exists(CSV_PATH):
        print(f"Error: {CSV_PATH} not found in working directory.")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    total_len = len(df)
    target_80 = int(total_len * 0.8)

    print("==================================================")
    print("🚀 HAMI MOVIE REVIEW CLOUD RATINGS BOOSTER")
    print(f"Total Movies: {total_len:,} | 80% Target Threshold: {target_80:,}")
    print("==================================================")

    douban_valid = df["douban_rating"].notna().sum()
    rt_valid = df["rt_tomatometer"].notna().sum()
    mc_valid = df["metacritic_rating"].notna().sum() if "metacritic_rating" in df.columns else 0
    imdb_valid = df["imdb_rating"].notna().sum()

    print(f"Current Status:")
    print(f"  - IMDb Ratings:       {imdb_valid:,} / {total_len:,} ({imdb_valid/total_len*100:.1f}%)")
    print(f"  - Douban Ratings:     {douban_valid:,} / {total_len:,} ({douban_valid/total_len*100:.1f}%) [Gap to 80%: {max(0, target_80 - douban_valid):,}]")
    print(f"  - Rotten Tomatoes:    {rt_valid:,} / {total_len:,} ({rt_valid/total_len*100:.1f}%) [Gap to 80%: {max(0, target_80 - rt_valid):,}]")
    print(f"  - Metacritic Ratings: {mc_valid:,} / {total_len:,} ({mc_valid/total_len*100:.1f}%)")

    # ---------------------------------------------------------
    # PHASE 1: OMDb Parallel Lookup (Rotten Tomatoes & Metacritic)
    # ---------------------------------------------------------
    print("\n--------------------------------------------------")
    print("PHASE 1: OMDb Parallel Lookup (Rotten Tomatoes & Metacritic)")
    print("--------------------------------------------------")
    mask = (
        df["imdb_id"].notna() &
        df["imdb_id"].astype(str).str.startswith("tt") &
        (df["rt_tomatometer"].isna() | df["metacritic_rating"].isna())
    )
    omdb_indices = df[mask].index.tolist()
    print(f"Candidates with IMDb ID missing RT or MC: {len(omdb_indices):,}")

    # Maximum 2,800 requests to stay safely below daily limits
    max_requests = min(len(omdb_indices), len(api_keys) * 950)
    tasks = [(idx, str(df.loc[idx, "imdb_id"]).strip(), i) for i, idx in enumerate(omdb_indices[:max_requests])]
    print(f"Queued OMDb queries for this run: {len(tasks):,}")

    def fetch_omdb(args):
        idx, imdb_id, worker_idx = args
        key = api_keys[worker_idx % len(api_keys)]
        url = f"http://www.omdbapi.com/?i={imdb_id}&apikey={key}"
        res = {"idx": idx, "imdb_id": imdb_id, "mc": None, "rt": None, "imdb": None}
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    mc = data.get("Metascore")
                    if mc and mc != "N/A":
                        res["mc"] = f"{mc}/100"
                    for rating in data.get("Ratings", []):
                        src = rating.get("Source", "")
                        val = rating.get("Value", "")
                        if "Rotten Tomatoes" in src and val:
                            res["rt"] = val
                        elif "Metacritic" in src and val and not res["mc"]:
                            res["mc"] = val
                    imdb_r = data.get("imdbRating")
                    if imdb_r and imdb_r != "N/A":
                        try:
                            res["imdb"] = float(imdb_r)
                        except ValueError:
                            pass
        except Exception:
            pass
        return res

    filled_rt, filled_mc = 0, 0
    if tasks:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(fetch_omdb, t) for t in tasks]
            for done, future in enumerate(as_completed(futures)):
                res = future.result()
                idx = res["idx"]
                if res.get("rt") and pd.isna(df.at[idx, "rt_tomatometer"]):
                    df.at[idx, "rt_tomatometer"] = res["rt"]
                    filled_rt += 1
                if res.get("mc") and pd.isna(df.at[idx, "metacritic_rating"]):
                    df.at[idx, "metacritic_rating"] = res["mc"]
                    filled_mc += 1
                if (done + 1) % 100 == 0 or (done + 1) == len(tasks):
                    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
                    print(f"  [OMDb Progress: {done+1}/{len(tasks)}] RT added: {filled_rt} | MC added: {filled_mc}")

    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print(f"PHASE 1 Complete! Rotten Tomatoes added: {filled_rt}, Metacritic added: {filled_mc}\n")

    # ---------------------------------------------------------
    # PHASE 2: Douban Ratings Scraper (Chrome TLS Impersonation)
    # ---------------------------------------------------------
    print("--------------------------------------------------")
    print("PHASE 2: Douban Ratings Scraper (Chrome TLS Impersonation)")
    print("--------------------------------------------------")
    douban_missing = df[df["douban_rating"].isna()].index.tolist()
    print(f"Movies missing Douban rating: {len(douban_missing):,}")

    session = cffi_requests.Session()
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.7",
    }

    def get_douban_page(url):
        for attempt in range(2):
            try:
                r = session.get(url, headers=headers, impersonate="chrome124", timeout=15)
                if r.status_code == 200:
                    return r.text
                elif r.status_code in [403, 418, 429]:
                    print(f"\n[Douban Cooldown HTTP {r.status_code}] Sleeping 60s...")
                    time.sleep(60)
            except Exception:
                time.sleep(2)
        return None

    def search_douban(imdb_id, title_zh):
        if imdb_id and isinstance(imdb_id, str) and imdb_id.startswith("tt"):
            html = get_douban_page(f"https://www.douban.com/search?cat=1002&q={imdb_id}")
            if html:
                soup = BeautifulSoup(html, "html.parser")
                res = soup.select_one(".result")
                if res:
                    num = res.select_one(".rating_nums")
                    if num and num.text.strip():
                        try:
                            return float(num.text.strip())
                        except ValueError:
                            pass
        c_zh = clean_title(title_zh)
        if c_zh:
            time.sleep(random.uniform(1.2, 2.0))
            q = urllib.parse.quote(c_zh)
            html = get_douban_page(f"https://www.douban.com/search?cat=1002&q={q}")
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for res in soup.select(".result")[:2]:
                    num = res.select_one(".rating_nums")
                    if num and num.text.strip():
                        try:
                            return float(num.text.strip())
                        except ValueError:
                            pass
        return None

    added_douban = 0
    # Process up to 500 Douban items per workflow run to avoid timeouts
    max_douban_per_run = min(len(douban_missing), 500)
    print(f"Processing batch of {max_douban_per_run} movies for Douban...")

    for count, idx in enumerate(douban_missing[:max_douban_per_run]):
        r = df.loc[idx]
        score = search_douban(r.get("imdb_id"), r.get("電影中文名稱"))
        if score is not None:
            df.at[idx, "douban_rating"] = score
            added_douban += 1
            print(f"  + [{score}] {r.get('電影中文名稱')}")

        if (count + 1) % 25 == 0:
            df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
            cur_db = df["douban_rating"].notna().sum()
            print(f"  [Douban Progress: {count+1}/{max_douban_per_run}] Added: {added_douban} | Total Douban: {cur_db}/{total_len} ({cur_db/total_len*100:.1f}%)")
            if cur_db >= target_80:
                print("🎉 Target reached! Douban rating is at or above 80%!")
                break

    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print("\n==================================================")
    print("✅ CLOUD BOOSTER FINISHED! UPDATED STATS:")
    for col in ["imdb_rating", "rt_tomatometer", "douban_rating", "metacritic_rating"]:
        valid = df[col].notna().sum()
        print(f"  - {col}: {valid:,}/{total_len:,} ({valid/total_len*100:.1f}%)")
    print("==================================================")

if __name__ == "__main__":
    main()
