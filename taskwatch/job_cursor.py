"""Opaque integer positions in the v2 projection stream."""

_VERSION = 2
_SHIFT = 256
_OFFSET_BITS = 64
_OFFSET_MASK = (1 << _OFFSET_BITS) - 1
_GENERATION_MASK = (1 << 128) - 1


def encode(generation: str, offset: int) -> int:
    """Keep CLI cursors integers while distinguishing the replaced snapshot stream."""
    if type(offset) is not int or not 0 <= offset <= _OFFSET_MASK:
        raise ValueError("cursor offset is out of range")
    return (_VERSION << _SHIFT) | (int(generation, 16) << _OFFSET_BITS) | offset


def decode(cursor: int) -> tuple[str, int] | None:
    """Old byte/inode cursors and unknown formats cannot identify this stream."""
    if type(cursor) is not int or cursor < 0 or cursor >> _SHIFT != _VERSION:
        return None
    if cursor & (((1 << _SHIFT) - 1) ^ ((1 << 192) - 1)):
        return None
    return f"{(cursor >> _OFFSET_BITS) & _GENERATION_MASK:032x}", cursor & _OFFSET_MASK
