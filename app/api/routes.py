import asyncio
import json
from urllib.parse import urlparse
from fastapi import APIRouter, Query, HTTPException, Response
from fastapi.responses import StreamingResponse

from app.models.schemas import CrawlRequest, SearchRequest, ChatRequest
from app.services.indexer import (
    index_pages, 
    search_pages, 
    save_chat_session, 
    save_chat_message, 
    get_chat_sessions, 
    get_chat_messages, 
    get_crawl_history,
    delete_site_data,
    is_session_valid
)
from app.services.crawler import Crawler, SearchProvider
from app.services.llm import generate_answer
from app.services.chat import generate_chat_response
from app.services.formatters import format_to_json, format_to_markdown
from app.services.smart_search import smart_search_logic

router = APIRouter()

@router.get("/search")
async def search(q: str = Query(..., min_length=2)):
    try:
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
    crawler = Crawler(max_pages=max_pages)
    try:
        if url:
            crawled_pages = await crawler.crawl(url, restrict_domain=True)
            if not crawled_pages:
                return {"results": [], "metadata": {"pages_crawled": 0, "message": "No pages crawled."}}
            
            pages_stored = await index_pages(crawled_pages)
            results = await search_pages(q)
            
            ai_answer = None
            if results:
                ai_answer = await asyncio.to_thread(generate_answer, q, results)

            final_results = []
            for r in results:
                r.pop("content", None)
                final_results.append(r)
            
            return {
                "results": final_results,
                "ai_answer": ai_answer,
                "metadata": {"pages_crawled": pages_stored, "query": q, "site": url}
            }
        else:
            results = await crawler.autonomous_search(q)
            return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/chat/site")
async def chat_with_site(request: ChatRequest, session_id: str = Query(None)):
    try:
        # 1. Ensure we have a valid session
        current_session_id = session_id
        
        # Check if session exists if id provided
        if current_session_id:
            if not await is_session_valid(current_session_id):
                # If it doesn't exist or ID is invalid, treat as new session
                current_session_id = None

        if not current_session_id:
            current_session_id = await save_chat_session(request.site)
        
        # 2. Save user message
        await save_chat_message(current_session_id, "user", request.message)

        # 3. Search context
        search_results = await search_pages(request.message, limit=5)
        context_items = [
            {"url": r["id"], "title": r.get("title", r["id"]), "content": r.get("content", "")}
            for r in search_results
        ]
        
        async def event_stream():
            full_response = ""
            try:
                # Send session ID first
                yield f"data: {json.dumps({'sessionId': str(current_session_id)})}\n\n"

                async for chunk in generate_chat_response(
                    message=request.message,
                    targetSite=request.site,
                    context_items=context_items,
                    history=request.history
                ):
                    full_response += chunk
                    yield f"data: {json.dumps({'content': chunk})}\n\n"
                
                # 4. Save assistant response
                await save_chat_message(current_session_id, "assistant", full_response)
                
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                import traceback
                print(f"Stream error: {e}")
                traceback.print_exc()
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/chats")
async def fetch_chat_sessions():
    try:
        return await get_chat_sessions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/chats/{session_id}")
async def fetch_chat_messages(session_id: str):
    try:
        return await get_chat_messages(session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/crawls")
async def fetch_crawl_history():
    try:
        return await get_crawl_history()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/history/crawls")
async def delete_crawl(url: str = Query(...)):
    try:
        await delete_site_data(url)
        return {"status": "success", "message": f"Deleted records for {url}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/scrape")
async def scrape_page(request: CrawlRequest, format: str = Query("json", pattern="^(json|md|metadata)$")):
    crawler = Crawler()
    try:
        data = await crawler.scrape_detailed(request.url)
        if format == "json":
            return data.get("json", data)
        elif format == "md":
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
    try:
        results = await search_pages(q)
        data = [{"url": r["id"], "title": r.get("title", r["id"]), "snippet": r.get("snippet", "")} for r in results]
        
        if format == "json":
            content = format_to_json(data, q)
            media_type = "application/json"
            filename = f"search_results_{q.replace(' ', '_')}.json"
        else:
            content = format_to_markdown(data, q)
            media_type = "text/markdown"
            filename = f"search_results_{q.replace(' ', '_')}.md"
        
        return Response(content=content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/smartsearch")
async def smartsearch_endpoint(request: SearchRequest):
    try:
        return await smart_search_logic(request.url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
