"""Relay stage that caps bulk transfer size.

Bulk SUBMIT with transfer_buffer_length above max_transfer is split into
sequential sub-requests and reassembled into one RET_SUBMIT. The board's
dwc3 controller has no dma-coherent property, so every DMA mapping goes
through swiotlb, which cannot map more than IO_TLB_SEGSIZE(128) * 2KB in one
go. A single 512KB URB therefore fails with -EAGAIN and takes the whole port
down. 128KB chunks stay well below that ceiling.

Stage position: first in the chain, closest to the client. This stage changes
the number of requests, so it owns the request/response correlation for every
sub-request it creates and no stage behind it may do the same. The anchor is the
client because the USB/IP seqnum -- the token USBIP_RET_SUBMIT is matched on --
is the client's, and this stage mints seqnums of its own.

The stage is three modules: ``wire`` (header layout and the seqnum table),
``handshake`` (OP_REQ_IMPORT and its descriptors) and ``relay`` (chunking and the
two forwarding loops). The stage contract -- ANCHOR, create, handle -- is
assembled here.
"""

from __future__ import annotations

from .relay import Options, create, handle

# Where this stage sits in the relay, from who consumes what it rewrites: it
# mints USB/IP seqnums, which only the client correlates. See usbip/__init__.py.
ANCHOR = "client"

__all__ = ["ANCHOR", "Options", "create", "handle"]
