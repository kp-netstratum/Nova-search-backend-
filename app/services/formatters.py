import json
from datetime import datetime

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
