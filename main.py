import sys
import asyncio

# FIX: Set the event loop policy at the absolute top for Windows
if sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import json
import base64
import platform
import concurrent.futures
from datetime import datetime
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException, WebSocket, WebSocketDisconnect, APIRouter
from fastapi.responses import Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

from src.indexer import init_db, index_pages, search_pages
from src.crawler import Crawler, SearchProvider
from src.llm import generate_answer
from src.chat import generate_chat_response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log the loop type for verification
    loop = asyncio.get_running_loop()
    print(f"Current Event Loop: {type(loop).__name__}")
    
    # Initialize Database
    try:
        await init_db()
        print("Database initialized.")
    except Exception as e:
        print(f"Failed to initialize database: {e}")
    
    yield

app = FastAPI(lifespan=lifespan)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()

class CrawlRequest(BaseModel):
    url: str
    max_pages: int = 20

class SearchRequest(BaseModel):
    url: str

class ChatRequest(BaseModel):
    message: str
    site: str
    history: list = []


def format_to_json(data, query=None):
    """Convert data to JSON format."""
    export_data = {
        "query": query,
        "timestamp": datetime.now().isoformat(),
        "results": data
    }
    return json.dumps(export_data, indent=2, ensure_ascii=False)

def format_to_markdown(data, query=None):
    """Convert data to Markdown format."""
    md_lines = []
    if query:
        md_lines.append(f"# Search Results: {query}\n")
    md_lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    md_lines.append(f"**Total Results:** {len(data)}\n\n")
    md_lines.append("---\n\n")
    
    for idx, item in enumerate(data, 1):
        # Fallback to ID if title is missing (since DB schema removed title)
        title = item.get('title', item.get('id', 'Untitled'))
        md_lines.append(f"## {idx}. {title}\n\n")
        
        url_val = item.get('id', item.get('url'))
        if url_val:
            md_lines.append(f"**URL:** [{url_val}]({url_val})\n\n")
        
        # Handle metadata if present (from scrape)
        if 'metadata' in item and item['metadata']:
            md_lines.append("### Metadata\n\n")
            for key, value in item['metadata'].items():
                if value:
                    md_lines.append(f"- **{key.replace('_', ' ').title()}:** {value}\n")
            md_lines.append("\n")
        
        # Handle headers if present (from scrape)
        if 'headers' in item and item['headers']:
            md_lines.append("### Headers\n\n")
            for level, headers in item['headers'].items():
                if headers:
                    md_lines.append(f"#### {level.upper()}\n")
                    for header in headers:
                        md_lines.append(f"- {header}\n")
                    md_lines.append("\n")
        
        # Handle snippet or content
        if 'snippet' in item:
            md_lines.append("### Snippet\n\n")
            md_lines.append(f"{item['snippet']}\n\n")
        elif 'content' in item:
            md_lines.append("### Content\n\n")
            content = item['content'][:1000] + "..." if len(item.get('content', '')) > 1000 else item.get('content', '')
            md_lines.append(f"{content}\n\n")
        
        # Handle images if present (from scrape)
        if 'images' in item and item['images']:
            md_lines.append("### Images\n\n")
            for img in item['images'][:5]:  # Limit to first 5 images
                md_lines.append(f"- ![Image]({img.get('src', '')}) {img.get('alt', '')}\n")
            md_lines.append("\n")
        
        # Handle links if present (from scrape)
        if 'links' in item and item['links']:
            md_lines.append("### Links\n\n")
            for link in item['links'][:10]:  # Limit to first 10 links
                md_lines.append(f"- [{link.get('text', 'Link')}]({link.get('href', '')})\n")
            md_lines.append("\n")
        
        md_lines.append("---\n\n")
    
    return "\n".join(md_lines)

@router.get("/search")
async def search(q: str = Query(..., min_length=2)):
    try:
        # Search PostgreSQL
        results = await search_pages(q)
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search/live")
async def live_search(q: str = Query(..., min_length=2)):
    provider = SearchProvider()
    try:
        results = await provider.live_search(q)
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search/site")
async def site_search(
    q: str = Query(..., min_length=2), 
    url: str = Query(None),
    max_pages: int = Query(15, ge=1, le=100)
):
    """Search within a specific site. Crawls the site, stores data locally, then searches."""
    crawler = Crawler(max_pages=max_pages)
    try:
        if url:
            # Step 1: Crawl the site and get all pages (restricted to the domain)
            crawled_pages = await crawler.crawl(url, restrict_domain=True)
            
            if not crawled_pages:
                return {
                    "results": [],
                    "metadata": {
                        "pages_crawled": 0,
                        "pages_stored": 0,
                        "message": "No pages were crawled from the site."
                    }
                }
            
            # Step 2: Store ALL crawled pages in the database
            pages_stored = await index_pages(crawled_pages)
            
            if pages_stored == 0:
                return {
                    "results": [],
                    "metadata": {
                        "pages_crawled": len(crawled_pages),
                        "pages_stored": 0,
                        "message": "Pages were crawled but could not be stored in the database."
                    }
                }
            
            # Step 3: Now search the database (data is already stored)
            results = await search_pages(q)
            
            # Contextual Answer Generation (RAG)
            ai_answer = None
            if results:
                ai_answer = await asyncio.to_thread(generate_answer, q, results)

            # Remove large content before sending to frontend to keep payload light
            final_results = []
            for r in results:
                r.pop("content", None)
                final_results.append(r)
            
            return {
                "results": final_results,
                "ai_answer": ai_answer,
                "metadata": {
                    "pages_crawled": pages_stored,
                    "pages_stored": pages_stored,
                    "query": q,
                    "site": url
                }
            }
        else:
            results = await crawler.autonomous_search(q)
            return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/chat/site")
async def chat_with_site(request: ChatRequest):
    """Chat endpoint that searches indexed data and streams Ollama responses."""
    try:
        # Search the database for relevant context
        search_results = await search_pages(request.message, limit=5)
        
        context_items = [
            {
                "url": r["id"],
                "title": r.get("title", r["id"]),
                "content": r.get("content", "")
            }
            for r in search_results
        ]
        
        # Stream the chat response
        async def event_stream():
            try:
                async for chunk in generate_chat_response(
                    message=request.message,
                    targetSite=request.site,
                    context_items=context_items,
                    history=request.history
                ):
                    # Send as Server-Sent Events format
                    yield f"data: {json.dumps({'content': chunk})}\n\n"
                
                # Send done signal
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                # logger.error(f"Error in chat stream: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Recommended for SSE through Nginx
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/scrape")
async def scrape_page(request: CrawlRequest, format: str = Query("json", pattern="^(json|md|metadata)$")):
    crawler = Crawler()
    try:
        data = await crawler.scrape_detailed(request.url)
        
        # User wants to "remove the json, markdown and metdata format in json"
        # So we flatten the response based on format.
        
        if format == "json":
            # Return just the extracted data
            return data.get("json", data)
        elif format == "md":
            # Return just the markdown string (wrapped in object for JSON response consistency or raw text?)
            # Frontend expects object with fields? No, frontend handles it.
            # But standardized API usually returns JSON.
            # Let's return { "content": ... } or similar.
            # Actually, `uiSlice` expects `data` and then checks format.
            return {"content": data.get("markdown", "")} 
        elif format == "metadata":
            return {"metadata": data.get("metadata", {})}
            
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/crawl")
async def start_crawl(request: CrawlRequest):
    crawler = Crawler(max_pages=request.max_pages)
    try:
        results = await crawler.crawl(request.url)
        await index_pages(results)
        return {"status": "success", "pages_crawled": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search/download")
async def download_search(q: str = Query(..., min_length=2), format: str = Query("json", pattern="^(json|md)$")):
    """Download search results as JSON or Markdown file."""
    try:
        results = await search_pages(q)

        data = [
            {
                "url": r["id"],
                "title": r.get("title", r["id"]),
                "snippet": r.get("snippet", "")
            }
            for r in results
        ]
        
        if format == "json":
            content = format_to_json(data, q)
            media_type = "application/json"
            filename = f"search_results_{q.replace(' ', '_')}.json"
        else:
            content = format_to_markdown(data, q)
            media_type = "text/markdown"
            filename = f"search_results_{q.replace(' ', '_')}.md"
        
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search/live/download")
async def download_live_search(q: str = Query(..., min_length=2), format: str = Query("json", pattern="^(json|md)$")):
    """Download live search results as JSON or Markdown file."""
    provider = SearchProvider()
    try:
        results = await provider.live_search(q)
        
        if format == "json":
            content = format_to_json(results, q)
            media_type = "application/json"
            filename = f"live_search_{q.replace(' ', '_')}.json"
        else:
            content = format_to_markdown(results, q)
            media_type = "text/markdown"
            filename = f"live_search_{q.replace(' ', '_')}.md"
        
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search/site/download")
async def download_site_search(
    q: str = Query(..., min_length=2), 
    url: str = Query(None), 
    format: str = Query("json", pattern="^(json|md)$"),
    max_pages: int = Query(15, ge=1, le=100)
):
    """Download site search results as JSON or Markdown file."""
    crawler = Crawler(max_pages=max_pages)
    try:
        if url:
            # Step 1: Crawl the site and get all pages (restricted to the domain)
            crawled_pages = await crawler.crawl(url, restrict_domain=True)
            
            if not crawled_pages:
                raise HTTPException(status_code=404, detail="No pages were crawled from the site.")
            
            # Step 2: Store ALL crawled pages in the database
            pages_stored = await index_pages(crawled_pages)
            
            if pages_stored == 0:
                raise HTTPException(status_code=500, detail="Pages were crawled but could not be stored in the database.")
            
            # Step 3: Now search the database
            results = await search_pages(q)
            
        else:
            results = await crawler.autonomous_search(q)
        
        if format == "json":
            content = format_to_json(results, q)
            media_type = "application/json"
            filename = f"site_search_{q.replace(' ', '_')}.json"
        else:
            content = format_to_markdown(results, q)
            media_type = "text/markdown"
            filename = f"site_search_{q.replace(' ', '_')}.md"
        
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/scrape/download")
async def download_scrape(request: CrawlRequest, format: str = Query("json", pattern="^(json|md)$")):
    """Download scraped page data as JSON or Markdown file."""
    crawler = Crawler()
    try:
        data = await crawler.scrape_detailed(request.url)
        
        if format == "json":
            # If data already has markdown and metadata structure, return as-is
            if isinstance(data, dict) and "markdown" in data and "metadata" in data:
                content = json.dumps(data, indent=2, ensure_ascii=False)
            else:
                # Fallback to old format
                results = [data] if isinstance(data, dict) else data
                content = format_to_json(results, request.url)
            media_type = "application/json"
            filename = f"scraped_{urlparse(request.url).netloc.replace('.', '_')}.json"
        else:
            # If data has markdown field, use it directly
            if isinstance(data, dict) and "markdown" in data:
                content = data["markdown"]
            else:
                # Fallback to old format
                results = [data] if isinstance(data, dict) else data
                content = format_to_markdown(results, request.url)
            media_type = "text/markdown"
            filename = f"scraped_{urlparse(request.url).netloc.replace('.', '_')}.md"
        
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/smartsearch")
async def google_search_playwright(request: SearchRequest):
    import platform
    import concurrent.futures
    
    querry = request.url  # The 'url' field contains the search query
    # add a + between the words of the querry
    q = querry.replace(" ", "+")

    if platform.system() == 'Windows':
        # Use sync playwright in a thread pool on Windows
        from playwright.sync_api import sync_playwright
        
        def run_playwright_sync(query_str):
            results = []
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    viewport={'width': 1920, 'height': 1080},
                    extra_http_headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                )
                page = context.new_page()

                try:
                    # Navigate to search
                    print(f"Searching for: {query_str}")
                    page.goto(f"https://www.google.com/search?q={query_str}", timeout=60000, wait_until="domcontentloaded")
                    
                    # Wait for results or consent button
                    try:
                        # Try to handle cookie consent if it appears
                        consent_btn = page.query_selector("button:has-text('Accept all'), button:has-text('I agree'), button:has-text('Accept'), button:has-text('Agree')")
                        if consent_btn:
                            print("Consent button found, clicking...")
                            consent_btn.click()
                            page.wait_for_load_state("networkidle", timeout=5000)
                        
                        # Wait for the main search container or h3s
                        page.wait_for_selector("div#search, h3", timeout=50000)
                    except Exception as e:
                        print(f"Wait for selector error: {e}")
                        print(f"Current Page title: {page.title()}")
                        # Diagnostic: Save HTML on failure
                        with open("search_debug.html", "w", encoding="utf-8") as f:
                            f.write(page.content())
                        print("Saved page content to search_debug.html for diagnosis.")

                    # Primary: div.g is the standard result container
                    search_results = page.query_selector_all("div.g")

                    urlList = []
                    urlScrapeData = []
                    
                    for item in search_results[:5]:
                        title_el = item.query_selector("h3")
                        link_el = item.query_selector("a")
                        snippet_el = item.query_selector("div.VwiC3b, div.IsZ6hd, div.kb098d")
                        
                        if title_el and link_el:
                            href = link_el.get_attribute("href")
                            if href and href.startswith("http"):
                                urlList.append(href)
                                results.append({
                                    "title": title_el.inner_text(),
                                    "link": href,
                                    "snippet": snippet_el.inner_text() if snippet_el else "",
                                })
                    
                    # print(urlList, results, "urlList")

                    # Fallback: find h3s which are typically result titles
                    if not results:
                        h3s = page.query_selector_all("h3")
                        for h3 in h3s[:5]:
                            # Find the closest parent anchor tag
                            parent = h3.query_selector("xpath=ancestor::a")
                            if parent:
                                href = parent.get_attribute("href")
                                if href and href.startswith("http"):
                                    # Try to find a snippet nearby (sibling of the title's container)
                                    # This is tricky without a specific class, so we might just get title/link
                                    urlList.append(href)
                                    results.append({
                                        "title": h3.inner_text(),
                                        "link": href,
                                        "snippet": "", 
                                    })
                    print(urlList, results, "urlList")
                    # scrape each url
                    crawler = Crawler()
                    for url in urlList:
                        try:
                            print(f"Scraping: {url}")
                            # Use the existing page to fetch content
                            page.goto(url, timeout=30000, wait_until="domcontentloaded")
                            # Wait a bit for potential hydration
                            page.wait_for_timeout(2000)
                            html = page.content()
                            
                            # Use crawler class for robust extraction (it doesn't need its own playwright here)
                            title, content = crawler.extract_data(html)
                            
                            urlScrapeData.append({
                                "title": title or page.title(),
                                "url": url,
                                "content": content,
                            })
                        except Exception as e:
                            print(f"Error scraping {url}: {e}")

                    # use ai to get the best results based on the question
                    
                    return urlScrapeData

                except Exception as e:
                    print(f"Error during search: {e}")
                finally:
                    browser.close()
            return []
        
        # Run sync playwright in thread pool
        import asyncio
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            scraped_results = await loop.run_in_executor(
                executor, run_playwright_sync, q
            )
        
        # Contextual Answer Generation (RAG)
        ai_answer = None
        if scraped_results:
            ai_answer = await asyncio.to_thread(generate_answer, querry, scraped_results)

        return {
            "query": querry,
            "results": scraped_results,
            "ai_answer": ai_answer
        }
    else:
        # Use async playwright on non-Windows systems (Linux, macOS)
        results = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={'width': 1920, 'height': 1080}
            )
            page = await context.new_page()

            try:
                await page.goto(f"https://www.google.com/search?q={q}", timeout=60000, wait_until="domcontentloaded")

                # Wait for results or consent button
                try:
                    consent_btn = await page.query_selector("button:has-text('Accept all'), button:has-text('I agree'), button:has-text('Accept'), button:has-text('Agree')")
                    if consent_btn:
                        await consent_btn.click()
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    
                    await page.wait_for_selector("div#search, h3", timeout=50000)
                except Exception as e:
                    print(f"Wait for selector error: {e}")
                    # Diagnostic: Save HTML on failure
                    content = await page.content()
                    with open("search_debug_async.html", "w", encoding="utf-8") as f:
                        f.write(content)
                    print("Saved page content to search_debug_async.html for diagnosis.")

                # Primary: div.g is the standard result container
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

                # Fallback: find h3s which are typically result titles
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

        return {
            "query": q,
            "results": results,
        }

@router.websocket("/ws/smartsearch")
async def smartsearch_websocket(websocket: WebSocket):
    await websocket.accept()
    
    main_loop = asyncio.get_running_loop()
    current_action = ["Initializing browser..."]

    async def send_json_safe(data):
        try:
            await websocket.send_json(data)
        except:
            pass

    async def run_async_scraper(query_str, websocket_loop):
        from playwright.async_api import async_playwright
        import base64
        
        scraped_results = []
        stop_heartbeat = asyncio.Event()
        
        async def heartbeat_async(page_obj):
            while not stop_heartbeat.is_set():
                try:
                    screenshot = await page_obj.screenshot()
                    asyncio.run_coroutine_threadsafe(
                        send_json_safe({
                            "type": "live_frame",
                            "screenshot": base64.b64encode(screenshot).decode('utf-8'),
                            "action": current_action[0]
                        }),
                        websocket_loop
                    )
                except: pass
                await asyncio.sleep(0.1)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080},
            )
            page = await context.new_page()
            h_task = asyncio.create_task(heartbeat_async(page))
            
            try:
                # Search Google
                current_action[0] = "Searching Google..."
                await page.goto(f"https://www.google.com/search?q={query_str}", timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
                
                try:
                    btn = await page.query_selector("button:has-text('Accept all'), button:has-text('I agree')")
                    if btn:
                        current_action[0] = "Handling consent..."
                        await btn.click()
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    await page.wait_for_selector("div#search, h3", timeout=50000)
                except: pass

                current_action[0] = "Extracting results..."
                search_results = await page.query_selector_all("div.g")
                urlList = []
                for item in search_results[:5]:
                    link = await item.query_selector("a")
                    if link:
                        href = await link.get_attribute("href")
                        if href and href.startswith("http"): urlList.append(href)
                
                if not urlList:
                    h3s = await page.query_selector_all("h3")
                    for h3 in h3s[:5]:
                        parent = await h3.query_selector("xpath=ancestor::a")
                        if parent:
                            href = await parent.get_attribute("href")
                            if href and href.startswith("http"): urlList.append(href)

                crawler = Crawler()
                for i, url in enumerate(urlList[:3]):
                    try:
                        current_action[0] = f"Visiting result {i+1}: {url[:50]}..."
                        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                        current_action[0] = f"Capturing content from page {i+1}..."
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
                        await page.wait_for_timeout(1000)
                        html = await page.content()
                        title, content = crawler.extract_data(html)
                        scraped_results.append({"title": title or await page.title(), "url": url, "content": content})
                    except Exception as e:
                        print(f"Error processing {url}: {e}")

                stop_heartbeat.set()
                await h_task
                return scraped_results
            finally:
                await browser.close()

    def thread_worker(query_str, websocket_loop, result_future):
        """Worker function for the dedicated thread."""
        if platform.system() == 'Windows':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        
        try:
            results = new_loop.run_until_complete(run_async_scraper(query_str, websocket_loop))
            websocket_loop.call_soon_threadsafe(result_future.set_result, results)
        except Exception as e:
            websocket_loop.call_soon_threadsafe(result_future.set_exception, e)
        finally:
            new_loop.close()

    try:
        data = await websocket.receive_text()
        request_data = json.loads(data)
        query = request_data.get("url")
        if not query:
            await send_json_safe({"error": "Query is required"})
            await websocket.close()
            return

        q = query.replace(" ", "+")
        
        # Determine if we need to run in a separate thread (Windows SelectorLoop workaround)
        is_windows = platform.system() == 'Windows'
        is_selector_loop = "SelectorEventLoop" in type(main_loop).__name__
        
        if is_windows and is_selector_loop:
            await send_json_safe({"status": "Running Windows compatibility mode (Async-in-Thread)..."})
            result_future = main_loop.create_future()
            from threading import Thread
            t = Thread(target=thread_worker, args=(q, main_loop, result_future), daemon=True)
            t.start()
            scraped_results = await result_future
        else:
            # Direct async implementation for Linux/Mac or Proactor-enabled main loop
            await send_json_safe({"status": f"Starting live browser session for '{query}'..."})
            scraped_results = await run_async_scraper(q, main_loop)

        # Common AI generation part
        await send_json_safe({"status": f"Scraped {len(scraped_results)} pages. Generating AI answer..."})
        ai_answer = None
        if scraped_results:
            ai_answer = await asyncio.to_thread(generate_answer, query, scraped_results)

        await send_json_safe({
            "query": query,
            "results": scraped_results,
            "ai_answer": ai_answer,
            "done": True
        })

    except WebSocketDisconnect:
        print("WebSocket disconnected")
    except Exception as e:
        await send_json_safe({"error": str(e)})
    finally:
        try:
            await websocket.close()
        except: pass
app.include_router(router, prefix="/app")
