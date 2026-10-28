"""Face switching per emotion (calm / happy / sad): swaps the session's avatar frames, with a cross-fade.

Runs inside the LiveTalking process (imports its logger/avatars).
set_emotion(sess, emotion, speaking) is called by the vendor receiver, the framed receiver and the /cyber_gf/emotion route.
"""
import os

import cv2
from utils.logger import logger

EMOTION_PREFIX = "wav2lip256_emo_"
EMOTION_AVATARS = {"neutral": "calm", "happy": "happy", "sad": "sad"}      # vendor code -> suffix; angry/fear/unknown -> calm
EMOTION_SPEAK = os.environ.get("EMOTION_SPEAK", "0") == "1"    # 1 = while an utterance plays, use the "<emotion>_speak" face if one was built
EMOTION_FADE = int(os.environ.get("EMOTION_FADE_FRAMES", "8"))     # cross-fade length (video frames, ~25 fps) when the face changes; 0 = hard cut
_EMO_CACHE = {}


def _install_fade(sess):
    """Wrap this session's video output once: remember the last frame sent, and for EMOTION_FADE frames after a face swap
    blend it into the new face (sess._fade_left counts down)."""
    out = getattr(sess, "output", None)
    if out is None or getattr(sess, "_fade_installed", False) or not hasattr(out, "push_video_frame"):
        return
    orig = out.push_video_frame
    sess._fade_installed, sess._fade_left, sess._fade_prev, sess._fade_last = True, 0, None, None

    def push(frame, *a, **k):
        n = sess._fade_left
        if n > 0 and sess._fade_prev is not None and sess._fade_prev.shape == frame.shape:
            alpha = 1.0 - n / (EMOTION_FADE + 1.0)
            frame = cv2.addWeighted(sess._fade_prev, 1.0 - alpha, frame, alpha, 0)
            sess._fade_left = n - 1
        sess._fade_last = frame
        return orig(frame, *a, **k)
    out.push_video_frame = push


def _emo_avatar(emotion, speaking=False):
    from avatars.wav2lip_avatar import load_avatar
    aid = EMOTION_PREFIX + EMOTION_AVATARS.get(emotion, "calm")
    if speaking and EMOTION_SPEAK and os.path.isdir(f"./data/avatars/{aid}_speak"):
        aid += "_speak"     # calmer face for lip-sync (wav2lip smears wide smiles / moving heads); the idle face keeps the full expression
    if aid not in _EMO_CACHE:
        _EMO_CACHE[aid] = load_avatar(aid)
    return aid, _EMO_CACHE[aid]


def set_emotion(sess, emotion, speaking=False):
    """Swap the session's face to the one for `emotion` (its "_speak" variant while speaking, if enabled and built).
    Returns the avatar id now shown, or None if not applicable."""
    cur = getattr(getattr(sess, "opt", None), "avatar_id", "") or ""
    if not cur.startswith(EMOTION_PREFIX) or not hasattr(sess, "face_list_cycle"):
        return None
    aid, (frames, faces, coords) = _emo_avatar(emotion, speaking)
    if getattr(sess, "_emotion_avatar", None) == aid:
        return aid
    if EMOTION_FADE > 0:
        _install_fade(sess)
        if getattr(sess, "_fade_last", None) is not None:
            sess._fade_prev, sess._fade_left = sess._fade_last, EMOTION_FADE
    sess.frame_list_cycle, sess.face_list_cycle, sess.coord_list_cycle = frames, faces, coords
    sess._emotion_avatar = aid
    logger.info("[cyber_gf] emotion %s -> %s", emotion, aid)
    return aid
