import requests
from bs4 import BeautifulSoup
import os
import json
import yaml
from urllib.parse import urldefrag, urljoin

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

def _canonical_url(url, base_url):
    candidate = urljoin(base_url, str(url or "").strip())
    candidate, _fragment = urldefrag(candidate)
    if not candidate.startswith(("http://", "https://")):
        return ""
    return candidate.rstrip("/")


def find_primary_source(full_text, links, original_url, config):
    if not links:
        return original_url

    normalized_links = []
    candidate_urls = set()
    for link in links[:150]:
        candidate_url = _canonical_url(link.get("url"), original_url)
        if not candidate_url:
            continue
        candidate_urls.add(candidate_url)
        normalized_links.append(
            {
                "text": str(link.get("text", ""))[:50],
                "url": candidate_url,
            }
        )
    if not candidate_urls:
        return original_url
        
    prompt = f"""
    You are an AI tasked with finding the PRIMARY SOURCE link from a news article.
    A primary source is an official blog post, a research paper (e.g., arxiv), an SEC filing, or an official press release.
    It is NOT another news reporting site (like The Verge, Bloomberg, TechCrunch).
    
    Here is a list of links found in the news article:
    {json.dumps(normalized_links, ensure_ascii=False)}
    
    If one of these links clearly points to the primary official source of the news, return its URL.
    Otherwise, return "{original_url}".
    
    Output strictly in JSON:
    {{
      "primary_url": "url_string"
    }}
    """
    
    result_json = _call_llm_with_fallback(prompt, config, title_context="Primary Source Finder")
                
    if result_json:
        found_url = _canonical_url(
            result_json.get("primary_url", original_url), original_url
        )
        if found_url in candidate_urls:
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
    
    if primary_url and _canonical_url(primary_url, url) != _canonical_url(url, url):
        print(f"  [Deep Dive] Found primary source: {primary_url}", flush=True)
        primary_text, _ = fetch_full_text(primary_url)
        if len(primary_text.strip()) < 200:
            print(
                "  [Deep Dive] Primary source text is unavailable or too short; "
                "skipping unverified Deep Dive.",
                flush=True,
            )
            return None
        analysis_text = primary_text
    else:
        print(
            "  [Deep Dive] No independent primary source found; "
            "skipping unverified Deep Dive.",
            flush=True,
        )
        return None
        
    prompt = f"""
    You are a top-tier Silicon Valley VC Analyst. Read this raw text (which may be a news article or an official primary source).
    Write a hardcore, professional 500-word Investment Research Report (Deep Dive) based strictly on the facts presented.
    
    Focus on:
    1. Deep tech architecture / Product innovation
    2. Financial metrics / Market size / Valuations
    3. Strategic impact / Competitor moat
    
    Ignore journalistic fluff, ads, or unrelated text.
    Use only facts explicitly present in the verified primary source below.
    Do not invent projections, customer commitments, production volumes, market
    shares, dates, or financial figures. If a requested point is absent, say it
    is not disclosed.
    Write the report in {config.get('output', {}).get('language', 'Chinese')}.
    Use professional markdown formatting (headings, bullet points, bold text).
    
    Output strictly in JSON format matching this schema:
    {{
      "report": "Your full markdown report here"
    }}
    
    Raw Text:
    {analysis_text[:25000]}
    """
    
    result_json = _call_llm_with_fallback(prompt, config, system_prompt="You are a professional VC analyst designed to output JSON.", title_context="Deep Dive Generator")
            
    if result_json:
        report_content = result_json.get("report", "")
        
        # P2.15: 移除对死代码 heuristics.yaml 的无用写入逻辑
        return {
            "primary_url": primary_url,
            "report_content": report_content,
            "evidence_mode": "verified_primary",
        }
        
    return None
