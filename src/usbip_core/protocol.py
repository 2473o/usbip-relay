"""Wire-level guards shared by the relay stages.

The usbip header carries transfer_buffer_length as a signed 32-bit field chosen
by the client.  Every stage allocates a buffer of exactly that size before it
reads the payload, so the size has to be bounded by the relay rather than by the
peer: a single 0x7fffffff announcement would otherwise reserve 2 GiB per
connection.
"""

from __future__ import annotations

# Largest payload a stage is willing to allocate for one URB.  The dwc3 stack
# never builds anything close to this (bulk URBs are a few hundred KiB at most),
# so anything above it is a broken or hostile peer, not a real transfer.
MAX_TRANSFER_BYTES = 32 * 1024 * 1024


class ProtocolError(Exception):
    """The peer sent something the relay cannot safely follow."""


def check_transfer_length(length: int, what: str = "urb") -> int:
    """Reject impossible transfer lengths before they reach an allocation."""
    if length < 0:
        raise ProtocolError("%s length %d is negative" % (what, length))
    if length > MAX_TRANSFER_BYTES:
        raise ProtocolError(
            "%s length %d exceeds the %d byte relay limit" % (what, length, MAX_TRANSFER_BYTES)
        )
    return length
