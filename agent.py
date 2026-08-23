import csv
import json
import os
import re
import pandas as pd
from openai import OpenAI
from playwright.sync_api import sync_playwright

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Exact Kimi K3 client structure pointing to Gemini's free OpenAI endpoint
client = (
    OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    if GEMINI_API_KEY
    else None
)


def sanitize_html(html_content: str) -> str:
    """Strips unnecessary HTML elements to reduce context size."""
    text = re.sub(
        r"<(script|style|svg|path)[^>]*>.*?</\1>",
        "",
        html_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:15000]


def kimi_extract_contacts(cleaned_text: str) -> dict:
    """Uses free Gemini API via OpenAI SDK format to extract contacts."""
    if not client:
        return {"email": "N/A", "phone": "N/A", "social_links": []}

    prompt = (
        "Extract official business contact details from the text below. "
        "Return a JSON object with keys: 'email' (string or 'N/A'), "
        "'phone' (string or 'N/A'), and 'social_links' (array of strings).\n\n"
        f"WEBSITE TEXT:\n{cleaned_text}"
    )

    try:
        response = client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {
                    "role": "system",
                    "content": "You are a structured data extraction engine. Output strictly valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        data = json.loads(response.choices[0].message.content)
        return {
            "email": data.get("email", "N/A"),
            "phone": data.get("phone", "N/A"),
            "social_links": data.get("social_links", []),
        }
    except Exception as e:
        print(f"  [!] AI Extraction Error: {e}")
        return {"email": "N/A", "phone": "N/A", "social_links": []}


def quick_regex_email(text: str) -> str:
    """Scans for valid domain emails before calling the AI API."""
    pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    matches = re.findall(pattern, text)
    ignore_extensions = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

    for email in matches:
        email_lower = email.lower()
        if (
            not email_lower.endswith(ignore_extensions)
            and "sentry" not in email_lower
        ):
            return email
    return "N/A"


def append_lead_to_csv(lead_data: dict, filepath: str = "leads_output.csv"):
    """Appends lead data to the output CSV file."""
    file_exists = os.path.isfile(filepath)
    fieldnames = [
        "Company Name",
        "Category",
        "City",
        "Address",
        "Phone",
        "Website",
        "Email",
        "Social Links",
    ]

    with open(filepath, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(lead_data)


def mark_input_task_done(index: int, filepath: str = "leads_input.csv"):
    """Updates queue status from pending to done."""
    df = pd.read_csv(filepath)
    df.at[index, "Status"] = "done"
    df.to_csv(filepath, index=False)


def run_scraping_agent():
    input_file = "leads_input.csv"
    if not os.path.exists(input_file):
        print(f"[!] Input file '{input_file}' not found.")
        return

    df = pd.read_csv(input_file)
    pending_tasks = df[df["Status"].str.lower() == "pending"]

    if pending_tasks.empty:
        print("[*] All tasks are already completed.")
        return

    print(f"[*] Found {len(pending_tasks)} pending tasks...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for idx, row in pending_tasks.iterrows():
            specialty = row["Specialty"]
            city = row["City"]
            query = f"{specialty} in {city}"
            print(f"\n[+] Task {idx + 1}: Searching '{query}'")

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = context.new_page()

            try:
                page.goto(
                    f"https://www.google.com/maps/search/{query.replace(' ', '+')}",
                    timeout=60000,
                )
                page.wait_for_timeout(3000)

                # Cookie Consent Dismissal
                if "consent.google" in page.url:
                    for selector in [
                        'button[aria-label*="Reject"]',
                        'button[aria-label*="Accept"]',
                    ]:
                        if page.locator(selector).count() > 0:
                            page.locator(selector).first.click()
                            page.wait_for_timeout(2000)
                            break

                feed = page.locator('div[role="feed"]')
                if feed.count() == 0:
                    print("  [-] No results panel found.")
                    mark_input_task_done(idx, input_file)
                    context.close()
                    continue

                for _ in range(3):
                    feed.evaluate("el => el.scrollBy(0, 1000)")
                    page.wait_for_timeout(1500)

                cards = page.locator('div[role="article"]').all()
                print(f"  [*] Processing {len(cards)} listings...")

                for i in range(min(len(cards), 5)):
                    try:
                        card = page.locator('div[role="article"]').nth(i)
                        card.click()
                        page.wait_for_timeout(2500)

                        name = (
                            page.locator("h1").first.inner_text()
                            if page.locator("h1").count() > 0
                            else "N/A"
                        )
                        website_el = page.locator('a[data-item-id="authority"]')
                        website_url = (
                            website_el.get_attribute("href")
                            if website_el.count() > 0
                            else "N/A"
                        )
                        address_el = page.locator(
                            'button[data-item-id*="address"]'
                        )
                        address = (
                            address_el.inner_text()
                            if address_el.count() > 0
                            else "N/A"
                        )

                        email = "N/A"
                        phone = "N/A"
                        socials = []

                        if website_url != "N/A" and website_url.startswith(
                            "http"
                        ):
                            site_page = context.new_page()
                            try:
                                site_page.goto(
                                    website_url,
                                    timeout=20000,
                                    wait_until="domcontentloaded",
                                )
                                site_html = site_page.content()
                                cleaned_text = sanitize_html(site_html)

                                email = quick_regex_email(site_html)

                                # AI Fallback via Gemini (OpenAI SDK structure)
                                if email == "N/A":
                                    ai_results = kimi_extract_contacts(
                                        cleaned_text
                                    )
                                    email = ai_results.get("email", "N/A")
                                    phone = ai_results.get("phone", "N/A")
                                    socials = ai_results.get(
                                        "social_links", []
                                    )

                            except Exception as site_err:
                                print(
                                    f"  [!] Site error ({website_url}): {site_err}"
                                )
                            finally:
                                site_page.close()

                        lead_record = {
                            "Company Name": name,
                            "Category": specialty,
                            "City": city,
                            "Address": address,
                            "Phone": phone,
                            "Website": website_url,
                            "Email": email,
                            "Social Links": (
                                ", ".join(socials) if socials else "N/A"
                            ),
                        }

                        append_lead_to_csv(lead_record)
                        print(f"  [✓] Scraped Lead: {name} | Email: {email}")

                    except Exception as card_err:
                        print(f"  [!] Card error {i+1}: {card_err}")
                        continue

                mark_input_task_done(idx, input_file)

            except Exception as task_err:
                print(f"  [!] Task Error: {task_err}")
            finally:
                context.close()

        browser.close()


if __name__ == "__main__":
    run_scraping_agent()
