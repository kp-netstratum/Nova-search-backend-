import ollama
import logging
from typing import List, Dict, AsyncGenerator
from app.core.config import settings

logger = logging.getLogger(__name__)

async def generate_chat_response(
    message: str,
    targetSite: str,
    context_items: List[Dict],
    history: List[Dict] = None,
    model: str = settings.AI_MODEL
) -> AsyncGenerator[str, None]:
    """
    Generates a streaming chat response using Ollama based on the provided context.
    
    Args:
        message: The user's current message.
        targetSite: The domain being discussed.
        context_items: A list of dicts containing 'title', 'content', and 'url' from search.
        history: Previous conversation messages [{"role": "user"|"assistant", "content": str}].
        model: The model to use (default from settings).
        
    Yields:
        Chunks of the generated response as they arrive.
    """
    if history is None:
        history = []
    
    # Build context from search results
    context_str = ""
    if context_items:
        context_str = "Here is relevant information from the crawled website:\n\n"
        for idx, item in enumerate(context_items, 1):  
            context_str += f"[Source {idx}] {item.get('title', 'Untitled')}\n"
            context_str += f"URL: {item.get('url', '')}\n"
            content = item.get('content', '')
            context_str += f"Content: {content}\n\n"
    
    # Build system prompt
    system_prompt = f"""You are a helpful AI assistant that answers questions based on crawled website data from {targetSite}. 
    Use the provided context to give accurate, relevant answers. If the information isn't in the context, politely say so. 
    Keep your answers clear and concise. Always cite sources when possible. make the answers precise as possible."""
    
    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add conversation history (limit to last 10 messages)
    if history:
        messages.extend(history[-10:])
    
    # Add current user message with context
    user_message = f"{context_str}\n\nUser Question: {message}" if context_str else message
    messages.append({"role": "user", "content": user_message})
    
    logger.info(f"Generating chat response for site: {targetSite}")
    try:
        # Use AsyncClient for non-blocking I/O
        client = ollama.AsyncClient()
        
        # Stream the response
        stream = await client.chat(
            model=model,
            messages=messages,
            stream=True
        )
        
        async for chunk in stream:
            if 'message' in chunk and 'content' in chunk['message']:
                yield chunk['message']['content']
                
    except ollama.ResponseError as e:
        logger.error(f"Ollama response error: {e}")
        if e.status_code == 404:
            yield f"Error: Model '{model}' not found. Please run `ollama pull {model}`."
        else:
            yield "Error generating answer. Please try again."
    except Exception as e:
        logger.error(f"Ollama connection error: {e}")
        yield "Error: Could not connect to Ollama. Please ensure Ollama is running."
