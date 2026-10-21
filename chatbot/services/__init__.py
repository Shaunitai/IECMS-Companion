"""Service layer for IECMS Companion.

Every module in this package has a single responsibility and is deliberately
kept free of HTTP/Django view concerns so that it can be imported and unit
tested in isolation.

Modules
-------
llm_service
    Provider-specific cloud LLM access (Phase 3).
retrieval_service
    Approved-knowledge-base search (Phase 2).
explanation_service
    Orchestrates retrieval -> grounded explanation generation (Phase 1 mock,
    Phase 3 real implementation).
safety_service
    Legal-advice boundary enforcement (Phase 4).
privacy_service
    Personal-identifier detection and input rejection (Phase 4).
citation_service
    Citation validation against retrieved evidence (Phase 3).
"""
