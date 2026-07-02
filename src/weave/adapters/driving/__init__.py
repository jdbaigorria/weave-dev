"""weave.adapters.driving — Inbound adapters (the outside calls in).

Channels: chat (request/response) and voice (async/streaming over a persistent
transport). They translate external input into use-case calls and never leak
transport concerns into the domain.
"""
