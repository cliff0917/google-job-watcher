import json
import os
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

JOBS_URL = (
    "https://www.google.com/about/careers/applications/"
    'jobs/results?q=%22Software%20Engineer%22&location=Taiwan&target_level=MID'
)
STATE_FILE = Path("jobs.json")
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]


def load_jobs():
    if not STATE_FILE.exists():
        return []
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_jobs(jobs):
    STATE_FILE.write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_job(job):
    if isinstance(job, str):
        return {
            "id": job,
            "title": job,
            "link": JOBS_URL,
        }
    return {
        "id": job.get("id") or job.get("title"),
        "title": job.get("title", ""),
        "link": job.get("link", JOBS_URL),
    }


def fetch_jobs():
    jobs = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(JOBS_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        headings = page.locator("h3")
        count = headings.count()
        print(f"Found {count} h3 headings")

        for i in range(count):
            heading = headings.nth(i)

            try:
                title = heading.inner_text(timeout=3000).strip()
            except Exception:
                continue

            if "Software Engineer" not in title:
                continue
            if "Senior" in title: # custom filter to exclude senior roles
                continue
            if "," not in title:
                continue
            if "Equal opportunity" in title:
                continue
            if title in seen:
                continue

            # print(f"Trying title: {title}")

            link = JOBS_URL

            try:
                learn_more = heading.locator("xpath=following::a[1]")
                href = learn_more.get_attribute("href")

                # print("href:", href)

                if href:
                    link = urljoin(
                        "https://www.google.com/about/careers/applications/",
                        href,
                    )
                    link = link.split("?")[0]

            except Exception as e:
                print(f"Could not resolve link for {title}: {e}")
                link = JOBS_URL

            # print("title:", title)
            # print("link:", link)
            # print("-" * 60)

            jobs.append({
                "id": link if link != JOBS_URL else title,
                "title": title,
                "link": link,
            })
            seen.add(title)

        browser.close()

    return jobs


def fetch_job_detail_text(link):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(link, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        text = page.locator("body").inner_text()

        browser.close()

    return text


def extract_qualifications(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    min_lines = []
    pref_lines = []

    mode = None

    stop_headers = {
        "About the job",
        "Responsibilities",
        "Information collected and processed as part of your Google Careers profile and any job applications you choose to submit is subject to Google's Applicant and Candidate Privacy Policy.",
        "Equal Opportunity",
        "Equal opportunity",
    }

    for line in lines:
        if line == "Minimum qualifications:":
            mode = "min"
            continue
        elif line == "Preferred qualifications:":
            mode = "pref"
            continue
        elif line in stop_headers:
            mode = None
            continue

        if mode == "min":
            min_lines.append(line)
        elif mode == "pref":
            pref_lines.append(line)

    return "\n".join(min_lines), "\n".join(pref_lines)


def translate_text(text, section_name=""):
    api_key = os.environ["GOOGLE_API_KEY"]

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/gemini-2.5-flash:generateContent?key={api_key}"
    )

    source_text = text.strip()
    if not source_text:
        return "[[MIN]]\n無\n\n[[PREF]]\n無"

    prompt = f"""
請將以下 Google 職缺中的 {section_name} 內容翻譯成繁體中文。

要求：
1. 忠實翻譯，不要摘要，不要改寫，不要補充不存在的資訊
2. 保持條列格式
3. 若原文是短句，就翻成短句
4. 不要加前言、結語或說明
5. 請務必保留 [[MIN]] 和 [[PREF]] 這兩個標記，不要翻譯、不要刪除

原文如下：
{source_text}
"""

    resp = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}]
        },
        timeout=60,
    )

    if not resp.ok:
        print("Gemini API failed")
        print("status:", resp.status_code)
        print("body:", resp.text)
        resp.raise_for_status()

    data = resp.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        print(f"Gemini response parse failed: {e}")
        print(data)
        return "[[MIN]]\n翻譯失敗\n\n[[PREF]]\n翻譯失敗"


def build_job_message(job):
    title = job["title"]
    link = job.get("link", JOBS_URL)

    detail_text = fetch_job_detail_text(link)
    min_q, pref_q = extract_qualifications(detail_text)

    combined = (
        "[[MIN]]\n"
        f"{min_q}\n\n"
        "[[PREF]]\n"
        f"{pref_q}"
    )

    translated = translate_text(combined, "qualifications")

    min_part = "無"
    pref_part = "無"

    if "[[PREF]]" in translated:
        parts = translated.split("[[PREF]]", 1)
        min_part = parts[0].replace("[[MIN]]", "").strip() or "無"
        pref_part = parts[1].strip() or "無"
    else:
        min_part = translated.replace("[[MIN]]", "").strip() or "無"

    msg = (
        "🚨 Google 新職缺\n\n"
        f"[{title}]({link})\n\n"
        "基本資格：\n"
        f"{min_part}\n\n"
        "加分條件：\n"
        f"{pref_part}\n\n"
    )

    if len(msg) > 1900:
        msg = msg[:1900] + "\n\n...(truncated)"

    return msg


def send_discord_message(new_jobs):
    for job in new_jobs:
        msg = build_job_message(job)

        resp = requests.post(
            WEBHOOK_URL,
            json={"content": msg},
            timeout=30,
        )
        resp.raise_for_status()


def main():
    old_jobs_raw = load_jobs()
    old_jobs = [normalize_job(job) for job in old_jobs_raw]

    current_jobs = fetch_jobs()

    old_ids = {job["id"] for job in old_jobs}
    new_jobs = [job for job in current_jobs if job["id"] not in old_ids]

    if not old_jobs:
        save_jobs(current_jobs)
        print(f"Initialized with {len(current_jobs)} jobs.")
        return

    if new_jobs:
        print(f"Found {len(new_jobs)} new jobs.")
        send_discord_message(new_jobs)
    else:
        print("No new jobs found.")

    save_jobs(current_jobs)


if __name__ == "__main__":
    main()