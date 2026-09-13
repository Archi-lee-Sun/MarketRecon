import os
from typing import Dict, Any, List, Optional
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from duckduckgo_search import DDGS
import httpx
from google.genai.errors import ClientError
from google.api_core.exceptions import ResourceExhausted
from pydantic import ValidationError
from langchain_core.exceptions import OutputParserException
import logging
from urllib.parse import quote
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import re

from state import OfferListContainer, ProductOffer, SearchStrategy, AgentState
from prompts import (
    get_query_refiner_prompt,
    get_extractor_prompt,
    get_synthesizer_prompt
)

SITE_TEMPLATES: dict[str, str] = {
    "ebay.com": "https://www.ebay.com/sch/i.html?_nkw={query}",
    "extra.ge": "https://extra.ge/search?k={query}",
    "psp.ge": "https://psp.ge/catalogsearch/result?q={query}",
    "aversi.ge": "https://www.aversi.ge/ka/aversi/act/searchMedicineAI/?kw_ka={query}&ka_search=on",
    "ee.ge": "https://ee.ge/search/{query}",
    "gstore.ge": "https://gstore.ge/?s={query}&post_type=product",
    "nordstromrack.com": "https://www.nordstromrack.com/search?keyword={query}",
    "levi.com": "https://www.levi.com/US/en_US/search/{query}",
    "time.ge": "https://time.ge/en/search/?q={query}",
    "mymarket.ge": "https://www.mymarket.ge/products/?Keyword={query}",
}


llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    temperature=0.1
)

logger = logging.getLogger(__name__)

def refine_user_query(user_query: str) -> SearchStrategy:
    prompt_text = get_query_refiner_prompt()
    try:
        structured_llm = llm.with_structured_output(SearchStrategy)
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=user_query),
        ]
        return structured_llm.invoke(messages)
    except (ResourceExhausted, ClientError) as e:
        logger.error(f"Gemini quota/rate-limit error in refine_user_query: {e}")
        return SearchStrategy(
            is_valid_query=False,
            clarification_message="სერვისი დროებით მიუწვდომელია, სცადეთ მოგვიანებით.",
            direct_urls=[],
            refined_keywords=[],
            target_domains=[],
            min_price=None,
            max_price=None,
        )
    except (ValidationError, OutputParserException) as e:
        logger.error(f"Structured output validation failed in refine_user_query: {e}")
        return SearchStrategy(
            is_valid_query=False,
            clarification_message="მოთხოვნის დამუშავება ვერ მოხერხდა, გთხოვთ ჩაწეროთ პროდუქტთან დაკავშირებული უფრო კონკრეტული მოთხოვნა.",
            direct_urls=[],
            refined_keywords=[],
            target_domains=[],
            min_price=None,
            max_price=None,
        )


def scrape_urls(urls: List[str]) -> List[dict[str, str]]:
    raw_docs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-No-Cache": "true"
    }

    with httpx.Client(timeout=15.0, headers=headers) as client:
        for url in urls:
            try:
                response = client.get(f"https://r.jina.ai/{url}")
                response.raise_for_status()
                if response.text.strip():
                    raw_docs.append({
                        "url": url,
                        "content": response.text
                    })
            except httpx.HTTPStatusError as e:
                logger.error(f"'{url}' returned {e.response.status_code}")
                continue
            except httpx.RequestError as e:
                logger.error(f"Network error scraping '{url}': {e}")
                continue

        return raw_docs


def build_search_url(domain: str, query: str) -> Optional[str]:
    template = SITE_TEMPLATES.get(domain)
    if not template:
        return None
    return template.format(query=quote(query))


def try_opensearch_template(domain: str) -> Optional[str]:
    """Returns a reusable template ('{query}' placeholder, not filled in), or None."""
    try:
        homepage = httpx.get(f"https://{domain}", timeout=10.0).text
        soup = BeautifulSoup(homepage, "html.parser")
        link = soup.find("link", rel="search", type="application/opensearchdescription+xml")
        if not link or not link.get("href"):
            return None

        descriptor_url = link["href"]
        if not descriptor_url.startswith("http"):
            descriptor_url = f"https://{domain}{descriptor_url}"

        xml_text = httpx.get(descriptor_url, timeout=10.0).text
        root = ET.fromstring(xml_text)
        url_elem = root.find(".//{http://a9.com/-/spec/opensearch/1.1/}Url[@type='text/html']")
        if url_elem is None:
            return None
        template = url_elem.get("template")
        return template.replace("{searchTerms}", "{query}")
    except Exception as e:
        logger.warning(f"OpenSearch discovery failed for {domain}: {e}")
        return None


def try_form_scrape_template(domain: str) -> Optional[str]:
    """Returns a reusable template ('{query}' placeholder, not filled in), or None."""
    try:
        homepage = httpx.get(f"https://{domain}", timeout=10.0).text
        soup = BeautifulSoup(homepage, "html.parser")
        form = soup.find("form", attrs={"role": "search"}) or soup.find("form", action=re.compile("search", re.I))
        if not form:
            return None

        input_field = form.find("input", {"type": "search"}) or form.find("input", {"type": "text"})
        if not input_field or not input_field.get("name"):
            return None

        action = form.get("action", "")
        param = input_field["name"]
        base = action if action.startswith("http") else f"https://{domain}{action}"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{param}={{query}}"
    except Exception as e:
        logger.warning(f"Form scrape discovery failed for {domain}: {e}")
        return None


def jina_search_fallback(domain: str, query: str) -> str:
    """Not a template — a finished URL, so it's never cached in SITE_TEMPLATES."""
    return f"https://s.jina.ai/{quote(query)}?site={domain}"


def discover_search_template(domain: str) -> Optional[str]:
    """Finds a reusable template for the domain (query not filled in). No query needed here."""
    return try_opensearch_template(domain) or try_form_scrape_template(domain)


def search_and_scrape(strategy: SearchStrategy) -> List[dict[str, str]]:
    urls_to_scrape: List[str] = []

    if strategy.direct_urls:
        urls_to_scrape = strategy.direct_urls
    else:
        for refined_keyword in strategy.refined_keywords:
            for domain in strategy.target_domains:
                if domain in SITE_TEMPLATES:
                    urls_to_scrape.append(build_search_url(domain, refined_keyword))
                else:
                    template = discover_search_template(domain)
                    if template:
                        SITE_TEMPLATES[domain] = template
                        urls_to_scrape.append(build_search_url(domain, refined_keyword))
                    else:
                        urls_to_scrape.append(jina_search_fallback(domain, refined_keyword))

    urls_to_scrape = list(dict.fromkeys(urls_to_scrape))
    if not urls_to_scrape:
        logger.warning("No URLs found to scrape.")
        return []

    return scrape_urls(urls_to_scrape)



def extract_offers_node(raw_docs: List[dict[str , str]]) -> List[ProductOffer] :
    if not raw_docs :
        logger.warning("No raw documents provided for offer extraction.")
        return []

    prompt_text = get_extractor_prompt()
    all_offers: List[ProductOffer] = []
    structured_llm = llm.with_structured_output(OfferListContainer)

    for doc in raw_docs: 
        source_url = doc.get("url" , "unknown url")
        content = doc.get("content" , "")

        if not content.strip() :
            logger.warning(f"Skipping document with empty content for URL: {source_url}")
            continue

        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(
                content=f"Source Listing URL: {source_url}\n\nDocument Markdown Content:\n{content}"
            ),
        ]

        try :
            result: OfferListContainer = structured_llm.invoke(messages)
            if result and result.offers:
                all_offers.extend(result.offers)
        except (ResourceExhausted, ClientError) as e:
            logger.error(f"Gemini quota/rate-limit error in extract_offers_node for {source_url}: {e}")
            continue
        except (ValidationError, OutputParserException) as e:
            logger.error(f"Structured output validation failed in extract_offers_node for {source_url}: {e}")
            continue
        except Exception as e:
            logger.critical(f"Unexpected INTERNAL error processing {source_url}: {e}", exc_info=True)
            continue

    return all_offers


def validate_offers_node(extracted_offers: List[ProductOffer] , strategy: SearchStrategy) -> List[ProductOffer]:
    if not extracted_offers :
        logger.warning("No extracted offers provided for validation.")
        return []

    validated_offers: List[ProductOffer] = []

    for offer in extracted_offers:
        if not offer.in_stock:
            continue

        if strategy.min_price is not None and offer.price < strategy.min_price :
            continue

        if strategy.max_price is not None and offer.price > strategy.max_price :
            continue 

        validated_offers.append(offer)

    return validated_offers
   


def synthesize_final_report_node(validated_offers: List[ProductOffer], user_query: str) -> str:
    if not validated_offers:
        logger.warning("No validated offers to synthesize into a report.")
        return "თქვენი მოთხოვნის შესაბამისი პროდუქტი ვერ მოიძებნა."

    sorted_offers = sorted(validated_offers, key=lambda o: o.price)

    offers_text = "\n".join(
         f"- {o.product_name} | {o.price} {o.currency} | {o.store_name} | {o.product_url}"
         for o in sorted_offers
    )
    
    prompt_text = get_synthesizer_prompt()
    messages = [
        SystemMessage(content=prompt_text),
        HumanMessage(content=f"User query: {user_query}\n\nOffers (sorted by price ascending):\n{offers_text}")
    ]

    try:
        response = llm.invoke(messages)
        return response.content
    except (ResourceExhausted, ClientError) as e:
        logger.error(f"Gemini quota/rate-limit error in synthesize_final_report_node: {e}")
        return "სერვისი დროებით მიუწვდომელია, სცადეთ მოგვიანებით."
    except Exception as e:
        logger.critical(f"Unexpected error in synthesize_final_report_node: {e}", exc_info=True)
        return "ანგარიშის გენერირებისას მოხდა შეცდომა."