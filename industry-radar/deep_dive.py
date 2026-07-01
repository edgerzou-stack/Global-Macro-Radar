import requests
from bs4 import BeautifulSoup
import os
import json
import yaml
from llm_router import _call_llm_with_fallback

def fetch_full_text(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.extract()
            
        text = soup.get_text(separator='\n', strip=True)
        links = []
        for a in soup.find_all('a', href=True):
            links.append({"text": a.get_text(strip=True)[:50], "url": a['href']})
            
        return text[:20000], links
    except Exception as e:
        print(f"Standard fetch failed for {url}: {e}. Falling back to Jina Reader...", flush=True)
        try:
            jina_url = f"https://r.jina.ai/{url}"
            jina_headers = {"X-Return-Format": "markdown"}
            resp = requests.get(jina_url, headers=jina_headers, timeout=20)
            resp.raise_for_status()
            return resp.text[:20000], []
        except Exception as jina_e:
            print(f"Jina fetch failed for {url}: {jina_e}", flush=True)
            return "", []

def find_primary_source(full_text, links, original_url, config):
    if not links:
        return original_url
        
    prompt = f"""
    You are an AI tasked with finding the PRIMARY SOURCE link from a news article.
    A primary source is an official blog post, a research paper (e.g., arxiv), an SEC filing, or an official press release.
    It is NOT another news reporting site (like The Verge, Bloomberg, TechCrunch).
    
    Here is a list of links found in the news article:
    {json.dumps(links[:150], ensure_ascii=False)}
    
    If one of these links clearly points to the primary official source of the news, return its URL.
    Otherwise, return "{original_url}".
    
    Output strictly in JSON:
    {{
      "primary_url": "url_string"
    }}
    """
    
    result_json = _call_llm_with_fallback(prompt, config, title_context="Primary Source Finder")
                
    if result_json:
        found_url = result_json.get("primary_url", original_url)
        if found_url.startswith("http"):
            return found_url
            
    return original_url

def generate_deep_dive_report(article, config):
    url = article.get('link')
    if not url:
        return None
        
    print(f"  [Deep Dive] Triggered for: {article['title'][:50]}...", flush=True)
    
    original_text, links = fetch_full_text(url)
    if not original_text:
        return None
        
    primary_url = find_primary_source(original_text, links, url, config)
    
    if primary_url and primary_url != url:
        print(f"  [Deep Dive] Found primary source: {primary_url}", flush=True)
        primary_text, _ = fetch_full_text(primary_url)
        analysis_text = primary_text if len(primary_text) > 800 else original_text
    else:
        print(f"  [Deep Dive] No external primary source found, analyzing original article.", flush=True)
        analysis_text = original_text
        primary_url = url
        
    prompt = f"""
    You are a top-tier Silicon Valley VC Analyst. Read this raw text (which may be a news article or an official primary source).
    Write a hardcore, professional 500-word Investment Research Report (Deep Dive) based strictly on the facts presented.
    
    Focus on:
    1. Deep tech architecture / Product innovation
    2. Financial metrics / Market size / Valuations
    3. Strategic impact / Competitor moat
    
    Ignore journalistic fluff, ads, or unrelated text.
    Write the report in {config.get('output', {}).get('language', 'Chinese')}.
    Use professional markdown formatting (headings, bullet points, bold text).
    
    ALSO, extract exactly 3 cutting-edge trending technology keywords (single words or short phrases in English, e.g. "AGI", "Solid-state battery") that are central to this article.
    
    Output strictly in JSON format matching this schema:
    {{
      "report": "Your full markdown report here",
      "trending_keywords": ["keyword1", "keyword2", "keyword3"]
    }}
    
    Raw Text:
    {analysis_text[:25000]}
    """
    
    result_json = _call_llm_with_fallback(prompt, config, system_prompt="You are a professional VC analyst designed to output JSON.", title_context="Deep Dive Generator")
            
    if result_json:
        report_content = result_json.get("report", "")
        trending_keywords = result_json.get("trending_keywords", [])
        
        # Auto-evolution: append new keywords to heuristics.yaml
        if trending_keywords:
            try:
                heuristics_path = os.path.join(os.path.dirname(__file__), "heuristics.yaml")
                with open(heuristics_path, 'r', encoding='utf-8') as f:
                    heuristics = yaml.safe_load(f)
                    
                existing_tech_kws = [k.lower() for k in heuristics['keywords']['hardcore_tech']]
                added = []
                for kw in trending_keywords:
                    if kw.lower() not in existing_tech_kws:
                        heuristics['keywords']['hardcore_tech'].append(kw.lower())
                        added.append(kw)
                        
                if added:
                    with open(heuristics_path, 'w', encoding='utf-8') as f:
                        yaml.dump(heuristics, f, allow_unicode=True, sort_keys=False)
                    print(f"  [Auto-Evolution] Learned new keywords: {added}", flush=True)
            except Exception as e:
                print(f"  [Auto-Evolution] Error updating heuristics: {e}", flush=True)
                
        return {
            "primary_url": primary_url,
            "report_content": report_content
        }
        
    return None
