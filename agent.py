import os
import re
import time
import random
import pandas as pd
from urllib.parse import urlparse, urljoin, unquote
from playwright.sync_api import sync_playwright

print("\n=============================================")
print("   OFFLINE-READY ZERO-BLEED LEAD ENGINE      ")
print("=============================================\n")

# Configuration
INPUT_FILE = "leads_input.csv"
OUTPUT_FILE = "leads_output.xlsx"
OUTPUT_CSV_FILE = "leads_output.csv"

# Set HEADLESS=False in environment variables for local GUI debugging
HEADLESS_MODE = os.environ.get("HEADLESS", "true").lower() == "true"

# Blacklist filters for false-positive emails
EMAIL_BLACKLIST_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.css', '.js', '.woff', '.ttf'
)
EMAIL_BLACKLIST_DOMAINS = (
    'sentry.io', 'wixpress.com', 'domain.com', 'example.com', 'schema.org', 'gravatar.com'
)


def get_clean_domain(url):
    """Extracts base domain from a given URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return None


def duckduckgo_search_fallback(page, company_name, city):
    """
    CAPTCHA-FREE FALLBACK: Uses DuckDuckGo Lite to find the business website
    if missing on Google Maps.
    """
    try:
        search_query = f"{company_name} {city} website"
        page.goto("https://lite.duckduckgo.com/lite/", timeout=15000, wait_until="domcontentloaded")
        
        page.fill('input[name="q"]', search_query)
        page.click('input[type="submit"]')
        page.wait_for_timeout(1500)
        
        links = page.locator('a[href]').all()
        for link in links:
            href = link.get_attribute("href") or ""
            
            if "uddg=" in href:
                match = re.search(r'uddg=([^&]+)', href)
                if match:
                    href = unquote(match.group(1))

            href_lower = href.lower()
            if href.startswith("http") and not any(k in href_lower for k in [
                "duckduckgo.com", "google.com", "youtube.com", "facebook.com", "instagram.com", 
                "yelp.com", "linkedin.com", "twitter.com", "x.com", "tripadvisor.com", "foursquare.com", 
                "mapquest.com", "yellowpages.com", "groupon.com"
            ]):
                return href
    except Exception as e:
        print(f"      [!] DDG Search Fallback warning: {e}")
    return "N/A"


def deep_crawl_website(context, website_url, existing_phone):
    """
    DEEP CRAWLER ENGINE: Scrapes target website homepage + contact pages
    for emails (mailto/regex), phone numbers, and social media channels.
    """
    data = {
        "Email": "N/A", 
        "Phone": existing_phone if existing_phone else "N/A", 
        "Facebook": "N/A", 
        "Instagram": "N/A", 
        "LinkedIn": "N/A", 
        "Twitter": "N/A"
    }

    if not website_url or website_url == "N/A" or "google.com" in website_url:
        return data

    if not website_url.startswith("http"):
        website_url = "https://" + website_url

    page = None
    try:
        page = context.new_page()
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        })

        try:
            page.goto(website_url, timeout=12000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
        except Exception:
            return data

        domain = get_clean_domain(website_url)
        pages_to_visit = [website_url]

        # Find key contact/about subpages
        try:
            anchors = page.locator("a[href]").all()
            subpage_urls = set()
            for a in anchors:
                href = a.get_attribute("href") or ""
                href_lower = href.lower()
                if any(term in href_lower for term in ["contact", "about", "info", "reach", "location", "team"]):
                    full_sub_url = urljoin(website_url, href)
                    if domain and domain in get_clean_domain(full_sub_url) or "":
                        subpage_urls.add(full_sub_url)
            pages_to_visit.extend(list(subpage_urls)[:2])
        except Exception:
            pass

        for target_url in pages_to_visit:
            if target_url != website_url:
                try:
                    page.goto(target_url, timeout=8000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000)
                except Exception:
                    continue

            # 1. PRIORITY EMAIL EXTRACTION: mailto: links
            if data["Email"] == "N/A":
                try:
                    mailto_links = page.locator('a[href^="mailto:"]').all()
                    for mailto in mailto_links:
                        raw_href = mailto.get_attribute("href") or ""
                        clean_email = raw_href.replace("mailto:", "").split("?")[0].strip()
                        if clean_email and "@" in clean_email:
                            data["Email"] = clean_email
                            break
                except Exception:
                    pass

            # 2. FALLBACK EMAIL EXTRACTION: Regex scan on raw HTML
            if data["Email"] == "N/A":
                raw_html = page.content()
                scraped_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', raw_html)
                for email in scraped_emails:
                    email_lower = email.lower()
                    if any(email_lower.endswith(ext) for ext in EMAIL_BLACKLIST_EXTENSIONS):
                        continue
                    if any(b_domain in email_lower for b_domain in EMAIL_BLACKLIST_DOMAINS):
                        continue
                    data["Email"] = email
                    break

            # 3. PHONE EXTRACTION FALLBACK
            if data["Phone"] in ["N/A", ""]:
                # Check tel: links first
                try:
                    tel_links = page.locator('a[href^="tel:"]').all()
                    if tel_links:
                        tel_href = tel_links[0].get_attribute("href") or ""
                        data["Phone"] = tel_href.replace("tel:", "").strip()
                except Exception:
                    pass

                # Fallback to Regex on page inner text
                if data["Phone"] in ["N/A", ""]:
                    body_text = page.locator("body").inner_text() or ""
                    phone_match = re.search(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', body_text)
                    if phone_match:
                        data["Phone"] = phone_match.group(0)

            # 4. SOCIAL MEDIA LINK EXTRACTION
            try:
                links = page.locator("a[href]").all()
                for link in links:
                    href = link.get_attribute("href") or ""
                    href_lower = href.lower()
                    if "facebook.com" in href_lower and data["Facebook"] == "N/A":
                        data["Facebook"] = href
                    elif "instagram.com" in href_lower and data["Instagram"] == "N/A":
                        data["Instagram"] = href
                    elif "linkedin.com" in href_lower and data["LinkedIn"] == "N/A":
                        data["LinkedIn"] = href
                    elif any(t in href_lower for t in ["twitter.com", "x.com"]) and data["Twitter"] == "N/A":
                        data["Twitter"] = href
            except Exception:
                pass

    except Exception as e:
        print(f"      [!] Error crawling website {website_url}: {e}")
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass

    return data


def is_missing_website(url):
    """Checks if a URL string is invalid or missing."""
    if url is None:
        return True
    cleaned = str(url).strip()
    if not cleaned:
        return True
    return cleaned.lower() in {"n/a", "none"}


def run_pipeline():
    # Ensure input queue file exists
    if not os.path.exists(INPUT_FILE):
        template_df = pd.DataFrame({"Specialty": ["Clinic"], "City": ["Abbeville"]})
        template_df.to_csv(INPUT_FILE, index=False)
        print(f"[+++] Created template queue spreadsheet: '{INPUT_FILE}'")
        return

    df_tasks = pd.read_csv(INPUT_FILE)
    df_tasks.columns = [str(c).strip().capitalize() for c in df_tasks.columns]
    specialty_col = 'Speciality' if 'Speciality' in df_tasks.columns else 'Specialty'

    if specialty_col not in df_tasks.columns or 'City' not in df_tasks.columns:
        print("[-] Data Header Error: CSV columns must be distinctly labeled 'Specialty' and 'City'.")
        return

    seen_names = set()
    total_tasks = len(df_tasks)
    print(f"[+] Initializing Scraper Engine across {total_tasks} target tasks...")
    print(f"[+] Running Mode: {'Headless (Offline/CI/CD)' if HEADLESS_MODE else 'Headed (GUI)'}\n")

    all_output_rows = []
    missing_website_rows = []
    output_columns = [
        "Searched Specialty", "Searched City", "Company Name", "Phone Number",
        "Website", "Email/Gmail", "Facebook", "Instagram", "LinkedIn", "Twitter/X"
    ]

    # Initialize Playwright Engine ONCE globally
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS_MODE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        for task_idx, row in df_tasks.iterrows():
            target_specialty = str(row[specialty_col]).strip()
            target_city = str(row['City']).strip()

            if pd.isna(target_specialty) or pd.isna(target_city) or not target_specialty or not target_city:
                continue

            search_query = f"{target_specialty} in {target_city}"
            print(f"=========================================================================")
            print(f"[*] TASK [{task_idx + 1}/{total_tasks}]: {search_query.upper()}")
            print(f"=========================================================================")

            # Create an isolated browser context per task
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            maps_page = context.new_page()
            search_page = context.new_page()

            clean_url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"

            try:
                maps_page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
                maps_page.wait_for_timeout(3000)
            except Exception as e:
                print(f"    [!] Initial map load adjustment: {e}")

            # Locate feed container
            listings_feed = maps_page.locator('div[role="feed"]')
            if listings_feed.count() == 0:
                listings_feed = maps_page.locator('div[aria-label*="Results for"]')

            # Scroll and load result cards
            last_count = 0
            strikes = 0
            while True:
                try:
                    listings_feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                except Exception:
                    maps_page.keyboard.press("PageDown")

                maps_page.wait_for_timeout(1200)

                current_count = maps_page.locator('a[href*="/maps/place/"]').count()
                if current_count == last_count:
                    strikes += 1
                    if strikes >= 3:
                        break
                else:
                    strikes = 0

                if current_count >= 30:  # Extraction cap per keyword batch
                    break
                last_count = current_count

            cards = maps_page.locator('a[href*="/maps/place/"]').all()
            total_cards = len(cards)
            print(f"    [+] Found {total_cards} properties matching query.")

            current_city_leads = []

            for card in cards:
                try:
                    raw_name = card.get_attribute("aria-label") or "Unknown Business"
                    name = raw_name.strip()
                    if name in seen_names or name == "Unknown Business":
                        continue

                    card.click()
                    maps_page.wait_for_timeout(1500)

                    phone = "N/A"
                    website = "N/A"

                    # Website Extraction
                    web_el = maps_page.locator('a[data-item-id="authority"]')
                    if web_el.count() > 0:
                        website = web_el.get_attribute("href") or "N/A"
                    else:
                        all_panel_links = maps_page.locator('div[role="main"] a[href]').all()
                        for link in all_panel_links:
                            href = link.get_attribute("href") or ""
                            if "google.com" not in href and href.startswith("http"):
                                website = href
                                break

                    # Phone Extraction
                    phone_el = maps_page.locator('button[data-item-id^="phone:tel:"]')
                    if phone_el.count() > 0:
                        phone = phone_el.get_attribute("data-item-id").replace("phone:tel:", "").strip()
                    else:
                        panel_main = maps_page.locator('div[role="main"]').first
                        if panel_main.count() > 0:
                            panel_text = panel_main.inner_text() or ""
                            phone_match = re.search(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', panel_text)
                            if phone_match:
                                phone = phone_match.group(0)

                    current_city_leads.append({
                        "Target Specialty": target_specialty,
                        "Target City": target_city,
                        "Company Name": name,
                        "Phone Number": phone,
                        "Website": website
                    })
                    seen_names.add(name)

                except Exception as e:
                    print(f"      [!] Card processing error: {e}")
                    continue

            # Fallback search for missing websites
            if current_city_leads:
                print("    [*] Discovering missing websites via DuckDuckGo...")
                for lead in current_city_leads:
                    if is_missing_website(lead["Website"]):
                        discovered_url = duckduckgo_search_fallback(search_page, lead["Company Name"], lead["Target City"])
                        lead["Website"] = discovered_url

                # Deep Crawling for Emails and Social Media Links
                print("    [*] Deep crawling target domains for emails and social handles...")
                for idx, base_lead in enumerate(current_city_leads):
                    print(f"      [{idx + 1}/{len(current_city_leads)}] Domain Audit: {base_lead['Company Name']}")
                    crawl_data = deep_crawl_website(context, base_lead["Website"], base_lead["Phone Number"])

                    row_data = {
                        "Searched Specialty": base_lead["Target Specialty"],
                        "Searched City": base_lead["Target City"],
                        "Company Name": base_lead["Company Name"],
                        "Phone Number": crawl_data["Phone"],
                        "Website": base_lead["Website"],
                        "Email/Gmail": crawl_data["Email"],
                        "Facebook": crawl_data["Facebook"],
                        "Instagram": crawl_data["Instagram"],
                        "LinkedIn": crawl_data["LinkedIn"],
                        "Twitter/X": crawl_data["Twitter"]
                    }

                    all_output_rows.append(row_data)
                    if is_missing_website(base_lead["Website"]):
                        missing_website_rows.append(row_data)

            # Close context per task to release memory resources
            context.close()

            # INCREMENTAL SAVE: Save state after each keyword finishes
            if all_output_rows:
                all_df = pd.DataFrame(all_output_rows, columns=output_columns)
                missing_df = pd.DataFrame(missing_website_rows, columns=output_columns)

                with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
                    all_df.to_excel(writer, sheet_name="All Data", index=False)
                    missing_df.to_excel(writer, sheet_name="No Website", index=False)

                all_df.to_csv(OUTPUT_CSV_FILE, index=False)
                print(f"[+++] Progress saved cleanly for task {task_idx + 1}/{total_tasks}.\n")

        browser.close()

    print(f"\n[+++] RUN COMPLETE! Data compiled into '{OUTPUT_FILE}' and '{OUTPUT_CSV_FILE}'")


if __name__ == "__main__":
    run_pipeline()
