import asyncio
import json
import base64
import platform
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.crawler import Crawler

router = APIRouter()

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
            await send_json_safe({"type": "error", "message": "No query provided"})
            return

        result_future = main_loop.create_future()
        import threading
        t = threading.Thread(target=thread_worker, args=(query, main_loop, result_future))
        t.start()
        
        scraped_results = await result_future
        
        from app.services.llm import generate_answer
        ai_answer = await asyncio.to_thread(generate_answer, query, scraped_results)
        
        await send_json_safe({
            "type": "results",
            "results": scraped_results,
            "ai_answer": ai_answer,
            "done": True
        })
        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await send_json_safe({"type": "error", "message": str(e)})
