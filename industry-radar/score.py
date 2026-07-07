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
    import difflib
    
    # Pre-deduplicate using local string matching to save LLM tokens
    local_dedup_groups = [] # list of lists of articles
    for a in sorted_articles:
        long_text = a.get('content', a.get('summary', ''))
        if long_text: long_text = long_text[:800]
        text_to_match = (a.get('title', '') + " " + long_text).lower()
        
        found_group = False
        for group in local_dedup_groups:
            # Compare against the first article in the group
            rep = group[0]
            rep_text = rep.get('content', rep.get('summary', ''))
            if rep_text: rep_text = rep_text[:800]
            rep_match = (rep.get('title', '') + " " + rep_text).lower()
            
            similarity = difflib.SequenceMatcher(None, text_to_match, rep_match).ratio()
            if similarity > 0.85:
                group.append(a)
                found_group = True
                break
                
        if not found_group:
            local_dedup_groups.append([a])
            
    print(f"Local pre-deduplication grouped {len(sorted_articles)} articles into {len(local_dedup_groups)} groups.", flush=True)

    if len(local_dedup_groups) <= 1:
        # If local grouping already reduced it to 1, just return the first of the group
        final_list = []
        for g in local_dedup_groups:
            best_article = max(g, key=lambda x: x.get('score_data', {}).get('innovation_score', 0) + x.get('score_data', {}).get('traffic_score', 0))
            final_list.append(best_article)
        return final_list

    # Prepare payload for LLM from the reduced groups
    payload = []
    for i, group in enumerate(local_dedup_groups):
        a = group[0] # Use the representative for LLM scoring
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
    
    CRITICAL GROUPING RULES:
    1. If two articles are about the EXACT SAME company's funding round, valuation, or acquisition, THEY ARE DUPLICATES. Even if one highlights "$5B valuation" and the other highlights "$800M funding" or "$1B sales", if it's the same company's milestone event, GROUP THEM.
    2. If two articles are about the same product launch or major update from the same company, GROUP THEM.
    3. Be aggressive in grouping. We want to avoid reading about the same company's event twice.

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
        # Fallback: just pick the best from each local group
        final_list = []
        for g in local_dedup_groups:
            best_article = max(g, key=lambda x: x.get('score_data', {}).get('innovation_score', 0) + x.get('score_data', {}).get('traffic_score', 0))
            final_list.append(best_article)
        return final_list
        
    final_articles = []
    processed = set()
    for group in groups:
        if not group:
            continue
        valid_group_indices = [idx for idx in group if 0 <= idx < len(local_dedup_groups) and idx not in processed]
        if not valid_group_indices:
            continue
            
        # Flatten the local groups corresponding to the LLM chosen indices into a single big group
        combined_articles = []
        for idx in valid_group_indices:
            combined_articles.extend(local_dedup_groups[idx])
            processed.add(idx)
            
        base_article = combined_articles[0]
        
        if len(combined_articles) > 1:
            sources = set([base_article.get('source', '')])
            max_inn = base_article.get('score_data', {}).get('innovation_score', 0)
            max_tra = base_article.get('score_data', {}).get('traffic_score', 0)
            
            # Collect all unique titles, summaries, and justifications
            titles_to_merge = []
            summaries_to_merge = []
            justs_to_merge = []
            
            # Add base article
            ds_base = base_article.get('score_data', {})
            if base_article.get('title'): titles_to_merge.append(base_article['title'])
            if ds_base.get('translated_title'): titles_to_merge.append(ds_base['translated_title'])
            if base_article.get('summary'): summaries_to_merge.append(base_article['summary'])
            if ds_base.get('translated_summary'): summaries_to_merge.append(ds_base['translated_summary'])
            if ds_base.get('justification'): justs_to_merge.append(ds_base['justification'])
            
            for dup_art in combined_articles[1:]:
                sources.add(dup_art.get('source', ''))
                ds = dup_art.get('score_data', {})
                max_inn = max(max_inn, ds.get('innovation_score', 0))
                max_tra = max(max_tra, ds.get('traffic_score', 0))
                
                if dup_art.get('title') and dup_art['title'] not in titles_to_merge: titles_to_merge.append(dup_art['title'])
                if ds.get('translated_title') and ds['translated_title'] not in titles_to_merge: titles_to_merge.append(ds['translated_title'])
                if dup_art.get('summary') and dup_art['summary'] not in summaries_to_merge: summaries_to_merge.append(dup_art['summary'])
                if ds.get('translated_summary') and ds['translated_summary'] not in summaries_to_merge: summaries_to_merge.append(ds['translated_summary'])
                just = ds.get('justification', '')
                if just and just not in justs_to_merge: justs_to_merge.append(just)
                    
            if len(sources) > 1:
                max_tra = min(10.0, max_tra + (len(sources) - 1) * 0.5)
                
            if 'score_data' not in base_article:
                base_article['score_data'] = {}
                
            # Call LLM to synthesize
            lang = config.get('output', {}).get('language', 'Chinese')
            synth_prompt = f"""
            You are a master news editor. I have multiple news articles reporting on the exact same event from different angles or highlighting different metrics.
            Your task is to synthesize them into ONE perfect, comprehensive summary.
            
            Collected Titles:
            {json.dumps(titles_to_merge, ensure_ascii=False)}
            
            Collected Summaries:
            {json.dumps(summaries_to_merge, ensure_ascii=False)}
            
            Collected Editor Justifications:
            {json.dumps(justs_to_merge, ensure_ascii=False)}
            
            Please generate:
            1. A 'translated_title' in {lang} that captures all key metrics (e.g. if one says 800M funding and another says 5B valuation, include both if possible, or pick the most impactful).
            2. A 'translated_summary' in {lang} that is ONE SINGLE SENTENCE (MAX 50 CHARS) synthesizing the most important facts.
            3. A 'justification' in {lang} (1 sentence) combining the viewpoints of why this event is highly important.
            
            Return STRICTLY in JSON matching this schema:
            {{
              "translated_title": "string",
              "translated_summary": "string",
              "justification": "string"
            }}
            """
                
            try:
                synth_res = _call_llm_with_fallback(synth_prompt, config, system_prompt="You are a helpful JSON-outputting news editor.", title_context="News Synthesis")
                if synth_res:
                    if synth_res.get("translated_title"):
                        base_article['score_data']['translated_title'] = synth_res["translated_title"]
                    if synth_res.get("translated_summary"):
                        base_article['score_data']['translated_summary'] = synth_res["translated_summary"]
                    if synth_res.get("justification"):
                        base_article['score_data']['justification'] = synth_res["justification"]
                else:
                    base_article['score_data']['justification'] = " | ".join(justs_to_merge)
            except Exception as e:
                print(f"Synthesis failed: {e}")
                base_article['score_data']['justification'] = " | ".join(justs_to_merge)
                
            base_article['source'] = ", ".join([s for s in sources if s])
            base_article['score_data']['innovation_score'] = round(float(max_inn), 1)
            base_article['score_data']['traffic_score'] = round(float(max_tra), 1)
                
            final_articles.append(base_article)
        else:
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
