"""Continuation window: recognised sentence pieces wait for coalesce_ms of silence, then go on together.

Same rule as the early voice server: a pause inside one thought (longer than the VAD end-silence) splits it into two
utterances; holding the first transcript until the silence has lasted coalesce_ms merges the follow-up into the same
question, so one thought gets one answer. Pure logic, no I/O.
"""


class Coalescer:
    def __init__(self, coalesce_ms=1000, max_segments=6):
        self.coalesce_ms, self.max_segments = coalesce_ms, max_segments
        self.pending = []

    def add(self, text):
        """New transcript piece. Returns the merged text to send now, or None while waiting."""
        if self.coalesce_ms <= 0:
            return text
        self.pending.append(text)
        if len(self.pending) >= self.max_segments:
            return self.flush()
        return None

    def due(self, triggered, silence_since_last_ms):
        """Call regularly: merged text when the speaker is quiet long enough, else None."""
        if self.pending and not triggered and silence_since_last_ms >= self.coalesce_ms:
            return self.flush()
        return None

    def flush(self):
        text = "".join(self.pending).strip()
        self.pending = []
        return text or None
