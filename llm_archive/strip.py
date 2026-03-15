import re


_FENCED_CODE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_TILDE_CODE = re.compile(r"~~~[^\n]*\n.*?~~~", re.DOTALL)
_BLANK_LINES = re.compile(r"\n{3,}")


def strip_code_blocks(text: str) -> str:
    text = _FENCED_CODE.sub("", text)
    text = _TILDE_CODE.sub("", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()
