from whoosh.fields import Schema, TEXT, ID
from whoosh.index import create_in, open_dir, exists_in
import os

SCHEMA = Schema(
    url=ID(stored=True, unique=True),
    title=TEXT(stored=True),
    content=TEXT(stored=True)
)

def get_index(index_dir="index"):
    if not os.path.exists(index_dir):
        os.mkdir(index_dir)
        return create_in(index_dir, SCHEMA)
    
    if exists_in(index_dir):
        return open_dir(index_dir)
    else:
        return create_in(index_dir, SCHEMA)

def index_pages(pages, index_dir="index"):
    """Store pages in the local index. Returns the number of pages stored."""
    if not pages:
        return 0
    
    ix = get_index(index_dir)
    writer = ix.writer()
    
    stored_count = 0
    for page in pages:
        try:
            writer.update_document(
                url=page["url"],
                title=page.get("title", ""),
                content=page.get("content", "")
            )
            stored_count += 1
        except Exception as e:
            # Log error but continue with other pages
            import logging
            logging.error(f"Error indexing page {page.get('url', 'unknown')}: {e}")
    
    # Commit all changes - this ensures data is persisted before returning
    writer.commit()
    
    return stored_count

if __name__ == "__main__":
    # Example usage
    test_pages = [
        {"url": "https://example.com", "title": "Example", "content": "This is a test page content."}
    ]
    index_pages(test_pages)
    print("Indexed test pages.")
