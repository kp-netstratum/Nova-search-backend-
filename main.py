

import asyncio
import json
from datetime import datetime
from urllib.parse import urlparse
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from whoosh.qparser import QueryParser, MultifieldParser, OrGroup
from whoosh.analysis import StemmingAnalyzer
from src.indexer import get_index, index_pages
from src.crawler import Crawler, SearchProvider
from src.llm import generate_answer
from pydantic import BaseModel
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log the loop type for verification
    loop = asyncio.get_running_loop()
    print(f"Current Event Loop: {type(loop).__name__}")
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

class CrawlRequest(BaseModel):
    url: str
    max_pages: int = 20

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
        md_lines.append(f"## {idx}. {item.get('title', 'Untitled')}\n\n")
        if 'url' in item:
            md_lines.append(f"**URL:** [{item['url']}]({item['url']})\n\n")
        
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

@app.get("/search")
def search(q: str = Query(..., min_length=2)):
    try:
        ix = get_index()
        with ix.searcher() as searcher:
            # Using Stemming and OrGroup for better intent matching
            parser = MultifieldParser(
                ["title", "content"], 
                ix.schema, 
                fieldboosts={"title": 2.5},
                group=OrGroup.factory(0.9)
            )
            query = parser.parse(q)
            results = searcher.search(query, limit=20)

            return [
                {
                    "url": r["url"],
                    "title": r.get("title", r["url"]),
                    "snippet": (r.highlights("content") or r["content"][:200]) + "..."
                }
                for r in results
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/live")
async def live_search(q: str = Query(..., min_length=2)):
    provider = SearchProvider()
    try:
        results = await provider.live_search(q)
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/site")
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
            
            # Step 2: Store ALL crawled pages in the local index FIRST
            pages_stored = index_pages(crawled_pages)
            
            if pages_stored == 0:
                return {
                    "results": [],
                    "metadata": {
                        "pages_crawled": len(crawled_pages),
                        "pages_stored": 0,
                        "message": "Pages were crawled but could not be stored in the database."
                    }
                }
            
            # Step 3: Now search the local index (data is already stored)
            # Get a fresh index instance to ensure we see the newly committed data
            ix = get_index()
            with ix.searcher() as searcher:
                parser = MultifieldParser(
                    ["title", "content"], 
                    ix.schema, 
                    fieldboosts={"title": 2.5},
                    group=OrGroup.factory(0.9)
                )
                query_obj = parser.parse(q)
                search_results = searcher.search(query_obj, limit=20)
                
                results = [
                    {
                        "url": r["url"],
                        "title": r.get("title", r["url"]),
                        "snippet": (r.highlights("content") or r["content"][:200]) + "...",
                        "content": r.get("content", "") # Include full content for context
                    }
                    for r in search_results
                ]
                
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

@app.post("/scrape")
async def scrape_page(request: CrawlRequest):
    crawler = Crawler()
    try:
        data = await crawler.scrape_detailed(request.url)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/crawl")
async def start_crawl(request: CrawlRequest):
    crawler = Crawler(max_pages=request.max_pages)
    try:
        results = await crawler.crawl(request.url)
        index_pages(results)
        return {"status": "success", "pages_crawled": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/download")
def download_search(q: str = Query(..., min_length=2), format: str = Query("json", regex="^(json|md)$")):
    """Download search results as JSON or Markdown file."""
    try:
        ix = get_index()
        with ix.searcher() as searcher:
            parser = MultifieldParser(
                ["title", "content"], 
                ix.schema, 
                fieldboosts={"title": 2.5},
                group=OrGroup.factory(0.9)
            )
            query = parser.parse(q)
            results = searcher.search(query, limit=20)

            data = [
                {
                    "url": r["url"],
                    "title": r.get("title", r["url"]),
                    "snippet": (r.highlights("content") or r["content"][:200]) + "..."
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

@app.get("/search/live/download")
async def download_live_search(q: str = Query(..., min_length=2), format: str = Query("json", regex="^(json|md)$")):
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

@app.get("/search/site/download")
async def download_site_search(
    q: str = Query(..., min_length=2), 
    url: str = Query(None), 
    format: str = Query("json", regex="^(json|md)$"),
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
            
            # Step 2: Store ALL crawled pages in the local index FIRST
            pages_stored = index_pages(crawled_pages)
            
            if pages_stored == 0:
                raise HTTPException(status_code=500, detail="Pages were crawled but could not be stored in the database.")
            
            # Step 3: Now search the local index (data is already stored)
            # Get a fresh index instance to ensure we see the newly committed data
            ix = get_index()
            with ix.searcher() as searcher:
                parser = MultifieldParser(
                    ["title", "content"], 
                    ix.schema, 
                    fieldboosts={"title": 2.5},
                    group=OrGroup.factory(0.9)
                )
                query_obj = parser.parse(q)
                search_results = searcher.search(query_obj, limit=20)
                
                results = [
                    {
                        "url": r["url"],
                        "title": r.get("title", r["url"]),
                        "snippet": (r.highlights("content") or r["content"][:200]) + "..."
                    }
                    for r in search_results
                ]
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

@app.post("/scrape/download")
async def download_scrape(request: CrawlRequest, format: str = Query("json", regex="^(json|md)$")):
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
