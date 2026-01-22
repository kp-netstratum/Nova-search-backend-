import asyncio
import aiohttp
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import tldextract
import logging
import uuid
from datetime import datetime
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_SEEDS = [
    "https://en.wikipedia.org/wiki/Main_Page",
    "https://www.bbc.com/news",
    "https://www.reuters.com",
    "https://www.medium.com",
    "https://www.theverge.com",
    "https://www.wired.com",
    "https://www.nature.com",
    "https://www.bloomberg.com"
]

class Crawler:
    def __init__(self, max_pages=50):
        self.visited = set()
        self.max_pages = max_pages
        self.results = []

    def _sync_fetch(self, page, url):
        """Internal synchronous fetch using Playwright."""
        url = self.normalize_url(url)
        try:
            # Navigate with a generous timeout and wait for content to load
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000) # Wait for hydration
            return page.content()
        except Exception as e:
            logger.error(f"Playwright error on {url}: {e}")
            return None

    def normalize_url(self, url):
        """Ensures the URL has a scheme (defaults to https://)"""
        if not url: return url
        parsed = urlparse(url)
        if not parsed.scheme:
            return "https://" + url
        return url

    def score_link(self, url, query):
        """Simple heuristic to score links based on query relevance in URL"""
        query_words = (query or "").lower().split()
        score = 0
        url_lower = url.lower()
        for word in query_words:
            if word in url_lower:
                score += 10
        return score

    def extract_links(self, html, base_url, query=None, restrict_domain=False):
        soup = BeautifulSoup(html, "html.parser")
        links = []
        try:
            domain_info = tldextract.extract(base_url)
            base_domain = domain_info.registered_domain
        except:
            base_domain = ""
        
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            parsed = urlparse(href)
            if parsed.scheme in ["http", "https"]:
                if restrict_domain and base_domain:
                    try:
                        link_domain = tldextract.extract(href).registered_domain
                        if link_domain != base_domain:
                            continue
                    except:
                        continue
                        
                score = self.score_link(href, query)
                links.append((score, href))
        
        links.sort(key=lambda x: x[0], reverse=True)
        return [l[1] for l in links]

    def extract_data(self, html):
        """Robust text extraction using BeautifulSoup."""
        soup = BeautifulSoup(html, "html.parser")
        
        title = "Untitled"
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        elif soup.h1:
            title = soup.h1.get_text(strip=True)

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.decompose()
        
        content_area = soup.find(["article", "main"]) or soup.body or soup
        text = content_area.get_text(" ", strip=True)
        text = " ".join(text.split())
        
        return title, text

    def html_to_markdown(self, html, base_url):
        """Convert HTML to Markdown format."""
        soup = BeautifulSoup(html, "html.parser")
        
        # Remove unwanted elements but keep structure
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.decompose()
        
        # Process main content area
        content_area = soup.find(["article", "main"]) or soup.body or soup
        
        def process_element(elem):
            """Recursively process HTML elements to markdown."""
            if elem is None:
                return ""
            
            if isinstance(elem, str):
                return elem.strip()
            
            if not hasattr(elem, 'name'):
                return ""
            
            tag_name = elem.name
            if tag_name is None:
                return ""
            
            children_text = "".join(process_element(child) for child in elem.children if child)
            
            if tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                level = int(tag_name[1])
                text = elem.get_text(strip=True)
                return f"\n{'#' * level} {text}\n\n" if text else ""
            elif tag_name == "p":
                text = elem.get_text(strip=True)
                return f"{text}\n\n" if text else ""
            elif tag_name == "a":
                text = elem.get_text(strip=True)
                href = elem.get("href", "")
                if href:
                    href = urljoin(base_url, href)
                    return f"[{text}]({href})" if text else ""
                return text
            elif tag_name == "img":
                alt = elem.get("alt", "")
                src = elem.get("src", "")
                if src:
                    src = urljoin(base_url, src)
                    return f"![{alt}]({src})\n" if alt else f"![Image]({src})\n"
                return ""
            elif tag_name in ["strong", "b"]:
                text = elem.get_text(strip=True)
                return f"**{text}**" if text else ""
            elif tag_name in ["em", "i"]:
                text = elem.get_text(strip=True)
                return f"*{text}*" if text else ""
            elif tag_name == "li":
                text = elem.get_text(strip=True)
                return f"- {text}\n" if text else ""
            elif tag_name == "br":
                return "\n"
            elif tag_name == "hr":
                return "\n---\n\n"
            elif tag_name in ["ul", "ol"]:
                return f"\n{children_text}\n"
            else:
                return children_text
        
        markdown = process_element(content_area)
        
        # Clean up markdown
        # Remove excessive newlines
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        # Clean up whitespace
        markdown = "\n".join(line.rstrip() for line in markdown.split("\n"))
        markdown = markdown.strip()
        
        return markdown

    def _sync_crawl_worker(self, start_urls, query, restrict_domain):
        """Synchronous crawling logic to be run in a thread."""
        local_results = []
        local_visited = set()
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 800},
                ignore_https_errors=True
            )
            page = context.new_page()
            
            # Queue stores (score, url, parent_url)
            queue = [(100, self.normalize_url(url), "") for url in start_urls]

            while queue and len(local_visited) < self.max_pages:
                queue.sort(key=lambda x: x[0], reverse=True)
                score, url, parent_url = queue.pop(0)
                
                if url in local_visited:
                    continue

                logger.info(f"Crawling ({score}): {url}")
                html = self._sync_fetch(page, url)
                if not html:
                    continue

                local_visited.add(url)
                
                # Use markdown instead of plain text
                markdown_content = self.html_to_markdown(html, url)
                title, _ = self.extract_data(html) # Keep title extraction for metadata if needed, though not in DB req
                
                # Get children URLs
                new_links = self.extract_links(html, url, query, restrict_domain)
                
                local_results.append({
                    "id": url, # Using URL as ID
                    "parentUrl": parent_url,
                    "childrenUrls": new_links,
                    "content": markdown_content,
                    "createdAt": int(datetime.utcnow().timestamp()), # Epoch
                    # "title": title # Keeping title in object just in case, but indexer will ignore if not in schema.
                                     # Actually, let's keep it in dict, indexer filters.
                    "title": title 
                })

                for link in new_links:
                    if link not in local_visited and not any(q[1] == link for q in queue):
                        link_score = self.score_link(link, query)
                        if link_score > 0 or len(local_visited) < 10:
                            if len(queue) < self.max_pages * 5:
                                queue.append((link_score, link, url))
                                
            browser.close()
        return local_results

    async def crawl(self, start_urls, query=None, restrict_domain=False):
        if isinstance(start_urls, str):
            start_urls = [start_urls]
        return await asyncio.to_thread(self._sync_crawl_worker, start_urls, query, restrict_domain)

    async def autonomous_search(self, query):
        """Starts from seeds and explores to find matches for the query intent."""
        logger.info(f"Starting autonomous discovery for: {query}")
        self.max_pages = 20 # Keep it relatively fast
        results = await self.crawl(DEFAULT_SEEDS, query=query)
        return await self.rank_results(results, query)

    async def search_site(self, start_url, query):
        """Search within a specific site."""
        results = await self.crawl(start_url, query=query, restrict_domain=True)
        return await self.rank_results(results, query)

    async def rank_results(self, results, query):
        """Rank results in memory using simple scoring (no DB or Whoosh)."""
        if not results: return []
        
        query_terms = query.lower().split()
        scored_results = []
        
        for res in results:
            score = 0
            title = res.get("title", "").lower()
            content = res.get("content", "").lower()
            
            # Simple scoring
            for term in query_terms:
                score += title.count(term) * 3
                score += content.count(term)
            
            # Smart snippet generation
            snippet = res.get("content", "")[:200]
            # Try to find a snippet containing query terms
            for term in query_terms:
                idx = content.find(term)
                if idx != -1:
                    start = max(0, idx - 60)
                    end = min(len(content), idx + 140)
                    snippet = "..." + res.get("content", "")[start:end] + "..."
                    break
            
            scored_results.append({
                "url": res["url"],
                "title": res["title"],
                "snippet": snippet,
                "score": score
            })
            
        # Sort by score descending
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results

    def _sync_scrape_detailed(self, url):
        """Synchronous detailed scrape logic to be run in a thread."""
        scrape_id = str(uuid.uuid4())
        cached_at = datetime.utcnow().isoformat() + "Z"
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 800},
                ignore_https_errors=True
            )
            page = context.new_page()
            
            html = self._sync_fetch(page, url)
            if not html:
                browser.close()
                return {"error": "Could not fetch page"}

            soup = BeautifulSoup(html, "html.parser")
            title, content = self.extract_data(html)
            
            # Convert HTML to markdown
            markdown = self.html_to_markdown(html, url)
            
            # Extract metadata
            meta_tags = soup.find_all("meta")
            metadata_dict = {}
            for meta in meta_tags:
                name = meta.get("name") or meta.get("property") or meta.get("http-equiv")
                content = meta.get("content")
                if name and content:
                    metadata_dict[name.lower()] = content
            
            # Extract specific metadata fields
            theme_color = metadata_dict.get("theme-color", "#000000")
            viewport = metadata_dict.get("viewport", "width=device-width,initial-scale=1maximum-scale=1,user-scalable=yes")
            
            # Extract description
            description = metadata_dict.get("description", "")
            if not description:
                og_desc = soup.find("meta", attrs={"property": "og:description"})
                if og_desc:
                    description = og_desc.get("content", "")
            
            # Extract language
            lang = soup.find("html", lang=True)
            language = lang.get("lang", "en") if lang else "en"
            
            # Extract favicon
            favicon = soup.find("link", rel=lambda x: x and ("icon" in x.lower() or "shortcut" in x.lower()))
            if favicon:
                favicon = urljoin(url, favicon.get("href", ""))
            else:
                favicon = urljoin(url, "/favicon.ico")
            
            # Get page title
            page_title = title
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                page_title = og_title.get("content")
            
            # Extract images
            images = []
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if src:
                    images.append({
                        "src": urljoin(url, src),
                        "alt": img.get("alt", "")
                    })
            
            # Extract links
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href:
                    links.append({
                        "href": urljoin(url, href),
                        "text": a.get_text(strip=True)
                    })

            browser.close()
            
            # JSON Data structure
            json_data = {
                "title": title,
                "content": content,
                "url": url,
                "images": images,
                "links": links,
                "metadata": metadata_dict # Include raw metadata dict here too
            }

            # Build response in the requested format
            return {
                "json": json_data,
                "markdown": markdown,
                "metadata": {
                    "theme-color": theme_color,
                    "viewport": viewport,
                    "title": page_title,
                    "language": language,
                    "description": description,
                    "favicon": favicon,
                    "scrapeId": scrape_id,
                    "sourceURL": url,
                    "url": url,
                    "statusCode": 200,
                    "contentType": "text/html",
                    "proxyUsed": "basic",
                    "cacheState": "hit",
                    "cachedAt": cached_at,
                    "creditsUsed": 1,
                    "concurrencyLimited": False
                }
            }

    async def scrape_detailed(self, url):
        return await asyncio.to_thread(self._sync_scrape_detailed, url)

class SearchProvider:
    def __init__(self):
        self.search_url = "https://html.duckduckgo.com/html/"

    async def live_search(self, query: str):
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.post(self.search_url, data={"q": query}, timeout=10) as resp:
                    if resp.status != 200: return []
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    results = []
                    for entry in soup.select(".result"):
                        title_tag = entry.select_one(".result__a")
                        snippet_tag = entry.select_one(".result__snippet")
                        if title_tag:
                            results.append({
                                "url": title_tag["href"],
                                "title": title_tag.get_text(strip=True),
                                "snippet": snippet_tag.get_text(strip=True) if snippet_tag else ""
                            })
                    return results[:10]
            except:
                return []
