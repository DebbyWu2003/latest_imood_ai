"""Sentence segmenter for streamed LLM text ."""
import re


class Segmenter:
    """Emit whole sentences (ending in 。！？…); merge tiny ones; comma fallback for very long runs."""

    def __init__(self, s):
        self.end, self.comma = set(s["sentence_end_chars"]), set(s["comma_chars"])
        self.min, self.max, self.buf = s["min_segment_chars"], s["max_segment_chars"], ""

    @staticmethod
    def speakable(t):
        return len(re.sub(r"[\W_]+", "", t))

    def feed(self, text):
        self.buf += text
        out, start = [], 0
        i = 0
        while i < len(self.buf):
            if self.buf[i] in self.end:
                j = i + 1
                while j < len(self.buf) and (self.buf[j] in self.end or self.buf[j] in "」』”’）)\"'"):
                    j += 1
                if self.speakable(self.buf[start:j]) >= self.min:
                    out.append(self.buf[start:j]); start = j
                i = j
                continue
            i += 1
        rest = self.buf[start:]
        if self.speakable(rest) >= self.max:              # safety fallback: cut at the last comma
            k = max((rest.rfind(c) for c in self.comma), default=-1)
            if k > 0 and self.speakable(rest[:k + 1]) >= self.min:
                out.append(rest[:k + 1]); start += k + 1
        self.buf = self.buf[start:]
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return [rest] if rest else []
