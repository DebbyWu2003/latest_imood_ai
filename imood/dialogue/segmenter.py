"""Sentence segmenter for streamed LLM text ."""
import re


class Segmenter:
    """Emit whole sentences (ending in 。！？…); merge tiny ones; comma fallback for very long runs."""

    def __init__(self, config):
        self.end_chars, self.comma_chars = set(config["sentence_end_chars"]), set(config["comma_chars"])
        self.min_chars, self.max_chars, self.buffer = config["min_segment_chars"], config["max_segment_chars"], ""

    @staticmethod
    def speakable(text):
        return len(re.sub(r"[\W_]+", "", text))

    def feed(self, text):
        self.buffer += text
        segments, segment_start = [], 0
        i = 0
        while i < len(self.buffer):
            if self.buffer[i] in self.end_chars:
                j = i + 1
                while j < len(self.buffer) and (self.buffer[j] in self.end_chars or self.buffer[j] in "」』”’）)\"'"):
                    j += 1
                if self.speakable(self.buffer[segment_start:j]) >= self.min_chars:
                    segments.append(self.buffer[segment_start:j]); segment_start = j
                i = j
                continue
            i += 1
        remainder = self.buffer[segment_start:]
        if self.speakable(remainder) >= self.max_chars:              # safety fallback: cut at the last comma
            cut_index = max((remainder.rfind(comma) for comma in self.comma_chars), default=-1)
            if cut_index > 0 and self.speakable(remainder[:cut_index + 1]) >= self.min_chars:
                segments.append(remainder[:cut_index + 1]); segment_start += cut_index + 1
        self.buffer = self.buffer[segment_start:]
        return segments

    def flush(self):
        remainder, self.buffer = self.buffer, ""
        return [remainder] if remainder else []
