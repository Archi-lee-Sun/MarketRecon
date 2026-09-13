from typing import TypedDict, List, Optional
from pydantic import BaseModel, Field

class SearchStrategy(BaseModel):
    is_valid_query: bool = Field(
        description="True if the request relates to a product or purchase, False if it is unclear or irrelevant."
    )

    clarification_message: Optional[str] = Field(
        description="If is_valid_query is False, the clarification text sent to the user."
    )

    direct_urls: List[str] = Field(
        default_factory=list,
        description="Exact product URLs the user provided directly, if any. If non-empty, search is skipped and these are scraped directly."
    )

    refined_keywords: List[str] = Field(
        description="List of refined search phrases for DuckDuckGo. Only used when direct_urls is empty."
    )

    target_domains: list[str] = Field(
        default_factory=list,
        description="List of target online stores or domains to prefer/filter search results by. Only used when direct_urls is empty."
    )

    min_price: Optional[float] = Field(
        None, description="Minimum price filter for the search."
    )

    max_price: Optional[float] = Field(
        None, description="Maximum price filter for the search."
    )
    

class ProductOffer(BaseModel):
    product_name: str = Field(description="Exact product name")
    price: float = Field(description="Price in numbers (e.g., 129.99)")
    currency: str = Field(description="Currency, for example USD, GEL, EUR")
    store_name: str = Field(description="Store or website name")
    product_url: str = Field(description="Direct link to the product")
    in_stock: bool = Field(description="Whether the item is in stock")


class OfferListContainer(BaseModel):
    offers: List[ProductOffer] = Field(
        description="List of extracted product offers from the markdown content"
        )    

class AgentState(TypedDict):
    user_query: str                          
    search_strategy: Optional[SearchStrategy] 
    raw_docs: List[str]                     
    extracted_offers: List[ProductOffer] 
    validation_status: str                    
    final_report: str                         
    error_message: Optional[str]