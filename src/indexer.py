import asyncio
import asyncpg
from config import settings

DATABASE_URL = settings.DATABASE_URL

async def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    """Initialize the database schema."""
    conn = await get_db_connection()
    try:
        # DROP table to ensure schema update (for dev environment)
        await conn.execute("DROP TABLE IF EXISTS pages CASCADE;")

        # Create table with new structure using quotes to preserve camelCase
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY,
                "parentUrl" TEXT,
                "childrenUrls" TEXT[],
                content TEXT,
                "createdAt" BIGINT,
                "searchVector" TSVECTOR
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
        
        print("Database initialized successfully with new schema (quoted identifiers).")
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

            parentUrl = page.get("parentUrl", "")
            childrenUrls = page.get("childrenUrls", [])
            content = page.get("content", "")
            createdAt = page.get("createdAt", 0)
            
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
