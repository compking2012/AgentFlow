def valid_title(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= 120
