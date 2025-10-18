from typing import Iterable


def human_bytes(
        n: int,
        *,
        base: int = 1000,
        decimals: int = 0,
        units: Iterable[str] = ("b", "kb", "mb", "gb", "tb", "pb", "eb", "zb", "yb")
) -> str:
    """
    Convert a byte count to a human-readable string.

    Args:
        n: Byte count (e.g., from os.stat().st_size).
        base: 1000 for SI (kb, mb, ...), 1024 for IEC-like step size.
        decimals: Decimal places for non-byte units (0 -> '66kb', 1 -> '1.5gb').
        units: Unit suffixes to use. Defaults to lowercase ('kb'); swap for ('B','kB','MB',...) if preferred.

    Returns:
        A compact string like '66kb', '1mb', '1.5gb', or '999b'.
    """
    if n < 0:
        raise ValueError("Byte size cannot be negative")

    i = 0
    max_i = len(tuple(units)) - 1
    while n >= base and i < max_i:
        n /= base
        i += 1

    if i == 0 or decimals == 0:
        # Bytes or integer formatting requested
        return f"{int(n if i else n)}{tuple(units)[i]}"

    return f"{n:.{decimals}f}{tuple(units)[i]}".rstrip("0").rstrip(".")
