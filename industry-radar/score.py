import os
import json
from datetime import datetime
from dotenv import load_dotenv
from llm_router import _call_llm_with_fallback

load_dotenv()

def score_article(article, config):
    prompt = f"""
    You are an expert industry analyst and VC. Evaluate this tech news article based on the dual-track criteria.
    
    Target Industries: {', '.join([ind['name'] for ind in config.get('industries', [])])}
    
    Criteria:
    {config.get('importance_criteria', '')}
    
    Article Title: {article['title']}
    Article Summary: {article['summary']}
    Current Date: {datetime.now().strftime('%Y-%m-%d')}
    
    Tasks:
    1. Determine if this article is relevant to the tech industry (True/False). 
       **CRITICAL REJECTION RULES: You MUST set is_relevant to False if the article is:**
       - A news roundup, summary, or digest (e.g., "Top 10 news", "Morning brief", "Weekly digest", "8点1氪", "氪星晚报", "晚报").
       - A shopping deal, discount, or advertisement (e.g., "Prime Day deals", "Black Friday", "Save $50 on...", "优惠精选", "购物指南").
       - Re-hashed old news or an old event being re-reported as new (e.g., "炒冷饭" - a breakthrough or event that actually happened months or years prior to the Current Date). If you recognize the event as historical relative to the Current Date, YOU MUST REJECT IT by setting is_relevant to False.
    2. If relevant, score its 'innovation_score' (0.0-10.0, can use 1 decimal place e.g., 7.5).
    3. Score its 'traffic_score' (0.0-10.0, can use 1 decimal place e.g., 8.2).
    4. Provide a concise 1-sentence justification explaining the scores in {config.get('output', {}).get('language', 'Chinese')}.
    5. Provide the translation of the 'Article Title' into {config.get('output', {}).get('language', 'Chinese')}.
    6. Provide a HIGHLY CONDENSED summary of the article content. **CRITICAL RULE: The translated_summary MUST be ONE SINGLE SENTENCE and MUST NOT exceed 50 Chinese characters. Be extremely brief.**
    
    You must output strictly in JSON format matching this schema:
    {{
      "is_relevant": boolean,
      "innovation_score": float,
      "traffic_score": float,
      "justification": string,
      "translated_title": string,
      "translated_summary": string
    }}
    """
    
    result = _call_llm_with_fallback(prompt, config, title_context=article['title'][:30])
    if result:
        return result
        
    return {"innovation_score": 0, "traffic_score": 0, "justification": "All API endpoints failed or unconfigured", "is_relevant": False, "translated_title": article['title'], "translated_summary": "Error"}

def deduplicate_articles(articles, config):
    if len(articles) <= 1:
        return articles
        
    from llm_router import _call_llm_with_fallback
    import json
    
    # Sort articles by published_at (earliest first)
    sorted_articles = sorted(articles, key=lambda x: x.get('published_at', '9999-12-31'))
    
    from llm_router import _call_llm_with_fallback
    import json

    # Prepare payload for LLM
    payload = []
    for i, a in enumerate(sorted_articles):
        # Pass the original long summary or content (up to 800 chars) instead of the truncated 50-char summary
        # This gives the LLM enough context to realize they are the same event.
        long_text = a.get('content', a.get('summary', ''))
        if long_text:
            long_text = long_text[:800]
        
        payload.append({
            "id": i,
            "title": a.get('title', ''),
            "text": long_text
        })
        
    prompt = f"""
    You are a professional industry analyst. I have a list of tech news articles. Some of them are reporting on the exact same underlying event, just from different news outlets (e.g., they might use slightly different numbers or phrasing to describe the same event).
    Your task is to identify all duplicates and group them together.
    Here is the JSON list of articles:
    {json.dumps(payload, ensure_ascii=False, indent=2)}

    Return your answer strictly in JSON format matching this schema:
    {{
      "groups": [[int, ...], [int]] // A list of lists of IDs. Each inner list represents a unique event and contains the IDs of articles discussing it.
    }}
    """
    
    try:
        res = _call_llm_with_fallback(prompt, config, system_prompt="You are a helpful assistant designed to output JSON.", title_context="dedup_batch")
        groups = res.get("groups", [])
    except Exception as e:
        print(f"LLM Deduplication error: {e}. Falling back to returning original articles.", flush=True)
        return sorted_articles
        
    final_articles = []
    processed = set()
    for group in groups:
        if not group:
            continue
        valid_group = [idx for idx in group if 0 <= idx < len(sorted_articles) and idx not in processed]
        if not valid_group:
            continue
            
        base_idx = valid_group[0]
        base_article = sorted_articles[base_idx]
        processed.add(base_idx)
        
        if len(valid_group) > 1:
            sources = set([base_article['source']])
            max_inn = base_article.get('score_data', {}).get('innovation_score', 0)
            max_tra = base_article.get('score_data', {}).get('traffic_score', 0)
            justifications = [base_article.get('score_data', {}).get('justification', '')]
            
            for dup_idx in valid_group[1:]:
                dup_art = sorted_articles[dup_idx]
                processed.add(dup_idx)
                sources.add(dup_art['source'])
                ds = dup_art.get('score_data', {})
                max_inn = max(max_inn, ds.get('innovation_score', 0))
                max_tra = max(max_tra, ds.get('traffic_score', 0))
                just = ds.get('justification', '')
                if just and just not in justifications:
                    justifications.append(just)
                    
            if len(sources) > 1:
                max_tra = min(10.0, max_tra + (len(sources) - 1) * 0.5)
                
            if 'score_data' not in base_article:
                base_article['score_data'] = {}
                
            base_article['source'] = ", ".join(sources)
            base_article['score_data']['innovation_score'] = round(float(max_inn), 1)
            base_article['score_data']['traffic_score'] = round(float(max_tra), 1)
            base_article['score_data']['justification'] = " | ".join(justifications)
            
        final_articles.append(base_article)
        
    # Add back any articles that weren't included in any group
    for i, a in enumerate(sorted_articles):
        if i not in processed:
            final_articles.append(a)
            
    return final_articles

def pre_filter_articles_batch(articles_batch, config):
    payload = []
    for a in articles_batch:
        payload.append({
            "id": a["id"],
            "title": a["title"],
            "summary": a["summary"][:100]
        })
        
    prompt = f"""
    You are a fast content filter for a tech/VC radar. 
    You will receive a list of articles. For each article, determine if it is relevant to Hardcore Tech, Investment, or cutting-edge innovation.
    
    Target Industries: {', '.join([ind['name'] for ind in config.get('industries', [])])}
    
    CRITICAL REJECTION RULES: Return is_relevant=false if the article is:
    1. A news roundup/digest (e.g. "Morning brief", "晚报").
    2. A shopping deal, discount, ad (e.g. "Black Friday", "Save $50", "促销").
    3. Re-hashed old news or gossip.
    
    Input JSON:
    {json.dumps(payload, ensure_ascii=False)}
    
    Return STRICTLY a JSON object matching this schema exactly:
    {{
      "results": [
        {{"id": integer, "is_relevant": boolean}}
      ]
    }}
    """
    
    result = _call_llm_with_fallback(prompt, config, title_context=f"Pre-filter Batch ({len(articles_batch)} items)")
    return result

def score_articles_batch(articles_batch, config):
    payload = []
    for a in articles_batch:
        payload.append({
            "id": a["id"],
            "title": a["title"],
            "summary": a["summary"][:300]
        })
        
    prompt = f"""
    You are an expert industry analyst and VC. Evaluate these tech news articles based on dual-track criteria.
    
    Target Industries: {', '.join([ind['name'] for ind in config.get('industries', [])])}
    
    Criteria:
    {config.get('importance_criteria', '')}
    
    Input Articles JSON:
    {json.dumps(payload, ensure_ascii=False)}
    
    For EACH article in the input, provide:
    1. 'innovation_score' (0.0-10.0, allows 1 decimal place)
    2. 'traffic_score' (0.0-10.0, allows 1 decimal place)
    3. 'justification': 1-sentence explanation of scores in {config.get('output', {}).get('language', 'Chinese')}
    4. 'translated_title': Translate title to {config.get('output', {}).get('language', 'Chinese')}
    5. 'translated_summary': HIGHLY CONDENSED summary (MAX 50 Chinese characters)
    
    Return STRICTLY a JSON object matching this schema exactly:
    {{
      "results": [
        {{
          "id": integer,
          "is_relevant": true,
          "innovation_score": number (e.g. 8.5),
          "traffic_score": number (e.g. 8.5),
          "justification": string,
          "translated_title": string,
          "translated_summary": string
        }}
      ]
    }}
    """
    
    result = _call_llm_with_fallback(prompt, config, title_context=f"Score Batch ({len(articles_batch)} items)")
    
    if result and "results" in result:
        trusted_sources = config.get("trusted_sources", [])
        for res_item in result["results"]:
            # Find original article by id
            a_id = res_item.get("id")
            original_a = next((a for a in articles_batch if a["id"] == a_id), None)
            if original_a and original_a.get("source") in trusted_sources:
                # Boost innovation score for trusted sources
                base_score = float(res_item.get("innovation_score", 0))
                boosted = min(10.0, base_score + 1.0)
                res_item["innovation_score"] = boosted
                # Add a marker to the justification
                res_item["justification"] = f"[🌟顶级信源加权] {res_item.get('justification', '')}"
                
    return result
