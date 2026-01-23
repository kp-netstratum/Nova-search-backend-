
import ollama
import logging
from config import settings

logger = logging.getLogger(__name__)

def generate_answer(query: str, context_items: list, model: str = settings.AI_MODEL) -> str:
    """
    Generates an answer using Ollama based on the provided context.
    
    Args:
        query: The user's search query.
        context_items: A list of dicts containing 'title', 'content', and 'url'.
        model: The model to use (default: llama3.2).
        
    Returns:
        The generated answer as a string, or None if generation fails.
    """
    if not context_items:
        return None

    # Construct the prompt
    context_str = ""
    for item in context_items[:5]: # Take top 5 results
        context_str += f"Source: {item.get('title', 'Untitled')} ({item.get('url', '')})\n"
        # Truncate content to avoid token limits if necessary, though simpler models might handle it
        content = item.get('content', '')[:1000] 
        context_str += f"Content: {content}\n\n"

    prompt = f"""You are a helpful AI assistant. Use the following context to answer the user's question. 
If the answer is not in the context, say so politely. Keep your answer concise and relevant.

Context:
{context_str}

Question: {query}

Answer:"""

    try:
        response = ollama.chat(model=model, messages=[
            {
                'role': 'user',
                'content': prompt,
            },
        ])
        return response['message']['content']
    except ollama.ResponseError as e:
        logger.error(f"Ollama response error: {e}")
        if e.status_code == 404:
            return f"Error: Model '{model}' not found. Please run `ollama pull {model}`."
        return "Error generating answer."
    except Exception as e:
        logger.error(f"Ollama connection error: {e}")
        # Return none so the UI doesn't show a broken AI box
        return None
