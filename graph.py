import logging
from typing import Dict, Any, Literal
from langgraph.graph import StateGraph, START, END

from state import AgentState
from nodes import (
    refine_user_query,
    search_and_scrape,
    extract_offers_node,
    validate_offers_node,
    synthesize_final_report_node,
)


logger = logging.getLogger(__name__)


def refine_query_node_wrapper(state: AgentState) -> Dict[str, Any]:
    user_query = state.get("user_query" , "")
    refined_query = refine_user_query(user_query)

    return {"search_strategy": refined_query}


def search_and_scrape_node_wrapper(state: AgentState) -> Dict[str, Any]:
    strategy = state.get("search_strategy")
    if not strategy or not strategy.is_valid_query:
        return {"raw_docs": []}

    raw_docs = search_and_scrape(strategy)
    return {"raw_docs": raw_docs}


def extract_offers_node_wrapper(state: AgentState) -> Dict[str, Any]:
    raw_docs = state.get("raw_docs", [])
    offers = extract_offers_node(raw_docs)
    return {"extracted_offers": offers}


def validator_node_wrapper(state: AgentState) -> Dict[str, Any]:
    offers = state.get("extracted_offers", [])
    strategy = state.get("search_strategy")

    if not strategy:
        return {"extracted_offers": [], "validation_status": "missing_strategy"}

    validated_offers = validate_offers_node(offers, strategy)
    status = "success" if validated_offers else "no_offers_matched"
    return {
        "extracted_offers": validated_offers,
        "validation_status": status,
    }


def synthesize_report_node_wrapper(state: AgentState) -> Dict[str, Any]:
    strategy = state.get("search_strategy")
    if strategy and not strategy.is_valid_query:
        return {"final_report": strategy.clarification_message or "მოთხოვნის დამუშავება ვერ მოხერხდა."}

    validated_offers = state.get("extracted_offers", [])
    user_query = state.get("user_query", "")
    final_report = synthesize_final_report_node(validated_offers, user_query)
    return {"final_report": final_report}


def route_after_refinement(state: AgentState) -> Literal["search_and_scrape", "synthesize_report"]:
    strategy = state.get("search_strategy")
    if strategy and strategy.is_valid_query:
        return "search_and_scrape"

    logger.info("The request is unknown or invalid. The scraping step will be skipped.")
    return "synthesize_report"


builder = StateGraph(AgentState)

builder.add_node("refine_query", refine_query_node_wrapper)
builder.add_node("search_and_scrape", search_and_scrape_node_wrapper)
builder.add_node("extract_offers", extract_offers_node_wrapper)
builder.add_node("validator", validator_node_wrapper)
builder.add_node("synthesize_report", synthesize_report_node_wrapper)

builder.add_edge(START, "refine_query")

builder.add_conditional_edges(
    "refine_query" , 
    route_after_refinement,
    {
        "search_and_scrape": "search_and_scrape",
        "synthesize_report": "synthesize_report",
    }
                )

builder.add_edge("search_and_scrape" , "extract_offers")
builder.add_edge("extract_offers" , "validator")
builder.add_edge("validator" , "synthesize_report")
builder.add_edge("synthesize_report" , END)

app = builder.compile()