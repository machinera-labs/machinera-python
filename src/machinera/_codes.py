from ._contract import ERROR_CODES, ErrorCode


def __getattr__(name: str) -> ErrorCode:
    try:
        return ERROR_CODES[name]
    except KeyError:
        raise AttributeError(name) from None
