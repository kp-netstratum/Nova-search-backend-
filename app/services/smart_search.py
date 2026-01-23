import asyncio
import json
import base64
import platform
import concurrent.futures
from playwright.async_api import async_playwright
from app.services.crawler import Crawler
from app.services.llm import generate_answer

async def run_playwright_search(query_str: str):
    """Core logic for Google search using Playwright (Async version)."""
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        try:
            await page.goto(f"https://www.google.com/search?q={query_str}", timeout=60000, wait_until="domcontentloaded")
            try:
                consent_btn = await page.query_selector("button:has-text('Accept all'), button:has-text('I agree'), button:has-text('Accept'), button:has-text('Agree')")
                if consent_btn:
                    await consent_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=5000)
                await page.wait_for_selector("div#search, h3", timeout=50000)
            except: pass

            search_results = await page.query_selector_all("div.g")
            for item in search_results[:5]:
                title_el = await item.query_selector("h3")
                link_el = await item.query_selector("a")
                snippet_el = await item.query_selector("div.VwiC3b, div.IsZ6hd, div.kb098d")
                if title_el and link_el:
                    href = await link_el.get_attribute("href")
                    if href and href.startswith("http"):
                        results.append({
                            "title": await title_el.inner_text(),
                            "link": href,
                            "snippet": await snippet_el.inner_text() if snippet_el else "",
                        })
            
            if not results:
                h3s = await page.query_selector_all("h3")
                for h3 in h3s[:5]:
                    parent = await h3.query_selector("xpath=ancestor::a")
                    if parent:
                        href = await parent.get_attribute("href")
                        if href and href.startswith("http"):
                            results.append({
                                "title": await h3.inner_text(),
                                "link": href,
                                "snippet": "",
                            })
            return results
        finally:
            await browser.close()

def run_playwright_sync_worker(query_str):
    """Sync version of Playwright search for Windows thread pool."""
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
        )
        page = context.new_page()
        try:
            page.goto(f"https://www.google.com/search?q={query_str}", timeout=60000, wait_until="domcontentloaded")
            try:
                consent_btn = page.query_selector("button:has-text('Accept all'), button:has-text('I agree')")
                if consent_btn:
                    consent_btn.click()
                    page.wait_for_load_state("networkidle", timeout=5000)
                page.wait_for_selector("div#search, h3", timeout=50000)
            except: pass

            search_results = page.query_selector_all("div.g")
            urlList = []
            final_scraped = []
            
            for item in search_results[:5]:
                title_el = item.query_selector("h3")
                link_el = item.query_selector("a")
                if title_el and link_el:
                    href = link_el.get_attribute("href")
                    if href and href.startswith("http"): urlList.append(href)
            
            if not urlList:
                h3s = page.query_selector_all("h3")
                for h3 in h3s[:5]:
                    parent = h3.query_selector("xpath=ancestor::a")
                    if parent:
                        href = parent.get_attribute("href")
                        if href and href.startswith("http"): urlList.append(href)

            crawler = Crawler()
            for url in urlList:
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    html = page.content()
                    title, content = crawler.extract_data(html)
                    final_scraped.append({
                        "title": title or page.title(),
                        "url": url,
                        "content": content,
                    })
                except: continue
            return final_scraped
        finally:
            browser.close()

async def smart_search_logic(query: str):
    """Unified smart search logic handling platform differences."""
    if platform.system() == 'Windows':
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            scraped_results = await loop.run_in_executor(
                executor, run_playwright_sync_worker, query.replace(" ", "+")
            )
        ai_answer = None
        if scraped_results:
            ai_answer = await asyncio.to_thread(generate_answer, query, scraped_results)
        return {
            "query": query,
            "results": scraped_results,
            "ai_answer": ai_answer
        }
    else:
        results = await run_playwright_search(query.replace(" ", "+"))
        return {
            "query": query,
            "results": results,
        }
