import asyncio
import asyncpg
import uuid
from app.core.config import settings
from urllib.parse import urlparse, urlunparse

DATABASE_URL = settings.DATABASE_URL

def normalize_site_url(url: str) -> str:
    """
    Standardize a URL to prevent duplicate site entries.
    Removes fragments, lowercases scheme/netloc, and handles trailing slashes.
    """
    if not url:
        return url
    
    # Handle simple scheme-less URLs
    if "://" not in url:
        url = "https://" + url
        
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() if parsed.scheme else "https"
    netloc = parsed.netloc.lower()
    path = parsed.path
    
    # Strip trailing slash for consistency
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    elif not path:
        path = "/"
        
    # Reconstruct without query strings or fragments for 'site' identification
    return urlunparse((scheme, netloc, path, "", "", ""))

async def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    """Initialize the database schema."""
    conn = await get_db_connection()
    try:
        # DROP tables to ensure schema update (for dev environment)
        # Using CASCADE to handle relationships
        # await conn.execute("DROP TABLE IF EXISTS chat_messages CASCADE;")
        # await conn.execute("DROP TABLE IF EXISTS chat_sessions CASCADE;")
        # await conn.execute("DROP TABLE IF EXISTS pages CASCADE;")
        # await conn.execute("DROP TABLE IF EXISTS sites CASCADE;")

        # 1. Create sites table (The Root)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sites (
                url TEXT PRIMARY KEY,
                "createdAt" BIGINT
            );
        """)

        # 2. Create pages table with FK to sites
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY,
                "parentUrl" TEXT REFERENCES sites(url) ON DELETE CASCADE,
                "childrenUrls" TEXT[],
                content TEXT,
                "createdAt" BIGINT,
                "searchVector" TSVECTOR
            );
        """)

        # 3. Create chat sessions table with FK to sites
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                "targetSite" TEXT REFERENCES sites(url) ON DELETE CASCADE,
                title TEXT,
                "createdAt" BIGINT
            );
        """)

        # 4. Create chat messages table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                "sessionId" UUID REFERENCES chat_sessions(id) ON DELETE CASCADE,
                role TEXT,
                content TEXT,
                "createdAt" BIGINT
            );
        """)

        # Create GIN index for full-text search
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS search_vector_idx ON pages USING GIN("searchVector");
        """)
        
        # Create trigger to automatically update search_vector
        await conn.execute("""
            CREATE OR REPLACE FUNCTION pages_tsvector_trigger() RETURNS trigger AS $$
            BEGIN
                NEW."searchVector" :=
                    setweight(to_tsvector('english', coalesce(NEW.content, '')), 'A');
                RETURN NEW;
            END
            $$ LANGUAGE plpgsql;
        """)

        await conn.execute("""
            DROP TRIGGER IF EXISTS tsvectorupdate ON pages;
            CREATE TRIGGER tsvectorupdate BEFORE INSERT OR UPDATE
            ON pages FOR EACH ROW EXECUTE FUNCTION pages_tsvector_trigger();
        """)
        
        print("Database initialized successfully with robust linking (sites root table).")
    finally:
        await conn.close()

async def save_chat_session(target_site, title=None):
    """Create a new chat session."""
    target_site = normalize_site_url(target_site)
    if not title:
        title = f"Chat with {target_site}"
    
    conn = await get_db_connection()
    try:
        import time
        created_at = int(time.time())

        # Ensure site exists in the root 'sites' table
        await conn.execute("""
            INSERT INTO sites (url, "createdAt")
            VALUES ($1, $2)
            ON CONFLICT (url) DO NOTHING;
        """, target_site, created_at)
        
        row = await conn.fetchrow("""
            INSERT INTO chat_sessions ("targetSite", title, "createdAt")
            VALUES ($1, $2, $3)
            RETURNING id;
        """, target_site, title, created_at)
        return row["id"]
    finally:
        await conn.close()

async def save_chat_message(session_id, role, content):
    """Save a single chat message."""
    conn = await get_db_connection()
    try:
        import time
        created_at = int(time.time())
        # Ensure session_id is a UUID object
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)
            
        await conn.execute("""
            INSERT INTO chat_messages ("sessionId", role, content, "createdAt")
            VALUES ($1, $2, $3, $4);
        """, session_id, role, content, created_at)
    finally:
        await conn.close()

async def is_session_valid(session_id):
    """Check if a session ID exists in the database."""
    conn = await get_db_connection()
    try:
        if isinstance(session_id, str):
            try:
                session_id = uuid.UUID(session_id)
            except ValueError:
                return False
                
        row = await conn.fetchrow("""
            SELECT id FROM chat_sessions WHERE id = $1;
        """, session_id)
        return row is not None
    finally:
        await conn.close()

async def get_chat_sessions():
    """Fetch all chat sessions."""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT id, "targetSite", title, "createdAt"
            FROM chat_sessions
            ORDER BY "createdAt" DESC;
        """)
        return [dict(row) for row in rows]
    finally:
        await conn.close()

async def get_chat_messages(session_id):
    """Fetch all messages for a session."""
    conn = await get_db_connection()
    try:
        # Ensure session_id is a UUID object
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        rows = await conn.fetch("""
            SELECT role, content, "createdAt"
            FROM chat_messages
            WHERE "sessionId" = $1
            ORDER BY "createdAt" ASC;
        """, session_id)
        return [dict(row) for row in rows]
    finally:
        await conn.close()

async def get_crawl_history():
    """Fetch unique sites that have been crawled."""
    conn = await get_db_connection()
    try:
        # Query the sites table directly
        rows = await conn.fetch("""
            SELECT url as "parentUrl", "createdAt"
            FROM sites
            ORDER BY "createdAt" DESC;
        """)
        return [{"url": row["parentUrl"], "createdAt": row["createdAt"]} for row in rows]
    finally:
        await conn.close()

async def delete_site_data(url):
    """Delete all pages and associated data for a specific site."""
    url = normalize_site_url(url)
    conn = await get_db_connection()
    try:
        # Deleting from the root 'sites' table will cascade to pages, sessions, and messages
        await conn.execute("""
            DELETE FROM sites
            WHERE url = $1;
        """, url)
    finally:
        await conn.close()

async def index_pages(pages_data):
    """
    Index a list of pages into PostgreSQL.
    pages_data: List of dicts matching the new schema
    """
    if not pages_data:
        return 0

    conn = await get_db_connection()
    count = 0
    try:
        for page in pages_data:
            id_val = page.get("id")
            if not id_val:
                # Fallback if id is missing but url exists
                id_val = page.get("url")
            
            if not id_val:
                continue

            parentUrl = normalize_site_url(page.get("parentUrl", ""))
            childrenUrls = page.get("childrenUrls", [])
            content = page.get("content", "")
            createdAt = page.get("createdAt", 0)

            # Ensure site exists in root table
            await conn.execute("""
                INSERT INTO sites (url, "createdAt")
                VALUES ($1, $2)
                ON CONFLICT (url) DO NOTHING;
            """, parentUrl, createdAt)
            
            # Upsert (Insert or Update) with quoted identifiers
            await conn.execute("""
                INSERT INTO pages (id, "parentUrl", "childrenUrls", content, "createdAt")
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id) 
                DO UPDATE SET 
                "parentUrl" = EXCLUDED."parentUrl",
                "childrenUrls" = EXCLUDED."childrenUrls",
                content = EXCLUDED.content,
                "createdAt" = EXCLUDED."createdAt";
            """, id_val, parentUrl, childrenUrls, content, createdAt)
            count += 1
            
    except Exception as e:
        print(f"Error indexing pages: {e}")
        raise e
    finally:
        await conn.close()
    
    return count

async def search_pages(query, limit=10):
    """
    Search for pages using full-text search.
    """
    if not query:
        return []

    conn = await get_db_connection()
    results = []
    try:
        # Perform search using quoted identifiers
        rows = await conn.fetch("""
            SELECT 
                id,
                "parentUrl",
                "childrenUrls",
                content,
                "createdAt",
                ts_headline('english', content, websearch_to_tsquery('english', $1), 'StartSel=<b>, StopSel=</b>') as snippet,
                ts_rank("searchVector", websearch_to_tsquery('english', $1)) as rank
            FROM pages
            WHERE "searchVector" @@ websearch_to_tsquery('english', $1)
            ORDER BY rank DESC
            LIMIT $2;
        """, query, limit)
        
        for row in rows:
            results.append({
                "id": row["id"],
                "parentUrl": row["parentUrl"],
                "childrenUrls": row["childrenUrls"],
                "content": row["content"],
                "createdAt": row["createdAt"],
                "snippet": row["snippet"] or row["content"][:200] + "..."
            })
            
    except Exception as e:
        print(f"Error searching pages: {e}")
        return []
    finally:
        await conn.close()

    return results
